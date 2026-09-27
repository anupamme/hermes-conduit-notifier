import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]


def _load_plugin_api():
    # The dashboard imports plugin_api.py by file path, not as a package.
    spec = importlib.util.spec_from_file_location("conduit_plugin_api", ROOT / "dashboard" / "plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_plugin_api()
NOW = datetime(2026, 9, 27, 12, 0, 0, tzinfo=timezone.utc)


def env(**values):
    return lambda key: values.get(key)


def test_manifest_mounts_the_api_under_the_plugin_name():
    manifest = json.loads((ROOT / "dashboard" / "manifest.json").read_text())
    assert manifest["name"] == "conduit_push"
    assert manifest["api"] == "plugin_api.py"
    assert manifest["tab"]["hidden"] is True
    assert (ROOT / "dashboard" / manifest["entry"]).exists()


def test_status_reports_missing_key_without_calling_google():
    assert api.gemini_live_status(env()) == {"available": False, "reason": "no_api_key", "model": "gemini-3.8-live"}


def test_status_accepts_google_api_key_and_model_override():
    status = api.gemini_live_status(env(GOOGLE_API_KEY="g-key", CONDUIT_GEMINI_LIVE_MODEL="models/gemini-4-live"))
    assert status == {"available": True, "model": "gemini-4-live"}


def test_gemini_key_wins_over_google_key():
    assert api.resolve_api_key(env(GEMINI_API_KEY=" gem ", GOOGLE_API_KEY="goog")) == "gem"


def test_token_is_single_use_model_locked_and_short_lived():
    calls = []

    def post(url, key, body):
        calls.append((url, key, body))
        return {"name": "auth_tokens/abc123"}

    result = api.mint_gemini_live_token(env(GEMINI_API_KEY="secret"), post, now=NOW)

    url, key, body = calls[0]
    assert url == api.TOKEN_URL
    assert key == "secret"
    assert body == {
        "uses": 1,
        "expireTime": "2026-09-27T12:30:00Z",
        "newSessionExpireTime": "2026-09-27T12:01:00Z",
        "liveConnectConstraints": {"model": "models/gemini-3.8-live"},
    }
    assert result == {
        "token": "auth_tokens/abc123",
        "expires_at": "2026-09-27T12:30:00Z",
        "new_session_expires_at": "2026-09-27T12:01:00Z",
        "model": "gemini-3.8-live",
        "websocket_url": api.WEBSOCKET_URL,
    }
    assert "secret" not in json.dumps(result)


def test_token_without_key_is_a_503():
    with pytest.raises(api.TokenError) as raised:
        api.mint_gemini_live_token(env(), lambda *_: pytest.fail("must not call Google"), now=NOW)
    assert raised.value.status == 503


def test_token_response_without_a_name_is_a_502():
    with pytest.raises(api.TokenError) as raised:
        api.mint_gemini_live_token(env(GEMINI_API_KEY="secret"), lambda *_: {"error": "nope"}, now=NOW)
    assert raised.value.status == 502


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api, "_env_value", lambda key: {"GEMINI_API_KEY": "secret"}.get(key))
    monkeypatch.setattr(api, "_post_json", lambda url, key, body: {"name": "auth_tokens/xyz"})
    app = FastAPI()
    app.include_router(api.router, prefix="/api/plugins/conduit_push")
    return TestClient(app)


def test_routes_return_status_and_token(client):
    status = client.get("/api/plugins/conduit_push/gemini-live/status").json()
    assert status == {"ok": True, "available": True, "model": "gemini-3.8-live"}

    token = client.post("/api/plugins/conduit_push/gemini-live/token").json()
    assert token["ok"] is True
    assert token["token"] == "auth_tokens/xyz"
    assert "secret" not in json.dumps(token)


def test_token_route_maps_failures_to_http_status(client, monkeypatch):
    monkeypatch.setattr(api, "_env_value", lambda key: None)
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]
