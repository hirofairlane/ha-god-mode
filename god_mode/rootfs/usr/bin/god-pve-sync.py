#!/usr/bin/env python3
"""
god-pve-sync.py - polls the Proxmox API of every `pve_node` host that
has a `pve_token_id` + `pve_token_secret` set, and writes a snapshot of
its VMs/LXCs to /data/pve_children/<node>.json.

The collector exposes the aggregated result at /api/pve_children. The
dashboard renders it per pve_node so guests show up even when they're
stopped (i.e. without needing SSH or an agent inside them).

stdlib only. SSL certs of Proxmox are usually self-signed so we disable
verification — this is a local LAN with API tokens, not a public CA.
"""
from __future__ import annotations
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_DIR        = Path(os.environ.get("GOD_DATA_DIR", "/data"))
OPTS_FILE       = DATA_DIR / "options.json"
OUT_DIR         = DATA_DIR / "pve_children"
SUMMARY_FILE    = OUT_DIR / "_summary.json"
DEFAULT_INTERVAL = 300  # seconds
TIMEOUT          = 8

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def load_options() -> dict:
    try:
        return json.loads(OPTS_FILE.read_text())
    except Exception as e:
        print(f"[pve-sync] cannot read {OPTS_FILE}: {e}", file=sys.stderr, flush=True)
        return {}


def pve_get(node_addr: str, path: str, token_id: str, token_secret: str) -> dict | None:
    url = f"https://{node_addr}:8006/api2/json{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"PVEAPIToken={token_id}={token_secret}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=SSL_CTX) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        print(f"[pve-sync] {node_addr}{path} HTTP {e.code}: {body}", flush=True)
        return None
    except Exception as e:
        print(f"[pve-sync] {node_addr}{path} {type(e).__name__}: {e}", flush=True)
        return None


def fetch_node_snapshot(node_name: str, node_addr: str, token_id: str, token_secret: str) -> dict:
    """Returns a normalized snapshot for one PVE node:
        {
          "node": "...",
          "addr": "...",
          "ok": True/False,
          "error": "..." (only when ok=false),
          "ts": <epoch>,
          "node_status": { cpu_pct, mem_pct, disk_pct, uptime_s, ... },
          "guests": [
            { "vmid", "name", "type", "status", "cpu_pct", "mem_pct",
              "mem_used_b", "mem_max_b", "disk_used_b", "disk_max_b",
              "uptime_s", "tags" }, ...
          ]
        }
    """
    snap = {
        "node": node_name,
        "addr": node_addr,
        "ts": int(time.time()),
        "ok": False,
    }

    # Cluster resources gives us nodes + qemu + lxc in one call
    res = pve_get(node_addr, "/cluster/resources", token_id, token_secret)
    if not res:
        snap["error"] = "cluster/resources call failed"
        return snap
    items = res.get("data", [])

    # node_status: pick the row whose .node == node_name AND .type == "node"
    node_row = next(
        (r for r in items if r.get("type") == "node" and r.get("node") == node_name),
        None,
    )
    if node_row is None:
        # Single-node cluster — use the only available row, by addr if needed.
        single = [r for r in items if r.get("type") == "node"]
        if single:
            node_row = single[0]
            snap["node"] = node_row.get("node") or node_name

    if node_row:
        max_mem  = node_row.get("maxmem")  or 0
        used_mem = node_row.get("mem")     or 0
        max_disk = node_row.get("maxdisk") or 0
        used_dsk = node_row.get("disk")    or 0
        snap["node_status"] = {
            "status":      node_row.get("status"),
            "cpu_pct":     round((node_row.get("cpu") or 0) * 100, 1),
            "mem_pct":     round((used_mem / max_mem) * 100, 1) if max_mem else None,
            "mem_used_b":  used_mem,
            "mem_max_b":   max_mem,
            "disk_pct":    round((used_dsk / max_disk) * 100, 1) if max_disk else None,
            "disk_used_b": used_dsk,
            "disk_max_b":  max_disk,
            "uptime_s":    node_row.get("uptime"),
            "level":       node_row.get("level"),
        }

    # guests: type in {qemu, lxc} and .node == snap["node"]
    guests = []
    for r in items:
        if r.get("type") not in ("qemu", "lxc"):
            continue
        if r.get("node") and snap.get("node") and r["node"] != snap["node"]:
            continue
        max_mem = r.get("maxmem") or 0
        mem     = r.get("mem")    or 0
        max_dsk = r.get("maxdisk") or 0
        dsk     = r.get("disk")    or 0
        guests.append({
            "vmid":        r.get("vmid"),
            "name":        r.get("name"),
            "type":        r.get("type"),                 # qemu | lxc
            "status":      r.get("status"),               # running | stopped
            "cpu_pct":     round((r.get("cpu") or 0) * 100, 1),
            "cpu_cores":   r.get("maxcpu"),
            "mem_pct":     round((mem / max_mem) * 100, 1) if max_mem else None,
            "mem_used_b":  mem,
            "mem_max_b":   max_mem,
            "disk_pct":    round((dsk / max_dsk) * 100, 1) if max_dsk else None,
            "disk_used_b": dsk,
            "disk_max_b":  max_dsk,
            "uptime_s":    r.get("uptime") or 0,
            "tags":        r.get("tags") or "",
            "template":    bool(r.get("template")),
        })
    guests.sort(key=lambda g: (g["status"] != "running", g["vmid"] or 0))
    snap["guests"] = guests
    snap["counts"] = {
        "total":    len(guests),
        "running":  sum(1 for g in guests if g["status"] == "running"),
        "stopped":  sum(1 for g in guests if g["status"] == "stopped"),
        "vms":      sum(1 for g in guests if g["type"] == "qemu"),
        "lxcs":     sum(1 for g in guests if g["type"] == "lxc"),
    }
    snap["ok"] = True
    return snap


def write_snapshot(snap: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"{snap['node']}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snap, indent=2))
    tmp.replace(path)


def sync_once() -> dict:
    opts = load_options()
    summary = {"ts": int(time.time()), "nodes": []}
    for host in opts.get("hosts", []):
        if host.get("category") != "pve_node":
            continue
        tok_id  = host.get("pve_token_id")
        tok_sec = host.get("pve_token_secret")
        if not tok_id or not tok_sec:
            summary["nodes"].append({
                "node": host["name"], "addr": host["addr"],
                "ok": False, "error": "no token configured",
            })
            continue
        snap = fetch_node_snapshot(host["name"], host["addr"], tok_id, tok_sec)
        write_snapshot(snap)
        summary["nodes"].append({
            "node":   snap["node"],
            "addr":   snap["addr"],
            "ok":     snap["ok"],
            "error":  snap.get("error"),
            "counts": snap.get("counts"),
        })
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2))
    return summary


def main_loop():
    opts = load_options()
    interval = max(60, int(opts.get("pve_sync_interval", DEFAULT_INTERVAL)))
    print(f"[pve-sync] starting, interval={interval}s", flush=True)
    while True:
        try:
            summary = sync_once()
            ok = sum(1 for n in summary["nodes"] if n["ok"])
            total = len(summary["nodes"])
            print(f"[pve-sync] {time.strftime('%H:%M:%S')} - {ok}/{total} nodes ok", flush=True)
        except Exception as e:
            print(f"[pve-sync] loop error: {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "once":
        summary = sync_once()
        print(json.dumps(summary, indent=2))
        sys.exit(0)
    main_loop()
