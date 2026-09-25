#!/usr/bin/env python3
"""API HTTP del registro de red. Corre en zeratul; la consume GOD-mode.

Por que aqui y no dentro del addon de HA:

- **Las llaves de los routers se quedan en zeratul.** Reservar una IP significa
  escribir en la configuracion del OpenWrt por SSH. Dar ese acceso al addon
  seria ampliar la superficie por comodidad.
- **Sobrevive a que HA no este.** Si Home Assistant se cae o se reinicia, el
  registro sigue respondiendo — que es justo cuando interesa saber que hay en la
  red.

GOD-mode lo consume por proxy, igual que ya hace con su colector.

Autenticacion por token compartido: esta API **modifica el DHCP de la casa**, y
la LAN no es de fiar (lo aprendimos con `virus-legacy`, que no esta aislada).
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from red_db_lib import abrir, ahora

PUERTO = 9877
ROUTER = "root@192.168.0.1"
TOKEN = Path("/etc/red-api.token")
MAC_OK = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
IP_OK = re.compile(r"^192\.168\.[01]\.\d{1,3}$")
PERFILES = ("pleno", "nube", "local")
ESTADOS = ("pendiente", "aprobado", "retirado", "bloqueado")
# Que ES el aparato, que es distinto de `perfil` (que es su politica de salida).
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
    """Crea o actualiza la reserva estatica en el OpenWrt principal.

    Esto es lo que evita que el portatil cambie de IP. Se hace con `uci`, con
    copia previa, y aplicando solo un `dnsmasq restart` — que NO corta el wifi,
    al contrario que un `wifi reload`.
    """
    if not MAC_OK.match(mac) or not IP_OK.match(ip):
        return False, "MAC o IP no validas"
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
        return False, (r.stderr or r.stdout or "sin respuesta")[:200]
    c = abrir()
    c.execute("""INSERT INTO reservas VALUES (?,?,?,?)
                 ON CONFLICT(mac) DO UPDATE SET ip=excluded.ip, nombre=excluded.nombre,
                 ts=excluded.ts""", (mac, ip, nombre, ahora()))
    c.commit()
    return True, f"{nombre} -> {ip}"


def actualizar(mac: str, cuerpo: dict) -> tuple[bool, str]:
    if not MAC_OK.match(mac):
        return False, "MAC no valida"
    c = abrir()
    sets, args = [], []
    if "descripcion" in cuerpo:
        # Escrita a mano => firme, y el cruce con HA ya no la pisa.
        sets += ["descripcion=?", "firme=1", "origen='manual'"]
        args.append(cuerpo["descripcion"][:300])
    if "perfil" in cuerpo:
        if cuerpo["perfil"] not in PERFILES + ("",):
            return False, "perfil no valido"
        sets.append("perfil=?")
        args.append(cuerpo["perfil"] or None)
    if "tipo" in cuerpo:
        if cuerpo["tipo"] not in TIPOS + ("",):
            return False, "tipo no valido"
        sets.append("tipo=?")
        args.append(cuerpo["tipo"] or None)
    if "estado" in cuerpo:
        if cuerpo["estado"] not in ESTADOS:
            return False, "estado no valido"
        sets.append("estado=?")
        args.append(cuerpo["estado"])
    if not sets:
        return False, "nada que cambiar"
    sets.append("actualizado=?")
    args += [ahora(), mac]
    cur = c.execute(f"UPDATE dispositivos SET {', '.join(sets)} WHERE mac=?", args)
    c.commit()
    # Sin esto se contestaba "guardado" aunque la MAC no existiera.
    if cur.rowcount == 0:
        return False, "esa MAC no esta en el registro"
    return True, "guardado"


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
        self._responde(404, {"error": "no existe"})

    def do_POST(self):
        if not self._auth():
            return self._responde(401, {"error": "token"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            cuerpo = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._responde(400, {"error": "json invalido"})
        p = self.path.split("?")[0].rstrip("/")
        if "/reserva/" in p:
            mac = p.rsplit("/reserva/", 1)[1].lower()
            ok, msg = reservar(mac, cuerpo.get("ip", ""), cuerpo.get("nombre", ""))
            return self._responde(200 if ok else 400, {"ok": ok, "mensaje": msg})
        if "/dispositivo/" in p:
            mac = p.rsplit("/dispositivo/", 1)[1].lower()
            ok, msg = actualizar(mac, cuerpo)
            return self._responde(200 if ok else 400, {"ok": ok, "mensaje": msg})
        self._responde(404, {"error": "no existe"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"  red-api en :{PUERTO}  (token: {'si' if TOKEN.exists() else 'NO'})")
    ThreadingHTTPServer(("0.0.0.0", PUERTO), H).serve_forever()
