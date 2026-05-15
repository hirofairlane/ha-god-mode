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
import sys
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
    "detect_os":     "/usr/share/god-mode/ansible/playbooks/detect_os.yml",
}
BOOTSTRAP_SCRIPT = "/usr/bin/god-bootstrap.sh"
PVE_SYNC_SCRIPT = "/usr/bin/god-pve-sync.py"
CATEGORIES = [
    "pve_node", "pve_vm", "pve_lxc",
    "server", "rpi",
    "desktop_linux", "desktop_win", "desktop_mac",
    "openwrt",
]
# chip -> (label, mdi icon). Used in the host table for visual cue.
CHIP_ICONS = {
    "rpi4":      ("RPi 4",      "mdi:raspberry-pi"),
    "rpi5":      ("RPi 5",      "mdi:raspberry-pi"),
    "rpi3":      ("RPi 3",      "mdi:raspberry-pi"),
    "rpi_zero":  ("RPi Zero",   "mdi:raspberry-pi"),
    "orange_pi": ("Orange Pi",  "mdi:square-rounded"),
    "nuc":       ("Intel NUC",  "mdi:cpu-64-bit"),
    "pikvm":     ("PiKVM",      "mdi:monitor-shimmer"),
    "rockpi":    ("Rock Pi",    "mdi:chip"),
}

PORT = 8099


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<title>GOD Mode</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#0e1116; --panel:#161b22; --panel2:#0d1117; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --accent:#f0883e; --link:#79c0ff;
    --ok:#3fb950; --warn:#d29922; --ko:#f85149;
    --cat-pve_node:#a371f7; --cat-pve_vm:#bc8cff; --cat-pve_lxc:#bc8cff;
    --cat-server:#79c0ff; --cat-rpi:#f2cc60;
    --cat-desktop_linux:#3fb950; --cat-desktop_win:#58a6ff; --cat-desktop_mac:#ff7b72;
    --cat-openwrt:#d29922;
  }
  *,*::before,*::after { box-sizing:border-box; }
  html,body { margin:0; padding:0; background:var(--bg); color:var(--text);
              font-family:ui-monospace,SFMono-Regular,Menlo,monospace; }
  a { color:var(--link); text-decoration:none; }
  nav.top {
    display:flex; align-items:center; gap:0.6rem; flex-wrap:wrap;
    background:var(--panel); border-bottom:1px solid var(--border);
    padding:0.6rem 1rem; position:sticky; top:0; z-index:10;
  }
  nav.top h1 { margin:0; color:var(--accent); font-size:1.1rem; }
  nav.top .tabs { display:flex; gap:0.2rem; flex-wrap:wrap; flex:1; }
  nav.top .tabs a {
    padding:0.4rem 0.7rem; border-radius:5px;
    color:var(--text); border:1px solid transparent;
  }
  nav.top .tabs a:hover { background:var(--panel2); }
  nav.top .tabs a.active {
    background:var(--panel2); border-color:var(--border); color:var(--accent);
  }
  nav.top .meta { color:var(--muted); font-size:0.85rem; }
  main { padding:1rem; max-width:1600px; margin:0 auto; }
  .view { display:none; }
  .view.active { display:block; }
  .panel { background:var(--panel); border:1px solid var(--border);
           border-radius:6px; padding:1rem; margin-bottom:1rem; }
  .panel h2 { margin:0 0 0.6rem 0; color:var(--link); font-size:1rem;
              border-bottom:1px solid var(--border); padding-bottom:0.3rem; }
  pre { background:var(--panel2); padding:0.6rem; border-radius:4px;
        overflow-x:auto; white-space:pre-wrap; word-break:break-all;
        border:1px solid var(--border); font-size:0.85rem; }
  button {
    background:#238636; color:white; border:none; padding:0.4rem 0.8rem;
    border-radius:5px; cursor:pointer; font-family:inherit; font-size:0.85rem;
    margin-right:0.3rem; margin-bottom:0.3rem;
  }
  button:hover { background:#2ea043; }
  button.secondary { background:#21262d; border:1px solid var(--border); }
  button.secondary:hover { background:#30363d; }
  button.danger { background:#da3633; }
  button.danger:hover { background:#f85149; }
  button.tiny { padding:0.15rem 0.4rem; font-size:0.75rem; margin:0 0.1rem; }
  input, select {
    background:var(--panel2); color:var(--text); border:1px solid var(--border);
    padding:0.35rem; border-radius:4px; font-family:inherit; font-size:0.85rem;
    width:100%; box-sizing:border-box;
  }
  label { display:block; margin-bottom:0.2rem; font-size:0.8rem; color:var(--muted); }
  table { width:100%; border-collapse:collapse; font-size:0.85rem; }
  th, td { padding:0.35rem 0.5rem; text-align:left; border-bottom:1px solid var(--border); }
  th { background:var(--panel2); color:var(--muted); cursor:pointer;
       user-select:none; position:sticky; top:48px; }
  th.sortable:hover { color:var(--accent); }
  th.sorted-asc::after { content:" ↑"; color:var(--accent); }
  th.sorted-desc::after { content:" ↓"; color:var(--accent); }
  tr.clickable { cursor:pointer; }
  tr.clickable:hover td { background:var(--panel2); }
  .ok { color:var(--ok); } .ko { color:var(--ko); } .warn { color:var(--warn); }
  .muted { color:var(--muted); font-size:0.85rem; }
  .row { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px,1fr)); gap:0.6rem; }
  .stat { background:var(--panel2); border:1px solid var(--border); border-radius:5px;
          padding:0.7rem; }
  .stat .label { font-size:0.75rem; color:var(--muted); text-transform:uppercase; }
  .stat .value { font-size:1.6rem; color:var(--text); font-weight:bold; margin-top:0.2rem; }
  .stat .value.ok { color:var(--ok); } .stat .value.ko { color:var(--ko); }
  .stat .value.warn { color:var(--warn); }
  .cat-pve_node{color:var(--cat-pve_node);} .cat-pve_vm{color:var(--cat-pve_vm);}
  .cat-pve_lxc{color:var(--cat-pve_lxc);} .cat-server{color:var(--cat-server);}
  .cat-rpi{color:var(--cat-rpi);} .cat-desktop_linux{color:var(--cat-desktop_linux);}
  .cat-desktop_win{color:var(--cat-desktop_win);} .cat-desktop_mac{color:var(--cat-desktop_mac);}
  .cat-openwrt{color:var(--cat-openwrt);}
  .child-running { color:var(--ok); } .child-stopped { color:var(--muted); }
  .pve-card { background:var(--panel2); border:1px solid var(--border);
              border-radius:5px; padding:0.7rem; margin-bottom:0.8rem; }
  .pve-card h3 { margin:0; color:var(--cat-pve_node); font-size:0.95rem; }
  .pve-card .meta { color:var(--muted); font-size:0.8rem; margin:0.3rem 0 0.5rem 0; }
  .bar { background:var(--panel2); height:4px; border-radius:2px; overflow:hidden; }
  .bar > div { height:100%; background:var(--ok); }
  .bar > div.warn { background:var(--warn); } .bar > div.ko { background:var(--ko); }
  .filter-bar { display:flex; gap:0.5rem; flex-wrap:wrap; margin-bottom:0.6rem; align-items:center; }
  .filter-bar input { width:200px; }
  .filter-bar select { width:auto; }
  .spark { display:block; }
  .gauge-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr)); gap:0.6rem; }
  .gauge { background:var(--panel2); border:1px solid var(--border); border-radius:5px; padding:0.7rem; }
  .gauge .label { color:var(--muted); font-size:0.75rem; text-transform:uppercase; }
  .gauge .value { font-size:1.6rem; font-weight:bold; }
  #log { max-height:400px; overflow:auto; font-size:0.8rem; }
  .pill { display:inline-block; padding:0.05rem 0.4rem; border-radius:10px;
          font-size:0.7rem; background:var(--panel2); border:1px solid var(--border); }
