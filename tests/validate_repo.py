#!/usr/bin/env python3
"""Static repo validation used by CI (and runnable locally).

Checks:
  * the add-on / repository / build YAML files parse,
  * the Lovelace package YAML files parse,
  * every Python entrypoint compiles,
  * config.yaml `version` matches the io.hass.version LABEL in the Dockerfile.

Exit non-zero on any failure.
"""
from __future__ import annotations

import py_compile
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ADDON = ROOT / "god_mode"


def main() -> int:
    errors: list[str] = []

    # --- YAML manifests parse ------------------------------------------------
    yaml_files = [
        "god_mode/config.yaml",
        "god_mode/build.yaml",
        "repository.yaml",
    ]
    # Lovelace package YAML shipped in the rootfs.
    yaml_files += [
        str(p.relative_to(ROOT))
        for p in (ADDON / "rootfs/usr/share/god-mode/lovelace/packages").glob("*.yaml")
    ]
    for rel in yaml_files:
        try:
            yaml.safe_load((ROOT / rel).read_text())
            print(f"OK   yaml {rel}")
        except Exception as exc:  # noqa: BLE001 - report any parse error
            errors.append(f"YAML parse failed for {rel}: {exc}")

    # --- Python entrypoints compile -----------------------------------------
    for py in sorted((ADDON / "rootfs/usr/bin").glob("*.py")):
        try:
            py_compile.compile(str(py), doraise=True)
            print(f"OK   py_compile {py.relative_to(ROOT)}")
        except py_compile.PyCompileError as exc:
            errors.append(f"py_compile failed for {py.relative_to(ROOT)}: {exc}")

    # --- version sync: config.yaml  <->  Dockerfile LABEL --------------------
    cfg_version = yaml.safe_load((ADDON / "config.yaml").read_text())["version"]
    dockerfile = (ADDON / "Dockerfile").read_text()
    m = re.search(r'io\.hass\.version\s*=\s*"([^"]+)"', dockerfile)
    docker_version = m.group(1) if m else None
    if cfg_version == docker_version:
        print(f"OK   version in sync: {cfg_version}")
    else:
        errors.append(
            f"version mismatch: config.yaml={cfg_version} Dockerfile io.hass.version={docker_version}"
        )

    if errors:
        print("\nFAIL:")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("\nAll static checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
