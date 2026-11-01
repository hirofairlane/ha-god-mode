"""Tests for auto_enroll's candidate selection.

Only the pure DB query (`candidatos`) is tested here — everything else in
the script does real SSH/TCP I/O on purpose, which has no business running
under pytest. Builds its own sqlite connection directly (passed into
`candidatos(c)`) instead of going through `red_db_lib.abrir()`, so this
stays independent of whatever RED_DB another test module set at import
time.
"""
from __future__ import annotations

import importlib.util
import sqlite3
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
    return _load("red_db_lib_test", REGISTRO / "red_db_lib.py")


@pytest.fixture(scope="module")
def auto_enroll(db_lib):
    import sys
    sys.path.insert(0, str(REGISTRO))
    return _load("auto_enroll_test", REGISTRO / "auto_enroll.py")


@pytest.fixture
def conn(db_lib):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(db_lib.ESQUEMA)
    db_lib.migrar(c)
    return c


def _inserta(c, mac, estado="pendiente", tipo=None, vivo=1):
    c.execute(
        "INSERT INTO dispositivos (mac, estado, tipo, vivo, ip, hostname) "
        "VALUES (?,?,?,?,?,?)",
        (mac, estado, tipo, vivo, "192.0.2.1", "x"),
    )
    c.commit()


def test_dispositivo_pendiente_sin_tipo_es_candidato(conn, auto_enroll):
    _inserta(conn, "aa:aa:aa:aa:aa:01")
    assert [d["mac"] for d in auto_enroll.candidatos(conn)] == ["aa:aa:aa:aa:aa:01"]


def test_tipo_no_ssh_se_excluye(conn, auto_enroll):
    _inserta(conn, "aa:aa:aa:aa:aa:02", tipo="movil")
    assert auto_enroll.candidatos(conn) == []


def test_tipo_servidor_si_es_candidato(conn, auto_enroll):
    _inserta(conn, "aa:aa:aa:aa:aa:03", tipo="servidor")
    assert len(auto_enroll.candidatos(conn)) == 1


@pytest.mark.parametrize("estado", ["aprobado", "bloqueado", "retirado"])
def test_estados_finales_se_excluyen(conn, auto_enroll, estado):
    _inserta(conn, "aa:aa:aa:aa:aa:04", estado=estado)
    assert auto_enroll.candidatos(conn) == []


def test_dispositivo_muerto_no_es_candidato(conn, auto_enroll):
    _inserta(conn, "aa:aa:aa:aa:aa:05", vivo=0)
    assert auto_enroll.candidatos(conn) == []


def test_ya_probado_se_excluye(conn, auto_enroll, db_lib):
    _inserta(conn, "aa:aa:aa:aa:aa:06")
    conn.execute(
        "INSERT INTO avistamientos (mac, ts, evento, detalle) VALUES (?,?,?,?)",
        ("aa:aa:aa:aa:aa:06", db_lib.ahora(), "ssh_probado", "tcp_22_cerrado"),
    )
    conn.commit()
    assert auto_enroll.candidatos(conn) == []
