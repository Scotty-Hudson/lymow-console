"""End-to-end API tests: auth, state, map, commands, settings, WS.

Everything runs against SimulatedMower — these tests ALSO prove the
command pipeline works with zero network dependencies outside ASGI.
"""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        provider="sim",
        data_dir=tmp_path,
        session_secret="test-session-secret",
        _env_file=None,
    )


@pytest.fixture
def sync_client(settings):
    app = create_app(settings)
    with TestClient(app) as client:  # runs lifespan
        yield client


async def login(client: AsyncClient) -> None:
    r = await client.post("/api/auth/login", json={"password": "console-pass"})
    assert r.status_code == 200


@pytest.fixture
async def api(settings):
    """Async client with lifespan running and a logged-in session."""
    app = create_app(settings)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            await login(client)
            yield client


class TestAuth:
    def test_setup_and_login_flow(self, sync_client):
        r = sync_client.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json() == {"needs_setup": True, "authenticated": False}
        r = sync_client.post("/api/auth/login", json={"password": "first-pass"})
        assert r.status_code == 200
        r = sync_client.get("/api/auth/status")
        assert r.json()["needs_setup"] is False
        assert r.json()["authenticated"] is True

    def test_wrong_password_rejected(self, sync_client):
        sync_client.post("/api/auth/login", json={"password": "right-one"})
        sync_client.post("/api/auth/logout")
        r = sync_client.post("/api/auth/login", json={"password": "wrong"})
        assert r.status_code == 401

    def test_api_requires_auth(self, settings):
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/api/state").status_code == 401
            assert client.get("/api/map").status_code == 401
            assert client.post(
                "/api/commands/pause", json={}
            ).status_code == 401

    def test_real_provider_unconfigured_never_connects(self, tmp_path):
        """provider=real with no credentials/device: the app must boot and
        stay offline — no connection attempt, no crash."""
        s = Settings(provider="real", data_dir=tmp_path,
                     session_secret="k", _env_file=None)
        app = create_app(s)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"password": "pw"})
            snap = client.get("/api/state").json()
            assert snap["simulator"] is False
            assert snap["online"] is False
            assert snap["telemetry"].get("battery") is None
            # control commands refuse cleanly (transport not connected)
            r = client.post("/api/commands/pause", json={})
            assert r.status_code == 409  # matrix: unknown state, no buttons


class TestStateAndMap:
    async def test_state_snapshot_shape(self, api):
        r = await api.get("/api/state")
        assert r.status_code == 200
        snap = r.json()
        assert snap["simulator"] is True
        assert snap["online"] is True
        assert snap["telemetry"]["battery"] == 72
        assert snap["activity"] == "docked"
        assert snap["allowed_actions"]["start_mowing"] is not None
        assert snap["map_ready"] is True

    async def test_map_document_has_zones_dock_and_gaps(self, api):
        r = await api.get("/api/map")
        m = r.json()
        assert m["available"] is True
        assert {z["name"] for z in m["zones"]} == {"Front Lawn", "Back Lawn"}
        assert m["dock"]["x"] == pytest.approx(4.5)
        feats = {u["feature"] for u in m["unsupported"]}
        assert feats == {"no_go_zones", "mowing_path"}


