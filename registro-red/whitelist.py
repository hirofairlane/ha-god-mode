#!/usr/bin/env python3
"""Pushes the registry's whitelist down to the OpenWrt routers.

What it does: devices marked `bloqueado` (blocked) in the registry end up in
each AP's `maclist`, and get kicked off the wifi. Nothing else.

=========================  WHY `deny` MODE  =========================

OpenWrt has two `macfilter` modes:

    allow  - only who's on the list gets in   (whitelist on the router)
    deny   - everyone gets in except the list (blacklist on the router)

This program **always** uses `deny`, even though the registry itself is a
whitelist. The reason: with `allow`, any single mistake — a mistyped MAC, a
phone rotating its MAC, a device that hasn't been censused yet — locks out
half the house, and fixing it means getting to the router over a cable. With
`deny`, the worst case is that something that should be blocked stays
connected, which is a visible failure that doesn't cut anyone off.

The whitelist lives in the registry (`estado='aprobado'`); the router only
ever runs the negative half of it.

===========================  WHAT IT NEVER TOUCHES  ===============================

1. **Backhaul interfaces.** One AP has `wireless.bh5` with
   `macfilter='allow'` and the link's MAC: that's the radio link to another
   AP. Writing there cuts that AP off from the network. Rule: any section
   whose `macfilter` is `allow`, and any section whose name starts with
   `bh`, is ignored.
2. **Interfaces with no known SSID.** If we don't know what network it is,
   it isn't touched.

==========================  AND WHAT IT NEVER DOES  ===========================

**Never writes `macfilter='disable'`.** Setting that where there was no
option before stops `radio0` from coming up at all on OpenWrt 25.12: the log
loops ("Configuring phy0 / Preparing phy0-ap0 / Tearing down phy0"),
`retry_setup_failed` gets stuck at true, and the 2.4 GHz band disappears.
This cost us two APs down for almost a day. To stop filtering, `uci delete`
is used instead.

It also avoids `wifi reload`, which drops every client on the radio. The
`maclist` is applied with `uci commit` (persistent), and the blocked device
is kicked with `ubus call hostapd.<iface> del_client`, which only affects
that one device.

Usage:
    ./whitelist.py                  # dry run: says what it would do
    ./whitelist.py --aplicar        # writes to the routers
    ./whitelist.py --router Router1 --aplicar
    ./whitelist.py --auditar        # what the routers are already blocking on their own
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG  # noqa: E402
from red_db_lib import abrir  # noqa: E402

# Name -> IP, from infra.local.json. Some AP may be behind a radio link and
# not always respond; it's tried anyway, and if it doesn't answer that's
# reported and the rest continue.
ROUTERS = CONFIG["access_points"]

MAC_OK = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Sections that are never touched (see module header).
PREFIJOS_INTOCABLES = ("bh", "backhaul", "mesh", "wds")


def macs_bloqueadas() -> list[str]:
    """The MACs the registry says must not be on the wifi."""
    c = abrir()
    filas = c.execute(
        "SELECT mac FROM dispositivos WHERE estado='bloqueado' ORDER BY mac"
    ).fetchall()
    return [f["mac"].lower() for f in filas if MAC_OK.match(f["mac"].lower())]


def parsea_wireless(salida: str) -> dict[str, dict]:
    """Converts `uci show wireless` output into section -> options.

    A pure function, so it can be tested without routers in front of it.
    """
    secciones: dict[str, dict] = {}
    for linea in salida.splitlines():
        linea = linea.strip()
        if not linea or "=" not in linea:
            continue
        izquierda, derecha = linea.split("=", 1)
        partes = izquierda.split(".")
        if len(partes) != 3:
            continue          # wireless.<section> (no option): not relevant
        _, seccion, opcion = partes
        valor = derecha.strip()
        if opcion == "maclist":
            # uci lists several values in quotes, space-separated
            secciones.setdefault(seccion, {})[opcion] = [
                v.strip("'\"").lower() for v in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", valor)
            ]
        else:
            secciones.setdefault(seccion, {})[opcion] = valor.strip("'\"")
    return secciones


def gestionable(seccion: str, opciones: dict) -> tuple[bool, str]:
    """Can we write this interface's maclist? And if not, why."""
    if any(seccion.lower().startswith(p) for p in PREFIJOS_INTOCABLES):
        return False, "backhaul by name"
    if opciones.get("macfilter") == "allow":
        return False, "in allow mode (backhaul or manual whitelist)"
    if not opciones.get("ssid"):
        return False, "no ssid"
    return True, ""


