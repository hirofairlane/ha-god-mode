# Registro de red

Inventario vivo de todo lo que se conecta a la casa. Se ve y se edita desde la
pestaña **Red** del add-on GOD Mode.

La idea de fondo: **nada se da de alta a mano**. El censo recoge lo que hay,
todo entra como `pendiente`, e identificar es opcional. El producto no es una
lista completa, es *la lista de desconocidos, que va menguando*. El intento
anterior murió porque exigía dar de alta cada aparato antes de verlo.

## Las piezas

```
  routers OpenWrt ──┐
  (asociaciones)    │
                    ├─► recolectar-red.sh ─► censo-db.py ─► red.db ─┬─► red-api.py ─► add-on GOD Mode
  DHCP del principal┘        (cada hora)       (SQLite)             │      (:9877)      (pestaña Red)
                                                                    ├─► metricas-influx.py ─► InfluxDB
  Home Assistant ──────────► cruzar-ha.py ────────────┘             └─► whitelist.py ─► routers OpenWrt
```

| Fichero | Qué hace |
|---|---|
| `recolectar-red.sh` | entra por SSH a los cinco routers: `station dump` por interfaz con su SSID, más leases y reservas del principal |
| `censo-db.py` | pasada horaria (`censo-red.timer` en zeratul). Decide quién está vivo |
| `red_db_lib.py` | esquema y utilidades. La ruta de la BD se fija con `RED_DB` |
| `red-api.py` | API HTTP en LXC 104 (`:9877`), con token. Es lo que consume el add-on |
| `cruzar-ha.py` | cruza con el registro de dispositivos de Home Assistant |
| `tipificar.py` | deduce el tipo de aparato leyendo las descripciones |
| `whitelist.py` | lleva los bloqueos a los routers. **Lee su cabecera antes de usarlo** |
| `metricas-influx.py` | series temporales a InfluxDB |

## Tres decisiones que conviene entender

**Vivo no es tener lease.** Un aparato está vivo si está asociado a un AP *o*
responde al ping. La orangepizero que expulsamos seguía teniendo lease nueve
horas después de estar apagada; contarla como viva habría escondido el
problema.

**SQLite para el registro, InfluxDB para la historia.** El registro son filas
que se editan (descripción, estado, tipo) y eso InfluxDB no lo hace: no tiene
UPDATE. Las métricas, al revés, no se editan nunca.

**Sólo se anotan los cambios.** Apuntar 80 aparatos cada hora son miles de
filas diarias de ruido. `avistamientos` guarda altas, apariciones,
desapariciones y cambios de IP o de AP, que es una línea temporal legible.

## Estados

```
pendiente   visto, sin revisar. Es el estado por defecto
aprobado    identificado y se queda. Esto es la whitelist
retirado    era nuestro, ya no existe
bloqueado   expulsado a propósito
```

Lo que dispara la alarma no es «hay algo nuevo» sino **«hay algo vivo que no
está aprobado»**, que es una pregunta que se puede ir vaciando.

## Tipos

`movil · tablet · portatil · pc · tele · streaming · altavoz · camara ·
enchufe · sensor · impresora · servidor · router · otro`

El tipo dice **qué es** el aparato; el perfil (`pleno`, `nube`, `local`) dice
**qué se le deja hacer**. Son cosas distintas a propósito: un enchufe Sonoff y
una tele pueden compartir política y no parecerse en nada.

Lo que no encaja va a `otro` con su descripción a mano. Añadir un tipo es
tocar `TIPOS` en `red-api.py`; la interfaz los lee de `/tipos` y no hay que
volver a desplegar el add-on.

## Puesta en marcha

```bash
export RED_DB=/ruta/a/red.db
./censo-db.py                    # una pasada
./tipificar.py                   # ensayo en seco
./tipificar.py --aplicar
./whitelist.py --auditar         # qué bloquean ya los routers
./whitelist.py                   # ensayo en seco de la sincronización
```

Todo lo que escribe tiene ensayo en seco por defecto.