class TestCommands:
    async def test_matrix_gating_rejects_pause_while_docked(self, api):
        r = await api.post("/api/commands/pause")
        assert r.status_code == 409

    async def test_start_then_state_moves_to_mowing(self, api):
        r = await api.post("/api/commands/start", json={})
        assert r.status_code == 200
        assert r.json()["outcome"] == "sent"
        r = await api.get("/api/state")
        assert r.json()["telemetry"]["work_status"] == 2  # WORK_STATUS_MOWING

    async def test_zone_start_by_name(self, api):
        r = await api.post("/api/commands/start", json={"zones": ["Back Lawn"]})
        assert r.status_code == 200
        r = await api.get("/api/state")
        assert [z["name"] for z in r.json()["task_zones"]] == ["Back Lawn"]

    async def test_zone_start_unknown_zone_rejected(self, api):
        r = await api.post("/api/commands/start", json={"zones": ["Nope"]})
        assert r.status_code == 409

    async def test_destructive_requires_confirm(self, api):
        r = await api.post("/api/commands/dock-cancel", json={"confirm": False})
        assert r.status_code == 409
        r = await api.post("/api/commands/dock-cancel", json={"confirm": True})
        assert r.status_code == 200

    async def test_force_reinit_requires_confirm(self, api):
        assert (await api.post("/api/commands/force-reinit", json={})).status_code == 409
        r = await api.post("/api/commands/force-reinit", json={"confirm": True})
        assert r.status_code == 200

    async def test_every_command_is_audited(self, api):
        await api.post("/api/commands/pause")          # rejected (docked)
        await api.post("/api/commands/start", json={})
        await api.post("/api/commands/dock-cancel", json={"confirm": True})
        rows = (await api.get("/api/audit")).json()
        actions = {r["action"] for r in rows}
        assert "start" in actions and "dock_cancel" in actions
        outcomes = {r["outcome"] for r in rows}
        assert any(o.startswith("rejected") for o in outcomes)
        assert all(r["simulator"] is True for r in rows)

    async def test_rr_config_roundtrip(self, api):
        r = await api.put("/api/config/return-to-dock", json={
            "enableRr": True, "rechargeBat": 20, "resumeBat": 95,
        })
        assert r.status_code == 200

    async def test_rr_config_validation(self, api):
        r = await api.put("/api/config/return-to-dock", json={
            "enableRr": True, "rechargeBat": 250,
        })
        assert r.status_code == 422

    async def test_query_map_and_schedules(self, api):
        r = await api.post("/api/commands/query-map")
        assert r.status_code == 200
        r = await api.post("/api/commands/query-schedules")
        assert r.status_code == 200


class TestSettings:
    async def test_cloud_credentials_write_only(self, api):
        r = await api.put("/api/settings/cloud", json={
            "lymow_username": "user@example.com",
            "lymow_password": "super-secret",
            "device_thing_name": "thing-9",
        })
        assert r.status_code == 200
        r = await api.get("/api/settings")
        s = r.json()
        assert s["lymow_username"] == "user@example.com"
        assert s["lymow_password"] == "***"
        assert "super-secret" not in r.text
        assert "console_password_hash" not in s
        r = await api.delete("/api/settings/cloud")
        assert r.status_code == 200

    async def test_region_validated_and_persisted(self, api):
        r = await api.put("/api/settings/cloud", json={
            "lymow_username": "u@e.com", "lymow_password": "x",
            "region": "moon-base-1",
        })
        assert r.status_code == 422
        r = await api.put("/api/settings/cloud", json={
            "lymow_username": "u@e.com", "lymow_password": "x",
            "region": "us-east-2",
        })
        assert r.status_code == 200
        s = (await api.get("/api/settings")).json()
        assert s["lymow_region"] == "us-east-2"

    async def test_discovery_requires_stored_credentials(self, api):
        r = await api.get("/api/discovery/devices")
        assert r.status_code == 409  # nothing configured yet

    async def test_discovery_roundtrip_with_stubbed_provider(self, api, monkeypatch):
        """No real cloud contact in tests: stub login + device list."""
        from app.services.real_provider import RealMowerProvider

        async def fake_login(self):
            pass

        async def fake_list(self):
            return [{"deviceThingName": "thing-7", "deviceName": "One Plus",
                     "deviceState": "online"}]

        monkeypatch.setattr(RealMowerProvider, "_login", fake_login)
        monkeypatch.setattr(RealMowerProvider, "list_devices", fake_list)
        await api.put("/api/settings/cloud", json={
            "lymow_username": "u@e.com", "lymow_password": "x",
            "region": "eu-west-1",
        })
        r = await api.get("/api/discovery/devices")
        assert r.status_code == 200
        assert r.json()[0]["deviceThingName"] == "thing-7"

    async def test_discovery_degrades_on_login_failure(self, api, monkeypatch):
        from app.services.real_provider import RealMowerProvider
        from app.lymow.auth import LymowAuthError

        async def bad_login(self):
            raise LymowAuthError("bad credentials")

        monkeypatch.setattr(RealMowerProvider, "_login", bad_login)
        await api.put("/api/settings/cloud", json={
            "lymow_username": "u@e.com", "lymow_password": "x",
            "region": "eu-west-1",
        })
        r = await api.get("/api/discovery/devices")
        assert r.status_code == 502
        assert "super" not in r.text  # no credential echo in errors


