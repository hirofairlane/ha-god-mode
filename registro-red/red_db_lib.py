"""Funciones comunes de la base de datos de red."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
# Configurable: si no, importar este modulo desde cualquier sitio crea una
# `red.db` vacia al lado del fichero — pasa, por ejemplo, al ejecutar los
# scripts desde el repositorio en vez de desde la copia desplegada.
DB = Path(os.environ.get("RED_DB", BASE / "red.db"))

ESQUEMA = """
CREATE TABLE IF NOT EXISTS dispositivos (
    mac          TEXT PRIMARY KEY,
    descripcion  TEXT,
    perfil       TEXT,          -- pleno | nube | local | NULL (sin clasificar)
    tipo         TEXT,          -- movil | tablet | ... | otro (ver TIPOS)
    ubicacion    TEXT,
    estado       TEXT NOT NULL DEFAULT 'pendiente',
    origen       TEXT,          -- inventario | HA | manual | dhcp | descubierto
    firme        INTEGER NOT NULL DEFAULT 0,   -- 0 = descripcion debil
    vendor       TEXT,
    primera_vez  TEXT,
    ultima_vez   TEXT,
    veces        INTEGER NOT NULL DEFAULT 0,
    vivo         INTEGER NOT NULL DEFAULT 0,
    ip           TEXT,
    hostname     TEXT,
    ap           TEXT,
    ssid         TEXT,
    actualizado  TEXT
);

CREATE TABLE IF NOT EXISTS avistamientos (
    mac    TEXT NOT NULL,
    ts     TEXT NOT NULL,
    evento TEXT NOT NULL,        -- alta | aparece | desaparece | cambia
    detalle TEXT,
    PRIMARY KEY (mac, ts, evento)
);

CREATE TABLE IF NOT EXISTS reservas (
    mac    TEXT PRIMARY KEY,
    ip     TEXT,
    nombre TEXT,
    ts     TEXT
);

CREATE INDEX IF NOT EXISTS idx_disp_estado ON dispositivos(estado);
CREATE INDEX IF NOT EXISTS idx_disp_vivo   ON dispositivos(vivo);
CREATE INDEX IF NOT EXISTS idx_avist_ts    ON avistamientos(ts);
"""


# Columnas anadidas despues de la creacion original. `CREATE TABLE IF NOT
# EXISTS` no toca una tabla que ya existe, asi que hay que anadirlas a mano.
COLUMNAS_NUEVAS = {
    "dispositivos": {"tipo": "TEXT"},
}


def migrar(c: sqlite3.Connection) -> None:
    for tabla, columnas in COLUMNAS_NUEVAS.items():
        tiene = {r[1] for r in c.execute(f"PRAGMA table_info({tabla})")}
        for nombre, tipo in columnas.items():
            if nombre not in tiene:
                c.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}")
    c.commit()


def abrir() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.executescript(ESQUEMA)
    migrar(c)
    return c


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


