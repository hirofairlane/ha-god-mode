#!/usr/bin/env python3
"""Cruza el censo de red con el registro de dispositivos de Home Assistant.

HA ya sabe muchisimo de estos aparatos: el nombre que les puso Sergio, el
fabricante, el modelo y la integracion. Y muchos guardan su **MAC** en
`connections`, que es justamente la clave del censo. Aprovecharlo es mejor que
pedir que se teclee lo mismo otra vez en otro sitio.

El registro de dispositivos NO se expone por la API REST: hay que ir por
WebSocket (`config/device_registry/list`).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

BASE = Path(__file__).resolve().parent
HA = "ws://192.168.1.131:8123/api/websocket"


async def registro(token: str) -> list:
    async with websockets.connect(HA, max_size=32 * 1024 * 1024) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        r = json.loads(await ws.recv())
        if r.get("type") != "auth_ok":
            raise SystemExit(f"  autenticacion rechazada: {r}")
        await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1 and m.get("type") == "result":
                return m.get("result", [])


def main() -> int:
    # Mismo fichero que usa censo-red.py, para no tener el token en dos sitios
    tok = Path("/etc/censo-red.token")
    if not tok.exists():
        tok = Path("/tmp/ha.token")
    token = tok.read_text().strip()
    disp = asyncio.run(registro(token))

    por_mac = {}
    for d in disp:
        for con in d.get("connections", []):
            if len(con) == 2 and con[0] == "mac":
                por_mac[con[1].lower()] = d

    import sys as _s
    _s.path.insert(0, str(BASE))
    from red_db_lib import abrir, ahora
    c = abrir()
    vivos = {r["mac"]: r for r in c.execute("SELECT * FROM dispositivos WHERE vivo=1")}
    todos = {r["mac"]: r for r in c.execute("SELECT * FROM dispositivos")}

    print(f"  dispositivos en HA ......... {len(disp)}")
    print(f"  de ellos, con MAC conocida . {len(por_mac)}")
    print(f"  que ademas estan en el censo: {len(set(por_mac) & set(todos))}")

    nuevos, mejorados = 0, 0
    for mac, d in por_mac.items():
        if mac not in todos:
            continue
        nombre = d.get("name_by_user") or d.get("name") or "?"
        partes = [nombre]
        if d.get("manufacturer"):
            partes.append(d["manufacturer"])
        if d.get("model"):
            partes.append(d["model"])
        desc = " · ".join(str(p) for p in partes)
        actual = todos[mac]
        # Se sobrescribe una descripcion DEBIL (la heuristica del fabricante)
        # pero NUNCA una escrita a mano (`origen='manual'`): HA sabe mas que la
        # heuristica, pero menos que Sergio.
        if actual["descripcion"] is None:
            c.execute("""UPDATE dispositivos SET descripcion=?, origen='HA', firme=1,
                         actualizado=? WHERE mac=?""", (desc, ahora(), mac))
            nuevos += 1
        elif not actual["firme"] and actual["origen"] != "manual":
            c.execute("""UPDATE dispositivos SET descripcion=?, origen='HA', firme=1,
                         actualizado=? WHERE mac=?""", (desc, ahora(), mac))
            mejorados += 1
    c.commit()
    print(f"  identificados nuevos ....... {nuevos}")
    print(f"  descripciones debiles mejoradas con datos de HA: {mejorados}")
    sin = c.execute("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND descripcion IS NULL").fetchone()[0]
    print(f"\n  vivos {len(vivos)} · sin identificar ahora: {sin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
