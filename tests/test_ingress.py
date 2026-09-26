"""El puerto de ingress y el que abre el servidor tienen que coincidir.

REGRESION: `ingress_port: 8099` se quito de config.yaml porque el linter
oficial de add-ons rechaza las claves puestas a su valor por defecto. Es
seguro **mientras** el defecto del Supervisor siga siendo 8099 y el servidor
siga escuchando ahi. Si alguno de los dos cambia y el otro no, la pestana deja
de cargar sin ningun error visible: el Supervisor proxea a un puerto donde no
hay nadie. Este test ata las dos puntas.
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
    """Lo que hacia fallar al linter oficial: claves que no aportan nada."""
    cfg = yaml.safe_load(CONFIG.read_text())
    for clave, defecto in (("boot", "auto"), ("host_network", False),
                           ("ingress_port", DEFECTO_SUPERVISOR)):
        assert cfg.get(clave, object()) != defecto, (
            f"'{clave}' esta puesto a su valor por defecto ({defecto!r}): "
            f"quitalo o el linter de add-ons falla"
        )
