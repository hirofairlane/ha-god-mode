#!/usr/bin/env python3
"""Finds new SSH-capable machines in the census and bootstraps them for
GOD Mode, so enrolling a host stops being "SSH in by hand and paste a key".

What it does, in order, for every alive device that isn't already blocked,
retired or approved, and whose `tipo` isn't an obviously non-SSH kind of
thing (phone, TV, smart plug, camera, printer, router, ...):

1. Probes TCP/22. Nothing else happens if it's closed — most of the census
   (phones, IoT) never gets touched.
2. If open, tries the GOD Mode pubkey (no password, `BatchMode=yes`). If
   that already works, the device is enrollable with zero further action.
3. Only with `--aplicar`, and only if step 2 failed: tries the default
   password from `infra.local.json` (`default_ssh_password`) exactly once.
   On success, installs the GOD Mode pubkey (same idempotent dance as the
   addon's own onboarding) so the password is never needed again.

Every device is probed at most once: the outcome is recorded as an
`avistamientos` event (`ssh_probado`), and later runs skip anything already
probed. That's a deliberate brake — this hits real devices on the LAN with
login attempts, and "probe everything every run" is how you get flagged by
whatever's watching those devices.

This never registers the device as a monitored GOD Mode host by itself:
the addon's onboarding API lives behind HA's ingress auth and isn't
reachable from here, and auto-registering arbitrary LAN devices as
root-SSH-managed hosts without a human looking first is a bad trade for
some saved clicks. What this script buys is that by the time a human
looks, the device already takes the GOD Mode key — onboarding is then
name + address + Add host, no password prompt.

Usage:
    ./auto_enroll.py                 # dry run: lists candidates, probes TCP/22 and the pubkey only
    ./auto_enroll.py --aplicar       # also tries the default password once per candidate and installs the pubkey on success
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG  # noqa: E402
from red_db_lib import abrir, ahora  # noqa: E402

GOD_PUBKEY_PATHS = (
    Path("/data/.ssh/id_ed25519.pub"),          # inside the GOD Mode addon container
    Path.home() / ".ssh" / "god_mode_id_ed25519.pub",  # local copy, if mirrored here
)

# Types that are never worth an SSH attempt — phones, IoT, appliances.
TIPOS_NO_SSH = {
    "movil", "tablet", "tele", "streaming", "altavoz", "camara",
    "enchufe", "sensor", "impresora", "router",
}

SSH_TIMEOUT = 8


def _god_pubkey() -> str | None:
    for p in GOD_PUBKEY_PATHS:
        if p.exists():
            return p.read_text().strip()
    return None


def _tcp_abierto(ip: str, puerto: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((ip, puerto), timeout=timeout):
            return True
    except OSError:
        return False


def _ssh_prueba(ip: str, user: str = "root") -> bool:
    """True if the GOD Mode pubkey already gets us in, no password."""
    r = subprocess.run(
        [*CONFIG["ssh_jump"],
         f"ssh -n -o BatchMode=yes -o ConnectTimeout={SSH_TIMEOUT} "
         f"-o StrictHostKeyChecking=accept-new {user}@{ip} echo OK_GOD_MODE"],
        capture_output=True, text=True, timeout=SSH_TIMEOUT + 10,
    )
    return "OK_GOD_MODE" in r.stdout


def _instalar_pubkey_con_password(ip: str, user: str, password: str, pubkey: str) -> tuple[bool, str]:
    """Same idempotent dance as the addon's own password bootstrap: writes
    to ~/.ssh/authorized_keys and, if present, /etc/dropbear/authorized_keys
    (OpenWrt). The password is only ever held in this process's memory."""
    cmd_remoto = (
        "set -e; mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qF '{pubkey}' ~/.ssh/authorized_keys || echo '{pubkey}' >> ~/.ssh/authorized_keys; "
        "if [ -d /etc/dropbear ] || command -v dropbear >/dev/null 2>&1; then "
        "  mkdir -p /etc/dropbear && touch /etc/dropbear/authorized_keys && "
        f"  grep -qF '{pubkey}' /etc/dropbear/authorized_keys || echo '{pubkey}' >> /etc/dropbear/authorized_keys; "
        "fi; echo OK_PUBKEY_INSTALLED"
    )
    try:
        r = subprocess.run(
            ["sshpass", "-e", "ssh",
             "-o", "BatchMode=no",
             "-o", "StrictHostKeyChecking=accept-new",
             "-o", "PreferredAuthentications=password",
             "-o", "PubkeyAuthentication=no",
             "-o", f"ConnectTimeout={SSH_TIMEOUT}",
             f"{user}@{ip}", cmd_remoto],
            env={"SSHPASS": password, "PATH": "/usr/bin:/bin"},
            capture_output=True, text=True, timeout=SSH_TIMEOUT + 15,
        )
        if "OK_PUBKEY_INSTALLED" in r.stdout:
            return True, "pubkey installed via default-password bootstrap"
        return False, "default password rejected"
    except FileNotFoundError:
        return False, "sshpass not installed on this host"
    except subprocess.TimeoutExpired:
        return False, "SSH connect timeout"


