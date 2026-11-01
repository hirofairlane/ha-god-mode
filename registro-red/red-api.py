#!/usr/bin/env python3
"""HTTP API for the network registry. Runs on its own host; GOD Mode consumes it.

Why here and not inside the HA addon:

- **The routers' SSH keys stay on this host.** Reserving an IP means writing
  to the OpenWrt config over SSH. Giving the addon that access would widen
  its attack surface just for convenience.
- **It survives HA being down.** If Home Assistant crashes or restarts, the
  registry keeps answering — which is exactly when you want to know what's
  on the network.

GOD Mode consumes it through a proxy, same as it already does for its
collector.

Authentication is a shared token: this API **modifies the house's DHCP
config**, and the LAN isn't trusted (learned that the hard way with a legacy
guest network that wasn't isolated).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import CONFIG
from red_db_lib import abrir, ahora

PUERTO = 9877
ROUTER = CONFIG["router"]
TOKEN = Path("/etc/red-api.token")
MAC_OK = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IP_OK = re.compile(r"^192\.168\.[01]\.\d{1,3}$")
PERFILES = ("pleno", "nube", "local")
ESTADOS = ("pendiente", "aprobado", "retirado", "bloqueado")
# What the device IS, which is different from `perfil` (its outbound policy).
TIPOS = ("movil", "tablet", "portatil", "pc", "tele", "streaming", "altavoz",
         "camara", "enchufe", "sensor", "impresora", "servidor", "router", "otro")


def filas(consulta: str, args=()) -> list[dict]:
    c = abrir()
    return [dict(r) for r in c.execute(consulta, args)]


def listar(filtro: str) -> list[dict]:
    base = """SELECT d.*, COALESCE(d.ip, r.ip) ip_efectiva,
                     COALESCE(d.hostname, r.nombre) nombre_efectivo,
                     (r.mac IS NOT NULL) tiene_reserva, r.ip ip_reservada
              FROM dispositivos d LEFT JOIN reservas r ON r.mac = d.mac """
    donde = {
        "vivos":       "WHERE d.vivo=1",
        "sin_aprobar": "WHERE d.vivo=1 AND d.estado!='aprobado'",
        "fantasmas":   "WHERE d.veces=0",
        "sin_tipo":    "WHERE d.vivo=1 AND d.tipo IS NULL",
        "todo":        "",
    }.get(filtro, "WHERE d.vivo=1")
    return filas(base + donde + " ORDER BY d.vivo DESC, COALESCE(d.ip, r.ip, 'zzz')")


def reservar(mac: str, ip: str, nombre: str) -> tuple[bool, str]:
    """Creates or updates the static reservation on the main OpenWrt router.

    This is what stops the device from changing IP. Done with `uci`, with a
    backup first, applying only a `dnsmasq restart` — which does NOT drop
    the wifi, unlike a `wifi reload`.
    """
    if not MAC_OK.match(mac) or not IP_OK.match(ip):
        return False, "invalid MAC or IP"
    nombre = re.sub(r"[^A-Za-z0-9_-]", "-", (nombre or "")[:40]) or f"dev-{mac[-5:].replace(':','')}"
    guion = f"""
      cp /etc/config/dhcp /etc/config/dhcp.bak.$(date +%Y%m%d-%H%M%S)
      exist=$(uci show dhcp | grep -i "\\.mac='{mac}'" | head -1 | cut -d. -f2)
      if [ -n "$exist" ]; then
        uci set dhcp.$exist.ip='{ip}'; uci set dhcp.$exist.name='{nombre}'; uci set dhcp.$exist.dns='1'
      else
        uci add dhcp host >/dev/null
        uci set dhcp.@host[-1].mac='{mac}'; uci set dhcp.@host[-1].ip='{ip}'
        uci set dhcp.@host[-1].name='{nombre}'; uci set dhcp.@host[-1].dns='1'
      fi
      uci commit dhcp && /etc/init.d/dnsmasq restart >/dev/null 2>&1 && echo OK
    """
    r = subprocess.run(["ssh", "-n", "-o", "ConnectTimeout=10", ROUTER, guion],
                       capture_output=True, text=True, timeout=60)
    if "OK" not in r.stdout:
        return False, (r.stderr or r.stdout or "no response")[:200]
    c = abrir()
    c.execute("""INSERT INTO reservas VALUES (?,?,?,?)
                 ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip, nombre=excluded.nombre,
                 ts=excluded.ts""", (mac, ip, nombre, ahora()))
    c.commit()
    return True, f"{nombre} -> {ip}"


def actualizar(mac: str, cuerpo: dict) -> tuple[bool, str]:
    if not MAC_OK.match(mac):
        return False, "invalid MAC"
    c = abrir()
    sets, args = [], []
    if "descripcion" in cuerpo:
        # Written by hand => firm, and the HA cross-reference no longer overwrites it.
        sets += ["descripcion=?", "firme=1", "origen='manual'"]
        args.append(cuerpo["descripcion"][:300])
    if "perfil" in cuerpo:
        if cuerpo["perfil"] not in PERFILES + ("",):
            return False, "invalid profile"
        sets.append("perfil=?")
        args.append(cuerpo["perfil"] or None)
    if "tipo" in cuerpo:
        if cuerpo["tipo"] not in TIPOS + ("",):
            return False, "invalid type"
        sets.append("tipo=?")
        args.append(cuerpo["tipo"] or None)
    if "estado" in cuerpo:
        if cuerpo["estado"] not in ESTADOS:
            return False, "invalid state"
        sets.append("estado=?")
        args.append(cuerpo["estado"])
    if not sets:
        return False, "nothing to change"
    sets.append("actualizado=?")
    args += [ahora(), mac]
    cur = c.execute(f"UPDATE dispositivos SET {', '.join(sets)} WHERE mac=?", args)
    c.commit()
    # Without this it would answer "saved" even if the MAC didn't exist.
    if cur.rowcount == 0:
        return False, "that MAC isn't in the registry"
    return True, "saved"


class H(BaseHTTPRequestHandler):
    def _auth(self) -> bool:
        if not TOKEN.exists():
            return True
        esperado = TOKEN.read_text().strip()
        dado = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        return dado == esperado

    def _responde(self, codigo: int, datos) -> None:
        b = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if not self._auth():
            return self._responde(401, {"error": "token"})
        p = self.path.split("?")[0].rstrip("/")
        if p.endswith("/salud"):
            c = abrir()
            return self._responde(200, {
                "ok": True,
                "registrados": c.execute("SELECT COUNT(*) FROM dispositivos").fetchone()[0],
                "vivos": c.execute("SELECT COUNT(*) FROM dispositivos WHERE vivo=1").fetchone()[0],
                "sin_aprobar": c.execute(
                    "SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND estado!='aprobado'").fetchone()[0],
                "sin_tipo": c.execute(
                    "SELECT COUNT(*) FROM dispositivos WHERE vivo=1 AND tipo IS NULL").fetchone()[0],
            })
        if "/dispositivos" in p:
            filtro = "vivos"
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if kv.startswith("filtro="):
                        filtro = kv.split("=", 1)[1]
            return self._responde(200, {"dispositivos": listar(filtro), "filtro": filtro})
        if "/tipos" in p:
            return self._responde(200, {"tipos": list(TIPOS), "perfiles": list(PERFILES)})
        if "/eventos" in p:
            return self._responde(200, {"eventos": filas(
                "SELECT * FROM avistamientos ORDER BY ts DESC LIMIT 60")})
        self._responde(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._responde(401, {"error": "token"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            cuerpo = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._responde(400, {"error": "invalid json"})
        p = self.path.split("?")[0].rstrip("/")
        if "/reserva/" in p:
            mac = p.rsplit("/reserva/", 1)[1].lower()
            ok, msg = reservar(mac, cuerpo.get("ip", ""), cuerpo.get("nombre", ""))
            return self._responde(200 if ok else 400, {"ok": ok, "mensaje": msg})
        if "/dispositivo/" in p:
            mac = p.rsplit("/dispositivo/", 1)[1].lower()
            ok, msg = actualizar(mac, cuerpo)
            return self._responde(200 if ok else 400, {"ok": ok, "mensaje": msg})
        self._responde(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"  red-api en :{PUERTO}  (token: {'si' if TOKEN.exists() else 'NO'})")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), H).serve_forever()
