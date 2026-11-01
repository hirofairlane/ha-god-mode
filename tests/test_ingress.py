"""The ingress port and the one the server actually opens must match.

REGRESSION: `ingress_port: 8099` was removed from config.yaml because the
official add-on linter rejects keys left at their default value. That's
safe **only while** the Supervisor's default stays 8099 and the server
keeps listening there. If either one changes and the other doesn't, the
sidebar tab stops loading with no visible error: the Supervisor proxies to
a port where nobody's listening. This test ties both ends together.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

RAIZ = Path(__file__).resolve().parent.parent
CONFIG = RAIZ / "god_mode" / "config.yaml"
WEBUI = RAIZ / "god_mode" / "rootfs" / "usr" / "bin" / "god-webui.py"

DEFECTO_SUPERVISOR = 8099


def puerto_del_servidor() -> int:
    m = re.search(r"(?m)^PORT\s*=\s*(\d+)", WEBUI.read_text())
    assert m, "no se encuentra PORT en god-webui.py"
    return int(m.group(1))


def test_ingress_apunta_donde_escucha_el_servidor():
    cfg = yaml.safe_load(CONFIG.read_text())
    assert cfg.get("ingress") is True, "el add-on se sirve por ingress"
    declarado = cfg.get("ingress_port", DEFECTO_SUPERVISOR)
    assert declarado == puerto_del_servidor(), (
        f"config.yaml deja el ingress en {declarado} pero god-webui.py escucha "
        f"en {puerto_del_servidor()}"
    )


def test_no_se_repiten_valores_por_defecto():
    """What made the official linter fail: keys that add nothing."""
    cfg = yaml.safe_load(CONFIG.read_text())
    for clave, defecto in (("boot", "auto"), ("host_network", False),
                           ("ingress_port", DEFECTO_SUPERVISOR)):
        assert cfg.get(clave, object()) != defecto, (
            f"'{clave}' is set to its default value ({defecto!r}): "
            f"remove it or the add-on linter fails"
        )
