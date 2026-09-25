#!/usr/bin/env python3
"""Pasada del censo contra la base de datos. Sustituye a censo-red.py.

Vivo = asociado a un AP ahora, O responde al ping. **Un lease no basta**: costo
un diagnostico erroneo, porque la `orangepizero` figuro como viva nueve horas
despues de ser expulsada de la wifi, con su lease aun vigente. Un lease dice
"estuvo aqui hace poco", no "esta aqui".

Registra en `avistamientos` **solo los cambios**. Anotar 74 aparatos cada 10
minutos serian 10.000 filas diarias de ruido; anotar altas, apariciones,
desapariciones y cambios de sitio da una linea temporal legible.
"""
from __future__ import annotations

import concurrent.futures
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from red_db_lib import abrir, ahora

RECOLECTOR = "/usr/local/bin/recolectar-red.sh"


def recolectar() -> dict:
    local = socket.gethostname().lower().startswith("zeratul")
    orden = ["bash", RECOLECTOR] if local else ["ssh", "-n", "zeratul", f"bash {RECOLECTOR}"]
    r = subprocess.run(orden, capture_output=True, text=True, timeout=300)
    d = {"lease": {}, "static": {}, "sta": {}}
    for linea in r.stdout.splitlines():
        p = linea.split("\t")
        if len(p) < 4:
            continue
        tipo, mac = p[0], p[1].lower()
        if tipo == "LEASE":
            d["lease"][mac] = {"ip": p[2], "host": p[3]}
        elif tipo == "STATIC":
            d["static"][mac] = {"ip": p[2], "nombre": p[3]}
        elif tipo == "STA":
            d["sta"][mac] = {"ap": p[2], "ssid": p[3]}
    return d


def pingar(ips: list[str]) -> set[str]:
    def uno(ip):
        return ip if subprocess.run(["ping", "-c1", "-W1", ip],
                                    capture_output=True, timeout=5).returncode == 0 else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as ex:
        return {r for r in ex.map(uno, ips) if r}


def main() -> int:
    t = ahora()
    d = recolectar()
    c = abrir()

    ips = {m: v["ip"] for m, v in d["lease"].items()}
    vivos = set(d["sta"])
    cand = (set(d["lease"]) | set(d["sta"])) - vivos
    responden = pingar([ips[m] for m in cand if m in ips])
    vivos |= {m for m in cand if ips.get(m) in responden}

    eventos = {"alta": 0, "aparece": 0, "desaparece": 0, "cambia": 0}

    previos = {r["mac"]: r for r in c.execute("SELECT * FROM dispositivos")}
    for mac in vivos:
        ip = ips.get(mac) or d["static"].get(mac, {}).get("ip")
        host = d["lease"].get(mac, {}).get("host") or d["static"].get(mac, {}).get("nombre")
        ap = d["sta"].get(mac, {}).get("ap")
        ssid = d["sta"].get(mac, {}).get("ssid")
        p = previos.get(mac)
        if p is None:
            c.execute("""INSERT INTO dispositivos (mac, origen, primera_vez, ultima_vez,
                         veces, vivo, ip, hostname, ap, ssid, actualizado)
                         VALUES (?,'descubierto',?,?,1,1,?,?,?,?,?)""",
                      (mac, t, t, ip, host, ap, ssid, t))
            c.execute("INSERT OR IGNORE INTO avistamientos VALUES (?,?,'alta',?)",
                      (mac, t, f"{ip} {host or ''} {ap or 'cable'}"))
            eventos["alta"] += 1
        else:
            if not p["vivo"]:
                c.execute("INSERT OR IGNORE INTO avistamientos VALUES (?,?,'aparece',?)",
                          (mac, t, f"{ip} {ap or 'cable'}"))
                eventos["aparece"] += 1
            elif (p["ip"], p["ap"]) != (ip, ap):
                c.execute("INSERT OR IGNORE INTO avistamientos VALUES (?,?,'cambia',?)",
                          (mac, t, f"{p['ip']}->{ip} {p['ap'] or 'cable'}->{ap or 'cable'}"))
                eventos["cambia"] += 1
            c.execute("""UPDATE dispositivos SET ultima_vez=?, veces=veces+1, vivo=1,
                         ip=?, hostname=COALESCE(?,hostname), ap=?, ssid=?, actualizado=?
                         WHERE mac=?""", (t, ip, host, ap, ssid, t, mac))

    for mac, p in previos.items():
        if p["vivo"] and mac not in vivos:
            c.execute("INSERT OR IGNORE INTO avistamientos VALUES (?,?,'desaparece',?)",
                      (mac, t, p["ip"] or ""))
            c.execute("UPDATE dispositivos SET vivo=0, actualizado=? WHERE mac=?", (t, mac))
            eventos["desaparece"] += 1

    for mac, v in d["static"].items():
        c.execute("""INSERT INTO reservas VALUES (?,?,?,?)
                     ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip,
                       nombre=excluded.nombre, ts=excluded.ts""",
                  (mac, v["ip"], v["nombre"], t))
    c.commit()

    def q(s):
        return c.execute(s).fetchone()[0]

    print(f"  vivos {len(vivos)} · registrados {q('SELECT COUNT(*) FROM dispositivos')} · "
          f"""sin aprobar {q("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND estado!='aprobado'")}""")
    print("  eventos: " + " · ".join(f"{k} {v}" for k, v in eventos.items() if v))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
