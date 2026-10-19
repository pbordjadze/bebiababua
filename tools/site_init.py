#!/usr/bin/env python3
"""Create site/ (git-ignored) from site.example/ and generate all keys.

Generates: WireGuard key pair + preshared key, the box's SSH host key (so
you can pin it before ever connecting) and the Kodi web password. Existing
values are never overwritten, so it is safe to re-run.
"""

import base64
import os
import re
import secrets
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.environ.get("BABUA_SITE", os.path.join(REPO, "site"))


def wg_keypair():
    if shutil.which("wg"):
        priv = subprocess.run(["wg", "genkey"], capture_output=True, text=True, check=True).stdout.strip()
        pub = subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, check=True).stdout.strip()
        return priv, pub
    # openssl fallback: raw X25519 keys are the last 32 bytes of the DER encodings
    der = subprocess.run(["openssl", "genpkey", "-algorithm", "X25519", "-outform", "DER"],
                         capture_output=True, check=True).stdout
    pub_der = subprocess.run(["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
                             input=der, capture_output=True, check=True).stdout
    return base64.b64encode(der[-32:]).decode(), base64.b64encode(pub_der[-32:]).decode()


def wg_pubkey(priv):
    if shutil.which("wg"):
        return subprocess.run(["wg", "pubkey"], input=priv, capture_output=True, text=True, check=True).stdout.strip()
    # PKCS#8 wrapper for a raw X25519 private key
    der = bytes.fromhex("302e020100300506032b656e04220420") + base64.b64decode(priv)
    pub_der = subprocess.run(["openssl", "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
                             input=der, capture_output=True, check=True).stdout
    return base64.b64encode(pub_der[-32:]).decode()


def set_value(text, key, value):
    return re.sub(rf"^{key}=.*$", f"{key}={value}", text, flags=re.M)


def get_value(text, key):
    m = re.search(rf"^{key}=(.*)$", text, flags=re.M)
    return m.group(1).strip() if m else ""


def main():
    if not os.path.isdir(SITE):
        shutil.copytree(os.path.join(REPO, "site.example"), SITE)
        print(f"created {SITE} from site.example/")
    os.chmod(SITE, 0o700)
    env_path = os.path.join(SITE, "secrets.env")
    text = open(env_path).read()

    if not get_value(text, "WG_PRIVATE_KEY"):
        priv, _ = wg_keypair()
        text = set_value(text, "WG_PRIVATE_KEY", priv)
    if not get_value(text, "WG_PRESHARED_KEY"):
        text = set_value(text, "WG_PRESHARED_KEY", base64.b64encode(secrets.token_bytes(32)).decode())
    if not get_value(text, "KODI_WEB_PASSWORD"):
        text = set_value(text, "KODI_WEB_PASSWORD", secrets.token_urlsafe(18))
    with open(env_path, "w") as fh:
        fh.write(text)
    os.chmod(env_path, 0o600)

    host_key = os.path.join(SITE, "ssh_host_ed25519_key")
    if not os.path.exists(host_key):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "babua-tv host key", "-f", host_key], check=True)

    keys = os.path.join(SITE, "authorized_keys")
    if not os.path.exists(keys) or not open(keys).read().strip():
        found = [os.path.expanduser(f"~/.ssh/{n}") for n in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub")]
        found = [p for p in found if os.path.exists(p)]
        with open(keys, "w") as fh:
            fh.write("# One public key per line. Add a second (backup) key, e.g. from your phone.\n")
            for p in found[:1]:
                fh.write(open(p).read())
        print(f"site/authorized_keys: {'added ' + found[0] if found else 'EMPTY - add your public key!'}")

    wg_pub = wg_pubkey(get_value(text, "WG_PRIVATE_KEY"))
    address = get_value(text, "WG_ADDRESS").split("/")[0] or "10.66.66.20"
    print(f"""
Next steps
  1. Fill in site/secrets.env: TAILSCALE_AUTHKEY (or join on site with
     `babua tailscale-up <key>`), NTFY_*, HEARTBEAT_URL, IPTV_M3U_URL.
  2. tools/babua check && tools/babua preview && tools/babua image

Only if you use your own WireGuard server instead of Tailscale, add this peer:
     [Peer]
     # babua-tv (grandparents)
     PublicKey = {wg_pub}
     PresharedKey = {get_value(text, 'WG_PRESHARED_KEY')}
     AllowedIPs = {address}/32
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
