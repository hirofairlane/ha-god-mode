#!/usr/bin/env python3
"""Imports ALL DHCP reservations into the registry, including stale ones.

Of the router's reservation entries, roughly half had no active lease. Those
are exactly the ones nobody recognizes — leftovers from devices that got
replaced, were set up once over a cable, or died years ago. Ignoring them is
what made the table unreadable; explicitly listing them as "never seen"
lets you work through them.

A reservation does NOT create a live device: it's inserted with `vivo=0`
and state `pendiente`. If it's never been seen, it says so.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from red_db_lib import abrir, ahora


def main() -> int:
    r = subprocess.run(["ssh", "-n", "zeratul", "bash /usr/local/bin/recolectar-red.sh"],
                       capture_output=True, text=True, timeout=300)
    c = abrir()
    t = ahora()
    nuevas = solo_reserva = 0
    for linea in r.stdout.splitlines():
        p = linea.split("\t")
        if len(p) < 4 or p[0] != "STATIC":
            continue
        mac, ip, nombre = p[1].lower(), p[2], p[3]
        c.execute("""INSERT INTO reservas (mac, ip, nombre, ts) VALUES (?,?,?,?)
                     ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip,
                       nombre=excluded.nombre, ts=excluded.ts""", (mac, ip, nombre, t))
        hay = c.execute("SELECT mac, veces FROM dispositivos WHERE mac=?", (mac,)).fetchone()
        if hay is None:
            # Reserva de algo que NUNCA hemos visto vivo. Es exactamente el tipo
            # de entrada que Sergio no reconoce.
            c.execute("""INSERT INTO dispositivos
                (mac, descripcion, origen, firme, ip, hostname, veces, vivo, actualizado)
                VALUES (?,?,?,0,?,?,0,0,?)""",
                (mac, None, "dhcp", ip, nombre, t))
            nuevas += 1
            solo_reserva += 1
    c.commit()

    total = c.execute("SELECT COUNT(*) FROM reservas").fetchone()[0]
    jamas = c.execute("""SELECT COUNT(*) FROM dispositivos
                         WHERE veces=0 AND origen='dhcp'""").fetchone()[0]
    print(f"  reservas en el DHCP ........... {total}")
    print(f"  incorporadas al registro ...... {nuevas}")
    print(f"  NUNCA vistas vivas ............ {jamas}  <- las que no reconoces")
    print(f"  total de dispositivos ......... "
          f"{c.execute('SELECT COUNT(*) FROM dispositivos').fetchone()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
