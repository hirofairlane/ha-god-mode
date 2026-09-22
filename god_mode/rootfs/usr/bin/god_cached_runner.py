#!/usr/bin/env python3
# =====================================================================
#  god_cached_runner.py — shared infra for fase-c on-demand wrappers.
#
#  Implements the cache-with-lock pattern used by god-discover.py and
#  reused by god-updates.py / god-audit.py. Each wrapper provides:
#    - PLAYBOOK    : path to the YAML playbook
#    - CACHE_DIR   : Path where the playbook writes <host>.raw.json
#    - CACHE_TTL_S : seconds before a refresh is forced
#    - parse(raw_doc) -> dict : turn the raw on-disk payload into the
#                                response body shape
#
#  The wrapper then calls `run_cached(host, refresh=False)` from this
#  module and forwards (status, body) back to the HTTP layer.
#
#  Pure stdlib.
# =====================================================================
from __future__ import annotations
import errno
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Callable

DATA_DIR     = Path(os.environ.get("GOD_DATA_DIR", "/data"))
LOCKS_DIR    = DATA_DIR / "locks"
INV_FILE     = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE     = DATA_DIR / "ansible" / "ansible.cfg"

LOCK_STALE_S = 180             # consider a lock dead after 3 minutes


def _lock_path(name: str, host: str) -> Path:
    return LOCKS_DIR / f"{name}-{host}.lock"


def _acquire_lock(name: str, host: str) -> bool:
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    p = _lock_path(name, host)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, f"{os.getpid()}\n{int(time.time())}\n".encode())
        os.close(fd)
        return True
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    try:
        age = time.time() - p.stat().st_mtime
        if age > LOCK_STALE_S:
            p.unlink(missing_ok=True)
            return _acquire_lock(name, host)
    except FileNotFoundError:
        return _acquire_lock(name, host)
    return False


def _release_lock(name: str, host: str) -> None:
    _lock_path(name, host).unlink(missing_ok=True)


def _cache_path(cache_dir: Path, host: str) -> Path:
    # Playbooks write <host>.raw.json; we expose <host>.json as the
    # parsed/enriched response (kept separately so a parse failure
    # doesn't corrupt the raw artefact).
    return cache_dir / f"{host}.raw.json"


def _cache_age_s(cache_dir: Path, host: str) -> float | None:
    p = _cache_path(cache_dir, host)
    if not p.exists():
        return None
    return time.time() - p.stat().st_mtime


def _read_raw(cache_dir: Path, host: str) -> dict | None:
    p = _cache_path(cache_dir, host)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _run_playbook(playbook: Path, host: str, timeout_s: int) -> tuple[int, str]:
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    env["ANSIBLE_STDOUT_CALLBACK"] = "default"
    env["ANSIBLE_FORCE_COLOR"] = "false"
    env.pop("ANSIBLE_CALLBACK_RESULT_FORMAT", None)
    try:
        proc = subprocess.run(
            ["ansible-playbook", str(playbook), "-i", str(INV_FILE), "--limit", host],
            capture_output=True, text=True, timeout=timeout_s, env=env,
        )
        return proc.returncode, proc.stderr[-1000:] if proc.returncode else ""
    except subprocess.TimeoutExpired:
        return -1, f"ansible-playbook TIMEOUT after {timeout_s}s"


def run_cached(
    *,
    name: str,                            # short id ("updates", "audit") for lock files
    playbook: Path,
    cache_dir: Path,
    parse: Callable[[dict], dict],
    cache_ttl_s: int,
    playbook_timeout_s: int,
    host: str,
    refresh: bool = False,
) -> tuple[int, dict]:
    """Generic cache-with-lock runner. Returns (http_status, body)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    age = _cache_age_s(cache_dir, host)
    use_cache = (not refresh) and (age is not None) and (age < cache_ttl_s)

    if use_cache:
        raw = _read_raw(cache_dir, host)
        if raw is not None:
            return 200, {
                "cache": "hit",
                "cached_age_s": int(age),
                "ttl_s": cache_ttl_s,
                **parse(raw),
            }

    if not _acquire_lock(name, host):
        raw = _read_raw(cache_dir, host)
        if raw is not None:
            return 202, {
                "cache": "stale_in_flight",
                "cached_age_s": int(age or 0),
                **parse(raw),
            }
        return 504, {"error": f"{name} lock held by another request, no cache available"}

    try:
        rc, stderr_tail = _run_playbook(playbook, host, playbook_timeout_s)
        if rc != 0:
            raw = _read_raw(cache_dir, host)
            if raw is not None:
                return 200, {
                    "cache": "stale_after_failed_refresh",
                    "cached_age_s": int(age or 0),
                    "refresh_error": f"ansible rc={rc}: {stderr_tail}",
                    **parse(raw),
                }
            return 503, {
                "error": f"{name} playbook failed and no cache available",
                "rc": rc, "stderr_tail": stderr_tail,
            }
        raw = _read_raw(cache_dir, host)
        if raw is None:
            return 503, {
                "error": f"{name} playbook succeeded but no cache written; "
                         "host may have been skipped (unknown OS) or unreachable",
            }
        return 200, {
            "cache": "miss", "cached_age_s": 0, "ttl_s": cache_ttl_s,
            **parse(raw),
        }
    finally:
        _release_lock(name, host)
