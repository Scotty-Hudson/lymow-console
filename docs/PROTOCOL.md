# Lymow One / One Plus — protocol research notes

This document records **only facts that were verified against the three
community reference projects and the fixtures shipped in
`backend/tests/fixtures/`**. Anything not listed here is unknown to this
project; the console deliberately does not guess endpoints, topics or
schemas.

Reference projects (cloned under `research/`):

| Repo | License | Used for |
|---|---|---|
| [Lymow-One-MQTT](https://github.com/Mortimer452/Lymow-One-MQTT) | MIT, © 2026 Aaron Gibbs | **protocol foundation** — protobuf schema, auth, MQTT, state matrix; code ported into `backend/app/lymow/` with attribution headers (`LICENSE-lymow-one-mqtt` at repo root) |
| [ha-lymow](https://github.com/8408323/ha-lymow) | — | attribution-only facts (camera RTSP endpoint) |
| [Lymow-HA](https://github.com/d3dfantasy99/Lymow-HA) | **no license** | documentation reference ONLY — its code was never copied |

---

## 1. Transport & topics

- The mower talks to Lymow's cloud (AWS IoT) — **there is no direct LAN
  API on the mower itself**. The console joins the same cloud channel
  with the owner's account credentials.
- MQTT over **WSS on port 443**. The AWS IoT endpoint path is a
  **SigV4 presigned URL** (IAM credentials vended by the Lymow REST API)
  — implemented in `app/lymow/sigv4.py` + `app/lymow/mqtt_client.py`.
- Topics (per device thing name):
  - `/device/{thing}/pbinput` — console → mower, one `PbInput` per message
  - `/device/{thing}/pboutput` — mower → everyone, `PbOutput` protobuf
  - `/device/{thing}/notify-app` — JSON control-plane events (e.g. device
    list changes)
- **Wire envelope:** every payload is JSON `{"message": "<base64 protobuf>"}`.
- Protocol version stamp on every command: `PB_VERSION = 40`
  (PB_VERSION_4_9; ha-lymow sends 49 — 40 is the value proven to work
  against the One Plus firmware used by Lymow-One-MQTT).
- Telemetry cadence: the mower broadcasts `PbOutput` roughly every 30 s
  idle, faster while a task is active. Three missed broadcasts ⇒ the
  console marks telemetry **stale** (`STALE_AFTER_S = 95`).

## 2. Cloud authentication

- Lymow cloud accounts are **AWS Cognito** user pools; sign-in via
  `pycognito` (`app/lymow/auth.py`).
- Verified region pool (`app/lymow/const.py` `API_ENDPOINTS`):
  `eu-west-1`, `us-east-2`, `ap-southeast-2`, `ap-east-1`. Each maps to a
  Lymow REST API domain and an AWS IoT domain.
- The REST API (device list, thing names, IoT credentials) is SigV4
  signed (`app/lymow/rest.py`).
- Credentials are stored **server-side only** (SQLite, file mode 0600,
  masked in every API response, never logged). The browser never sees
  them.

## 3. `PbInput` commands — the `userCtrl` table

Verified values (`app/lymow/const.py`):

| userCtrl | meaning | notes |
|---|---|---|
| 1 | CLEAN — start mowing | zone list rides in `map.goZones[].basicInfo.hashId` |
| 2 | DOCK | plain dock, **abandons task**; required when idle away from dock (GitHub #28: firmware silently ignores 33 there) |
| 3 | PAUSE | doubles as **clear-error** in ERROR state |
| 4 | RESUME | resume saved task |
| 19 | QUERY_MAP | mower replies with `btMap` zone catalog |
| 20 | QUERY_SCHEDULES | replies with `schedule` blob |
| 21 | PAUSE_DOCK | pause a running return-to-dock |
| 22 | RESUME_DOCK | resume a paused return-to-dock |
| 28 | FORCE_REINIT | mower re-initializes localization (destructive) |
| 33 | RECHARGE_DOCK | dock **keeping** task progress for later resume |

**Silent-reject behavior:** the firmware *ignores* commands that are
invalid for its current state — no nack, no error. The console therefore
runs a **2.5 s watchdog** (`EXPECTED_POST_STATES`,
`app/lymow/userctrl.py`): if neither `workStatus` nor `robotStatus`
reaches an expected post-state, the command is reported
`sent-unconfirmed` and **never retried** (most commands are not
idempotent — a doubled CLEAN could start a second task). Query commands
are exempt (no state transition expected).

## 4. State semantics

`work_status` / `robot_status` enum (same numbering, two fields):

```
0 NONE · 1 WAITING · 2 MOWING · 3 PAUSE · 4 DOCKING · 5 CHARGING
6 REMOTE_CONTROL · 7 ERROR · 8 RESUME · 9 ZONE_PARTITION
10 PAUSE_DOCKING · 11 UPDATING · 12 CHARGING_FULL
13 EMERGENCY_STOP · 14 ESCAPING · 15 RTT
```

The (work_status, robot_status, is_recharging) → (activity, buttons)
decision table is `app/lymow/state_matrix.py` (ported from
Lymow-One-MQTT; replaces its Home Assistant `features_for` so this
console has no HA dependency). Key verified behaviors:

- `robot_status` **overrides** `work_status` where they can disagree
  (e.g. rs=CHARGING while ws=DOCKING ⇒ docked, Start = Resume).
- The charging state forks on `isRecharging`: true ⇒ saved task exists
  ⇒ Start=RESUME(4); false ⇒ Start=CLEAN(1).
- rs=ERROR or rs=EMERGENCY_STOP hides Start/Dock; PAUSE(3) remains and
  acts as clear-error.
- Error lifecycle: firmware repeats `errorCodes` while erroring and does
  **not** send an explicit empty list on recovery; the state service
  clears cached codes on robotStatus 7 → non-7.

Merge rules (verified against real fixtures): snapshot submessages
(`robotInfo`, `pose`, `localizationInfo`, `wifiConfigRes`) replace;
sticky ones (`cleanInfo`, `deviceInfo`, `robotConfig`, …) merge;
`btMap` **replaces** (catalog replies must not accumulate); `rrConfig`
must be wholesale-replaced because proto3 cannot distinguish
`enableRr=false` from absent.

## 5. Map / zone catalog

- `QUERY_MAP(19)` → `PbOutput.btMap` blob. Parsing (`app/lymow/protocol.py
  parse_zone_catalog`) yields zones (hashId, name, polygon points in
  local ENU meters, enabled flag, `mowOrder` — non-null only while in a
  task), docking channels, `enuBasePoint` (surveyed RTK base GPS) and
  `runTimeConfig`.
- The blob contains **fields missing from the extracted `.proto`
  schema**; protobuf unknown-field round-tripping is byte-stable (test:
  `test_unknown_field_roundtrip_is_stable`), which several dev flows
  rely on.
- Real-fixture sanity anchor: a genuine One Plus map replies with 13
  zones including `Pool`/`8FyN5qA4`.
- Dock pose arrives separately as `PbOutput.chargingStationLoc`.
- **Protocol gaps (cannot be decoded, surfaced in the map API's
  `unsupported` list):**
  - **no-go zones** — `PbNoGoZone` is an empty schema placeholder;
  - **mowing path** — `PbPath` is an empty schema placeholder; the
    coverage path is not on the wire.
- The console is **read-only for map geometry**: zone editing stays in
  the official Lymow app (community edit paths unverified; a geometry
  mistake is unrecoverable without re-mapping).

## 6. Schedules

`QUERY_SCHEDULES(20)` replies with a `schedule` blob decodable via
`decode_schedules` (fixture: one real schedule decodes cleanly). The
console displays schedule queries but does not yet expose editing.

## 7. Camera (from ha-lymow's verified facts)

- The mower serves RTSP **on the LAN** at `rtsp://<mower-ip>:10022/h264ESVideoTest`
  (H.264, 640×480, **no authentication**).
- The console relays it through the backend (local `ffmpeg` subprocess →
  MJPEG multipart stream) so the browser never contacts the mower and
  never learns the RTSP URL. Missing ffmpeg degrades to an
  "unavailable + reason" API response.
- In simulator mode frames are synthesized locally (pure zlib PNG
  renderer — no ffmpeg/Pillow dependency).

## 8. Fixtures

`backend/tests/fixtures/*.bin` are **raw serialized `PbOutput`**
messages (JSON envelope stripped at capture time):
`query_map_response`, `schedule_response`, `state_broadcast`,
`clean_report`. Every protocol assumption above is exercised against
them in `tests/test_protocol.py`.

## 9. Console API contract (this project)

- `GET /api/state` and `WS /ws/telemetry` push the *same* document:
  `MowerStateService.snapshot()` — `{telemetry, status, activity,
  allowed_actions, online, stale, simulator, device_id, task_zones,
  map_ready, pose, last_update_epoch}`.
- `GET /api/map` returns the read-only map document plus `unsupported`.
- Control: `POST /api/commands/{start,pause,resume,dock,dock-cancel,
  force-reinit,query-map,query-schedules}`; destructive actions require
  `{"confirm": true}`; every attempt (including rejections) lands in the
  command audit table.
- Auth: single console password (PBKDF2-SHA256, 600k iterations) →
  itsdangerous-signed HttpOnly cookie; WebSocket auth via the same
  cookie or `?token=`. All `/api/**` except `/api/auth/**` require it.
