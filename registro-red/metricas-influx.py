#!/usr/bin/env python3
"""Publica las metricas del censo en InfluxDB, para poder verlas en Grafana.

El reparto y su motivo:

- **SQLite guarda el registro**: filas editables, con descripcion, perfil y
  estado de whitelist. Eso es algo que se muta, no una serie temporal, y meterlo
  en InfluxDB seria pelearse con la herramienta — no tiene UPDATE ni registros
  con clave que edites.
- **InfluxDB guarda la evolucion**: cuantos vivos, cuantos sin aprobar, cuantos
  sin identificar, en cada momento. Para eso si es la herramienta correcta.

Se escribe **directamente a InfluxDB desde zeratul**, sin pasar por HA: asi las
metricas siguen registrandose aunque Home Assistant este caido o reiniciandose,
que es justo cuando interesa saber que hay en la red.
"""
from __future__ import annotations

import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from red_db_lib import abrir

INFLUX = "http://192.168.1.131:8086/write?db=homeassistant"


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
    # Formato de linea de InfluxDB: medida,etiquetas campo=valor
    lineas = [f"censo_red {','.join(f'{k}={v}i' for k, v in metricas.items())}"]

    # Y un desglose por perfil, para ver como avanza la clasificacion
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