def planificar(secciones: dict[str, dict], bloqueadas: list[str]) -> dict[str, dict]:
    """What would need to change in each section. A pure function.

    Returns section -> {ssid, faltan, sobran, gestionable, motivo}
    (ssid, missing, extra, manageable, reason). `sobran` (extra) are MACs
    that are on the router and no longer blocked in the registry: they get
    removed, so the filter doesn't accumulate old junk.
    """
    plan: dict[str, dict] = {}
    for seccion, opciones in secciones.items():
        ok, motivo = gestionable(seccion, opciones)
        actual = set(opciones.get("maclist") or [])
        deseada = set(bloqueadas)
        plan[seccion] = {
            "ssid": opciones.get("ssid", ""),
            "gestionable": ok,
            "motivo": motivo,
            "actual": sorted(actual),
            "faltan": sorted(deseada - actual),
            "sobran": sorted(actual - deseada),
        }
    return plan


def auditar() -> int:
    """Lists what the routers block on their own. Writes nothing.

    This started out as a `--importar` that marked those MACs as `bloqueado`
    in the registry, and that was a serious mistake: the registry's `estado`
    is **global**, but these filters are **per AP**. Three MACs that showed
    up on one AP (a printer, a smart plug and a pool-pump controller) aren't
    kicked off the network: they're kept off THAT access point so they
    associate with another one. Importing them and syncing would have
    thrown them off the whole house, which is the exact opposite of why
    they were put there in the first place.

    So they get listed, left alone, and the decision of what to do with
    them belongs to whoever put them there.
    """
    encontradas: dict[str, list[str]] = {}
    for nombre, ip in ROUTERS.items():
        ok, salida = _ssh(ip, "uci show wireless")
        if not ok or "wireless." not in salida:
            print(f"  {nombre:10} NO ACCESS")
            continue
        for seccion, opciones in parsea_wireless(salida).items():
            puede, _ = gestionable(seccion, opciones)
            if not puede:
                continue           # backhaul doesn't count as a block
            for m in opciones.get("maclist") or []:
                encontradas.setdefault(m, []).append(f"{nombre}/{opciones.get('ssid', '')}")

    del_registro = set(macs_bloqueadas())
    ajenas = {m: d for m, d in encontradas.items() if m not in del_registro}
    if not ajenas:
        print("The routers aren't blocking anything outside the registry.")
        return 0

    c = abrir()
    print("Per-AP filters set by hand (NOT touched by this tool):\n")
    for mac, donde in sorted(ajenas.items()):
        fila = c.execute("SELECT descripcion FROM dispositivos WHERE mac=?", (mac,)).fetchone()
        quien = (fila["descripcion"] if fila else None) or "(not in the registry)"
        print(f"  {mac}  {quien[:40]:42} on {', '.join(donde)}")
    print("\nThese are per-access-point decisions, not network-wide bans.")
    return 0


def _ssh(ip: str, guion: str, timeout: int = 40) -> tuple[bool, str]:
    """Runs on the router, through the configured jump host (`ssh_jump`),
    since that's where the SSH keys to the routers live."""
    orden = [*CONFIG["ssh_jump"],
             f"ssh -n -o ConnectTimeout=8 -o BatchMode=yes root@{ip} {json.dumps(guion)}"]
    try:
        r = subprocess.run(orden, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr)
    except subprocess.TimeoutExpired:
        return False, "timeout"


