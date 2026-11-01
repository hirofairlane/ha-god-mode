"""Tests for alertas.py's pure logic: who's in the digest, and the cooldown.

Sending itself isn't tested here — with no telegram_bot_token configured
(the default), enviar_telegram() is a guaranteed, networkless no-op, which
is exactly the case these tests exercise.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
REGISTRO = RAIZ / "registro-red"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def db_lib():
    return _load("red_db_lib_test_alertas", REGISTRO / "red_db_lib.py")


@pytest.fixture(scope="module")
def alertas(db_lib):
    sys.path.insert(0, str(REGISTRO))
    return _load("alertas_test", REGISTRO / "alertas.py")


@pytest.fixture
def conn(db_lib):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db_lib.ESQUEMA)
    db_lib.migrar(c)
    return c


def _inserta(c, mac, estado="pendiente", vivo=1):
    c.execute(
        "INSERT INTO dispositivos (mac, estado, vivo, ip, hostname) VALUES (?,?,?,?,?)",
        (mac, estado, vivo, "192.0.2.1", "thing"),
    )
    c.commit()


def test_sin_aprobar_incluye_pendientes(conn, alertas):
    _inserta(conn, "aa:aa:aa:aa:aa:01")
    assert len(alertas.sin_aprobar(conn)) == 1


@pytest.mark.parametrize("estado", ["aprobado", "bloqueado", "retirado"])
def test_sin_aprobar_excluye_estados_finales(conn, alertas, estado):
    _inserta(conn, "aa:aa:aa:aa:aa:02", estado=estado)
    assert alertas.sin_aprobar(conn) == []


def test_sin_aprobar_excluye_muertos(conn, alertas):
    _inserta(conn, "aa:aa:aa:aa:aa:03", vivo=0)
    assert alertas.sin_aprobar(conn) == []


def test_enfriado_true_sin_historial(conn, alertas):
    assert alertas._enfriado(conn, "clave_test", 3600) is True


def test_enfriado_false_justo_despues_de_marcar(conn, alertas):
    alertas._marca_enviado(conn, "clave_test")
    assert alertas._enfriado(conn, "clave_test", 3600) is False


def test_enfriado_true_tras_pasar_el_cooldown(conn, alertas, db_lib):
    viejo = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat(timespec="seconds")
    conn.execute("INSERT INTO alertas (clave, ts) VALUES (?,?)", ("clave_test", viejo))
    conn.commit()
    assert alertas._enfriado(conn, "clave_test", 3600) is True


def test_mensaje_trunca_en_veinte(alertas):
    dispositivos = [{"nombre": f"d{i}", "ip": "1.2.3.4"} for i in range(25)]
    msg = alertas.construir_mensaje(dispositivos)
    assert "25 live" in msg
    assert "and 5 more" in msg


def test_enviar_sin_token_es_no_op(alertas):
    ok, detalle = alertas.enviar_telegram("hola")
    assert ok is False
    assert "not set" in detalle
