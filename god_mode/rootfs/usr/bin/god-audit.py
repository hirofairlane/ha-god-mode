#!/usr/bin/env python3
# =====================================================================
#  god-audit.py
#
#  Backend for /api/audit/<host>. Runs audit.yml playbook, parses the
#  multi-section `BEGIN <name>` … `END <name>` raw output into:
#    {
#      "host", "god_os", "fetched_at_unix",
#      "uptime": str, "uptime_s": int,
#      "disk":   [ { fs, type, size, used, avail, used_pct, mount } ],
#      "reboot_required": bool, "reboot_required_detail": str,
#      "services_failed": [ str, ... ],
#      "journal_errors_tail": [ str, ... ],
#      "docker":  [ str, ... ],
#      "loadavg": { "1m": float, "5m": float, "15m": float },
#      "mem":     { "total_mb": int, "used_mb": int, "free_mb": int },
#      "smart":   [ { device: str, lines: [str,...] } ],
#    }
#
#  Cache TTL: 6h (some signals like services_failed do flutter, but
#  for a weekly report 6h is plenty).
# =====================================================================
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

_HERE = Path(__file__).parent
_spec = importlib.util.spec_from_file_location("god_cached_runner", _HERE / "god_cached_runner.py")
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
run_cached = _mod.run_cached
DATA_DIR = _mod.DATA_DIR

CACHE_DIR = DATA_DIR / "cache" / "audit"
PLAYBOOK  = Path("/usr/share/god-mode/ansible/playbooks/audit.yml")
CACHE_TTL_S = 21600        # 6h
TIMEOUT_S   = 90


def _split_sections(raw: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []
    for line in raw.splitlines():
        s = line.rstrip("\r")
        if s.startswith("BEGIN "):
            current = s[6:].strip()
            buf = []
        elif s.startswith("END ") and current is not None:
            out[current] = buf
            current = None
            buf = []
        elif current is not None:
            buf.append(s)
    return out


def _parse_disk(lines: list[str]) -> list[dict]:
    # df -PT line format:
    # "Filesystem  Type     Size  Used Avail Use% Mounted on"
    out: list[dict] = []
    for line in lines:
        f = line.split()
        # Some df implementations put the FS path with spaces — keep the
        # last column as mount, second-to-last as Use%, etc.
        if len(f) < 7:
            continue
        try:
            used_pct = int(f[-2].rstrip("%"))
        except ValueError:
            used_pct = 0
        out.append({
            "fs":       f[0],
            "type":     f[1],
            "size":     f[2],
            "used":     f[3],
            "avail":    f[4],
            "used_pct": used_pct,
            "mount":    f[-1],
        })
    return out


def _parse_uptime_s(lines: list[str]) -> int:
    if not lines:
        return 0
    try:
        return int(lines[0].strip())
    except ValueError:
        return 0


def _parse_reboot(lines: list[str]) -> tuple[bool, str]:
    if not lines:
        return False, ""
    first = lines[0].strip().lower()
    yes = first.startswith("yes")
    detail = " ".join(ln.strip() for ln in lines if ln.strip())[:300]
    return yes, detail


def _parse_services_failed(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        ln = line.strip()
        if not ln or ln.startswith("(no "):
            continue
        # systemctl --failed --no-legend format: "UNIT  LOAD  ACTIVE  SUB  DESCRIPTION"
        first = ln.split()[0]
        if first.endswith(".service") or first.endswith(".timer") or "." in first:
            out.append(first)
    return out[:30]


def _parse_journal(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        ln = line.rstrip()
        if not ln or ln.startswith("(no "):
            continue
        out.append(ln[:240])
    return out[-30:]


def _parse_docker(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        ln = line.rstrip()
        if not ln or ln.startswith("(no "):
            continue
        out.append(ln[:160])
    return out[:60]


def _parse_load(lines: list[str]) -> dict:
    if not lines:
        return {}
    parts = lines[0].split()
    if len(parts) >= 3:
        try:
            return {"1m": float(parts[0]), "5m": float(parts[1]), "15m": float(parts[2])}
        except ValueError:
            pass
    return {"raw": lines[0]}


def _parse_mem(lines: list[str]) -> dict:
    # `free -m` typical:
    #   "              total        used        free      shared  buff/cache   available"
    #   "Mem:          15999       8000       2000        100        5999       6000"
    for line in lines:
        ln = line.strip()
        if ln.lower().startswith("mem:"):
            f = ln.split()
            try:
                return {
                    "total_mb": int(f[1]),
                    "used_mb":  int(f[2]),
                    "free_mb":  int(f[3]) if len(f) > 3 else None,
                }
            except (ValueError, IndexError):
                return {"raw": ln}
    return {}


def _parse_smart(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    current: dict | None = None
    for line in lines:
        ln = line.rstrip()
        if ln.startswith("--- "):
            # "--- /dev/sda ---"
            if current is not None:
                out.append(current)
            current = {"device": ln.strip(" -"), "lines": []}
        elif current is not None and ln.strip():
            current["lines"].append(ln.strip()[:160])
    if current is not None:
        out.append(current)
    return out[:8]


def parse(raw_doc: dict) -> dict:
    raw_text = raw_doc.get("raw") or ""
    sec = _split_sections(raw_text)
    return {
        "host":                   raw_doc.get("host"),
        "god_os":                 raw_doc.get("god_os"),
        "fetched_at_unix":        raw_doc.get("fetched_at_unix"),
        "uptime":                 (sec.get("uptime") or [""])[0].strip(),
        "uptime_s":               _parse_uptime_s(sec.get("uptime_s", [])),
        "disk":                   _parse_disk(sec.get("disk", [])),
        "reboot_required":        _parse_reboot(sec.get("reboot_required", []))[0],
        "reboot_required_detail": _parse_reboot(sec.get("reboot_required", []))[1],
        "services_failed":        _parse_services_failed(sec.get("services_failed", [])),
        "journal_errors_tail":    _parse_journal(sec.get("journal_errors", [])),
        "docker":                 _parse_docker(sec.get("docker", [])),
        "loadavg":                _parse_load(sec.get("load", [])),
        "mem":                    _parse_mem(sec.get("mem", [])),
        "smart":                  _parse_smart(sec.get("smart", [])),
    }


def audit(host: str, refresh: bool = False) -> tuple[int, dict]:
    return run_cached(
        name="audit",
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
        print("usage: god-audit.py <host> [--refresh]", file=sys.stderr)
        sys.exit(2)
    status, body = audit(sys.argv[1], refresh="--refresh" in sys.argv[2:])
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2))
    sys.exit(0 if status < 400 else 1)
