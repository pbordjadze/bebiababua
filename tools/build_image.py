#!/usr/bin/env python3
"""Build a ready-to-flash LibreELEC image for the Raspberry Pi 3.

Takes the official LibreELEC RPi2/3 image (pinned + checksummed in
tools/versions.json), and replaces its tiny, empty STORAGE partition with a
bigger one pre-filled with our /storage tree: add-ons, config, keys,
services. The boot/system partition is untouched, so this stays a stock,
updatable LibreELEC.

Why not let LibreELEC grow STORAGE on first boot as usual: its fs-resize
re-creates the filesystem (mke2fs) and refuses to run at all once
/storage/.kodi or .config exist. So the image ships STORAGE at its final size
(--storage-mb, default 3 GiB - plenty for this appliance) and never asks for
a resize. The rest of a larger SD card simply stays unused.

Needs Linux with e2fsprogs >= 1.43 (mke2fs -d) and either root or fakeroot.
`tools/babua image` runs it in Docker when that's not available.

  build_image.py [--site site] [--out build/out] [--storage-mb 3072]
"""

import argparse
import gzip
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import uuid
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools"))
import stage  # noqa: E402

CACHE = os.path.join(REPO, "build", "cache")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def fetch(url, expected):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, os.path.basename(url))
    if os.path.exists(path) and sha256(path) == expected:
        return path
    print("downloading", url)
    tmp = path + ".part"
    with urllib.request.urlopen(url, timeout=120) as resp, open(tmp, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    got = sha256(tmp)
    if got != expected:
        os.remove(tmp)
        sys.exit(f"checksum mismatch for {url}\n  expected {expected}\n  got      {got}")
    os.replace(tmp, path)
    return path


def partitions(img):
    with open(img, "rb") as fh:
        mbr = fh.read(512)
    if mbr[510:512] != b"\x55\xaa":
        sys.exit("image has no MBR")
    parts = []
    for i in range(4):
        entry = mbr[446 + 16 * i: 446 + 16 * (i + 1)]
        ptype = entry[4]
        start, count = struct.unpack("<II", entry[8:16])
        if ptype:
            parts.append((ptype, start * 512, count * 512))
    return parts


def ext4_identity(img, offset):
    """(uuid, label) from the ext4 superblock at offset+1024."""
    with open(img, "rb") as fh:
        fh.seek(offset + 1024)
        sb = fh.read(1024)
    if struct.unpack("<H", sb[0x38:0x3A])[0] != 0xEF53:
        sys.exit("STORAGE partition is not ext4")
    return str(uuid.UUID(bytes=sb[0x68:0x78])), sb[0x78:0x88].rstrip(b"\0").decode()


def sparse_copy(src_path, dst_path, offset):
    """Copy src into dst at offset, skipping all-zero blocks (dst is sparse)."""
    zero = bytes(1 << 20)
    with open(src_path, "rb") as src, open(dst_path, "r+b") as dst:
        pos = offset
        for block in iter(lambda: src.read(1 << 20), b""):
            if block != zero[:len(block)]:
                dst.seek(pos)
                dst.write(block)
            pos += len(block)


def set_partition_size(img, index, sectors):
    with open(img, "r+b") as fh:
        fh.seek(446 + 16 * index + 12)
        fh.write(struct.pack("<I", sectors))


def make_fs(tree, fs_path, size, fs_uuid, label):
    # same options as LibreELEC's own fs-resize (no reserved blocks)
    cmd = ["mke2fs", "-q", "-F", "-t", "ext4", "-m", "0", "-L", label, "-U", fs_uuid,
           "-E", "root_owner=0:0", "-d", tree, fs_path, f"{size // 1024}k"]
    if os.geteuid() == 0:
        subprocess.run(["chown", "-R", "0:0", tree], check=True)
        subprocess.run(cmd, check=True)
    elif shutil.which("fakeroot"):
        script = 'chown -R 0:0 "$1" && shift && exec "$@"'
        subprocess.run(["fakeroot", "sh", "-c", script, "sh", tree, *cmd], check=True)
    else:
        sys.exit("need root or fakeroot so files on the image are owned by root")
    subprocess.run(["e2fsck", "-fn", fs_path], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=os.path.join(REPO, "site"))
    ap.add_argument("--out", default=os.path.join(REPO, "build", "out"))
    ap.add_argument("--storage-mb", type=int, default=3072,
                    help="size of /storage (default 3072; the SD card must hold ~600 MB more)")
    args = ap.parse_args()

    if not shutil.which("mke2fs"):
        sys.exit("mke2fs not found (install e2fsprogs, or use `tools/babua image` which uses Docker)")

    versions = json.load(open(os.path.join(REPO, "tools", "versions.json")))
    le = versions["libreelec"]
    le_gz = fetch(le["url"], le["sha256"])
    addon_zips = {name: fetch(a["url"], a["sha256"]) for name, a in versions["addons"].items()}

    with tempfile.TemporaryDirectory() as tmp:
        tree = os.path.join(tmp, "storage")
        try:
            cfg = stage.stage(tree, args.site, first_boot=True)
        except stage.SiteError as exc:
            sys.exit(str(exc))

        addons_dir = os.path.join(tree, ".kodi", "addons")
        os.makedirs(addons_dir, exist_ok=True)
        for name, path in addon_zips.items():
            with zipfile.ZipFile(path) as zf:
                if not all(n.startswith(name + "/") for n in zf.namelist()):
                    sys.exit(f"{path}: unexpected layout")
                zf.extractall(addons_dir)
            for root, _, files in os.walk(os.path.join(addons_dir, name)):
                for f in files:
                    if f.endswith(".so") or ".so." in f or root.endswith("/bin"):
                        os.chmod(os.path.join(root, f), 0o755)

        # Tailscale (static ARM build) for remote admin
        ts = versions.get("tailscale")
        if ts:
            ts_dir = os.path.join(tree, ".config", "babua", "tailscale")
            os.makedirs(ts_dir, exist_ok=True)
            with tarfile.open(fetch(ts["url"], ts["sha256"])) as tf:
                for member in tf.getmembers():
                    name = os.path.basename(member.name)
                    if member.isfile() and name in ("tailscale", "tailscaled"):
                        with tf.extractfile(member) as src, open(os.path.join(ts_dir, name), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        os.chmod(os.path.join(ts_dir, name), 0o755)

        img = os.path.join(tmp, "image.img")
        with gzip.open(le_gz, "rb") as src, open(img, "wb") as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        parts = partitions(img)
        if len(parts) != 2 or parts[1][0] != 0x83:
            sys.exit(f"unexpected partition layout: {parts}")
        _, offset, orig_size = parts[1]
        fs_uuid, label = ext4_identity(img, offset)  # cmdline.txt mounts by this UUID

        size = max(args.storage_mb << 20, orig_size)
        set_partition_size(img, 1, size // 512)
        with open(img, "r+b") as fh:
            fh.truncate(offset + size)  # sparse; compresses to almost nothing

        used = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(tree) for f in fs)

        fs_path = os.path.join(tmp, "storage.ext4")
        make_fs(tree, fs_path, size, fs_uuid, label)
        sparse_copy(fs_path, img, offset)

        os.makedirs(args.out, exist_ok=True)
        name = f"{cfg.get('HOSTNAME', 'babua-tv')}-LibreELEC-{le['version']}.img.gz"
        total = offset + size
        out = os.path.join(args.out, name)
        with open(img, "rb") as src, gzip.open(out + ".part", "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst, 1 << 20)
        os.chmod(out + ".part", 0o600)
        os.replace(out + ".part", out)

    print(f"\nimage: {out}")
    print(f"sha256: {sha256(out)}")
    print(f"storage: {used >> 20} MiB used of {size >> 20} MiB (uuid {fs_uuid}); "
          f"needs an SD card of at least {total / 1e9:.1f} GB")
    print("Flash with Raspberry Pi Imager ('Use custom', NO OS customisation) or balenaEtcher.")
    print("The image contains your secrets - delete it after flashing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