</style>
</head><body>

<nav class="top">
  <h1>👁 GOD Mode</h1>
  <div class="tabs">
    <a href="#overview" data-view="overview">Overview</a>
    <a href="#hosts" data-view="hosts">Hosts</a>
    <a href="#proxmox" data-view="proxmox">Proxmox</a>
    <a href="#rpi" data-view="rpi">RPi</a>
    <a href="#openwrt" data-view="openwrt">OpenWrt</a>
    <a href="#onboard" data-view="onboard">Onboard</a>
    <a href="#config" data-view="config">Config</a>
  </div>
  <span class="meta">v0.4.0 · <span id="last-poll">…</span></span>
</nav>

<main>

<!-- =================== OVERVIEW =================== -->
<section id="view-overview" class="view active">
  <div class="panel">
    <h2>Resumen</h2>
    <div class="row" id="ov-stats">(cargando…)</div>
  </div>
  <div class="panel">
    <h2>Hosts con problemas</h2>
    <div id="ov-problems">(cargando…)</div>
  </div>
</section>

<!-- =================== HOSTS =================== -->
<section id="view-hosts" class="view">
  <div class="panel">
    <h2>Hosts (<span id="hosts-count">0</span>)</h2>
    <div class="filter-bar">
      <input id="hosts-filter" placeholder="filtrar por nombre o IP…" oninput="renderHosts()">
      <select id="hosts-cat-filter" onchange="renderHosts()">
        <option value="">todas las categorías</option>
      </select>
      <button class="secondary" onclick="actionPlaybook('gather')">🔬 Gather now</button>
      <button class="secondary" onclick="actionPlaybook('ping')">📡 Ping all</button>
    </div>
    <table id="hosts-table">
      <thead><tr>
        <th class="sortable" data-key="name">host</th>
        <th class="sortable" data-key="_category">cat</th>
        <th class="sortable" data-key="_ok">status</th>
        <th class="sortable" data-key="cpu_pct">cpu</th>
        <th class="sortable" data-key="mem_pct">mem</th>
        <th class="sortable" data-key="disk_max_pct">disk</th>
        <th class="sortable" data-key="temp_max_c">temp</th>
        <th class="sortable" data-key="updates_pending">upd</th>
        <th class="sortable" data-key="_polled_at">last</th>
        <th>actions</th>
      </tr></thead>
      <tbody></tbody>
    </table>
  </div>
</section>

<!-- =================== PROXMOX =================== -->
<section id="view-proxmox" class="view">
  <div class="panel">
    <h2>Proxmox nodes</h2>
    <p class="muted">VMs/LXCs descubiertos vía API por cada <code>pve_node</code> con token configurado. Incluye guests apagados.</p>
    <div class="filter-bar">
      <button class="secondary" onclick="pveSyncNow()">🔄 Sync now</button>
      <span id="pve-status" class="muted"></span>
    </div>
    <div id="pve-container">(cargando…)</div>
  </div>
</section>

<!-- =================== RPi =================== -->
<section id="view-rpi" class="view">
  <div class="panel">
    <h2>RPi & SBC</h2>
    <p class="muted">Equipos pequeños: RPi, NUCs, Orange Pi, PiKVM…</p>
    <div id="rpi-container">(cargando…)</div>
  </div>
</section>

<!-- =================== OpenWrt =================== -->
<section id="view-openwrt" class="view">
  <div class="panel">
    <h2>OpenWrt routers</h2>
    <div id="openwrt-container">(cargando…)</div>
  </div>
</section>

<!-- =================== HOST DETAIL =================== -->
<section id="view-host" class="view">
  <div class="panel">
    <h2 id="host-title">Host detail</h2>
    <div id="host-meta" class="muted"></div>
  </div>
  <div class="panel">
    <h2>Métricas actuales</h2>
    <div class="gauge-grid" id="host-gauges">(cargando…)</div>
  </div>
  <div class="panel">
    <h2>Histórico (últimas 6h, in-memory)</h2>
    <div id="host-charts">(cargando…)</div>
    <p class="muted">Para histórico largo, consulta Grafana/InfluxDB.</p>
  </div>
  <div class="panel">
    <h2>Acciones</h2>
    <div id="host-actions"></div>
  </div>
