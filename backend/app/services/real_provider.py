"""RealMowerProvider — Lymow cloud auth + AWS IoT MQTT transport.

Adapted from the Lymow-One-MQTT coordinator (MIT, Aaron Gibbs): same
connect flow, startup queries, lazy QUERY_MAP refire rules, reconnect
with fresh presigned URLs, and 15-minute REST online poll.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from app.lymow import protocol
from app.lymow.auth import CognitoAuth, LymowAuthError
from app.lymow.const import (
    API_ENDPOINTS,
    USER_CTRL_QUERY_MAP,
    USER_CTRL_QUERY_SCHEDULES,
    WORK_STATUS_MOWING,
    WORK_STATUS_WAITING,
)
from app.lymow.mqtt_client import MqttClient
from app.lymow.rest import LymowREST
from app.services.provider import MowerProvider, PublishError

_LOGGER = logging.getLogger(__name__)

_REST_POLL_INTERVAL = timedelta(minutes=15)
_QUERY_MAP_WAIT_SECONDS = 3.0


class RealMowerProvider(MowerProvider):
    """One device, one AWS IoT MQTT-over-WSS connection (per upstream design)."""

    simulator = False

    def __init__(
        self,
        *,
        region: str,
        device_id: str,
        email: str | None = None,
        password: str | None = None,
    ) -> None:
        super().__init__(device_id)
        self.region = region
        self.email = email
        self.password = password
        self.host = API_ENDPOINTS[region]["iotDomain"]
        self._session: aiohttp.ClientSession | None = None
        self.auth: CognitoAuth | None = None
        self.rest: LymowREST | None = None
        self.mqtt: MqttClient | None = None
        self._rest_online = False
        self._last_mqtt_at: datetime | None = None
        self._prev_work_status: int | None = None
        self._catalog_retry_count = 0
        self._last_catalog_query_at = datetime.min.replace(tzinfo=UTC)
        self._rest_poll_task: asyncio.Task | None = None
        self._shutting_down = False
        self._reconnecting = False
        self._state_ref: dict[str, Any] = {}  # set by MowerStateService

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        self._shutting_down = False
        self._session = aiohttp.ClientSession()
        self.auth = CognitoAuth(self.region, self._session)
        self.rest = LymowREST(self.region, self.auth, self._session)

        await self._login()

        # Initial REST poll (online flag + camera IP before MQTT lands).
        await self._do_rest_poll()

        self.mqtt = MqttClient(
            thing_name=self.device_id,
            host=self.host,
            region=self.region,
            auth=self.auth,
            on_pboutput=self._handle_pboutput,
            on_notify_app=self.on_notify_app,
            on_disconnect_async=self._handle_disconnect,
        )
        await self.mqtt.connect()
        self.on_connection_changed(True)

        # Startup queries — fire-and-forget; responses arrive on /pboutput.
        await self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        await self._publish_userctrl(USER_CTRL_QUERY_SCHEDULES)
        await self._publish_raw(protocol.encode_upload_robot_config())
        self._last_catalog_query_at = datetime.now(UTC)

        self._rest_poll_task = asyncio.create_task(self._rest_poll_loop())

    async def _login(self) -> None:
        assert self.auth is not None
        if self.email and self.password:
            await self.auth.login_srp(self.email, self.password)
        else:
            raise LymowAuthError("No Lymow credentials configured")
        await self.auth.get_aws_credentials()

    async def stop(self) -> None:
        self._shutting_down = True
        if self._rest_poll_task:
            self._rest_poll_task.cancel()
            try:
                await self._rest_poll_task
            except asyncio.CancelledError:
                pass
        if self.mqtt:
            await self.mqtt.disconnect()
        if self._session:
            await self._session.close()
        self.on_connection_changed(False)

    def is_connected(self) -> bool:
        return bool(self.mqtt and self.mqtt.is_connected)

    # ── Discovery ───────────────────────────────────────────────

    async def list_devices(self) -> list[dict[str, Any]]:
        assert self.rest is not None and self.auth is not None
        devices = await self.rest.get_device_list()
        out = []
        for d in devices:
            thing = d.get("deviceThingName") or d.get("thingName") or d.get("name")
            if not thing:
                continue
            out.append(
                {
                    "deviceThingName": thing,
                    "deviceName": d.get("deviceName")
                    or d.get("displayName")
                    or d.get("name")
                    or "(unnamed)",
                    "deviceState": d.get("deviceState"),
                }
            )
        return out

    # ── Inbound ─────────────────────────────────────────────────

    def _handle_pboutput(self, raw_envelope: bytes) -> None:
        self._last_mqtt_at = datetime.now(UTC)
        self.on_pboutput(raw_envelope)

    # ── Online resolution (REST authoritative, fresh MQTT overrides) ──

    @property
    def rest_online(self) -> bool:
        return self._rest_online

    @property
    def last_mqtt_at(self) -> datetime | None:
        return self._last_mqtt_at

    async def _rest_poll_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(_REST_POLL_INTERVAL.total_seconds())
                await self._do_rest_poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("REST poll cycle failed; retrying next interval")

    async def _do_rest_poll(self) -> None:
        assert self.rest is not None
        info = await self.rest.get_device_info(self.device_id)
        if not info:
            self._rest_online = False
        else:
            ds = info.get("deviceState") or info.get("device_state") or "offline"
            self._rest_online = ds == "online"
            ip = info.get("ipAddress") or info.get("ip_address")
            if ip:
                self._state_ref.setdefault("rest_ip_address", ip)
                self._state_ref["rest_ip_address"] = ip
            fw = (
                info.get("softwareVersion")
                or info.get("software_version")
                or info.get("fwVersion")
                or info.get("fw_version")
            )
            if fw and isinstance(fw, str):
                self._state_ref["rest_firmware_version"] = fw
        self.on_connection_changed(self.is_connected())

    # ── Reconnect (fresh creds + freshly signed WSS URL) ────────

    def _handle_disconnect(self) -> None:
        if self._shutting_down or self._reconnecting:
            return
        _LOGGER.warning("MQTT disconnected for %s — refreshing and reconnecting", self.device_id)
        self.on_connection_changed(False)
        asyncio.get_running_loop().create_task(self._reconnect_with_fresh_creds())

    async def _reconnect_with_fresh_creds(self) -> None:
        if self._shutting_down or self._reconnecting:
            return
        self._reconnecting = True
        backoff = 5
        try:
            while not self._shutting_down:
                try:
                    assert self.mqtt is not None
                    await self.mqtt.disconnect()
                    if self._shutting_down:
                        return
                    await self.mqtt.connect()
                    _LOGGER.info("MQTT reconnected for %s with fresh URL", self.device_id)
                    self.on_connection_changed(True)
                    await self._publish_userctrl(
                        USER_CTRL_QUERY_MAP, with_query_map_flag=True
                    )
                    return
                except Exception as exc:
                    if self._shutting_down:
                        return
                    _LOGGER.warning("Reconnect failed for %s: %s (retry %ss)",
                                    self.device_id, exc, backoff)
                    try:
                        await asyncio.sleep(backoff)
                    except asyncio.CancelledError:
                        return
                    backoff = min(backoff * 2, 300)
        finally:
            self._reconnecting = False

    # ── Lazy QUERY_MAP catalog refire (upstream rules) ──────────

    def maybe_refire_query_map(self, cleanreport_arrived: bool = False) -> None:
        ri = self._state_ref.get("robotInfo")
        new_ws = getattr(ri, "workStatus", None) if ri is not None else None
        transitioned = (
            self._prev_work_status == WORK_STATUS_WAITING
            and new_ws == WORK_STATUS_MOWING
        )
        self._prev_work_status = new_ws

        catalog = self._state_ref.get("zone_catalog")
        complete = (
            catalog is not None
            and len(catalog.zones) > 0
            and catalog.runtime_config is not None
        )
        if complete and not transitioned and not cleanreport_arrived:
            return
        now = datetime.now(UTC)
        since = (now - self._last_catalog_query_at).total_seconds()
        should = transitioned or cleanreport_arrived or (
            self._catalog_retry_count < 5 and since > 60
        )
        if not should:
            return
        if transitioned or cleanreport_arrived:
            self._catalog_retry_count = 0
        else:
            self._catalog_retry_count += 1
        self._last_catalog_query_at = now
        asyncio.get_running_loop().create_task(
            self._publish_userctrl(USER_CTRL_QUERY_MAP, with_query_map_flag=True)
        )

    # ── Publish ─────────────────────────────────────────────────

    async def _publish_userctrl(
        self,
        user_ctrl: int,
        with_query_map_flag: bool = False,
        zone_hash_ids: list[str] | None = None,
    ) -> None:
        if user_ctrl == USER_CTRL_QUERY_MAP and with_query_map_flag:
            raw = protocol.encode_query_map()
        elif user_ctrl == 1 and zone_hash_ids is not None:
            raw = protocol.encode_start_zones(zone_hash_ids)
        else:
            raw = protocol.encode_userctrl(user_ctrl)
        await self.publish_pbinput(raw)

    async def _publish_raw(self, raw: bytes) -> None:
        await self.publish_pbinput(raw)

    async def publish_pbinput(self, raw_pbinput: bytes) -> None:
        if not self.mqtt or not self.mqtt.is_connected:
            raise PublishError("MQTT not connected")
        if not self.mqtt.publish_pbinput(raw_pbinput):
            raise PublishError(f"MQTT publish failed (device {self.device_id})")
