#!/usr/bin/env python3
"""Lleva la whitelist del registro a los routers OpenWrt.

Qué hace: los aparatos marcados `bloqueado` en el registro acaban en la
`maclist` de cada AP, y se les echa de la wifi. Nada más.

=========================  POR QUÉ EN MODO `deny`  =========================

OpenWrt tiene dos modos de `macfilter`:

    allow  - solo entra quien está en la lista  (whitelist en el router)
    deny   - entra todo el mundo menos la lista (blacklist en el router)

Este programa usa **siempre `deny`**, aunque el registro sea una whitelist.
El motivo es que con `allow` un fallo cualquiera —una MAC mal escrita, un
móvil que rota su MAC, un aparato que aún no se ha censado— deja fuera a
media casa, y para arreglarlo hay que llegar al router por cable. Con `deny`
el peor caso es que algo que debería estar bloqueado siga conectado, que es
un fallo que se ve y no corta nada.

La whitelist vive en el registro (`estado='aprobado'`); el router solo
ejecuta la parte negativa.

===========================  LO QUE NO TOCA  ===============================

1. **Interfaces de backhaul.** El Salón tiene `wireless.bh5` con
   `macfilter='allow'` y la MAC del enlace: es la radio que une el salón con
   la caseta. Escribir ahí deja la caseta incomunicada. Regla: se ignora toda
   sección cuyo `macfilter` sea `allow`, y toda sección cuyo nombre empiece
   por `bh`.
2. **Interfaces sin SSID conocido.** Si no sabemos qué red es, no se toca.

==========================  Y LO QUE NUNCA HACE  ===========================

**Nunca escribe `macfilter='disable'`.** Poner ese valor donde antes no había
opción impide que `radio0` levante en OpenWrt 25.12: el log entra en bucle
("Configuring phy0 / Preparing phy0-ap0 / Tearing down phy0"),
`retry_setup_failed` se queda en true y la banda de 2,4 GHz desaparece. Nos
costó dos APs caídos casi un día. Para dejar de filtrar se usa `uci delete`.

También evita `wifi reload`, que tira a todos los clientes de la radio. La
`maclist` se aplica con `uci commit` (persistente) y al aparato bloqueado se
le echa con `ubus call hostapd.<iface> del_client`, que solo le afecta a él.

Uso:
    ./whitelist.py                # ensayo en seco: dice qué haría
    ./whitelist.py --aplicar      # escribe en los routers
    ./whitelist.py --router Sotano --aplicar\n    ./whitelist.py --auditar      # que bloquean los routers por su cuenta
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from red_db_lib import abrir  # noqa: E402

# Nombre -> IP. La Caseta entra por un enlace de radio y no siempre responde;
# se intenta igual, y si no contesta se dice y se sigue con los demas.
ROUTERS = {
    "Principal": "192.168.0.1",
    "Salon":     "192.168.1.6",
    "Sotano":    "192.168.1.7",
    "Arriba":    "192.168.1.8",
    "Caseta":    "192.168.1.3",
}

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
    """Convierte la salida de `uci show wireless` en sección -> opciones.

    Funcion pura, para poder probarla sin routers delante.
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
    """¿Podemos escribir la maclist de esta interfaz? Y si no, por qué."""
    if any(seccion.lower().startswith(p) for p in PREFIJOS_INTOCABLES):
        return False, "backhaul por nombre"
    if opciones.get("macfilter") == "allow":
        return False, "esta en modo allow (backhaul o whitelist manual)"
    if not opciones.get("ssid"):
        return False, "sin ssid"
    return True, ""


def planificar(secciones: dict[str, dict], bloqueadas: list[str]) -> dict[str, dict]:
    """Qué habría que cambiar en cada sección. Función pura.

    Devuelve seccion -> {ssid, faltan, sobran, gestionable, motivo}.
    `sobran` son MAC que estan en el router y ya no estan bloqueadas en el
    registro: se quitan, para que el filtro no se quede con basura vieja.
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
