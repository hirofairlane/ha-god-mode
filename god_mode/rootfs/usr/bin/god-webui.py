#!/usr/bin/env python3
# =====================================================================
#  god-webui.py - HA ingress UI for the GOD Mode add-on
#
#  Single-page app served on port 8099 (HA ingress proxies it):
#    - Shows the SSH public key with a copy button
#    - Shows the current host status from collector cache
#    - "Run install_agent" / "Run ping" / "Reload inventory" actions
#    - Live tail of last poll log lines
#
#  Stdlib only.
# =====================================================================
from __future__ import annotations
import http.server
import json
import os
import socketserver
import subprocess
import threading
import urllib.request
from pathlib import Path

DATA_DIR    = Path("/data")
PUBKEY_FILE = DATA_DIR / ".ssh" / "id_ed25519.pub"
INV_FILE    = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE    = DATA_DIR / "ansible" / "ansible.cfg"
COLLECTOR_URL = "http://localhost:9876"
PLAYBOOKS = {
    "ping":          "/usr/share/god-mode/ansible/playbooks/ping.yml",
    "install_agent": "/usr/share/god-mode/ansible/playbooks/install_agent.yml",
    "gather":        "/usr/share/god-mode/ansible/playbooks/gather.yml",
}

PORT = 8099

INDEX_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>GOD Mode</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         background:#0e1116; color:#e6edf3; margin:0; padding:1rem; }
  h1 { color:#f0883e; margin:0 0 1rem 0; }
  h2 { color:#79c0ff; margin-top:2rem; border-bottom:1px solid #30363d; padding-bottom:0.3rem; }
  .panel { background:#161b22; border:1px solid #30363d; border-radius:6px;
           padding:1rem; margin-bottom:1rem; }
  pre { background:#0e1116; padding:0.6rem; border-radius:4px; overflow-x:auto;
        white-space:pre-wrap; word-break:break-all; border:1px solid #21262d; }
  button { background:#238636; color:white; border:none; padding:0.5rem 1rem;
           border-radius:6px; cursor:pointer; font-family:inherit; margin-right:0.5rem;
           margin-bottom:0.5rem; }
  button:hover { background:#2ea043; }
  button.secondary { background:#21262d; }
  button.secondary:hover { background:#30363d; }
  table { width:100%; border-collapse:collapse; margin-top:0.5rem; }
  th, td { padding:0.4rem 0.6rem; text-align:left; border-bottom:1px solid #21262d; }
  th { background:#21262d; }
  .ok { color:#3fb950; }
  .ko { color:#f85149; }
  .warn { color:#d29922; }
  .small { font-size:0.85rem; color:#8b949e; }
  #log { max-height:200px; overflow-y:auto; }
</style>
</head><body>
<h1>👁 GOD Mode</h1>
<div class="small">Centralized homelab monitoring · ingress UI</div>

<h2>1. SSH public key</h2>
<div class="panel">
  <p>Copy this key into <code>~/.ssh/authorized_keys</code> on every host you want to monitor.</p>
  <pre id="pubkey">loading...</pre>
  <button onclick="copyKey()">📋 Copy</button>
  <span id="copy-status" class="small"></span>
</div>

<h2>2. Hosts status</h2>
<div class="panel">
  <button onclick="refresh()">🔄 Refresh</button>
  <button class="secondary" onclick="run('ping')">📡 Ansible ping</button>
  <button class="secondary" onclick="run('install_agent')">📦 Install agent on all</button>
  <button class="secondary" onclick="run('gather')">🔬 Gather now</button>
  <table id="hosts">
    <thead><tr><th>host</th><th>status</th><th>cpu%</th><th>mem%</th><th>disk%</th><th>temp</th><th>updates</th><th>last poll</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>3. Action log</h2>
<div class="panel">
  <pre id="log">(none yet)</pre>
</div>

<script>
async function refresh() {
  try {
    const pub = await (await fetch('api/pubkey')).text();
    document.getElementById('pubkey').textContent = pub.trim();
  } catch(e) { document.getElementById('pubkey').textContent = 'ERROR: '+e; }
  try {
    const hosts = await (await fetch('api/hosts')).json();
    const tbody = document.querySelector('#hosts tbody');
    tbody.innerHTML = '';
    const now = Math.floor(Date.now()/1000);
    Object.entries(hosts).forEach(([name, m]) => {
      const tr = document.createElement('tr');
      const ok = m._ok;
      const age = m._polled_at ? (now - m._polled_at) + 's' : '?';
      const status = ok ? '<span class="ok">OK</span>' : '<span class="ko">'+(m._error||'fail')+'</span>';
      tr.innerHTML = `<td><b>${name}</b></td><td>${status}</td><td>${m.cpu_pct ?? '-'}</td><td>${m.mem_pct ?? '-'}</td><td>${m.disk_max_pct ?? '-'}</td><td>${m.temp_max_c ?? '-'}</td><td>${m.updates_pending ?? '-'}</td><td class="small">${age}</td>`;
      tbody.appendChild(tr);
    });
  } catch(e) {
    const tbody = document.querySelector('#hosts tbody');
    tbody.innerHTML = '<tr><td colspan=8 class="ko">Collector unreachable: '+e+'</td></tr>';
  }
}
function copyKey() {
  const text = document.getElementById('pubkey').textContent;
  navigator.clipboard.writeText(text).then(() => {
    document.getElementById('copy-status').textContent = '✓ copied';
    setTimeout(() => document.getElementById('copy-status').textContent='', 2000);
  });
}
async function run(action) {
  const log = document.getElementById('log');
  log.textContent = 'Running '+action+'...\\n';
  try {
    const r = await fetch('api/action/'+action, { method: 'POST' });
    const txt = await r.text();
    log.textContent = txt;
    refresh();
  } catch(e) { log.textContent = 'ERROR: '+e; }
}
refresh();
setInterval(refresh, 10000);
</script>
</body></html>
"""


def proxy_collector(path: str) -> tuple[int, bytes, str]:
    """Proxy GET to collector running on localhost:9876"""
    try:
        with urllib.request.urlopen(f"{COLLECTOR_URL}{path}", timeout=5) as r:
            return r.status, r.read(), r.headers.get_content_type() or "application/json"
    except Exception as e:
        return 502, json.dumps({"error": str(e)}).encode(), "application/json"


def run_playbook(name: str) -> str:
    pb = PLAYBOOKS.get(name)
    if not pb:
        return f"unknown action: {name}"
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    try:
        p = subprocess.run(
            ["ansible-playbook", pb, "-i", str(INV_FILE)],
            capture_output=True, text=True, timeout=300, env=env,
        )
        return f"=== {name} (rc={p.returncode}) ===\n{p.stdout[-4000:]}\n--- stderr ---\n{p.stderr[-1000:]}"
    except subprocess.TimeoutExpired:
        return f"=== {name} TIMEOUT after 300s ==="


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        # Ingress can prefix paths with /api/hassio_ingress/<token>/...
        # We strip everything before /api/ or root.
        p = self.path
        if p == "/" or p.endswith("/index.html") or "/api/" not in p and not p.startswith("/api/"):
            # Serve index.html for any non-/api request
            if p.startswith("/api/"):
                pass
            else:
                self._send(200, INDEX_HTML)
                return
        # /api/* -> proxy to collector or read pubkey/inventory
        # Strip everything up to and including "/api/"
        idx = p.rfind("/api/")
        if idx == -1:
            self._send(404, "not found", "text/plain")
            return
        sub = p[idx+1:]  # api/hosts, api/pubkey, api/inventory
        api_path = "/" + sub  # /api/hosts
        code, body, ctype = proxy_collector(api_path)
        self._send(code, body, ctype)

    def do_POST(self):
        # /api/action/<name>
        p = self.path
        idx = p.rfind("/api/action/")
        if idx == -1:
            self._send(404, "not found", "text/plain")
            return
        action = p[idx + len("/api/action/"):].strip("/")
        out = run_playbook(action)
        self._send(200, out, "text/plain; charset=utf-8")


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    print(f"[god-webui] listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
