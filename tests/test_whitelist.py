"""Tests de la whitelist de wifi.

Aqui se prueba la parte que decide QUE tocar, no la que lo escribe. Es
deliberado: el dano de este modulo no esta en el SSH, esta en elegir mal la
interfaz. Dos de estos tests son regresiones de averias reales.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
FICHERO = RAIZ / "registro-red" / "whitelist.py"


@pytest.fixture(scope="module")
def wl():
    spec = importlib.util.spec_from_file_location("whitelist", FICHERO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Salida real de `uci show wireless` del router del Sotano, recortada.
SOTANO = """wireless.radio0=wifi-device
wireless.wifinet0=wifi-iface
wireless.wifinet0.device='radio0'
wireless.wifinet0.ssid='virus'
wireless.wifinet0.maclist='00:1F:29:FF:CD:E5' '30:c6:f7:83:6a:6c'
wireless.wifinet0.macfilter='deny'
wireless.wifinet1.device='radio0'
wireless.wifinet1.ssid='virus-legacy'
wireless.wifinet1.macfilter='deny'
"""

# El Salon: `bh5` es el enlace de radio con la caseta.
SALON = """wireless.bh5=wifi-iface
wireless.bh5.ssid='backhaul-5g'
wireless.bh5.macfilter='allow'
wireless.bh5.maclist='62:45:cb:13:2c:ec'
wireless.wifinet0.ssid='virus'
"""


def test_parsea_maclist_con_varios_valores(wl):
    s = wl.parsea_wireless(SOTANO)
    assert s["wifinet0"]["ssid"] == "virus"
    assert s["wifinet0"]["maclist"] == ["00:1f:29:ff:cd:e5", "30:c6:f7:83:6a:6c"]
    assert s["wifinet0"]["macfilter"] == "deny"


def test_seccion_sin_maclist_no_revienta(wl):
    s = wl.parsea_wireless(SOTANO)
    assert "maclist" not in s["wifinet1"]
    plan = wl.planificar(s, ["aa:bb:cc:dd:ee:ff"])
    assert plan["wifinet1"]["faltan"] == ["aa:bb:cc:dd:ee:ff"]


def test_el_backhaul_del_salon_no_se_toca(wl):
    """REGRESION: escribir en `bh5` deja la caseta incomunicada.

    Se protege por dos vias a la vez (nombre y modo allow) a proposito: una
    sola barrera es una sola oportunidad de equivocarse.
    """
    plan = wl.planificar(wl.parsea_wireless(SALON), ["aa:bb:cc:dd:ee:ff"])
    assert plan["bh5"]["gestionable"] is False
    assert plan["wifinet0"]["gestionable"] is True


def test_modo_allow_nunca_es_gestionable(wl):
    ok, motivo = wl.gestionable("wifinet9", {"ssid": "x", "macfilter": "allow"})
    assert ok is False and "allow" in motivo


def test_interfaz_sin_ssid_no_se_toca(wl):
    ok, _ = wl.gestionable("wifinet9", {"macfilter": "deny"})
    assert ok is False


def test_quita_lo_que_ya_no_esta_bloqueado(wl):
    """Si una MAC deja de estar bloqueada en el registro, sale del router."""
    plan = wl.planificar(wl.parsea_wireless(SOTANO), ["00:1f:29:ff:cd:e5"])
    assert plan["wifinet0"]["sobran"] == ["30:c6:f7:83:6a:6c"]
    assert plan["wifinet0"]["faltan"] == []


def test_sin_cambios_no_propone_nada(wl):
    plan = wl.planificar(wl.parsea_wireless(SOTANO),
                         ["00:1f:29:ff:cd:e5", "30:c6:f7:83:6a:6c"])
    assert plan["wifinet0"]["faltan"] == [] and plan["wifinet0"]["sobran"] == []


def codigo_ejecutable() -> str:
    """El fuente sin comentarios ni docstrings.

    Hace falta porque la cabecera del modulo EXPLICA lo que no debe hacerse, y
    buscar esas cadenas en el fichero entero da falsos positivos: el primer
    intento de estos dos tests fallaba leyendo su propia documentacion.
    """
    import ast
    arbol = ast.parse(FICHERO.read_text())
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (nodo.body and isinstance(nodo.body[0], ast.Expr)
                    and isinstance(nodo.body[0].value, ast.Constant)
                    and isinstance(nodo.body[0].value.value, str)):
                nodo.body.pop(0)          # fuera el docstring
    return ast.unparse(arbol)             # ast.unparse ya descarta comentarios


def test_nunca_se_escribe_macfilter_disable():
    """REGRESION del apagon de 24 h.

    `macfilter='disable'` donde antes no habia opcion impide que radio0
    levante en OpenWrt 25.12. Para dejar de filtrar se usa `uci delete`.
    """
    codigo = codigo_ejecutable()
    assert "macfilter='disable'" not in codigo
    assert "uci delete wireless.{seccion}.macfilter" in codigo


def test_no_usa_wifi_reload():
    """`wifi reload` tira a todos los clientes de la radio.

    Para echar a uno solo esta `ubus call hostapd.<iface> del_client`.
    """
    codigo = codigo_ejecutable()
    assert "wifi reload" not in codigo
    assert "del_client" in codigo
