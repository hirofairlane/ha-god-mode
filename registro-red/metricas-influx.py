#!/usr/bin/env python3
"""Publishes census metrics to InfluxDB, so they can be graphed in Grafana.

The split and why:

- **SQLite holds the registry**: editable rows, with description, profile
  and whitelist state. That's something you mutate, not a time series, and
  forcing it into InfluxDB would mean fighting the tool — no UPDATE, no
  editable keyed records.
- **InfluxDB holds the evolution**: how many alive, how many unapproved, how
  many unidentified, at each point in time. That's exactly the right tool
  for that.

Writes go **directly to InfluxDB**, bypassing HA: that way metrics keep
being recorded even while Home Assistant is down or restarting, which is
exactly when you want to know what's on the network.
"""
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG
from red_db_lib import abrir

INFLUX = CONFIG["influx_url"]


def escribir(lineas: list[str]) -> int:
    cuerpo = "\n".join(lineas).encode()
    req = urllib.request.Request(INFLUX, data=cuerpo, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.status


def main() -> int:
    c = abrir()
    def q(s):
        return c.execute(s).fetchone()[0]

    metricas = {
        "vivos":            q("SELECT COUNT(*) FROM dispositivos WHERE vivo=1"),
        "registrados":      q("SELECT COUNT(*) FROM dispositivos"),
        "sin_identificar":  q("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND descripcion IS NULL"),
        "sin_aprobar":      q("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND estado!='aprobado'"),
        "identificacion_debil": q("SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND firme=0 AND descripcion IS NOT NULL"),
        "nunca_vistos":     q("SELECT COUNT(*) FROM dispositivos WHERE veces=0"),
        "reservas_dhcp":    q("SELECT COUNT(*) FROM reservas"),
    }
    # InfluxDB line format: measurement,tags field=value
    lineas = [f"censo_red {','.join(f'{k}={v}i' for k, v in metricas.items())}"]

    # Plus a breakdown by profile, to see how classification is progressing
    for fila in c.execute("""
        SELECT COALESCE(perfil,'sin_clasificar') p, COUNT(*) n
        FROM dispositivos WHERE vivo=1 GROUP BY p"""):
        lineas.append(f"censo_red_perfil,perfil={fila['p']} n={fila['n']}i")

    estado = escribir(lineas)
    print(f"  InfluxDB -> HTTP {estado} ({len(lineas)} lineas)")
    for k, v in metricas.items():
        print(f"    {k:<22} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