def aplica_router(nombre: str, ip: str, bloqueadas: list[str], escribir: bool,
                  retirar: bool = False) -> None:
    ok, salida = _ssh(ip, "uci show wireless")
    if not ok or "wireless." not in salida:
        print(f"  {nombre:10} NO ACCESS ({salida.strip()[:60]})")
        return

    plan = planificar(parsea_wireless(salida), bloqueadas)
    hubo = False
    for seccion, p in sorted(plan.items()):
        if not p["gestionable"]:
            if p["faltan"]:
                print(f"  {nombre:10} {seccion:12} LEFT ALONE — {p['motivo']}")
            continue
        sobran = p["sobran"] if retirar else []
        if p["sobran"] and not retirar:
            print(f"  {nombre:10} {seccion:12} {len(p['sobran'])} blocked on the router "
                  f"that the registry doesn't know about — left as is "
                  f"(--importar to adopt them, --retirar-sobrantes to remove them)")
        if not p["faltan"] and not sobran:
            continue
        hubo = True
        print(f"  {nombre:10} {seccion:12} ssid={p['ssid']:15} "
              f"+{len(p['faltan'])} -{len(p['sobran'])}")
        for m in p["faltan"]:
            print(f"    + block {m}")
        for m in sobran:
            print(f"    - unblock {m}")

        if not escribir:
            continue

        deseada = sorted(set(p["actual"]) - set(sobran) | set(p["faltan"]))
        if deseada:
            lista = " ".join(f"'{m}'" for m in deseada)
            guion = (
                f"cp /etc/config/wireless /etc/config/wireless.bak.$(date +%Y%m%d-%H%M%S); "
                f"uci delete wireless.{seccion}.maclist 2>/dev/null; "
                f"for m in {lista}; do uci add_list wireless.{seccion}.maclist=$m; done; "
                f"uci set wireless.{seccion}.macfilter='deny'; "
                f"uci commit wireless && echo APPLIED"
            )
        else:
            # With nobody blocked, the whole filter is removed. With
            # `uci delete`, NEVER with macfilter='disable' (see module header).
            guion = (
                f"cp /etc/config/wireless /etc/config/wireless.bak.$(date +%Y%m%d-%H%M%S); "
                f"uci delete wireless.{seccion}.maclist 2>/dev/null; "
                f"uci delete wireless.{seccion}.macfilter 2>/dev/null; "
                f"uci commit wireless && echo APPLIED"
            )
        ok, salida = _ssh(ip, guion)
        print(f"    -> {'applied' if 'APPLIED' in salida else 'ERROR: ' + salida.strip()[:80]}")

        # Kick the blocked device without restarting the radio: only affects it.
        for m in p["faltan"]:
            _ssh(ip, f"for i in $(ubus list | grep hostapd. | cut -d. -f2); do "
                     f"ubus call hostapd.$i del_client "
                     f"'{{\"addr\":\"{m}\",\"reason\":5,\"deauth\":true,\"ban_time\":60000}}' "
                     f"2>/dev/null; done; echo KICKED")

    if not hubo:
        print(f"  {nombre:10} up to date")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aplicar", action="store_true", help="write (default is dry run)")
    ap.add_argument("--router", help="only this router")
    ap.add_argument("--auditar", action="store_true",
                    help="list what's already blocked on the routers and not from the registry")
    ap.add_argument("--retirar-sobrantes", action="store_true",
                    help="remove from the router what the registry no longer blocks (see warning)")
    args = ap.parse_args()

    if args.auditar:
        return auditar()

    bloqueadas = macs_bloqueadas()
    print(f"Blocked in the registry: {len(bloqueadas)}")
    for m in bloqueadas:
        print(f"  {m}")
    if not args.aplicar:
        print("\n(dry run — nothing is written. Repeat with --aplicar)\n")

    for nombre, ip in ROUTERS.items():
        if args.router and args.router.lower() != nombre.lower():
            continue
        aplica_router(nombre, ip, bloqueadas, args.aplicar, args.retirar_sobrantes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
