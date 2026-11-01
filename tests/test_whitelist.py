"""Tests for the wifi whitelist.

This tests the part that decides WHAT to touch, not the part that writes it.
Deliberate: this module's real risk isn't the SSH, it's picking the wrong
interface. Two of these tests are regressions of real outages.
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


# Realistic shape of `uci show wireless` output, with example MACs and SSID.
SOTANO = """wireless.radio0=wifi-device
wireless.wifinet0=wifi-iface
wireless.wifinet0.device='radio0'
wireless.wifinet0.ssid='home-wifi'
wireless.wifinet0.maclist='aa:aa:aa:aa:aa:01' 'aa:aa:aa:aa:aa:02'
wireless.wifinet0.macfilter='deny'
wireless.wifinet1.device='radio0'
wireless.wifinet1.ssid='home-wifi-legacy'
wireless.wifinet1.macfilter='deny'
"""

# Un AP secundario: `bh5` es el enlace de radio (backhaul) con otro AP.
SALON = """wireless.bh5=wifi-iface
wireless.bh5.ssid='backhaul-5g'
wireless.bh5.macfilter='allow'
wireless.bh5.maclist='aa:aa:aa:aa:aa:03'
wireless.wifinet0.ssid='home-wifi'
"""


def test_parsea_maclist_con_varios_valores(wl):
    s = wl.parsea_wireless(SOTANO)
    assert s["wifinet0"]["ssid"] == "home-wifi"
    assert s["wifinet0"]["maclist"] == ["aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02"]
    assert s["wifinet0"]["macfilter"] == "deny"


def test_seccion_sin_maclist_no_revienta(wl):
    s = wl.parsea_wireless(SOTANO)
    assert "maclist" not in s["wifinet1"]
    plan = wl.planificar(s, ["aa:bb:cc:dd:ee:ff"])
    assert plan["wifinet1"]["faltan"] == ["aa:bb:cc:dd:ee:ff"]


def test_el_backhaul_del_salon_no_se_toca(wl):
    """REGRESSION: writing to `bh5` cuts off the AP on the other end of the link.

    Deliberately protected two ways at once (name and allow mode): a single
    barrier is a single chance to get it wrong.
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
    """If a MAC stops being blocked in the registry, it comes off the router."""
    plan = wl.planificar(wl.parsea_wireless(SOTANO), ["aa:aa:aa:aa:aa:01"])
    assert plan["wifinet0"]["sobran"] == ["aa:aa:aa:aa:aa:02"]
    assert plan["wifinet0"]["faltan"] == []


def test_sin_cambios_no_propone_nada(wl):
    plan = wl.planificar(wl.parsea_wireless(SOTANO),
                         ["aa:aa:aa:aa:aa:01", "aa:aa:aa:aa:aa:02"])
    assert plan["wifinet0"]["faltan"] == [] and plan["wifinet0"]["sobran"] == []


def codigo_ejecutable() -> str:
    """The source with no comments or docstrings.

    Needed because the module header EXPLAINS what it should never do, and
    searching for those strings across the whole file gives false
    positives: the first attempt at these two tests failed by reading its
    own documentation.
    """
    import ast
    arbol = ast.parse(FICHERO.read_text())
    for nodo in ast.walk(arbol):
        if isinstance(nodo, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if (nodo.body and isinstance(nodo.body[0], ast.Expr)
                    and isinstance(nodo.body[0].value, ast.Constant)
                    and isinstance(nodo.body[0].value.value, str)):
                nodo.body.pop(0)          # drop the docstring
    return ast.unparse(arbol)             # ast.unparse already drops comments


def test_nunca_se_escribe_macfilter_disable():
    """REGRESSION from a 24h outage.

    `macfilter='disable'` where there was no option before stops radio0
    from coming up on OpenWrt 25.12. `uci delete` is used to stop filtering
    instead.
    """
    codigo = codigo_ejecutable()
    assert "macfilter='disable'" not in codigo
    assert "uci delete wireless.{seccion}.macfilter" in codigo


def test_no_usa_wifi_reload():
    """`wifi reload` drops every client on the radio.

    `ubus call hostapd.<iface> del_client` exists to kick just one.
    """
    codigo = codigo_ejecutable()
    assert "wifi reload" not in codigo
    assert "del_client" in codigo
