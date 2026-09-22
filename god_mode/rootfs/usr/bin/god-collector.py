#!/usr/bin/env python3
# =====================================================================
#  god-collector.py
#
#  Loop:
#    1. read /data/options.json (poll_interval)
#    2. run `ansible-playbook gather.yml -i /data/ansible/inventory.yml`
#       -> playbook writes /data/metrics/<host>.json on controller side
#    3. read all /data/metrics/*.json into in-memory cache
#    4. expose:
#         GET /api/hosts             -> dict { name: metrics }
#         GET /api/host/<name>       -> single host JSON
#         GET /api/health            -> status, stale list, uptime
#         GET /api/pubkey            -> the SSH public key
#         GET /api/inventory         -> current Ansible inventory
#  Stdlib only.
# =====================================================================
from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path

# Sibling modules god-{discover,updates,audit}.py — filenames with a dash,
# can't `import` directly, so we go through importlib.util.
import importlib.util as _ilu
def _load_sibling(modname: str, path: str):
    spec = _ilu.spec_from_file_location(modname, path)
    mod = _ilu.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)       # type: ignore[union-attr]
    return mod
god_discover = _load_sibling("god_discover", "/usr/bin/god-discover.py")
god_updates  = _load_sibling("god_updates",  "/usr/bin/god-updates.py")
god_audit    = _load_sibling("god_audit",    "/usr/bin/god-audit.py")

DATA_DIR     = Path(os.environ.get("GOD_DATA_DIR", "/data"))
METRICS_DIR  = DATA_DIR / "metrics"
OPTS_FILE    = DATA_DIR / "options.json"
INV_FILE     = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE     = DATA_DIR / "ansible" / "ansible.cfg"
CATEGORIES_FILE = DATA_DIR / "ansible" / "categories.json"
CHIPS_FILE      = DATA_DIR / "ansible" / "chips.json"
PARENTS_FILE    = DATA_DIR / "ansible" / "parents.json"
PVE_CHILDREN_DIR = DATA_DIR / "pve_children"
PLAYBOOK     = "/usr/share/god-mode/ansible/playbooks/gather.yml"
PUBKEY_FILE  = DATA_DIR / ".ssh" / "id_ed25519.pub"

LISTEN_HOST  = "0.0.0.0"
LISTEN_PORT  = 9876

CACHE: dict[str, dict] = {}
LOCK = threading.Lock()
START_TS = int(time.time())

# In-memory ring buffer of metric history for sparklines / 6h trend.
# Schema: HISTORY[host][metric] = [(ts, value), ...]
# Kept short on purpose: long-term storage belongs in InfluxDB.
HISTORY: dict[str, dict[str, list[tuple[int, float]]]] = {}
HISTORY_MAX_POINTS = 360   # 6h @ 60s scan_interval
HISTORY_METRICS = ("cpu_pct", "mem_pct", "swap_pct", "disk_max_pct", "temp_max_c")


def load_options() -> dict:
    if OPTS_FILE.exists():
        try:
            return json.loads(OPTS_FILE.read_text())
        except Exception:
            pass
    return {"poll_interval": 60, "hosts": []}


def run_playbook() -> int:
    """Runs the gather playbook. Side effect: writes /data/metrics/<host>.json.
    Returns playbook return code."""
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    env["ANSIBLE_STDOUT_CALLBACK"] = "default"  # built-in
    env["ANSIBLE_FORCE_COLOR"] = "false"
    env.pop("ANSIBLE_CALLBACK_RESULT_FORMAT", None)
    try:
        proc = subprocess.run(
            ["ansible-playbook", PLAYBOOK, "-i", str(INV_FILE)],
            capture_output=True, text=True, timeout=180, env=env,
        )
        if proc.returncode != 0:
            print(f"[poll] ansible-playbook rc={proc.returncode}", flush=True)
            print(f"[poll] stderr tail: {proc.stderr[-500:]}", flush=True)
        return proc.returncode
    except subprocess.TimeoutExpired:
        print("[poll] ansible-playbook TIMEOUT", flush=True)
        return -1


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text()) or {}
        except Exception:
            pass
    return {}


def load_categories() -> dict[str, str]:
    return _load_json(CATEGORIES_FILE)


def load_chips() -> dict[str, str]:
    return _load_json(CHIPS_FILE)


def load_parents() -> dict[str, str]:
    return _load_json(PARENTS_FILE)


