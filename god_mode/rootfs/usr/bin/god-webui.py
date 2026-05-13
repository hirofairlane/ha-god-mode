#!/usr/bin/env python3
# =====================================================================
#  god-webui.py - HA ingress UI for the GOD Mode add-on
#
#  Single-page app served on port 8099 (HA ingress proxies it):
#    - Shows the SSH public key with a copy button
#    - Shows the current host status from collector cache
#    - Onboarding form: add new host -> auto provision
#    - "Run install_agent" / "Run ping" / "Reload inventory" actions
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
import time
import urllib.request
from pathlib import Path

DATA_DIR    = Path("/data")
PUBKEY_FILE = DATA_DIR / ".ssh" / "id_ed25519.pub"
INV_FILE    = DATA_DIR / "ansible" / "inventory.yml"
CFG_FILE    = DATA_DIR / "ansible" / "ansible.cfg"
OPTS_FILE   = DATA_DIR / "options.json"
COLLECTOR_URL = "http://localhost:9876"
PLAYBOOKS = {
    "ping":          "/usr/share/god-mode/ansible/playbooks/ping.yml",
    "install_agent": "/usr/share/god-mode/ansible/playbooks/install_agent.yml",
    "gather":        "/usr/share/god-mode/ansible/playbooks/gather.yml",
    "power":         "/usr/share/god-mode/ansible/playbooks/power.yml",
}
BOOTSTRAP_SCRIPT = "/usr/bin/god-bootstrap.sh"
CATEGORIES = ["pve_node", "server", "sbc", "desktop_linux", "desktop_win", "desktop_mac", "network"]

