# lymow-console

A standalone web console to **monitor and control a Lymow One / One Plus
robotic mower on your Ubuntu LAN** — no Home Assistant, no cloud UI.
FastAPI backend + React/TypeScript/Vite frontend, speaking the community
reverse-engineered mower protocol.

```
browser ──HTTP/WS── backend (FastAPI, localhost:8384) ──MQTT/WSS+REST── Lymow cloud ── mower
                          └──ffmpeg── RTSP ── mower camera (LAN)
```

## Features

- **Dashboard** — activity, battery/charging, task progress + zones,
  firmware state, errors, positioning quality (±cm from the mower's own
  accuracy report), WiFi/LTE, stale-telemetry badge.
- **Live updates** over a WebSocket (with reconnect + fallback polling).
- **Map (read-only)** — zones, docking channels, dock, live mower pose
  with accuracy circle, active-zone/task highlights, multi-zone start.
  Geometry editing deliberately stays in the official Lymow app.
- **Controls** — start / pause / resume / dock / cancel-task /
  force-reinit / zone-start, gated by the mower state matrix, with
  confirmation dialogs for destructive actions. Commands are audited and
  **never auto-retried** (the firmware silently rejects; commands are not
  idempotent).
- **Camera** — the mower's LAN RTSP feed relayed through the backend
  (the browser never touches the mower). Needs ffmpeg: either
  `sudo apt install ffmpeg` **or** the sudo-free wheel
  (`pip install "lymow-console-backend[camera]"` in the venv — a static
  ffmpeg binary; system install always wins). The relay requires the
  backend host to be able to route to the mower's LAN IP on port 10022
  (same subnet not required).
- **History & command audit** in local SQLite.
- **Simulated mower** (`LYMOW_PROVIDER=sim`, the default) — a
  protocol-faithful fake mower for safe development; tests prove it can
  never touch the network.

## Quick start (Ubuntu 22.04/24.04)

```bash
sudo apt install python3-venv nodejs npm        # ffmpeg optional (camera)

cd lymow-console
./dev.sh                                        # backend :8384 + dev UI :5173
```

Open http://127.0.0.1:5173 — the first login **initializes the console
password**.

### Production-style (built UI served by the backend)

```bash
cd frontend && npm install && npm run build && cd ..
cd backend && python3 -m venv .venv && .venv/bin/pip install -e .
LYMOW_PROVIDER=real .venv/bin/python -m uvicorn app.main:app \
    --host 127.0.0.1 --port 8384
```

Then open http://127.0.0.1:8384 and enter your Lymow account details
under **Settings** (stored server-side only, masked in every API
response, never logged):

- **Email + password** accounts: pick *Email + password*, enter them,
  pick your account region.
- **Google accounts** (how most Android users sign in — there is no
  password): pick *Sign in with Google* → *Open Google sign-in page* →
  sign in with Google in the popup → the browser then **fails to open
  the Lymow app** (expected: the callback URL is `myapp://…`) → copy
  that full failed URL from the address bar, paste it into the box, and
  click *Complete sign-in & find my mower*. A refresh token is stored
  server-side and renews automatically from then on.

Start with `LYMOW_PROVIDER=sim` until you choose to talk to the real
mower.

Configuration lives in environment variables — see `.env.example`.

### systemd

`deploy/lymow-console.service` (rootless, hardened, localhost-bound).
Adjust `User=` and paths, then:

```bash
sudo cp deploy/lymow-console.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now lymow-console
```

## Connecting from another machine (the safe way)

The server binds `127.0.0.1` by default and should stay that way. From a
laptop/phone use an SSH tunnel:

```bash
ssh -N -L 8384:127.0.0.1:8384 user@mowerhost
# then browse http://127.0.0.1:8384 locally
```

Exposing `0.0.0.0` is possible (`LYMOW_HOST=0.0.0.0`) but only ever
behind an authenticating TLS reverse proxy on your LAN — **never**
public internet. All control endpoints and the WebSocket require the
session cookie anyway.

## Testing

```bash
cd backend && .venv/bin/python -m pytest tests -q     # 155 tests
```

Highlights:

- `test_protocol.py` — the wire format verified against **real mower
  fixtures** (`tests/fixtures/*.bin`).
- `test_state_matrix.py` — the (work_status × robot_status ×
  is_recharging) decision table incl. firmware-quirk regressions.
- `test_simulator_isolation.py` — network stack is tripwired
  (sockets/MQTT/websockets/aiohttp); the *entire* command surface runs
  through the simulator proving **no simulated action can ever reach a
  real mower**.

## Security posture

- Console password: PBKDF2-SHA256 (600k iters) in SQLite; sessions are
  signed HttpOnly cookies (same cookie auths HTTP + WS).
- Lymow cloud credentials: server-side only, DB file `chmod 0600`,
  masked in all API views, never logged, never sent to the browser.
- Every command attempt (including refusals) is recorded in an audit
  table with its outcome.
- The backend **must** run on your LAN/localhost; the mower camera
  requires LAN reachability to the mower's IP.

## Known protocol gaps (unsupported, surfaced in the UI)

- **No-go zone geometry** and the **mowing coverage path** cannot be
  rendered: `PbNoGoZone`/`PbPath` are empty placeholders in the extracted
  community schema — nothing decodable is published on the wire.
- **Map editing** is intentionally not implemented (unverified write
  paths; mistakes are unrecoverable without re-mapping in the app).
- Firmware schedules are queryable (`query-schedules`) but not editable
  from this console.
- Remaining-time is only shown when the firmware itself reports it.

## Attribution & licensing

- This project: MIT (see `backend/pyproject.toml`).
- `backend/app/lymow/**` is derived from
  [Lymow-One-MQTT](https://github.com/Mortimer452/Lymow-One-MQTT) —
  Copyright (c) 2026 Aaron Gibbs, MIT — full text in
  `LICENSE-lymow-one-mqtt`.
- Camera facts verified via [ha-lymow](https://github.com/8408323/ha-lymow).
- [Lymow-HA](https://github.com/d3dfantasy99/Lymow-HA) (unlicensed) was
  consulted as documentation only — none of its code is included.
- `research/` holds the upstream clones for reference; it is not
  shipped. See `docs/PROTOCOL.md` for the protocol research record.
