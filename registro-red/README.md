# Network registry

A living inventory of everything that connects to the house network. Viewed
and edited from the **Network** tab of the GOD Mode add-on.

The core idea: **nothing gets registered by hand**. The census picks up
whatever exists, everything starts as `pendiente` (pending), and identifying
a device is optional. The product isn't a complete list — it's *the list of
unknowns, which keeps shrinking*. The previous attempt at this died because
it required registering every device before you could even see it.

## The pieces

```
  OpenWrt routers ──┐
  (associations)    │
                    ├─► recolectar-red.sh ─► censo-db.py ─► red.db ─┬─► red-api.py ─► GOD Mode add-on
  Main router's DHCP┘        (hourly)         (SQLite)              │      (:9877)      (Network tab)
                                                                     ├─► metricas-influx.py ─► InfluxDB
  Home Assistant ───────────► cruzar-ha.py ──────────────┘             ├─► whitelist.py ─► OpenWrt routers
                                                                        ├─► auto_enroll.py ─► SSH (new hosts)
                                                                        └─► alertas.py ─► Telegram
```

| File | What it does |
|---|---|
| `recolectar-red.sh` | SSHes into every router: `station dump` per interface with its SSID, plus leases and reservations from the main router |
| `censo-db.py` | hourly pass (`censo-red.timer` on the collector host). Decides who's alive |
| `red_db_lib.py` | schema and utilities. The DB path is set with `RED_DB` |
| `red-api.py` | HTTP API (`:9877`), token-authenticated. This is what the add-on consumes |
| `cruzar-ha.py` | cross-references with Home Assistant's device registry |
| `tipificar.py` | infers a device's type by reading its description |
| `whitelist.py` | pushes blocks down to the routers. **Read its header before using it** |
| `metricas-influx.py` | time series to InfluxDB |
| `config.py` | loads `infra.local.json` — the one place all the scripts above read real IPs/credentials from |
| `auto_enroll.py` | finds new SSH-capable devices and gets the GOD Mode pubkey onto them, so onboarding is a click instead of a manual key-paste. **Read its header before using `--aplicar`** |
| `alertas.py` | Telegram digest when something alive isn't approved — the "possible intruder" alert |

## Three decisions worth understanding

**Alive isn't the same as having a lease.** A device is alive if it's
associated to an AP *or* answers a ping. A rogue device we kicked off the
network once kept its DHCP lease for nine hours after being powered down;
counting that as "alive" would have hidden the problem.

**SQLite for the registry, InfluxDB for history.** The registry is rows that
get edited (description, state, type), and InfluxDB can't do that — no
UPDATE. Metrics, on the other hand, are never edited.

**Only changes get logged.** Logging 80 devices every hour would be
thousands of noisy rows a day. `avistamientos` (sightings) only records
additions, appearances, disappearances and IP/AP changes — a readable
timeline instead of noise.

## States

```
pendiente   seen, not reviewed yet. The default state
aprobado    identified and kept. This is the whitelist
retirado    used to be ours, doesn't exist anymore
bloqueado   deliberately kicked off
```

(These are the literal values stored in the SQLite schema — kept in Spanish
to avoid a breaking schema migration on an already-running registry.)

What triggers an alert isn't "something new showed up" but **"something
alive isn't approved"** — a question you can work down to zero over time.
`alertas.py` sends exactly that as a Telegram digest, at most once per
`alert_cooldown_s` (default 6h) — one message for the whole backlog, not
one per device.

## Types

`movil · tablet · portatil · pc · tele · streaming · altavoz · camara ·
enchufe · sensor · impresora · servidor · router · otro`

(mobile, tablet, laptop, pc, tv, streaming, speaker, camera, smart plug,
sensor, printer, server, router, other — again, literal enum values from
the schema, kept as-is.)

Type describes **what the device is**; profile (`pleno`, `nube`, `local` —
full access, cloud-only, local-only) describes **what it's allowed to do**.
These are deliberately separate: a Sonoff smart plug and a TV can share a
policy while having nothing else in common.

Anything that doesn't fit goes to `otro` with a free-text description. Adding
a new type means editing `TIPOS` in `red-api.py`; the UI reads them from
`/tipos`, so the add-on doesn't need to be redeployed.

## Configuration

Every script above reads its real IPs and credentials from
`infra.local.json` (gitignored — copy `infra.example.json` and fill it in).
See `config.py` for the full key list: `router`, `ha_url`, `influx_url`,
`access_points`, `ssh_jump` (how to reach the routers — direct SSH, or
through a jump host), `default_ssh_password` (for `auto_enroll.py`),
`telegram_bot_token` / `telegram_chat_id` (for `alertas.py`) and
`alert_cooldown_s`. Every key is optional: missing ones just disable that
feature without breaking anything else.

## Getting started

```bash
export RED_DB=/path/to/red.db
./censo-db.py                    # one pass
./tipificar.py                   # dry run
./tipificar.py --aplicar
./whitelist.py --auditar         # what the routers are already blocking
./whitelist.py                   # dry run of the sync
./auto_enroll.py                 # dry run: lists SSH-shaped candidates
./auto_enroll.py --aplicar       # also tries the default password once per candidate
./alertas.py                     # Telegram digest of live+unapproved devices
```

Everything that writes defaults to a dry run.