PORT = 8099


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>GOD Mode</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         background:#0e1116; color:#e6edf3; margin:0; padding:1rem; max-width:1400px; }
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
  button.danger { background:#da3633; }
  button.danger:hover { background:#f85149; }
  table { width:100%; border-collapse:collapse; margin-top:0.5rem; }
  th, td { padding:0.4rem 0.6rem; text-align:left; border-bottom:1px solid #21262d; }
  th { background:#21262d; }
  .ok { color:#3fb950; }
  .ko { color:#f85149; }
  .warn { color:#d29922; }
  .small { font-size:0.85rem; color:#8b949e; }
  .row { display:flex; gap:1rem; flex-wrap:wrap; }
  .row > div { flex: 1; min-width: 200px; }
  input, select { background:#0d1117; color:#e6edf3; border:1px solid #30363d;
                  padding:0.4rem; border-radius:4px; width:100%; box-sizing:border-box;
                  font-family:inherit; }
  label { display:block; margin-bottom:0.2rem; font-size:0.85rem; color:#8b949e; }
  .cat-pve_node { color:#a371f7; }
  .cat-server { color:#79c0ff; }
  .cat-sbc { color:#f2cc60; }
  .cat-desktop_linux { color:#3fb950; }
  .cat-desktop_win { color:#58a6ff; }
  .cat-desktop_mac { color:#ff7b72; }
  .cat-network { color:#d29922; }
  #log { max-height:300px; overflow-y:auto; }
</style>
</head><body>
<h1>👁 GOD Mode</h1>
<div class="small">Centralized homelab control plane · v0.3.x</div>

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
    <thead><tr><th>host</th><th>category</th><th>status</th><th>cpu%</th><th>mem%</th><th>disk%</th><th>temp</th><th>updates</th><th>last poll</th><th>actions</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<h2>3. Onboard new host</h2>
<div class="panel">
  <p class="small">Add a new host to monitoring. After saving, paste the SSH key into the host's <code>~/.ssh/authorized_keys</code>, then click <b>Install agent</b> to deploy the metrics agent.</p>
  <form id="onboard-form" onsubmit="onboard(event)">
    <div class="row">
      <div>
        <label>Name (a-z0-9_)</label>
        <input id="o-name" required pattern="^[a-z0-9_]+$" placeholder="myhost">
      </div>
      <div>
        <label>Address (IP or hostname)</label>
        <input id="o-addr" required placeholder="192.168.1.50">
      </div>
      <div>
        <label>SSH user</label>
        <input id="o-user" value="root">
      </div>
      <div>
        <label>SSH port</label>
        <input id="o-port" type="number" value="22">
      </div>
      <div>
        <label>Category</label>
        <select id="o-cat">
          <option value="pve_node">pve_node</option>
          <option value="server" selected>server</option>
          <option value="sbc">sbc</option>
          <option value="desktop_linux">desktop_linux</option>
          <option value="desktop_win">desktop_win</option>
          <option value="desktop_mac">desktop_mac</option>
          <option value="network">network</option>
        </select>
      </div>
    </div>
    <div style="margin-top:0.8rem;">
      <button type="submit">➕ Add host</button>
      <span id="onboard-status" class="small"></span>
    </div>
  </form>
</div>

<h2>4. Action log</h2>
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
    const entries = Object.entries(hosts);
    // Sort: ok first, then by category then by name
    entries.sort((a,b) => {
      const okA = a[1]._ok ? 0 : 1, okB = b[1]._ok ? 0 : 1;
      if (okA !== okB) return okA - okB;
      const cA = (a[1]._category||'zz'), cB = (b[1]._category||'zz');
      if (cA !== cB) return cA.localeCompare(cB);
      return a[0].localeCompare(b[0]);
    });
    entries.forEach(([name, m]) => {
      const tr = document.createElement('tr');
      const ok = m._ok;
      const age = m._polled_at ? (now - m._polled_at) + 's' : '-';
      const cat = m._category || '?';
      const status = ok ? '<span class="ok">OK</span>'
                        : (m._error === 'not_polled_yet' ? '<span class="warn">pending</span>'
                                                          : '<span class="ko">'+(m._error||'fail')+'</span>');
      const actions = `
        <button class="secondary" onclick="power('wake','${name}')" title="Wake on LAN">⏻</button>
        <button class="secondary" onclick="power('reboot','${name}')" title="Reboot via SSH">↻</button>
        <button class="danger" onclick="power('shutdown','${name}')" title="Shutdown via SSH">⏼</button>`;
      tr.innerHTML = `<td><b>${name}</b></td><td class="cat-${cat}">${cat}</td><td>${status}</td>` +
                     `<td>${m.cpu_pct ?? '-'}</td><td>${m.mem_pct ?? '-'}</td>` +
                     `<td>${m.disk_max_pct ?? '-'}</td><td>${m.temp_max_c ?? '-'}</td>` +
                     `<td>${m.updates_pending ?? '-'}</td><td class="small">${age}</td>` +
                     `<td>${actions}</td>`;
      tbody.appendChild(tr);
    });
  } catch(e) {
    const tbody = document.querySelector('#hosts tbody');
    tbody.innerHTML = '<tr><td colspan=9 class="ko">Collector unreachable: '+e+'</td></tr>';
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
  log.textContent = 'Running '+action+'...\n';
  try {
    const r = await fetch('api/action/'+action, { method: 'POST' });
    log.textContent = await r.text();
    refresh();
  } catch(e) { log.textContent = 'ERROR: '+e; }
}
async function power(action, host) {
  const log = document.getElementById('log');
  const verb = {wake:'Waking', reboot:'Rebooting', shutdown:'Shutting down'}[action] || action;
  if (action !== 'wake' && !confirm(`${verb} ${host}?`)) return;
  log.textContent = `${verb} ${host}...\n`;
  try {
    const r = await fetch(`api/power/${action}/${host}`, { method: 'POST' });
    log.textContent = await r.text();
    setTimeout(refresh, 3000);
  } catch(e) { log.textContent = `ERROR: ${e}`; }
}

async function onboard(ev) {
  ev.preventDefault();
  const status = document.getElementById('onboard-status');
  const log = document.getElementById('log');
  const body = {
    name: document.getElementById('o-name').value,
    addr: document.getElementById('o-addr').value,
    user: document.getElementById('o-user').value || 'root',
    port: parseInt(document.getElementById('o-port').value || '22'),
    category: document.getElementById('o-cat').value,
  };
  status.textContent = ' submitting...';
  try {
    const r = await fetch('api/onboard', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const txt = await r.text();
    log.textContent = txt;
    if (r.ok) {
      status.textContent = ' ✓ added (paste pubkey into host then click Install agent)';
      document.getElementById('onboard-form').reset();
      refresh();
    } else {
      status.textContent = ' ✗ ' + txt;
    }
  } catch(e) {
    status.textContent = ' ERROR: ' + e;
  }
}
refresh();
setInterval(refresh, 10000);
</script>
</body></html>
"""


def proxy_collector(path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(f"{COLLECTOR_URL}{path}", timeout=5) as r:
            return r.status, r.read(), r.headers.get_content_type() or "application/json"
    except Exception as e:
        return 502, json.dumps({"error": str(e)}).encode(), "application/json"


def run_playbook(name: str, limit: str | None = None, extra_vars: dict | None = None) -> str:
    pb = PLAYBOOKS.get(name)
    if not pb:
        return f"unknown action: {name}"
    env = os.environ.copy()
    env["ANSIBLE_CONFIG"] = str(CFG_FILE)
    cmd = ["ansible-playbook", pb, "-i", str(INV_FILE)]
    if limit:
        cmd.extend(["--limit", limit])
    if extra_vars:
        cmd.extend(["-e", json.dumps(extra_vars)])
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
        return f"=== {name}{(' --limit '+limit) if limit else ''} (rc={p.returncode}) ===\n{p.stdout[-4000:]}\n--- stderr ---\n{p.stderr[-1000:]}"
    except subprocess.TimeoutExpired:
        return f"=== {name} TIMEOUT after 300s ==="


def send_wol(mac: str) -> tuple[bool, str]:
    """Send a Wake-on-LAN magic packet for the given MAC address."""
    import socket
    try:
        mac_clean = mac.replace(":", "").replace("-", "").strip()
        if len(mac_clean) != 12:
            return False, f"invalid MAC: {mac}"
        mac_bytes = bytes.fromhex(mac_clean)
        magic = b"\xff" * 6 + mac_bytes * 16
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(magic, ("255.255.255.255", 9))
        sock.close()
        return True, f"WoL magic packet sent to {mac}"
    except Exception as e:
        return False, f"WoL failed: {e}"


def power_action(host: str, action: str) -> tuple[int, str]:
    """Wake / shutdown / reboot a configured host."""
    try:
        opts = json.loads(OPTS_FILE.read_text())
    except Exception as e:
        return 500, f"options.json: {e}"

    host_entry = next((h for h in opts.get("hosts", []) if h["name"] == host), None)
    if not host_entry:
        return 404, f"host '{host}' not in inventory"

    if action == "wake":
        mac = host_entry.get("mac")
        if not mac:
            return 400, f"host '{host}' has no 'mac' field in options.json"
        ok, msg = send_wol(mac)
        return (200 if ok else 500), msg

    if action in ("shutdown", "reboot"):
        result = run_playbook("power", limit=host, extra_vars={"action": action})
        return 200, result

    return 400, f"unknown action '{action}' (use wake|shutdown|reboot)"


def rerun_bootstrap() -> str:
    """Regenerates inventory.yml from options.json."""
    try:
        # bashio reads from /data/options.json at runtime - bootstrap script reads it.
        p = subprocess.run(
            ["/usr/bin/with-contenv", "bashio", BOOTSTRAP_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
        return f"rc={p.returncode}\nstdout:\n{p.stdout[-1500:]}\nstderr:\n{p.stderr[-500:]}"
    except Exception as e:
        return f"bootstrap exception: {e}"


def onboard_host(payload: dict) -> tuple[int, str]:
    """Adds a new host to /data/options.json, regenerates inventory."""
    # Validate
    required = ("name", "addr")
    for k in required:
        if not payload.get(k):
            return 400, f"missing field: {k}"
    name = payload["name"]
    if not name.replace("_", "").isalnum():
        return 400, "name must be [a-z0-9_]+"
    addr = payload["addr"]
    user = payload.get("user", "root")
    port = int(payload.get("port", 22))
    category = payload.get("category", "server")
    if category not in CATEGORIES:
        return 400, f"unknown category {category}; valid: {','.join(CATEGORIES)}"

    # Read options.json
    try:
        opts = json.loads(OPTS_FILE.read_text())
    except Exception as e:
        return 500, f"can't read options.json: {e}"

    # Dedupe
    if any(h["name"] == name for h in opts.get("hosts", [])):
        return 409, f"host '{name}' already exists"

    new_host = {"name": name, "addr": addr, "user": user, "port": port, "category": category}
    opts.setdefault("hosts", []).append(new_host)

    # Write back
    try:
        OPTS_FILE.write_text(json.dumps(opts, indent=2))
    except Exception as e:
        return 500, f"can't write options.json: {e}"

    # Regenerate inventory
    boot_log = rerun_bootstrap()

    return 200, (
        f"✓ Host '{name}' ({addr}) added to options.json under category '{category}'.\n"
        f"\n--- bootstrap re-run ---\n{boot_log}\n"
        f"\nNEXT STEPS:\n"
        f"  1) Copy the SSH pubkey above into {user}@{addr}:~/.ssh/authorized_keys\n"
        f"     ssh-copy-id -i /data/.ssh/id_ed25519.pub {user}@{addr}    (if you can SSH manually)\n"
        f"  2) Click 'Install agent on all' to deploy the metrics agent.\n"
    )


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
        p = self.path
        if "/api/" not in p:
            self._send(200, INDEX_HTML)
            return
        idx = p.rfind("/api/")
        sub = p[idx + 1:]
        api_path = "/" + sub
        code, body, ctype = proxy_collector(api_path)
        self._send(code, body, ctype)

    def do_POST(self):
        p = self.path
        # /api/action/<name>
        idx_action = p.rfind("/api/action/")
        if idx_action != -1:
            action = p[idx_action + len("/api/action/"):].strip("/")
            self._send(200, run_playbook(action), "text/plain; charset=utf-8")
            return
        # /api/onboard
        idx_onboard = p.rfind("/api/onboard")
        if idx_onboard != -1:
            length = int(self.headers.get("Content-Length", "0"))
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception as e:
                self._send(400, f"bad json: {e}", "text/plain")
                return
            code, msg = onboard_host(body)
            self._send(code, msg, "text/plain; charset=utf-8")
            return
        # /api/power/<action>/<host>
        idx_power = p.rfind("/api/power/")
        if idx_power != -1:
            parts = p[idx_power + len("/api/power/"):].strip("/").split("/")
            if len(parts) != 2:
                self._send(400, "usage: POST /api/power/<wake|shutdown|reboot>/<host>", "text/plain")
                return
            code, msg = power_action(parts[1], parts[0])
            self._send(code, msg, "text/plain; charset=utf-8")
            return
        self._send(404, "not found", "text/plain")


class ThreadingServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def main():
    server = ThreadingServer(("0.0.0.0", PORT), Handler)
    print(f"[god-webui] listening on :{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
