#!/usr/bin/env python3
# =====================================================================
#  god-discover.py
#
#  Backend for /api/discover/<host> (fase c).
#  - Runs ansible-playbook discover.yml --limit <host>
#  - Cache TTL 1h (override with ?refresh=1 in HTTP layer)
#  - Per-host lockfile to serialize concurrent requests
#  - Parses the probes_raw block emitted by the playbook into
#    structured ports/users/docker/pct/qm/disks fields.
#
#  Pure stdlib — invoked from god-collector.py.
# =====================================================================
from __future__ import annotations
import errno
import json
import os
import subprocess
import time
from pathlib import Path

DATA_DIR     = Path(os.environ.get("GOD_DATA_DIR", "/data"))
CACHE_DIR    = DATA_DIR / "cache" / "discover"
LOCKS_DIR    = DATA_DIR / "locks"
INV_FILE     = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE     = DATA_DIR / "ansible" / "ansible.cfg"
PLAYBOOK     = "/usr/share/god-mode/ansible/playbooks/discover.yml"

CACHE_TTL_S  = 3600           # 1 hour
LOCK_STALE_S = 180            # consider a lock dead after 3 minutes
PLAYBOOK_TIMEOUT_S = 90       # discover is heavier than gather


def _ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(host: str) -> Path:
    return CACHE_DIR / f"{host}.json"


def _lock_path(host: str) -> Path:
    return LOCKS_DIR / f"discover-{host}.lock"


def _acquire_lock(host: str) -> bool:
    """Atomic O_EXCL lock. Returns True on acquire, False on conflict.
    Stale locks (older than LOCK_STALE_S) are taken over."""
    p = _lock_path(host)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode())
        os.close(fd)
        return True
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    # Check staleness
    try:
        age = time.time() - p.stat().st_mtime
        if age > LOCK_STALE_S:
            p.unlink(missing_ok=True)
            return _acquire_lock(host)
    except FileNotFoundError:
        return _acquire_lock(host)
    return False


def _release_lock(host: str) -> None:
    _lock_path(host).unlink(missing_ok=True)


def _cache_age_s(host: str) -> float | None:
    p = _cache_path(host)
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def _read_cache(host: str) -> dict | None:
    p = _cache_path(host)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _parse_probes(raw: str) -> dict:
    """Parse the multi-section probes_raw text into structured fields."""
    out = {
        "ports": [],
        "users": [],
        "docker": [],
        "pct": [],
        "qm": [],
        "disks": [],
        "reboot_required": False,
        "uptime_s": 0,
    }
    if not raw:
        return out
    section = None
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("====") and s.endswith("===="):
            section = s.strip("=").strip()
            continue
        if not s:
            continue
        if section == "PORTS":
            # "0.0.0.0:22 users:((\"sshd\",pid=...))"
            parts = s.split(None, 1)
            addr = parts[0]
            proc = parts[1] if len(parts) > 1 else ""
            port = addr.rsplit(":", 1)[-1] if ":" in addr else addr
            try:
                out["ports"].append({
                    "port": int(port),
                    "bind": addr,
                    "process_raw": proc,
                })
            except ValueError:
                pass
        elif section == "USERS":
            # "sergio:1000:/home/sergio:/bin/bash"
            f = s.split(":")
            if len(f) >= 4:
                out["users"].append({
                    "name": f[0], "uid": int(f[1]) if f[1].isdigit() else f[1],
                    "home": f[2], "shell": f[3],
                })
        elif section == "DOCKER":
            # "name|image|status|ports"
            f = s.split("|")
            if len(f) >= 3:
                out["docker"].append({
                    "name": f[0], "image": f[1], "status": f[2],
                    "ports": f[3] if len(f) > 3 else "",
                })
        elif section == "PCT":
            # "VMID Status Lock Name" (whitespace-separated, columns)
            f = s.split()
            if len(f) >= 2 and f[0].isdigit():
                out["pct"].append({
                    "vmid": int(f[0]), "status": f[1],
                    "name": f[-1] if len(f) >= 3 else "",
                })
        elif section == "QM":
            # "VMID NAME STATUS MEM(MB) BOOTDISK(GB) PID"
            f = s.split()
            if len(f) >= 3 and f[0].isdigit():
                out["qm"].append({
                    "vmid": int(f[0]), "name": f[1], "status": f[2],
                })
        elif section == "DISKS":
            f = s.split(None, 3)
            if len(f) >= 2:
                out["disks"].append({
                    "name": f[0], "size": f[1],
                    "type": f[2] if len(f) > 2 else "",
                    "model": f[3] if len(f) > 3 else "",
                })
        elif section == "REBOOT":
            out["reboot_required"] = (s.lower() == "yes")
        elif section == "UPTIME":
            try:
                out["uptime_s"] = int(s)
            except ValueError:
                pass
    return out


