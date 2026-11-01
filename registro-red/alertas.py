#!/usr/bin/env python3
"""Sends one Telegram digest when something alive on the network isn't approved.

What triggers this isn't "a new device showed up" — half the census is
"pendiente" by design and that's fine. It's **"something alive right now
isn't approved"**: a live, unreviewed device sitting on the LAN. That
condition can be read as "a possible intruder" just as easily as "a gadget
nobody's gotten around to classifying yet" — this script doesn't try to
tell those apart, it just surfaces the question on a cooldown so it's
asked once, not every time this runs from a timer.

One message per cooldown window (`alert_cooldown_s` in infra.local.json,
default 6h), not one per device: a ten-device backlog is one digest,
not ten pings. The cooldown is tracked in the registry's own `alertas`
table, so there's no separate state file to lose.

Needs `telegram_bot_token` + `telegram_chat_id` in infra.local.json.
Without them this still runs and prints what it would have sent — useful
to see what would have fired before wiring up the bot.

Usage:
    ./alertas.py            # respects the cooldown
    ./alertas.py --forzar   # ignores the cooldown (for testing the bot wiring)
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG  # noqa: E402
from red_db_lib import abrir, ahora  # noqa: E402

CLAVE_DIGEST = "sin_aprobar"


def sin_aprobar(c) -> list[dict]:
    filas = c.execute("""
        SELECT mac, COALESCE(hostname, descripcion, mac) nombre, ip, primera_vez
        FROM dispositivos
        WHERE vivo=1 AND estado NOT IN ('aprobado','bloqueado','retirado')
        ORDER BY primera_vez
    """).fetchall()
    return [dict(f) for f in filas]


def _enfriado(c, clave: str, cooldown_s: int) -> bool:
    fila = c.execute("SELECT ts FROM alertas WHERE clave=?", (clave,)).fetchone()
    if not fila or not fila["ts"]:
        return True
    ultimo = datetime.fromisoformat(fila["ts"])
    return datetime.now(timezone.utc) - ultimo >= timedelta(seconds=cooldown_s)


def _marca_enviado(c, clave: str) -> None:
    c.execute("INSERT OR REPLACE INTO alertas (clave, ts) VALUES (?,?)", (clave, ahora()))
    c.commit()


def construir_mensaje(dispositivos: list[dict]) -> str:
    lineas = [f"🔎 {len(dispositivos)} live, unapproved device(s) in the network registry:"]
    for d in dispositivos[:20]:
        lineas.append(f"  • {d['nombre']} — {d['ip'] or '?'}")
    if len(dispositivos) > 20:
        lineas.append(f"  … and {len(dispositivos) - 20} more")
    lineas.append("\nReview them in the GOD Mode add-on's Network tab.")
    return "\n".join(lineas)


def enviar_telegram(texto: str) -> tuple[bool, str]:
    token = CONFIG.get("telegram_bot_token") or ""
    chat_id = CONFIG.get("telegram_chat_id") or ""
    if not token or not chat_id:
        return False, "telegram_bot_token/telegram_chat_id not set in infra.local.json"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": texto}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=10) as r:
            return r.status == 200, f"HTTP {r.status}"
    except Exception as e:
        return False, f"send failed: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--forzar", action="store_true", help="ignore the cooldown")
    args = ap.parse_args()

    c = abrir()
    pendientes = sin_aprobar(c)
    if not pendientes:
        print("Nothing alive and unapproved right now.")
        return 0

    cooldown = int(CONFIG.get("alert_cooldown_s") or 21600)
    if not args.forzar and not _enfriado(c, CLAVE_DIGEST, cooldown):
        print(f"{len(pendientes)} pending, but still within the "
              f"{cooldown // 3600}h cooldown — not sending.")
        return 0

    mensaje = construir_mensaje(pendientes)
    ok, detalle = enviar_telegram(mensaje)
    print(mensaje)
    print(f"\n{'sent' if ok else 'NOT sent'}: {detalle}")
    if ok:
        _marca_enviado(c, CLAVE_DIGEST)
    return 0 if ok or not (CONFIG.get("telegram_bot_token")) else 1


if __name__ == "__main__":
    sys.exit(main())
