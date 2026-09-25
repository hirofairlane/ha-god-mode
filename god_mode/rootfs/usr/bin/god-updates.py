#!/usr/bin/env python3
# =====================================================================
#  god-updates.py
#
#  Backend for /api/updates/<host>. Runs updates.yml playbook, parses
#  the per-OS raw output into a normalized shape:
#    {
#      "host": str, "god_os": str, "fetched_at_unix": int,
#      "manager": str,         # apt | dnf | zypper | pacman | opkg | brew | windows | unknown
#      "pending": int,
#      "security": int | null, # null if the manager doesn't separate security
#      "packages": [str, ...], # at most 200 entries
#      "truncated": bool,
#    }
#
#  Cache TTL: 6h (updates change slowly, polling too often wastes time).
# =====================================================================
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# Sibling import (filename has a dash so we go through importlib.util).
_HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("god_cached_runner", _HERE / "god_cached_runner.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
run_cached = _mod.run_cached
DATA_DIR = _mod.DATA_DIR

CACHE_DIR = DATA_DIR / "cache" / "updates"
PLAYBOOK  = Path("/usr/share/god-mode/ansible/playbooks/updates.yml")
CACHE_TTL_S = 21600        # 6h
TIMEOUT_S   = 120          # apt -s upgrade can be slow on a slow link
MAX_PACKAGES = 200

# Limits to avoid useless lines flooding the response.


def _parse_apt(raw: str) -> tuple[list[str], int | None]:
    pkgs: list[str] = []
    security: int | None = None
    sections = raw.split("---")
    if len(sections) >= 1:
        # `apt-get -s -qq upgrade` Inst lines: "Inst pkgname [oldver] (newver from-repo) []"
        for line in sections[0].splitlines():
            line = line.strip()
            if line.startswith("Inst "):
                parts = line.split()
                if len(parts) >= 2:
                    pkgs.append(parts[1])
    if len(sections) >= 2:
        # `apt list --upgradable` lines: "pkgname/repo version arch [upgradable from: oldver]"
        # Used as a fallback / dedupe source.
        for line in sections[1].splitlines():
            line = line.strip()
            if not line or "/" not in line:
                continue
            name = line.split("/", 1)[0]
            if name and name not in pkgs:
                pkgs.append(name)
    if len(sections) >= 3:
        last = sections[2].strip().splitlines()
        if last:
            try:
                security = int(last[-1].strip())
            except ValueError:
                security = None
    return pkgs, security


def _parse_dnf(raw: str) -> tuple[list[str], int | None]:
    # `dnf check-update --quiet`: "pkgname.arch  version  repo"
    pkgs: list[str] = []
    sections = raw.split("---")
    for line in sections[0].splitlines():
        line = line.strip()
        if not line or line.startswith(("Last metadata", "Obsoleting")):
            continue
        first = line.split()[0]
        if "." in first and not first.endswith(":"):
            pkgs.append(first)
    sec = None
    if len(sections) >= 2:
        try:
            sec = int(sections[1].strip().splitlines()[-1])
        except (ValueError, IndexError):
            sec = None
    return pkgs, sec


def _parse_zypper(raw: str) -> tuple[list[str], int | None]:
    pkgs: list[str] = []
    sections = raw.split("---")
    for line in sections[0].splitlines():
        line = line.strip()
        if line.startswith("v |"):
            # "v | repo | pkgname | newver | arch"
            cols = [c.strip() for c in line.split("|")]
            if len(cols) >= 3:
                pkgs.append(cols[2])
    sec = None
    if len(sections) >= 2:
        try:
            sec = int(sections[1].strip().splitlines()[-1])
        except (ValueError, IndexError):
            sec = None
    return pkgs, sec


def _parse_pacman(raw: str) -> tuple[list[str], int | None]:
    # `checkupdates` / `pacman -Qu`: "pkgname oldver -> newver"
    pkgs: list[str] = []
    for line in raw.split("---")[0].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if parts:
            pkgs.append(parts[0])
    return pkgs, None


def _parse_opkg(raw: str) -> tuple[list[str], int | None]:
    # `opkg list-upgradable`: "pkgname - oldver - newver"
    pkgs: list[str] = []
    for line in raw.split("---")[0].splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" - ")
        if parts:
            pkgs.append(parts[0])
    return pkgs, None


