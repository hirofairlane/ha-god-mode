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
    # SSH argv prefix used to run a command where the routers' SSH keys
    # live. The final remote command gets appended as the last argument.
    # Override if that's a container/jump host rather than a plain SSH
    # target (e.g. ["ssh", "bastion-host", "pct exec <ctid> --"]).
    "ssh_jump": ["ssh", "bastion-host"],
    # Fallback password auto_enroll.py tries, once per device, on machines
    # that don't yet have the GOD Mode pubkey. Empty disables that step —
    # auto_enroll.py still does the (harmless) tcp/22 + pubkey-only probing.
    "default_ssh_password": "",
    # Telegram bot used by alertas.py for "something alive isn't approved"
    # digests. registro-red runs outside Home Assistant, so it can't reuse
    # HA's notify service — it talks to the Bot API directly. Empty token
    # disables sending; alertas.py still prints what it would have sent.
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "alert_cooldown_s": 6 * 3600,
}


def _load() -> dict:
    local = _BASE / "infra.local.json"
    if local.exists():
        return {**DEFAULTS, **json.loads(local.read_text())}
    return DEFAULTS


CONFIG = _load()