def load_pve_children() -> dict[str, dict]:
    """Returns { node_name: snapshot } merged from /data/pve_children/*.json
    (excluding _summary.json)."""
    out: dict[str, dict] = {}
    if not PVE_CHILDREN_DIR.exists():
        return out
    for f in PVE_CHILDREN_DIR.glob("*.json"):
        if f.name.startswith("_"):
            continue
        try:
            d = json.loads(f.read_text())
            out[d.get("node") or f.stem] = d
        except Exception:
            pass
    return out


def load_inventory_meta() -> dict[str, dict]:
    """Returns { host_name: { addr, user, port, _pve_token_id, _pve_token_configured } }
    sourced from options.json. Used to inject the inventory-side metadata
    into the collector output so the dashboard can show IP / SSH endpoint
    and Proxmox token status without a separate fetch.

    `_pve_token_configured` is True iff pve_token_secret has a non-empty
    value — the dashboard uses it to surface a clear "missing secret"
    troubleshoot panel without exposing the secret itself."""
    out: dict[str, dict] = {}
    try:
        opts = json.loads(OPTS_FILE.read_text())
    except Exception:
        return out
    for h in opts.get("hosts", []) or []:
        name = h.get("name")
        if not name:
            continue
        entry: dict = {
            "addr": h.get("addr"),
            "user": h.get("user") or "root",
            "port": int(h.get("port") or 22),
        }
        if h.get("category") == "pve_node":
            entry["_pve_token_id"] = h.get("pve_token_id") or ""
            entry["_pve_token_configured"] = bool((h.get("pve_token_secret") or "").strip())
        out[name] = entry
    return out


def load_metrics_from_disk() -> dict[str, dict]:
    out = {}
    cats    = load_categories()
    chips   = load_chips()
    parents = load_parents()
    inv     = load_inventory_meta()
    def _inject_inv(name: str, entry: dict) -> None:
        m = inv.get(name) or {}
        if m.get("addr"): entry["addr"] = m["addr"]
        if m.get("user"): entry["user"] = m["user"]
        if m.get("port"): entry["port"] = m["port"]
        if "_pve_token_id" in m: entry["_pve_token_id"] = m["_pve_token_id"]
        if "_pve_token_configured" in m: entry["_pve_token_configured"] = m["_pve_token_configured"]
    # Include even hosts that haven't replied yet, so the dashboard can
    # render them as offline.
    for name, cat in cats.items():
        out[name] = {
            "_ok": False,
            "_error": "not_polled_yet",
            "_polled_at": 0,
            "_category": cat,
        }
        if name in chips:
            out[name]["_chip"] = chips[name]
        if name in parents:
            out[name]["_parent"] = parents[name]
        _inject_inv(name, out[name])
    if not METRICS_DIR.exists():
        return out
    for f in METRICS_DIR.glob("*.json"):
        name = f.stem
        try:
            d = json.loads(f.read_text())
            d["_ok"] = bool(d.get("_ok", True)) and "_error" not in d
            d["_polled_at"] = int(f.stat().st_mtime)
            d["_category"] = cats.get(name, "uncategorized")
            if name in chips:
                d["_chip"] = chips[name]
            if name in parents:
                d["_parent"] = parents[name]
            _inject_inv(name, d)
            out[name] = d
        except Exception as e:
            err_entry = {"_ok": False, "_error": f"parse: {e}", "_category": cats.get(name, "uncategorized")}
            _inject_inv(name, err_entry)
            out[name] = err_entry
    return out


def record_history(results: dict[str, dict]) -> None:
    """Append current metric values to the in-memory ring buffer."""
    now = int(time.time())
    for name, m in results.items():
        if not m.get("_ok"):
            continue
        h = HISTORY.setdefault(name, {})
        for metric in HISTORY_METRICS:
            v = m.get(metric)
            if v is None:
                continue
            try:
                fv = float(v)
            except (TypeError, ValueError):
                continue
            series = h.setdefault(metric, [])
            series.append((now, fv))
            if len(series) > HISTORY_MAX_POINTS:
                del series[: len(series) - HISTORY_MAX_POINTS]


def poll_loop():
    while True:
        opts = load_options()
        poll_every = max(15, int(opts.get("poll_interval", 60)))
        t0 = time.time()
        rc = run_playbook()
        results = load_metrics_from_disk()
        with LOCK:
            CACHE.clear()
            CACHE.update(results)
            record_history(results)
        ok = sum(1 for v in results.values() if v.get("_ok"))
        elapsed = time.time() - t0
        print(f"[poll] {time.strftime('%H:%M:%S')} - {ok}/{len(results)} ok rc={rc} ({elapsed:.1f}s)", flush=True)
        sleep_for = max(5, poll_every - elapsed)
        time.sleep(sleep_for)