</section>

<!-- =================== ONBOARD =================== -->
<section id="view-onboard" class="view">
  <div class="panel">
    <h2>Onboard new host</h2>
    <p class="muted">Añade un host. Si das password, el addon hace ssh-copy-id (transient), detect_os, install_agent y gather. Si no, asume que ya pegaste la pubkey manualmente.</p>
    <form id="onboard-form" onsubmit="onboardSubmit(event)">
      <div class="row">
        <div><label>Name (a-z0-9_)</label><input id="o-name" required pattern="^[a-z0-9_]+$" placeholder="myhost"></div>
        <div><label>Address (IP/hostname)</label><input id="o-addr" required placeholder="192.168.1.50"></div>
        <div><label>SSH user</label><input id="o-user" value="root"></div>
        <div><label>SSH port</label><input id="o-port" type="number" value="22"></div>
        <div><label>Category</label><select id="o-cat">
          <option value="pve_node">pve_node</option>
          <option value="server" selected>server</option>
          <option value="rpi">rpi</option>
          <option value="desktop_linux">desktop_linux</option>
          <option value="desktop_win">desktop_win</option>
          <option value="desktop_mac">desktop_mac</option>
          <option value="openwrt">openwrt</option>
        </select></div>
        <div><label>Chip (opcional)</label><input id="o-chip" placeholder="rpi4, rpi5, nuc, orange_pi, pikvm…"></div>
        <div><label>Parent pve_node (opcional)</label><input id="o-parent" placeholder="zeratul"></div>
        <div><label>Password (one-time, no persiste)</label><input id="o-pass" type="password" placeholder="vacío si pubkey ya está"></div>
      </div>
      <button type="submit">➕ Add host</button>
      <span id="onboard-status" class="muted"></span>
    </form>
  </div>
  <div class="panel">
    <h2>Action log</h2>
    <pre id="log">(sin acciones aún)</pre>
  </div>
</section>

<!-- =================== CONFIG =================== -->
<section id="view-config" class="view">
  <div class="panel">
    <h2>SSH public key</h2>
    <p class="muted">Pega esta clave en <code>~/.ssh/authorized_keys</code> de cada host a monitorear. (Persistida en <code>/homeassistant/god_mode/.ssh/</code>: sobrevive a reinstalación del addon.)</p>
    <pre id="pubkey">cargando…</pre>
    <button onclick="copyPubkey()" title="Copia la clave pública al portapapeles">📋 Copy key</button>
    <button class="secondary" onclick="toggleInstallCmd()" title="Muestra un comando ssh listo para pegar en tu terminal: pide usuario+host y el wrapper instalará la pubkey en authorized_keys del destino">🛠 Comando para enrolar</button>
    <span id="copy-status" class="muted"></span>
    <div id="install-cmd-box" style="display:none; margin-top:0.6rem;">
      <div class="row" style="margin-bottom:0.4rem;">
        <div><label>SSH user</label><input id="ic-user" value="root"></div>
        <div><label>Host / IP</label><input id="ic-host" placeholder="192.168.1.50"></div>
        <div><label>SSH port (opcional)</label><input id="ic-port" type="number" value="22"></div>
      </div>
      <p class="muted">Pega esto en tu terminal local; te pedirá la password del host una sola vez:</p>
      <pre id="install-cmd" style="white-space:pre-wrap;">_</pre>
      <button onclick="copyInstallCmd()" title="Copia el comando al portapapeles">📋 Copy command</button>
      <span id="ic-status" class="muted"></span>
    </div>
  </div>
  <div class="panel">
    <h2>Acciones globales</h2>
    <p class="muted">Operaciones masivas sobre TODOS los hosts del inventario. Pueden tardar varios minutos. El resultado se muestra abajo.</p>
    <button class="secondary" onclick="actionPlaybook('install_agent', null, 'global-out')"
            title="Despliega o reinstala el agente god-agent en TODOS los hosts. Necesita que la pubkey ya esté en authorized_keys de cada uno. Equivalente a: ansible-playbook install_agent.yml">📦 Install agent on ALL</button>
    <button class="secondary" onclick="actionPlaybook('detect_os', null, 'global-out')"
            title="Detecta el SO + arquitectura de cada host (cachea en /data/metadata/) para elegir agente correcto. Necesita SSH funcionando.">🔎 Detect OS on ALL</button>
    <button class="secondary" onclick="actionPlaybook('gather', null, 'global-out')"
            title="Fuerza un poll inmediato de métricas de todos los hosts. Normalmente el collector lo hace cada poll_interval (default 60s).">🔬 Gather now</button>
    <button class="secondary" onclick="actionPlaybook('ping', null, 'global-out')"
            title="Ansible ping a todos los hosts. Diagnóstico rápido de conectividad SSH.">📡 Ping all</button>
    <button class="secondary" onclick="pveSyncNow('global-out')"
            title="Lanza ya un god-pve-sync que consulta la API de cada pve_node con token configurado y refresca la lista de VMs/LXCs. Normalmente corre cada pve_sync_interval (default 5 min).">🔄 PVE sync now</button>
    <pre id="global-out" style="margin-top:0.6rem; max-height:400px; overflow:auto;">(sin acción ejecutada)</pre>
  </div>
  <div class="panel">
    <h2>Inventory (Ansible)</h2>
    <pre id="inventory-view" style="max-height:400px;">cargando…</pre>
  </div>
</section>

</main>

<script>
// ============================================================
// State
// ============================================================
const state = {
  hosts: {},
  pve: {},
  view: 'overview',
  selectedHost: null,
  hostsSort: { key: '_category', dir: 'asc' },
  lastPoll: 0,
};

