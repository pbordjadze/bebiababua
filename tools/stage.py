#!/usr/bin/env python3
"""Render the files that belong in the box's /storage.

Sources: device/ (static overlay), config/ (behaviour + channels, committed)
and site/ (secrets + keys, git-ignored). Used by build_image.py for the
flashable image and by `tools/babua deploy` for over-the-air updates.

  stage.py --out DIR         write the tree to DIR
  stage.py --tar [--update]  write a .tar.gz to stdout (owner root);
                             --update leaves out first-boot-only files
  stage.py --check           validate config and site, print problems
"""

import argparse
import io
import os
import shutil
import sys
import tarfile
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "device", ".config", "babua", "bin"))
import babua_lib as B  # noqa: E402

# Only written into a fresh image: LibreELEC or Kodi own them afterwards.
FIRST_BOOT_ONLY = {
    ".kodi/userdata/addon_data/service.libreelec.settings/oe_settings.xml",
    ".cache/ssh/ssh_host_ed25519_key",
    ".cache/ssh/ssh_host_ed25519_key.pub",
}

WANTS = {
    "multi-user.target.wants": ["babuad.service", "babua-vpn.service"],
    "timers.target.wants": ["babua-playlist.timer", "babua-health.timer",
                            "babua-streamcheck.timer", "babua-reboot.timer"],
    "kodi.service.wants": ["babua-prekodi.service"],
}


class SiteError(Exception):
    pass


def load_site(site_dir):
    secrets_path = os.path.join(site_dir, "secrets.env")
    if not os.path.exists(secrets_path):
        raise SiteError(f"{secrets_path} missing - run `tools/babua init` first")
    cfg = B.parse_env_file(os.path.join(REPO, "config", "babua.conf"))
    cfg.update(B.parse_env_file(secrets_path))
    return cfg


def check(cfg, site_dir):
    """Return (errors, warnings)."""
    errors, warnings = [], []
    keys_path = os.path.join(site_dir, "authorized_keys")
    keys = open(keys_path).read() if os.path.exists(keys_path) else ""
    if not any(line.startswith(("ssh-", "ecdsa-", "sk-")) for line in keys.splitlines()):
        errors.append("site/authorized_keys has no SSH public key - you would be locked out")
    if not os.path.exists(os.path.join(site_dir, "ssh_host_ed25519_key")):
        errors.append("site/ssh_host_ed25519_key missing - run `tools/babua init`")
    if not cfg.get("IPTV_M3U_URL"):
        warnings.append("IPTV_M3U_URL is empty - the TV will have no channels")
    if cfg.get("WG_ENDPOINT"):
        for key in ("WG_ADDRESS", "WG_ALLOWED_IPS", "WG_PRIVATE_KEY", "WG_SERVER_PUBLIC_KEY"):
            if not cfg.get(key):
                errors.append(f"{key} is empty but WG_ENDPOINT is set")
        if "0.0.0.0/0" in cfg.get("WG_ALLOWED_IPS", ""):
            warnings.append("WG_ALLOWED_IPS routes everything through the VPN (IPTV too)")
    else:
        warnings.append("WG_ENDPOINT empty - no VPN, remote admin only from grandma's LAN")
    if not cfg.get("NTFY_URL"):
        warnings.append("NTFY_URL empty - no alerts")
    if not cfg.get("HEARTBEAT_URL"):
        warnings.append("HEARTBEAT_URL empty - nobody notices if the box goes offline")
    try:
        sys.path.insert(0, os.path.join(REPO, "device", ".config", "babua", "bin"))
        import playlist
        playlist.load_channels(os.path.join(REPO, "config", "channels.toml"))
    except Exception as exc:  # noqa: BLE001
        errors.append(f"config/channels.toml: {exc}")
    return errors, warnings


def wireguard_config(cfg):
    lines = [
        "[provider_wireguard]",
        "Type = WireGuard",
        "Name = babua homelab",
        f"Host = {cfg['WG_ENDPOINT']}",
        f"WireGuard.Address = {cfg['WG_ADDRESS']}",
        f"WireGuard.PrivateKey = {cfg['WG_PRIVATE_KEY']}",
        f"WireGuard.PublicKey = {cfg['WG_SERVER_PUBLIC_KEY']}",
        f"WireGuard.AllowedIPs = {cfg['WG_ALLOWED_IPS']}",
        f"WireGuard.EndpointPort = {cfg.get('WG_ENDPOINT_PORT', '51820')}",
        "WireGuard.PersistentKeepalive = 25",
    ]
    if cfg.get("WG_PRESHARED_KEY"):
        lines.append(f"WireGuard.PresharedKey = {cfg['WG_PRESHARED_KEY']}")
    return "\n".join(lines) + "\n"


