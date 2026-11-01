#!/usr/bin/env python3
"""Cross-references the network census with Home Assistant's device registry.

HA already knows a lot about these devices: the name its owner gave them,
the manufacturer, the model and the integration. And many of them store
their **MAC** in `connections`, which is exactly the census's key. Reusing
that is better than asking for the same thing to be typed in twice.

The device registry isn't exposed over the REST API: it has to go through
the WebSocket API (`config/device_registry/list`).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG

BASE = Path(__file__).resolve().parent
HA = CONFIG["ha_url"]


async def registro(token: str) -> list:
    async with websockets.connect(HA, max_size=32 * 1024 * 1024) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": token}))
        r = json.loads(await ws.recv())
        if r.get("type") != "auth_ok":
            raise SystemExit(f"  authentication rejected: {r}")
        await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
        while True:
            m = json.loads(await ws.recv())
            if m.get("id") == 1 and m.get("type") == "result":
                return m.get("result", [])


def main() -> int:
    # Same file censo-red.py uses, so the token doesn't live in two places
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

    print(f"  devices in HA .............. {len(disp)}")
    print(f"  of those, with a known MAC . {len(por_mac)}")
    print(f"  that are also in the census: {len(set(por_mac) & set(todos))}")

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
        # A WEAK description (the manufacturer heuristic) gets overwritten,
        # but NEVER one written by hand (`origen='manual'`): HA knows more
        # than the heuristic, but less than the person who typed it in.
        if actual["descripcion"] is None:
            c.execute("""UPDATE dispositivos SET descripcion=?, origen='HA', firme=1,
                         actualizado=? WHERE mac=?""", (desc, ahora(), mac))
            nuevos += 1
        elif not actual["firme"] and actual["origen"] != "manual":
            c.execute("""UPDATE dispositivos SET descripcion=?, origen='HA', firme=1,
                         actualizado=? WHERE mac=?""", (desc, ahora(), mac))
            mejorados += 1
    c.commit()
    print(f"  newly identified ........... {nuevos}")
    print(f"  weak descriptions improved with HA data: {mejorados}")
    sin = c.execute("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND descripcion IS NULL").fetchone()[0]
    print(f"\n  alive {len(vivos)} · unidentified now: {sin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