class TestGoogleSignIn:
    """Federated (Google) flow: url helper, code paste exchange, secrets."""

    async def test_oauth_url_endpoint(self, api):
        r = await api.get("/api/settings/oauth-url?region=eu-west-1")
        assert r.status_code == 200
        body = r.json()
        assert body["url"].startswith("https://eu-auth.lymow.com/login?")
        assert "myapp://callback/" in body["redirect_hint"]

    async def test_oauth_exchange_stores_refresh_token_masked(self, api, monkeypatch):
        from app.lymow.auth import CognitoAuth
        from app.services.real_provider import RealMowerProvider

        async def fake_exchange(self, code):
            assert code == "abc123"
            self.id_token, self.access_token = "id", "acc"
            self.refresh_token = "REFRESH-SECRET"

        async def fake_aws(self):
            pass

        async def fake_list(self):
            return [{"deviceThingName": "thing-1", "deviceName": "One Plus",
                     "deviceState": "online"}]

        monkeypatch.setattr(CognitoAuth, "exchange_oauth_code", fake_exchange)
        monkeypatch.setattr(CognitoAuth, "get_aws_credentials", fake_aws)
        monkeypatch.setattr(RealMowerProvider, "list_devices", fake_list)

        r = await api.post("/api/settings/oauth-exchange", json={
            "pasted": "myapp://callback/?code=abc123&state=z",
            "region": "eu-west-1",
        })
        assert r.status_code == 200
        assert r.json()["devices"][0]["deviceThingName"] == "thing-1"

        s = (await api.get("/api/settings")).json()
        assert s["lymow_auth_method"] == "google"
        assert s["lymow_refresh_token"] == "***"
        assert "REFRESH-SECRET" not in (await api.get("/api/settings")).text

    async def test_oauth_exchange_rejects_paste_without_code(self, api):
        r = await api.post("/api/settings/oauth-exchange", json={
            "pasted": "myapp://callback/?error=access_denied",
            "region": "eu-west-1",
        })
        assert r.status_code == 422

    async def test_password_method_requires_both_fields(self, api):
        r = await api.put("/api/settings/cloud", json={
            "lymow_username": "u@e.com", "auth_method": "password",
        })
        assert r.status_code == 422

    def test_google_boot_without_device_stays_offline(self, tmp_path, monkeypatch):
        """google auth configured but no pinned device: auto-discovery is
        attempted, fails (stubbed), and the console must NOT connect."""
        from app.lymow.auth import CognitoAuth, LymowAuthError

        async def explode_refresh(self):
            raise LymowAuthError("network disabled in this test")

        monkeypatch.setattr(CognitoAuth, "refresh_tokens", explode_refresh)

        s = Settings(provider="real", data_dir=tmp_path,
                     session_secret="k", _env_file=None)
        import asyncio
        from app.db import Database

        async def seed():
            db = Database(s.db_path)
            await db.open()
            await db.set_setting("lymow_auth_method", "google")
            await db.set_setting("lymow_refresh_token", "rt")
            await db.set_setting("lymow_region", "eu-west-1")
            await db.close()

        asyncio.run(seed())
        app = create_app(s)
        with TestClient(app) as client:
            client.post("/api/auth/login", json={"password": "pw"})
            snap = client.get("/api/state").json()
            assert snap["online"] is False


class TestWebSocket:
    def test_anonymous_ws_closed(self, sync_client):
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc:
            with sync_client.websocket_connect("/ws/telemetry") as ws:
                ws.receive_json()
        assert exc.value.code == 4401

    def test_ws_delivers_state_with_token(self, sync_client):
        token = sync_client.app.state.session_signer.create()  # type: ignore[attr-defined]
        with sync_client.websocket_connect(f"/ws/telemetry?token={token}") as ws:
            msg = ws.receive_json()
        assert msg["type"] == "state"
        assert msg["payload"]["simulator"] is True
        assert msg["payload"]["telemetry"]["battery"] == 72


class TestCamera:
    async def test_camera_status_simulated(self, api):
        r = await api.get("/api/camera/status")
        c = r.json()
        assert c["mode"] == "simulated"
        assert c["available"] is True and c["live"] is True