def wifi_config(cfg):
    return (
        "[service_babua_wifi]\n"
        "Type = wifi\n"
        f"Name = {cfg['WIFI_SSID']}\n"
        f"Passphrase = {cfg['WIFI_PSK']}\n"
        "AutoConnect = true\n"
    )


def put(root, rel, data, mode=0o644):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)
    os.chmod(path, mode)


def stage(out, site_dir, first_boot=True):
    cfg = load_site(site_dir)
    errors, _ = check(cfg, site_dir)
    if errors:
        raise SiteError("\n".join(errors))

    shutil.copytree(os.path.join(REPO, "device"), out, dirs_exist_ok=True, symlinks=True)
    for rel in FIRST_BOOT_ONLY if not first_boot else ():
        p = os.path.join(out, rel)
        if os.path.exists(p):
            os.remove(p)

    bin_dir = os.path.join(out, ".config", "babua", "bin")
    for name in os.listdir(bin_dir):
        os.chmod(os.path.join(bin_dir, name), 0o755)
    shutil.rmtree(os.path.join(bin_dir, "__pycache__"), ignore_errors=True)

    # behaviour + channels + logos (committed)
    babua_dir = os.path.join(out, ".config", "babua")
    shutil.copy(os.path.join(REPO, "config", "babua.conf"), babua_dir)
    shutil.copy(os.path.join(REPO, "config", "channels.toml"), babua_dir)
    shutil.copytree(os.path.join(REPO, "config", "logos"), os.path.join(babua_dir, "logos"),
                    dirs_exist_ok=True, ignore=shutil.ignore_patterns(".gitkeep"))

    # secrets (site/)
    put(out, ".config/babua/secrets.env", open(os.path.join(site_dir, "secrets.env")).read(), 0o600)
    os.makedirs(os.path.join(out, ".ssh"), mode=0o700, exist_ok=True)
    put(out, ".ssh/authorized_keys", open(os.path.join(site_dir, "authorized_keys")).read(), 0o600)
    if first_boot:
        for name, mode in (("ssh_host_ed25519_key", 0o600), ("ssh_host_ed25519_key.pub", 0o644)):
            put(out, f".cache/ssh/{name}", open(os.path.join(site_dir, name), "rb").read(), mode)

    if cfg.get("WG_ENDPOINT"):
        put(out, ".config/wireguard/babua.config", wireguard_config(cfg), 0o600)
    if cfg.get("WIFI_SSID"):
        put(out, ".cache/connman/babua-wifi.config", wifi_config(cfg), 0o600)

    put(out, ".cache/hostname", cfg.get("HOSTNAME", "babua-tv") + "\n")
    put(out, ".cache/timezone", f"TIMEZONE={cfg.get('TIMEZONE', 'UTC')}\n")
    put(out, ".profile", 'PATH="$PATH:/storage/.config/babua/bin"\nexport PATH\n')

    sysd = os.path.join(out, ".config", "system.d")
    for wants, units in WANTS.items():
        os.makedirs(os.path.join(sysd, wants), exist_ok=True)
        for unit in units:
            link = os.path.join(sysd, wants, unit)
            if not os.path.lexists(link):
                os.symlink(f"../{unit}", link)
    return cfg


def to_tar(src, fileobj):
    def owner_root(info):
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        return info

    with tarfile.open(fileobj=fileobj, mode="w:gz") as tar:
        for name in sorted(os.listdir(src)):
            tar.add(os.path.join(src, name), arcname=name, filter=owner_root)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=os.path.join(REPO, "site"))
    ap.add_argument("--out")
    ap.add_argument("--tar", action="store_true")
    ap.add_argument("--update", action="store_true", help="leave out first-boot-only files")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    try:
        if args.check:
            cfg = load_site(args.site)
            errors, warnings = check(cfg, args.site)
            for w in warnings:
                print("warning:", w)
            for e in errors:
                print("ERROR:", e)
            print("ok" if not errors else "fix the errors above")
            return 1 if errors else 0
        if args.out:
            stage(args.out, args.site, first_boot=not args.update)
            return 0
        if args.tar:
            with tempfile.TemporaryDirectory() as tmp:
                stage(tmp, args.site, first_boot=not args.update)
                buf = io.BytesIO()
                to_tar(tmp, buf)
                sys.stdout.buffer.write(buf.getvalue())
            return 0
    except SiteError as exc:
        print(exc, file=sys.stderr)
        return 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