def candidatos(c) -> list[dict]:
    placeholders = ",".join("?" * len(TIPOS_NO_SSH))
    filas = c.execute(f"""
        SELECT d.* FROM dispositivos d
        WHERE d.vivo=1
          AND d.estado NOT IN ('bloqueado','retirado','aprobado')
          AND (d.tipo IS NULL OR d.tipo NOT IN ({placeholders}))
          AND NOT EXISTS (
              SELECT 1 FROM avistamientos a
              WHERE a.mac = d.mac AND a.evento = 'ssh_probado'
          )
        ORDER BY d.mac
    """, tuple(TIPOS_NO_SSH)).fetchall()
    return [dict(f) for f in filas]


def marcar_probado(c, mac: str, resultado: str) -> None:
    c.execute(
        "INSERT OR IGNORE INTO avistamientos (mac, ts, evento, detalle) VALUES (?,?,?,?)",
        (mac, ahora(), "ssh_probado", resultado),
    )
    c.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                     help="also try the default password once per candidate (default is probe-only)")
    args = ap.parse_args()

    pubkey = _god_pubkey()
    if not pubkey:
        print("No GOD Mode pubkey found locally (checked: "
              + ", ".join(str(p) for p in GOD_PUBKEY_PATHS) + "). "
              "Pull id_ed25519.pub from the addon's /data/.ssh/ first.")
        return 1

    password = CONFIG.get("default_ssh_password") or ""
    if args.aplicar and not password:
        print("--aplicar given but no 'default_ssh_password' set in infra.local.json; "
              "will still probe the pubkey, just won't try a password.")

    c = abrir()
    cands = candidatos(c)
    if not cands:
        print("No new SSH-shaped candidates (either none alive+unapproved, or all already probed).")
        return 0

    print(f"{len(cands)} candidate(s):\n")
    ready, bootstrapped, closed, no_pubkey, no_password = [], [], [], [], []
    for d in cands:
        ip = d.get("ip") or ""
        nombre = d.get("hostname") or d.get("descripcion") or d["mac"]
        if not ip or not _tcp_abierto(ip, 22):
            print(f"  {d['mac']}  {nombre[:30]:32} {ip or '?':16} tcp/22 closed")
            closed.append(d)
            marcar_probado(c, d["mac"], "tcp_22_cerrado")
            continue

        if _ssh_prueba(ip):
            print(f"  {d['mac']}  {nombre[:30]:32} {ip:16} ✓ GOD Mode key already works")
            ready.append(d)
            marcar_probado(c, d["mac"], "pubkey_ok")
            continue

        if not args.aplicar:
            print(f"  {d['mac']}  {nombre[:30]:32} {ip:16} tcp/22 open, pubkey not installed yet")
            continue  # don't mark probed: a later --aplicar run should still try it

        if not password:
            print(f"  {d['mac']}  {nombre[:30]:32} {ip:16} pubkey failed, no default password configured")
            no_password.append(d)
            marcar_probado(c, d["mac"], "sin_password_configurada")
            continue

        ok, msg = _instalar_pubkey_con_password(ip, "root", password, pubkey)
        print(f"  {d['mac']}  {nombre[:30]:32} {ip:16} {'✓ ' + msg if ok else '✗ ' + msg}")
        if ok:
            bootstrapped.append(d)
            marcar_probado(c, d["mac"], "password_bootstrap_ok")
        else:
            no_pubkey.append(d)
            marcar_probado(c, d["mac"], f"password_bootstrap_fallo: {msg}")

    print(f"\n{len(ready) + len(bootstrapped)} ready to onboard with no password "
          f"(GOD Mode's Onboard tab, with addr + name — SSH already works).")
    if closed:
        print(f"{len(closed)} had tcp/22 closed (not an SSH host, or firewalled).")
    if no_password or no_pubkey:
        print(f"{len(no_password) + len(no_pubkey)} need the pubkey pasted in by hand "
              f"(no default password configured, or it didn't work).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
