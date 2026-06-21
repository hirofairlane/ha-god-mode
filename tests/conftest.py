"""Test fixtures: import the dash-named add-on entrypoints as modules.

The GOD Mode scripts live in `god_mode/rootfs/usr/bin/*.py` and have
hyphenated filenames (e.g. `god-collector.py`), so they cannot be imported
with a normal `import`. We load them by path with importlib.

They are already importable without side effects: all network / mkdir /
process work is inside functions guarded by `if __name__ == "__main__":`.
Data dirs are read from the `GOD_DATA_DIR` env var (defaulting to /data),
which we point at a tmp dir per test.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "god_mode" / "rootfs" / "usr" / "bin"


def load_module(filename: str, data_dir: str | None = None):
    """Load one of the dash-named scripts as a fresh module object."""
    if data_dir is not None:
        os.environ["GOD_DATA_DIR"] = data_dir
    name = filename.replace("-", "_").replace(".py", "")
    spec = importlib.util.spec_from_file_location(name, BIN / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # raises if there were import-time side effects
    return mod
