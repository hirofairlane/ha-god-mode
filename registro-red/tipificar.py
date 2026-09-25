#!/usr/bin/env python3
"""Deduce el `tipo` de cada aparato a partir de lo que ya sabemos de el.

Por que existe: clasificar 180 aparatos a mano es exactamente la friccion que
hizo abandonar el enrollment anterior. Las descripciones del censo ya dicen
"Camara CCTV Hikvision" o "MacBook Pro de Sergio"; leerlas es gratis.

Dos reglas de diseno:

- **Solo rellena lo que esta vacio.** Nunca pisa un tipo puesto a mano.
- **Ante la duda, no inventa.** Lo que no encaja se queda en NULL y aparece en
  el filtro `sin_tipo` de la interfaz, que es donde se repasa.

El orden de las reglas importa: la primera que casa, gana. Por eso `camara` va
antes que `impresora` (si no, "PrusaMiniCam" saldria impresora).

Uso:
    ./tipificar.py            # ensayo en seco, no toca nada
    ./tipificar.py --aplicar  # escribe
"""
from __future__ import annotations

import re
import sys
from collections import Counter

from red_db_lib import abrir, ahora

# (tipo, patron). Se evalua contra descripcion + hostname + vendor, en minusculas.
REGLAS: list[tuple[str, str]] = [
    ("camara",    r"cctv|c[aá]mara|camera|ipcam|blink|espcam|videoportero|doorbell|reolink|hikvision"),
    ("altavoz",   r"\becho\b|alexa|sonos|homepod|altavoz|home[- ]theater|soundbar"),
    ("streaming", r"fire ?tv|chromecast|\broku\b|shield|apple ?tv|\bkodi\b"),
    ("tele",      r"\btv\b|\btele\b|televis|smart[- ]?tv"),
    ("movil",     r"m[oó]vil|pixel|redmi|iphone|galaxy|oneplus|\bphone\b|\bmoto\b|huawei p\d"),
    ("tablet",    r"tablet|\bipad\b|lenovo ?tab"),
    ("portatil",  r"macbook|laptop|port[aá]til|thinkpad|notebook|\bmbp\b"),
    ("pc",        r"\bpc\b|desktop|sobremesa|\bimac\b|workstation|\bnuc\b"),
    ("impresora", r"impresora|printer|laserjet|snapmaker|prusa|voron|\bhp[0-9a-f]{6}\b"),
    ("servidor",  r"\blxc\b|\bvm \d|servidor|proxmox|\bnas\b|home assistant|jarvis|ollama|"
                  r"plex|minecraft|frigate|docker|raspberry|openmower"),
    ("router",    r"router|openwrt|access ?point|repetidor|\bswitch\b|\bap\b"),
    ("enchufe",   r"sonoff|shelly pro|shelly \d|enchufe|smart ?plug|\brele\b|\brelay\b|regleta"),
    ("sensor",    r"sensor|airthings|radon|shelly ?em|term[oó]metro|consumos|inversor solar|"
                  r"sun2000|calidad aire"),
]

COMPILADAS = [(t, re.compile(p, re.I)) for t, p in REGLAS]


def adivina(texto: str) -> str | None:
    for tipo, patron in COMPILADAS:
        if patron.search(texto):
            return tipo
    return None


def main() -> int:
    aplicar = "--aplicar" in sys.argv
    c = abrir()
    filas = c.execute(
        "SELECT mac, descripcion, hostname, vendor, vivo FROM dispositivos "
        "WHERE tipo IS NULL ORDER BY vivo DESC, descripcion"
    ).fetchall()

    cuenta: Counter[str] = Counter()
    sin_pista: list[tuple[str, str]] = []
    cambios: list[tuple[str, str]] = []

    for f in filas:
        texto = " ".join(str(f[k] or "") for k in ("descripcion", "hostname", "vendor"))
        tipo = adivina(texto)
        if tipo:
            cuenta[tipo] += 1
            cambios.append((tipo, f["mac"]))
        elif f["vivo"]:
            sin_pista.append((f["mac"], (f["descripcion"] or f["hostname"] or "?")[:50]))

    print(f"sin tipo: {len(filas)}   deducidos: {len(cambios)}   "
          f"sin pista y vivos: {len(sin_pista)}")
    for tipo, n in cuenta.most_common():
        print(f"  {tipo:11} {n}")

    if sin_pista:
        print("\nVivos que se quedan sin clasificar (hay que mirarlos):")
        for mac, quien in sin_pista:
            print(f"  {mac}  {quien}")

    if not aplicar:
        print("\n(ensayo en seco — nada escrito. Repite con --aplicar)")
        return 0

    ts = ahora()
    c.executemany("UPDATE dispositivos SET tipo=?, actualizado=? WHERE mac=? AND tipo IS NULL",
                  [(t, ts, m) for t, m in cambios])
    c.commit()
    print(f"\nescritos {len(cambios)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
