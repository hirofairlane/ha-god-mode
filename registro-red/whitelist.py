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

# Nombre -> IP, desde infra.local.json. Algun AP puede entrar por un enlace de
# radio y no siempre responder; se intenta igual, y si no contesta se dice y
# se sigue con los demas.
ROUTERS = CONFIG["access_points"]

MAC_OK = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")

# Secciones que no se tocan jamas (ver cabecera).
PREFIJOS_INTOCABLES = ("bh", "backhaul", "mesh", "wds")


def macs_bloqueadas() -> list[str]:
    """Las MAC que el registro dice que no deben estar en la wifi."""
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
            continue          # wireless.<seccion> (sin opcion): no interesa
        _, seccion, opcion = partes
        valor = derecha.strip()
        if opcion == "maclist":
            # uci lista varios valores entre comillas separados por espacio
            secciones.setdefault(seccion, {})[opcion] = [
                v.strip("'\"").lower() for v in re.findall(r"'[^']*'|\"[^\"]*\"|\S+", valor)
            ]
        else:
            secciones.setdefault(seccion, {})[opcion] = valor.strip("'\"")
    return secciones


def gestionable(seccion: str, opciones: dict) -> tuple[bool, str]:
    """Can we write this interface's maclist? And if not, why."""
    if any(seccion.lower().startswith(p) for p in PREFIJOS_INTOCABLES):
        return False, "backhaul por nombre"
    if opciones.get("macfilter") == "allow":
        return False, "esta en modo allow (backhaul o whitelist manual)"
    if not opciones.get("ssid"):
        return False, "sin ssid"
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
    """Lista lo que los routers bloquean por su cuenta. No escribe nada.

    Empezo siendo un `--importar` que marcaba esas MAC como `bloqueado` en el
    registro, y era un error grave: el `estado` del registro es **global** y
    estos filtros son **por AP**. Las tres MAC que aparecieron en el Sotano
    (la impresora HP, el Shelly 4PM y el riego de la caseta) no estan
    expulsadas de la red: estan apartadas de ESE punto de acceso para que se
    asocien a otro. Importarlas y sincronizar las habria echado de toda la
    casa, que es justo lo contrario de lo que se quiso al ponerlas.

    Asi que se listan, se respetan, y la decision de que hacer con ellas es
    de quien las puso.
    """
    encontradas: dict[str, list[str]] = {}
    for nombre, ip in ROUTERS.items():
        ok, salida = _ssh(ip, "uci show wireless")
        if not ok or "wireless." not in salida:
            print(f"  {nombre:10} SIN ACCESO")
            continue
        for seccion, opciones in parsea_wireless(salida).items():
            puede, _ = gestionable(seccion, opciones)
            if not puede:
                continue           # el backhaul no cuenta como bloqueo
            for m in opciones.get("maclist") or []:
                encontradas.setdefault(m, []).append(f"{nombre}/{opciones.get('ssid', '')}")

    del_registro = set(macs_bloqueadas())
    ajenas = {m: d for m, d in encontradas.items() if m not in del_registro}
    if not ajenas:
        print("Los routers no bloquean nada que no venga del registro.")
        return 0

    c = abrir()
    print("Filtros por AP puestos a mano (NO los toca esta herramienta):\n")
    for mac, donde in sorted(ajenas.items()):
        fila = c.execute("SELECT descripcion FROM dispositivos WHERE mac=?", (mac,)).fetchone()
        quien = (fila["descripcion"] if fila else None) or "(no esta en el registro)"
        print(f"  {mac}  {quien[:40]:42} en {', '.join(donde)}")
    print("\nSon decisiones por punto de acceso, no expulsiones de la red.")
    return 0