def _parse_brew(raw: str) -> tuple[list[str], int | None]:
    pkgs: list[str] = []
    for line in raw.split("---")[0].splitlines():
        line = line.strip()
        if line and not line.startswith("==>"):
            pkgs.append(line.split()[0])
    return pkgs, None


def _parse_windows(raw: str) -> tuple[list[str], int | None]:
    # First non-blank line is "MGR=...", rest until "---" are titles.
    pkgs: list[str] = []
    sections = raw.split("---")
    for line in sections[0].splitlines():
        line = line.strip()
        if not line or line.startswith("MGR="):
            continue
        pkgs.append(line[:120])
    sec = None
    if len(sections) >= 2:
        try:
            sec = int(sections[1].strip().splitlines()[-1])
        except (ValueError, IndexError):
            sec = None
    return pkgs, sec


PARSERS = {
    "apt":              _parse_apt,
    "dnf":              _parse_dnf,
    "zypper":           _parse_zypper,
    "pacman":           _parse_pacman,
    "opkg":             _parse_opkg,
    "brew":             _parse_brew,
    "pswindowsupdate":  _parse_windows,
    "windows_basic":    _parse_windows,
}


def parse(raw_doc: dict) -> dict:
    """Turn the raw on-disk doc into the API response body."""
    raw_text = raw_doc.get("raw") or ""
    # First line is "MGR=<name>", rest is the body.
    first_nl = raw_text.find("\n")
    if first_nl < 0:
        return {
            "host": raw_doc.get("host"),
            "god_os": raw_doc.get("god_os"),
            "fetched_at_unix": raw_doc.get("fetched_at_unix"),
            "manager": "unknown",
            "pending": 0,
            "security": None,
            "packages": [],
            "truncated": False,
        }
    mgr_line = raw_text[:first_nl].strip()
    manager = mgr_line.split("=", 1)[1] if mgr_line.startswith("MGR=") else "unknown"
    body = raw_text[first_nl + 1:]

    parser = PARSERS.get(manager)
    if parser is None:
        return {
            "host": raw_doc.get("host"),
            "god_os": raw_doc.get("god_os"),
            "fetched_at_unix": raw_doc.get("fetched_at_unix"),
            "manager": manager,
            "pending": 0,
            "security": None,
            "packages": [],
            "truncated": False,
        }

    pkgs, security = parser(body)
    truncated = len(pkgs) > MAX_PACKAGES
    if truncated:
        pkgs = pkgs[:MAX_PACKAGES]
    return {
        "host": raw_doc.get("host"),
        "god_os": raw_doc.get("god_os"),
        "fetched_at_unix": raw_doc.get("fetched_at_unix"),
        "manager": manager,
        "pending": len(pkgs) if not truncated else MAX_PACKAGES,
        "security": security,
        "packages": pkgs,
        "truncated": truncated,
    }


def updates(host: str, refresh: bool = False) -> tuple[int, dict]:
    return run_cached(
        name="updates",
        playbook=PLAYBOOK,
        cache_dir=CACHE_DIR,
        parse=parse,
        cache_ttl_s=CACHE_TTL_S,
        playbook_timeout_s=TIMEOUT_S,
        host=host,
        refresh=refresh,
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: god-updates.py <host> [--refresh]", file=sys.stderr)
        sys.exit(2)
    status, body = updates(sys.argv[1], refresh="--refresh" in sys.argv[2:])
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2))
    sys.exit(0 if status < 400 else 1)