def _enrich(doc: dict) -> dict:
    """Post-process: parse probes_raw, drop the raw field for cleanliness."""
    parsed = _parse_probes(doc.pop("probes_raw", "") or "")
    doc.update(parsed)
    return doc


def _run_playbook(host: str) -> tuple[int, str]:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    env["ANSIBLE_STDOUT_CALLBACK"] = "default"
    env["ANSIBLE_FORCE_COLOR"] = "false"
    env.pop("ANSIBLE_CALLBACK_RESULT_FORMAT", None)
    try:
        proc = subprocess.run(
            ["ansible-playbook", PLAYBOOK, "-i", str(INV_FILE), "--limit", host],
            capture_output=True, text=True, timeout=PLAYBOOK_TIMEOUT_S, env=env,
        )
        return proc.returncode, proc.stderr[-1000:] if proc.returncode else ""
    except subprocess.TimeoutExpired:
        return -1, "ansible-playbook TIMEOUT"


def discover(host: str, refresh: bool = False) -> tuple[int, dict]:
    """Returns (http_status, body).
    - 200: fresh or cached
    - 202: locked (another request in flight); body contains last cached if any
    - 503: playbook failed and no cache
    - 504: lock timeout
    """
    _ensure_dirs()
    age = _cache_age_s(host)
    use_cache = (not refresh) and (age is not None) and (age < CACHE_TTL_S)

    if use_cache:
        cached = _read_cache(host)
        if cached is not None:
            return 200, {
                "cache": "hit", "cached_age_s": int(age),
                "ttl_s": CACHE_TTL_S, **_enrich(cached),
            }

    if not _acquire_lock(host):
        cached = _read_cache(host)
        if cached is not None:
            return 202, {
                "cache": "stale_in_flight", "cached_age_s": int(age or 0),
                **_enrich(cached),
            }
        return 504, {"error": "lock held by another request, no cache available"}

    try:
        rc, stderr_tail = _run_playbook(host)
        if rc != 0:
            cached = _read_cache(host)
            if cached is not None:
                return 200, {
                    "cache": "stale_after_failed_refresh",
                    "cached_age_s": int(age or 0),
                    "refresh_error": f"ansible rc={rc}: {stderr_tail}",
                    **_enrich(cached),
                }
            return 503, {
                "error": "ansible-playbook failed and no cache available",
                "rc": rc, "stderr_tail": stderr_tail,
            }
        cached = _read_cache(host)
        if cached is None:
            return 503, {
                "error": "playbook succeeded but no cache file written; "
                         "host may have been skipped (non-POSIX) or unreachable",
            }
        return 200, {
            "cache": "miss", "cached_age_s": 0, "ttl_s": CACHE_TTL_S,
            **_enrich(cached),
        }
    finally:
        _release_lock(host)


# CLI helper for manual testing inside the addon container.
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: god-discover.py <host> [--refresh]", file=sys.stderr)
        sys.exit(2)
    h = sys.argv[1]
    refresh = "--refresh" in sys.argv[2:]
    status, body = discover(h, refresh=refresh)
    print(f"HTTP {status}")
    print(json.dumps(body, indent=2))
    sys.exit(0 if status < 400 else 1)