def _ssh(ip: str, guion: str, timeout: int = 40) -> tuple[bool, str]:
    """Ejecuta en el router. Se va por LXC 104, que es quien tiene las llaves."""
    orden = ["ssh", "zeratul", f"pct exec 104 -- ssh -n -o ConnectTimeout=8 "
                               f"-o BatchMode=yes root@{ip} {json.dumps(guion)}"]
    try:
        r = subprocess.run(orden, capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0, (r.stdout or r.stderr)
    except subprocess.TimeoutExpired:
        return False, "timeout"


def aplica_router(nombre: str, ip: str, bloqueadas: list[str], escribir: bool,
                  retirar: bool = False) -> None:
    ok, salida = _ssh(ip, "uci show wireless")
    if not ok or "wireless." not in salida:
        print(f"  {nombre:10} SIN ACCESO ({salida.strip()[:60]})")
        return

    plan = planificar(parsea_wireless(salida), bloqueadas)
    hubo = False
    for seccion, p in sorted(plan.items()):
        if not p["gestionable"]:
            if p["faltan"]:
                print(f"  {nombre:10} {seccion:12} SE RESPETA — {p['motivo']}")
            continue
        sobran = p["sobran"] if retirar else []
        if p["sobran"] and not retirar:
            print(f"  {nombre:10} {seccion:12} {len(p['sobran'])} bloqueadas en el router "
                  f"que el registro no conoce — se dejan "
                  f"(--importar para adoptarlas, --retirar-sobrantes para quitarlas)")
        if not p["faltan"] and not sobran:
            continue
        hubo = True
        print(f"  {nombre:10} {seccion:12} ssid={p['ssid']:15} "
              f"+{len(p['faltan'])} -{len(p['sobran'])}")
        for m in p["faltan"]:
            print(f"    + bloquear {m}")
        for m in sobran:
            print(f"    - desbloquear {m}")

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
                f"uci commit wireless && echo APLICADO"
            )
        else:
            # Sin nadie bloqueado se retira el filtro entero. Con `uci delete`,
            # NUNCA con macfilter='disable' (ver cabecera).
            guion = (
                f"cp /etc/config/wireless /etc/config/wireless.bak.$(date +%Y%m%d-%H%M%S); "
                f"uci delete wireless.{seccion}.maclist 2>/dev/null; "
                f"uci delete wireless.{seccion}.macfilter 2>/dev/null; "
                f"uci commit wireless && echo APLICADO"
            )
        ok, salida = _ssh(ip, guion)
        print(f"    -> {'aplicado' if 'APLICADO' in salida else 'ERROR: ' + salida.strip()[:80]}")

        # Echar al bloqueado sin reiniciar la radio: solo le afecta a el.
        for m in p["faltan"]:
            _ssh(ip, f"for i in $(ubus list | grep hostapd. | cut -d. -f2); do "
                     f"ubus call hostapd.$i del_client "
                     f"'{{\"addr\":\"{m}\",\"reason\":5,\"deauth\":true,\"ban_time\":60000}}' "
                     f"2>/dev/null; done; echo ECHADO")

    if not hubo:
        print(f"  {nombre:10} al dia")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--aplicar", action="store_true", help="escribe (por defecto, ensayo en seco)")
    ap.add_argument("--router", help="solo este router")
    ap.add_argument("--auditar", action="store_true",
                    help="lista lo que ya esta bloqueado en los routers y no viene del registro")
    ap.add_argument("--retirar-sobrantes", action="store_true",
                    help="quita del router lo que el registro ya no bloquea (ver aviso)")
    args = ap.parse_args()

    if args.auditar:
        return auditar()

    bloqueadas = macs_bloqueadas()
    print(f"Bloqueadas en el registro: {len(bloqueadas)}")
    for m in bloqueadas:
        print(f"  {m}")
    if not args.aplicar:
        print("\n(ensayo en seco — no se escribe nada. Repite con --aplicar)\n")

    for nombre, ip in ROUTERS.items():
        if args.router and args.router.lower() != nombre.lower():
            continue
        aplica_router(nombre, ip, bloqueadas, args.aplicar, args.retirar_sobrantes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
