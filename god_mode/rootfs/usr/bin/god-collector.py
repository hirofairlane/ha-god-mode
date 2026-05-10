#!/usr/bin/env python3
# =====================================================================
#  god-collector.py - polls all hosts via Ansible playbook + serves
#  the resulting JSON over HTTP for the HA `rest:` integration.
#
#  Loop:
#    1. read /data/options.json (poll_interval, hosts list)
#    2. run `ansible-playbook gather.yml` against /data/ansible/inventory.yml
#    3. parse JSON output, cache per host in memory
#    4. expose:
#         GET /api/hosts             -> dict { name: metrics }
#         GET /api/host/<name>       -> single host JSON
#         GET /api/health            -> status, stale list, uptime
#         GET /api/pubkey            -> the SSH public key for propagation
#         GET /api/inventory         -> current Ansible inventory (parsed)
#  Stdlib only (http.server, threading, subprocess, json).
# =====================================================================
from __future__ import annotations
import http.server
import json
import os
import socketserver
import subprocess
import threading
import time
from pathlib import Path

# ---- Config from environment / Supervisor options.json ---------------
DATA_DIR     = Path(os.environ.get("GOD_DATA_DIR", "/data"))
OPTS_FILE    = DATA_DIR / "options.json"   # written by Supervisor at start
INV_FILE     = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE     = DATA_DIR / "ansible" / "ansible.cfg"
PLAYBOOK     = "/usr/share/god-mode/ansible/playbooks/gather.yml"
PUBKEY_FILE  = DATA_DIR / ".ssh" / "id_ed25519.pub"

LISTEN_HOST  = "0.0.0.0"
LISTEN_PORT  = 9876

CACHE: dict[str, dict] = {}
LOCK = threading.Lock()
START_TS = int(time.time())


def load_options() -> dict:
    if OPTS_FILE.exists():
        try:
            return json.loads(OPTS_FILE.read_text())
        except Exception:
            pass
    return {"poll_interval": 60, "hosts": []}


def run_playbook() -> dict[str, dict]:
    """Runs the gather playbook with json stdout callback. Returns
    {hostname: metrics_dict} merged from playbook results."""
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    env["ANSIBLE_STDOUT_CALLBACK"] = "json"
    env["ANSIBLE_LOAD_CALLBACK_PLUGINS"] = "true"
    env["ANSIBLE_FORCE_COLOR"] = "false"
    try:
        proc = subprocess.run(
            ["ansible-playbook", PLAYBOOK, "-i", str(INV_FILE)],
            capture_output=True, text=True, timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        print("[poll] ansible-playbook TIMEOUT", flush=True)
        return {}
    out: dict[str, dict] = {}
    if not proc.stdout.strip():
        print(f"[poll] empty stdout, stderr={proc.stderr[:300]!r}", flush=True)
        return {}
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[poll] bad ansible json: {e}", flush=True)
        return {}
    for play in result.get("plays", []):
        for task in play.get("tasks", []):
            name = task.get("task", {}).get("name", "")
            for host, hostdata in task.get("hosts", {}).items():
                # The 'set_fact' task returns ansible_facts in hostdata
                if "ansible_facts" in hostdata:
                    facts = hostdata["ansible_facts"]
                    metric = facts.get("god_metric")
                    if metric:
                        # parse JSON if it came as string
                        if isinstance(metric, str):
                            try:
                                metric = json.loads(metric)
                            except json.JSONDecodeError:
                                pass
                        out[host] = metric
                if hostdata.get("unreachable") or hostdata.get("failed"):
                    out.setdefault(host, {})
                    out[host].update({
                        "_ok": False,
                        "_error": hostdata.get("msg") or "unreachable",
                    })
    return out


def poll_loop():
    while True:
        opts = load_options()
        poll_every = max(15, int(opts.get("poll_interval", 60)))
        t0 = time.time()
        try:
            results = run_playbook()
        except Exception as e:
            print(f"[poll] exception: {e}", flush=True)
            results = {}
        ts_now = int(time.time())
        with LOCK:
            for name, metrics in results.items():
                if "_ok" not in metrics:
                    metrics["_ok"] = True
                metrics["_polled_at"] = ts_now
                CACHE[name] = metrics
        ok = sum(1 for v in results.values() if v.get("_ok"))
        elapsed = time.time() - t0
        print(f"[poll] {time.strftime('%H:%M:%S')} - {ok}/{len(results)} ok ({elapsed:.1f}s)", flush=True)
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
        self._send(404, {"error": "not found"})


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    threading.Thread(target=poll_loop, daemon=True).start()
    server = ThreadingServer((LISTEN_HOST, LISTEN_PORT), Handler)
    print(f"[god-collector] listening on http://{LISTEN_HOST}:{LISTEN_PORT}", flush=True)
    print(f"[god-collector] inventory={INV_FILE}, playbook={PLAYBOOK}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