// ============================================================
// Helpers
// ============================================================
function $(id) { return document.getElementById(id); }
function fmtBytes(b) {
  if (b == null || b === 0) return '-';
  const u = ['B','K','M','G','T','P']; let i=0, v=b;
  while (v>=1024 && i<u.length-1) { v/=1024; i++; }
  return v.toFixed(v<10?1:0) + u[i];
}
function fmtUptime(s) {
  if (!s) return '-';
  const d = Math.floor(s/86400), h = Math.floor((s%86400)/3600), m = Math.floor((s%3600)/60);
  if (d>0) return `${d}d${h}h`;
  if (h>0) return `${h}h${m}m`;
  return `${m}m`;
}
function fmtAge(ts) {
  if (!ts) return '-';
  const age = Math.floor(Date.now()/1000) - ts;
  if (age < 60) return age+'s';
  if (age < 3600) return Math.floor(age/60)+'m';
  return Math.floor(age/3600)+'h';
}
function statusCell(m) {
  if (m._ok) return '<span class="ok">OK</span>';
  if (m._error === 'not_polled_yet') return '<span class="warn">pending</span>';
  return `<span class="ko" title="${(m._error||'').replace(/"/g,'&quot;')}">FAIL</span>`;
}
function valueClass(v, warn, crit) {
  if (v == null) return '';
  v = parseFloat(v);
  if (v >= crit) return 'ko';
  if (v >= warn) return 'warn';
  return 'ok';
}
function escapeHtml(s) {
  return String(s).replace(/[<>&"]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;'}[c]));
}

// SVG sparkline. points: [[ts, val], ...]. Auto-scales.
function sparkline(points, w, h, color) {
  if (!points || points.length < 2) {
    return `<svg class="spark" width="${w}" height="${h}"></svg>`;
  }
  const xs = points.map(p => p[0]);
  const ys = points.map(p => p[1]);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xR = xMax - xMin || 1;
  const yR = (yMax - yMin) || 1;
  const pad = 2;
  const path = points.map((p, i) => {
    const x = pad + ((p[0]-xMin)/xR) * (w - 2*pad);
    const y = h - pad - ((p[1]-yMin)/yR) * (h - 2*pad);
    return (i===0?'M':'L') + x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
  const lastY = ys[ys.length-1];
  return `<svg class="spark" width="${w}" height="${h}">
    <path d="${path}" stroke="${color}" stroke-width="1.5" fill="none"/>
    <text x="${w-2}" y="${h-2}" text-anchor="end" font-size="9" fill="${color}">${lastY.toFixed(0)}</text>
  </svg>`;
}

// ============================================================
// Routing
// ============================================================
function route() {
  const hash = location.hash.replace(/^#/, '') || 'overview';
  const parts = hash.split('/');
  const view = parts[0];
  state.view = view;
  state.selectedHost = (view === 'host') ? parts[1] : null;

  document.querySelectorAll('.view').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('nav.top .tabs a').forEach(a => a.classList.remove('active'));
  const target = $('view-' + view);
  if (target) target.classList.add('active');
  const link = document.querySelector(`nav.top .tabs a[data-view="${view}"]`);
  if (link) link.classList.add('active');

  // Trigger view-specific render
  if (view === 'overview') renderOverview();
  else if (view === 'hosts') renderHosts();
  else if (view === 'proxmox') renderProxmox();
  else if (view === 'rpi') renderRpi();
  else if (view === 'openwrt') renderOpenwrt();
  else if (view === 'host' && state.selectedHost) renderHostDetail(state.selectedHost);
  else if (view === 'config') renderConfig();
}
window.addEventListener('hashchange', route);

// ============================================================
// Data fetching
// ============================================================
async function pollAll() {
  try {
    const [hosts, pve] = await Promise.all([
      fetch('api/hosts').then(r => r.json()),
      fetch('api/pve_children').then(r => r.json()),
    ]);
    state.hosts = hosts;
    state.pve = pve;
    state.lastPoll = Date.now();
    $('last-poll').textContent = new Date(state.lastPoll).toLocaleTimeString();
    // Re-render only the active view
    route();
  } catch(e) {
    $('last-poll').innerHTML = `<span class="ko">collector unreachable</span>`;
  }
}

// ============================================================
// Renderers
// ============================================================
function renderOverview() {
  const hs = Object.entries(state.hosts);
  const total = hs.length;
  const online = hs.filter(([n,m]) => m._ok).length;
  const pending = hs.filter(([n,m]) => m._error === 'not_polled_yet').length;
  const offline = total - online - pending;
  let warn=0, crit=0;
  hs.forEach(([n,m]) => {
    if (!m._ok) return;
    const cpu=+m.cpu_pct||0, mem=+m.mem_pct||0, dk=+m.disk_max_pct||0, tp=+m.temp_max_c||0;
    if (cpu>95||mem>95||dk>95||tp>85) crit++;
    else if (cpu>80||mem>85||dk>85||tp>75) warn++;
  });
  const pveTotal = Object.values(state.pve).reduce((a,p) => a + ((p.counts||{}).total||0), 0);
  const pveRun   = Object.values(state.pve).reduce((a,p) => a + ((p.counts||{}).running||0), 0);
  $('ov-stats').innerHTML = `
    <div class="stat"><div class="label">Hosts online</div><div class="value ${online===0?'ko':'ok'}">${online}/${total}</div></div>
    <div class="stat"><div class="label">Offline</div><div class="value ${offline>0?'ko':''}">${offline}</div></div>
    <div class="stat"><div class="label">Pending poll</div><div class="value ${pending>0?'warn':''}">${pending}</div></div>
    <div class="stat"><div class="label">Warnings</div><div class="value ${warn>0?'warn':''}">${warn}</div></div>
    <div class="stat"><div class="label">Críticos</div><div class="value ${crit>0?'ko':''}">${crit}</div></div>
    <div class="stat"><div class="label">PVE guests run</div><div class="value">${pveRun}/${pveTotal}</div></div>
  `;
  // Problemas (incluye offline + pending si total)
  const probs = hs.filter(([n,m]) => {
    if (!m._ok) return true;   // anything not OK is a problem (offline, pending, parse error, ...)
    const cpu=+m.cpu_pct||0, mem=+m.mem_pct||0, dk=+m.disk_max_pct||0, tp=+m.temp_max_c||0;
    return cpu>80||mem>85||dk>85||tp>75;
  });
  if (probs.length === 0) {
    $('ov-problems').innerHTML = '<p class="muted">✓ Todos los hosts OK, sin alertas.</p>';
  } else {
    let html = '<table><thead><tr><th>host</th><th>categoría</th><th>problema</th></tr></thead><tbody>';
    probs.forEach(([n,m]) => {
      let issues = [];
      if (!m._ok) issues.push(m._error || 'fail');
      else {
        if (+m.cpu_pct>=95) issues.push(`CPU ${m.cpu_pct}%`);
        else if (+m.cpu_pct>=80) issues.push(`CPU ${m.cpu_pct}%`);
        if (+m.mem_pct>=95) issues.push(`MEM ${m.mem_pct}%`);
        else if (+m.mem_pct>=85) issues.push(`MEM ${m.mem_pct}%`);
        if (+m.disk_max_pct>=95) issues.push(`DISK ${m.disk_max_pct}%`);
        else if (+m.disk_max_pct>=85) issues.push(`DISK ${m.disk_max_pct}%`);
        if (+m.temp_max_c>=85) issues.push(`TEMP ${m.temp_max_c}°C`);
        else if (+m.temp_max_c>=75) issues.push(`TEMP ${m.temp_max_c}°C`);
      }
      html += `<tr class="clickable" onclick="location.hash='host/${n}'">
        <td><b>${escapeHtml(n)}</b></td>
        <td class="cat-${m._category||'server'}">${m._category||'?'}</td>
        <td>${issues.join(' · ')}</td>
      </tr>`;
    });
    html += '</tbody></table>';
    $('ov-problems').innerHTML = html;
  }
}

function hostMatchesFilter(name, m) {
  const f = ($('hosts-filter').value || '').toLowerCase().trim();
  const cat = $('hosts-cat-filter').value;
  if (cat && (m._category || '') !== cat) return false;
  if (!f) return true;
  return name.toLowerCase().includes(f) ||
         String(m.addr || '').toLowerCase().includes(f);
}
function buildCatFilterOnce() {
  const sel = $('hosts-cat-filter');
  if (sel.options.length > 1) return;
  const cats = new Set(Object.values(state.hosts).map(m => m._category).filter(Boolean));
  Array.from(cats).sort().forEach(c => {
    const o = document.createElement('option');
    o.value = c; o.textContent = c; sel.appendChild(o);
  });
}
function renderHosts() {
  buildCatFilterOnce();
  let rows = Object.entries(state.hosts).filter(([n,m]) => hostMatchesFilter(n,m));
  // Sort
  const k = state.hostsSort.key, dir = state.hostsSort.dir === 'asc' ? 1 : -1;
  rows.sort((a,b) => {
    let va = (k==='name') ? a[0] : (a[1][k] ?? '');
    let vb = (k==='name') ? b[0] : (b[1][k] ?? '');
    if (typeof va === 'number' || typeof vb === 'number') {
      return (parseFloat(va)||0 - parseFloat(vb)||0) * dir;
    }
    return String(va).localeCompare(String(vb)) * dir;
  });
  $('hosts-count').textContent = rows.length;
  let html = '';
  rows.forEach(([n,m]) => {
    const cat = m._category || '?';
    html += `<tr class="clickable" onclick="if(event.target.tagName!=='BUTTON')location.hash='host/${n}'">
      <td><b>${escapeHtml(n)}</b><div class="muted" style="font-size:0.7rem">${escapeHtml(m.addr||'')}</div></td>
      <td class="cat-${cat}">${cat}</td>
      <td>${statusCell(m)}</td>
      <td class="${valueClass(m.cpu_pct,80,95)}">${m.cpu_pct ?? '-'}</td>
      <td class="${valueClass(m.mem_pct,85,95)}">${m.mem_pct ?? '-'}</td>
      <td class="${valueClass(m.disk_max_pct,85,95)}">${m.disk_max_pct ?? '-'}</td>
      <td class="${valueClass(m.temp_max_c,75,85)}">${m.temp_max_c ?? '-'}</td>
      <td>${m.updates_pending ?? '-'}</td>
      <td class="muted">${fmtAge(m._polled_at)}</td>
      <td>
        <button class="tiny secondary" onclick="event.stopPropagation();powerAction('wake','${n}')" title="WoL">⏻</button>
        <button class="tiny secondary" onclick="event.stopPropagation();powerAction('reboot','${n}')" title="reboot">↻</button>
        <button class="tiny danger" onclick="event.stopPropagation();powerAction('shutdown','${n}')" title="shutdown">⏼</button>
      </td>
    </tr>`;
  });
  $('hosts-table').querySelector('tbody').innerHTML = html;
  // Update sort indicators
  document.querySelectorAll('#hosts-table th.sortable').forEach(th => {
    th.classList.remove('sorted-asc','sorted-desc');
    if (th.dataset.key === k) th.classList.add(state.hostsSort.dir === 'asc' ? 'sorted-asc' : 'sorted-desc');
  });
}
function attachSortHandlers() {
  document.querySelectorAll('#hosts-table th.sortable').forEach(th => {
    th.onclick = () => {
      const k = th.dataset.key;
      if (state.hostsSort.key === k) {
        state.hostsSort.dir = state.hostsSort.dir === 'asc' ? 'desc' : 'asc';
      } else {
        state.hostsSort = { key: k, dir: 'asc' };
      }
      renderHosts();
    };
  });
}

function renderProxmox() {
  const nodes = Object.keys(state.pve).sort();
  if (nodes.length === 0) {
    $('pve-container').innerHTML = '<p class="muted">No hay pve_node con token configurado. Pon <code>pve_token_id</code> + <code>pve_token_secret</code> en las opciones del addon, por cada nodo.</p>';
    return;
  }
  let html = '';
  nodes.forEach(n => {
    const s = state.pve[n];
    if (!s.ok) {
      html += `<div class="pve-card"><h3>${n}</h3><div class="ko muted">error: ${escapeHtml(s.error||'?')}</div></div>`;
      return;
    }
    const ns = s.node_status || {}, c = s.counts || {};
    html += `<div class="pve-card">
      <h3>${n} <span class="muted">(${escapeHtml(s.addr)})</span></h3>
      <div class="meta">
        estado <b>${ns.status||'?'}</b> · CPU ${ns.cpu_pct??'-'}% · RAM ${ns.mem_pct??'-'}% (${fmtBytes(ns.mem_used_b)}/${fmtBytes(ns.mem_max_b)}) ·
        disco ${ns.disk_pct??'-'}% (${fmtBytes(ns.disk_used_b)}/${fmtBytes(ns.disk_max_b)}) ·
        uptime ${fmtUptime(ns.uptime_s)} ·
        guests: ${c.running||0}/${c.total||0} run (${c.vms||0} VM, ${c.lxcs||0} LXC)
      </div>
      <table>
        <thead><tr><th>vmid</th><th>name</th><th>type</th><th>status</th><th>cpu%</th><th>mem%</th><th>disk%</th><th>uptime</th><th>tags</th></tr></thead>
        <tbody>`;
    (s.guests || []).forEach(g => {
      const cls = g.status === 'running' ? 'child-running' : 'child-stopped';
      html += `<tr>
        <td>${g.vmid}</td>
        <td><b>${escapeHtml(g.name||'')}</b></td>
        <td>${g.type}</td>
        <td class="${cls}">${g.status}${g.template?' <span class="pill">tpl</span>':''}</td>
        <td class="${valueClass(g.cpu_pct,80,95)}">${g.cpu_pct ?? '-'}</td>
        <td class="${valueClass(g.mem_pct,85,95)}">${g.mem_pct ?? '-'}</td>
        <td class="${valueClass(g.disk_pct,85,95)}">${g.disk_pct ?? '-'}</td>
        <td class="muted">${fmtUptime(g.uptime_s)}</td>
        <td class="muted">${escapeHtml(g.tags||'')}</td>
      </tr>`;
    });
    html += '</tbody></table></div>';
  });
  $('pve-container').innerHTML = html;
}

function renderCategoryView(catName, containerId) {
  const rows = Object.entries(state.hosts).filter(([n,m]) => (m._category||'') === catName);
  if (rows.length === 0) {
    $(containerId).innerHTML = `<p class="muted">Ningún host con categoría <code>${catName}</code>.</p>`;
    return;
  }
  let html = `<table>
    <thead><tr><th>host</th><th>chip</th><th>status</th><th>cpu%</th><th>mem%</th><th>disk%</th><th>temp</th><th>uptime</th><th>last</th></tr></thead>
    <tbody>`;
  rows.forEach(([n,m]) => {
    html += `<tr class="clickable" onclick="location.hash='host/${n}'">
      <td><b>${escapeHtml(n)}</b><div class="muted" style="font-size:0.7rem">${escapeHtml(m.addr||'')}</div></td>
      <td class="muted">${escapeHtml(m._chip||'-')}</td>
      <td>${statusCell(m)}</td>
      <td class="${valueClass(m.cpu_pct,80,95)}">${m.cpu_pct ?? '-'}</td>
      <td class="${valueClass(m.mem_pct,85,95)}">${m.mem_pct ?? '-'}</td>
      <td class="${valueClass(m.disk_max_pct,85,95)}">${m.disk_max_pct ?? '-'}</td>
      <td class="${valueClass(m.temp_max_c,75,85)}">${m.temp_max_c ?? '-'}</td>
      <td>${fmtUptime(m.uptime_s)}</td>
      <td class="muted">${fmtAge(m._polled_at)}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  $(containerId).innerHTML = html;
}
function renderRpi() { renderCategoryView('rpi', 'rpi-container'); }
function renderOpenwrt() { renderCategoryView('openwrt', 'openwrt-container'); }

async function renderHostDetail(name) {
  const m = state.hosts[name];
  if (!m) {
    $('host-title').textContent = `Host '${name}' no encontrado`;
    $('host-meta').textContent = '';
    $('host-gauges').innerHTML = '';
    $('host-charts').innerHTML = '';
    $('host-actions').innerHTML = '';
    return;
  }
  $('host-title').innerHTML = `<span class="cat-${m._category||'server'}">${escapeHtml(name)}</span>`;
  let metaParts = [
    `IP: <b>${escapeHtml(m.addr || '?')}</b>`,
    `categoría: <span class="cat-${m._category||'?'}">${m._category||'?'}</span>`,
  ];
  if (m._chip) metaParts.push(`chip: <code>${escapeHtml(m._chip)}</code>`);
  if (m._parent) metaParts.push(`parent: <a href="#host/${m._parent}">${m._parent}</a>`);
  metaParts.push(`OS: ${escapeHtml(m._god_os || '?')}`);
  metaParts.push(`uptime: ${fmtUptime(m.uptime_s)}`);
  metaParts.push(`updates: ${m.updates_pending ?? '?'}`);
  metaParts.push(`SMART: ${escapeHtml(m.smart_health || '-')}`);
  metaParts.push(`status: ${statusCell(m)}`);
  $('host-meta').innerHTML = metaParts.join(' · ');

  // Gauges
  const gauges = [
    ['CPU', m.cpu_pct, '%', 80, 95],
    ['RAM', m.mem_pct, '%', 85, 95],
    ['Swap', m.swap_pct, '%', 50, 80],
    ['Disk', m.disk_max_pct, '%', 85, 95],
    ['Temp', m.temp_max_c, '°C', 75, 85],
    ['SMART temp', m.smart_temp_max, '°C', 55, 65],
    ['Load 1m', m.load_1m, '', 999, 9999],
    ['Updates', m.updates_pending, '', 999, 9999],
  ];
  $('host-gauges').innerHTML = gauges.map(([lbl,v,u,w,c]) => `
    <div class="gauge">
      <div class="label">${lbl}</div>
      <div class="value ${valueClass(v,w,c)}">${v ?? '-'}${v!=null?u:''}</div>
    </div>
  `).join('');

  // Charts: fetch history once
  try {
    const h = await (await fetch('api/history/' + encodeURIComponent(name))).json();
    const series = h.series || {};
    const W = 280, H = 60;
    const colors = { cpu_pct:'#79c0ff', mem_pct:'#3fb950', swap_pct:'#d29922',
                     disk_max_pct:'#a371f7', temp_max_c:'#ff7b72' };
    const labels = { cpu_pct:'CPU %', mem_pct:'RAM %', swap_pct:'Swap %',
                     disk_max_pct:'Disk %', temp_max_c:'Temp °C' };
    let html = '<div class="gauge-grid">';
    ['cpu_pct','mem_pct','swap_pct','disk_max_pct','temp_max_c'].forEach(k => {
      const pts = series[k] || [];
      html += `<div class="gauge">
        <div class="label">${labels[k]} <span class="muted">(${pts.length} pts)</span></div>
        ${sparkline(pts, W, H, colors[k])}
      </div>`;
    });
    html += '</div>';
    $('host-charts').innerHTML = html;
  } catch(e) {
    $('host-charts').innerHTML = `<p class="ko">history fetch failed: ${e}</p>`;
  }

  $('host-actions').innerHTML = `
    <button onclick="actionPlaybook('gather','${name}')">🔬 Gather now</button>
    <button class="secondary" onclick="actionPlaybook('install_agent','${name}')">📦 Reinstall agent</button>
    <button class="secondary" onclick="powerAction('wake','${name}')">⏻ WoL</button>
    <button class="secondary" onclick="powerAction('reboot','${name}')">↻ Reboot</button>
    <button class="danger" onclick="powerAction('shutdown','${name}')">⏼ Shutdown</button>
  `;
}

async function renderConfig() {
  try {
    const pub = await (await fetch('api/pubkey')).text();
    $('pubkey').textContent = pub.trim();
  } catch(e) { $('pubkey').textContent = 'ERROR: '+e; }
  try {
    const inv = await (await fetch('api/inventory')).text();
    $('inventory-view').textContent = inv;
  } catch(e) { $('inventory-view').textContent = 'ERROR: '+e; }
}

// ============================================================
// Actions
// ============================================================
function copyPubkey() {
  navigator.clipboard.writeText($('pubkey').textContent).then(() => {
    $('copy-status').textContent = '✓ copied';
    setTimeout(() => $('copy-status').textContent='', 2000);
  });
}

function buildInstallCmd() {
  const user = ($('ic-user').value || 'root').trim();
  const host = ($('ic-host').value || '<host>').trim();
  const portRaw = ($('ic-port').value || '22').trim();
  const port = parseInt(portRaw) || 22;
  const pub  = ($('pubkey').textContent || '').trim();
  if (!pub || pub === 'cargando…') return '# wait for pubkey to load';
  const sshOpts = (port !== 22) ? `-p ${port} ` : '';
  // Single-quoted remote payload: safe vs. local shell, escapes the
  // inner single quotes in the pubkey (the OpenSSH key never contains ')
  return `ssh ${sshOpts}${user}@${host} 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && grep -qxF "${pub}" ~/.ssh/authorized_keys || echo "${pub}" >> ~/.ssh/authorized_keys && echo OK_GOD_MODE_KEY_INSTALLED'`;
}
function refreshInstallCmd() { $('install-cmd').textContent = buildInstallCmd(); }
function toggleInstallCmd() {
  const box = $('install-cmd-box');
  const open = box.style.display === 'none';
  box.style.display = open ? 'block' : 'none';
  if (open) {
    refreshInstallCmd();
    ['ic-user','ic-host','ic-port'].forEach(id => {
      $(id).oninput = refreshInstallCmd;
    });
  }
}
function copyInstallCmd() {
  navigator.clipboard.writeText($('install-cmd').textContent).then(() => {
    $('ic-status').textContent = '✓ copied';
    setTimeout(() => $('ic-status').textContent='', 2000);
  });
}

async function actionPlaybook(action, host, outId) {
  // outId selects where to dump the output: defaults to onboarding's #log
  // but Config view passes 'global-out' so it shows where the buttons are.
  const out = $(outId || 'log');
  out.textContent = `Running ${action}${host?(' on '+host):''}…\n(esto puede tardar varios minutos para acciones masivas)`;
  try {
    const url = 'api/action/' + action + (host ? ('?limit=' + encodeURIComponent(host)) : '');
    const r = await fetch(url, { method: 'POST' });
    out.textContent = await r.text();
    pollAll();
  } catch(e) { out.textContent = 'ERROR: '+e; }
}
async function powerAction(action, host) {
  const out = $('log');
  const verb = {wake:'Waking', reboot:'Rebooting', shutdown:'Shutting down'}[action] || action;
  if (action !== 'wake' && !confirm(`${verb} ${host}?`)) return;
  out.textContent = `${verb} ${host}…\n`;
  try {
    const r = await fetch(`api/power/${action}/${host}`, { method: 'POST' });
    out.textContent = await r.text();
    setTimeout(pollAll, 3000);
  } catch(e) { out.textContent = `ERROR: ${e}`; }
}
async function pveSyncNow(outId) {
  const s = $('pve-status');
  if (s) s.textContent = ' syncing…';
  const out = outId ? $(outId) : null;
  if (out) out.textContent = 'Syncing Proxmox API…';
  try {
    const r = await fetch('api/pve_sync', { method: 'POST' });
    const txt = await r.text();
    if (s) s.textContent = r.ok ? ' ✓ synced' : (' ✗ ' + txt.slice(0,200));
    if (out) out.textContent = txt;
    setTimeout(() => { if (s) s.textContent=''; }, 4000);
    pollAll();
  } catch(e) {
    if (s) s.textContent = ' ✗ ' + e;
    if (out) out.textContent = 'ERROR: ' + e;
  }
}
async function onboardSubmit(ev) {
  ev.preventDefault();
  const status = $('onboard-status');
  const body = {
    name: $('o-name').value,
    addr: $('o-addr').value,
    user: $('o-user').value || 'root',
    port: parseInt($('o-port').value || '22'),
    category: $('o-cat').value,
    chip: $('o-chip').value || '',
    parent: $('o-parent').value || '',
    password: $('o-pass').value || '',
  };
  $('o-pass').value = '';   // wipe immediately
  status.textContent = ' submitting…';
  try {
    const r = await fetch('api/onboard', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const txt = await r.text();
    $('log').textContent = txt;
    if (r.ok) {
      status.textContent = ' ✓ added';
      $('onboard-form').reset();
      pollAll();
    } else {
      status.textContent = ' ✗ ' + txt.slice(0,200);
    }
  } catch(e) { status.textContent = ' ERROR: ' + e; }
}

// ============================================================
// Init
// ============================================================
attachSortHandlers();
route();
pollAll();
setInterval(pollAll, 10000);
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


def ssh_copy_id_with_password(addr: str, user: str, port: int, password: str) -> tuple[bool, str]:
    """Uses sshpass to push the GOD pubkey into ~/.ssh/authorized_keys
    of the target host. Password is only held in this process, never
    written to disk."""
    pubkey = (DATA_DIR / ".ssh" / "id_ed25519.pub").read_text().strip()
    # ssh-copy-id is the right tool but it's interactive; we do the
    # equivalent via shell on the remote.
    cmd_remote = (
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
        "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
        f"grep -qF '{pubkey}' ~/.ssh/authorized_keys || echo '{pubkey}' >> ~/.ssh/authorized_keys && "
        "echo OK_PUBKEY_INSTALLED"
    )
    try:
        p = subprocess.run(
            ["sshpass", "-e", "ssh",
                "-o", "BatchMode=no",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "PreferredAuthentications=password",
                "-o", "PubkeyAuthentication=no",
                "-o", "ConnectTimeout=8",
                "-p", str(port),
                f"{user}@{addr}",
                cmd_remote,
            ],
            env={**os.environ, "SSHPASS": password},
            capture_output=True, text=True, timeout=20,
        )
        if "OK_PUBKEY_INSTALLED" in p.stdout:
            return True, "pubkey installed via password bootstrap"
        return False, f"rc={p.returncode} stderr={p.stderr.strip()[:200]}"
    except FileNotFoundError:
        return False, "sshpass not installed in the addon container"
    except subprocess.TimeoutExpired:
        return False, "SSH connect timeout"
    except Exception as e:
        return False, f"exception: {e}"


def onboard_host(payload: dict) -> tuple[int, str]:
    """Adds a new host to /data/options.json, optionally bootstraps SSH
    with password, runs detect_os + install_agent + gather."""
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
    password = payload.get("password", "")  # optional, transient
    if category not in CATEGORIES:
        return 400, f"unknown category {category}; valid: {','.join(CATEGORIES)}"

    try:
        opts = json.loads(OPTS_FILE.read_text())
    except Exception as e:
        return 500, f"can't read options.json: {e}"

    if any(h["name"] == name for h in opts.get("hosts", [])):
        return 409, f"host '{name}' already exists"

    new_host = {"name": name, "addr": addr, "user": user, "port": port, "category": category}
    chip   = (payload.get("chip")   or "").strip()
    parent = (payload.get("parent") or "").strip()
    if chip:
        new_host["chip"] = chip
    if parent:
        new_host["parent"] = parent
    opts.setdefault("hosts", []).append(new_host)
    try:
        OPTS_FILE.write_text(json.dumps(opts, indent=2))
    except Exception as e:
        return 500, f"can't write options.json: {e}"

    log_lines = [f"✓ Host '{name}' ({addr}) added under category '{category}'."]

    # Step 1: regen inventory
    log_lines.append("\n=== bootstrap (regen inventory) ===")
    log_lines.append(rerun_bootstrap())

    # Step 2: optional password bootstrap
    if password:
        log_lines.append(f"\n=== ssh-copy-id (password bootstrap) ===")
        ok, msg = ssh_copy_id_with_password(addr, user, port, password)
        log_lines.append(("✓ " if ok else "✗ ") + msg)
        # We deliberately discard the password here — it never reaches disk.
        password = None
        if not ok:
            log_lines.append("Password bootstrap failed. Skipping detect_os / install_agent.")
            log_lines.append("Fix and retry, or install the pubkey manually then re-run.")
            return 200, "\n".join(log_lines)

    # Step 3: detect_os
    log_lines.append(f"\n=== detect_os --limit {name} ===")
    log_lines.append(run_playbook("detect_os", limit=name))

    # Step 4: install_agent
    log_lines.append(f"\n=== install_agent --limit {name} ===")
    log_lines.append(run_playbook("install_agent", limit=name))

    # Step 5: gather (one-shot, results visible immediately on next poll)
    log_lines.append(f"\n=== gather --limit {name} ===")
    log_lines.append(run_playbook("gather", limit=name))

    return 200, "\n".join(log_lines)


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
        # /api/pve_sync — force a one-shot Proxmox sync
        if p.rfind("/api/pve_sync") != -1:
            try:
                proc = subprocess.run(
                    [sys.executable, PVE_SYNC_SCRIPT, "once"],
                    capture_output=True, text=True, timeout=60,
                )
                body = f"rc={proc.returncode}\n{proc.stdout[-3000:]}\n--- stderr ---\n{proc.stderr[-1000:]}"
                self._send(200 if proc.returncode == 0 else 500, body, "text/plain; charset=utf-8")
            except Exception as e:
                self._send(500, f"pve_sync exception: {e}", "text/plain")
            return
        # /api/action/<name>[?limit=<host>]
        idx_action = p.rfind("/api/action/")
        if idx_action != -1:
            from urllib.parse import urlparse, parse_qs
            u = urlparse(p[idx_action + len("/api"):])  # rebuild "/action/<name>?..."
            action = u.path.split("/")[-1].strip()
            limit = parse_qs(u.query).get("limit", [None])[0]
            self._send(200, run_playbook(action, limit=limit), "text/plain; charset=utf-8")
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