# ---- HTTP Handler ----------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, body):
        if isinstance(body, str):
            data = body.encode("utf-8")
            ctype = "text/plain; charset=utf-8"
        else:
            data = json.dumps(body).encode("utf-8")
            ctype = "application/json"
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_ondemand(self, fn, label: str, *, strip: str):
        """Shared dispatch for /api/{discover,updates,audit}/<host>[?refresh=1].
        - Rejects unknown hosts with 404 (host must exist in inventory).
        - Catches uncaught exceptions in the wrapper and returns 500.
        """
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        host = u.path.rsplit("/", 1)[-1]
        qs = parse_qs(u.query)
        refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
        with LOCK:
            known = host in CACHE
        if not known:
            self._send(404, {"error": f"unknown host {host}"})
            return
        try:
            status, body = fn(host, refresh=refresh)
        except Exception as e:
            self._send(500, {"error": f"{label} crashed: {e!r}"})
            return
        self._send(status, body)

    def do_GET(self):
        if self.path == "/api/hosts":
            with LOCK:
                self._send(200, dict(CACHE))
            return
        if self.path.startswith("/api/host/"):
            name = self.path.split("/")[-1]
            with LOCK:
                d = CACHE.get(name)
            if d is None:
                self._send(404, {"error": f"unknown host {name}"})
            else:
                self._send(200, d)
            return
        if self.path == "/api/health":
            now = int(time.time())
            with LOCK:
                stale = [h for h, v in CACHE.items()
                         if (now - v.get("_polled_at", 0)) > load_options().get("poll_interval", 60) * 2]
                self._send(200, {
                    "status": "ok",
                    "hosts": len(CACHE),
                    "stale": stale,
                    "now": now,
                    "uptime_s": now - START_TS,
                })
            return
        if self.path == "/api/pubkey":
            try:
                self._send(200, PUBKEY_FILE.read_text().strip())
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        if self.path == "/api/inventory":
            try:
                self._send(200, INV_FILE.read_text())
            except Exception as e:
                self._send(500, {"error": str(e)})
            return
        if self.path == "/api/pve_children":
            self._send(200, load_pve_children())
            return
        if self.path.startswith("/api/pve_node/"):
            node = self.path.split("/")[-1]
            snap = load_pve_children().get(node)
            if snap is None:
                self._send(404, {"error": f"unknown pve_node {node}"})
            else:
                self._send(200, snap)
            return
        if self.path.startswith("/api/discover/"):
            return self._handle_ondemand(god_discover.discover, "discover", strip="/api/discover/")
        if self.path.startswith("/api/updates/"):
            return self._handle_ondemand(god_updates.updates, "updates", strip="/api/updates/")
        if self.path.startswith("/api/audit/"):
            return self._handle_ondemand(god_audit.audit, "audit", strip="/api/audit/")
        if self.path.startswith("/api/snapshot/"):
            # /api/snapshot/<host>[?refresh=1]
            # Combined view: gather metrics from CACHE + discover + updates +
            # audit. Returns whatever succeeds; each section carries its
            # own status (200/202/503) so the consumer can decide.
            from urllib.parse import urlparse, parse_qs
            u = urlparse(self.path)
            host = u.path.rsplit("/", 1)[-1]
            qs = parse_qs(u.query)
            refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "yes")
            with LOCK:
                known = host in CACHE
                metrics = dict(CACHE.get(host, {})) if known else None
            if not known:
                self._send(404, {"error": f"unknown host {host}"})
                return
            sections: dict = {"host": host, "metrics": metrics}
            for label, fn in (("discover", god_discover.discover),
                              ("updates",  god_updates.updates),
                              ("audit",    god_audit.audit)):
                try:
                    st, body = fn(host, refresh=refresh)
                except Exception as e:
                    sections[label] = {"_error": f"{label} crashed: {e!r}", "_status": 500}
                else:
                    sections[label] = {"_status": st, **body}
            self._send(200, sections)
            return
        if self.path.startswith("/api/history/"):
            # /api/history/<host>[?metric=cpu_pct,mem_pct]
            from urllib.parse import parse_qs, urlparse
            u = urlparse(self.path)
            host = u.path.split("/")[-1]
            qs = parse_qs(u.query)
            metrics = qs.get("metric", [",".join(HISTORY_METRICS)])[0].split(",")
            with LOCK:
                h = HISTORY.get(host, {})
                out = {m: list(h.get(m, [])) for m in metrics if m in HISTORY_METRICS}
            self._send(200, {"host": host, "series": out})
            return
        self._send(404, {"error": "not found"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    METRICS_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[god-collector] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[god-collector] inventory={INV_FILE}, playbook={PLAYBOOK}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
