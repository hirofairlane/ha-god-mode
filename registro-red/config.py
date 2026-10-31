"""Local network endpoints for the registro-red scripts.

Real IPs/hostnames are never committed. Copy `infra.example.json` to
`infra.local.json` (gitignored) next to this file and fill in your own
router, Home Assistant and InfluxDB addresses.
"""
from __future__ import annotations

import json
from pathlib import Path

_BASE = Path(__file__).resolve().parent

DEFAULTS = {
    "router": "root@192.168.1.1",
    "ha_url": "ws://192.168.1.2:8123/api/websocket",
    "influx_url": "http://192.168.1.2:8086/write?db=homeassistant",
    "access_points": {"Router1": "192.168.1.1"},
}


def _load() -> dict:
    local = _BASE / "infra.local.json"
    if local.exists():
        return {**DEFAULTS, **json.loads(local.read_text())}
    return DEFAULTS


CONFIG = _load()
