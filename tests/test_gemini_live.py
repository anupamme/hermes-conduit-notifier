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
    assert url == "https://generativelanguage.googleapis.com/v1alpha/auth_tokens"
    assert key == "secret"
    assert body == {
        "uses": 1,
        "expireTime": "2026-09-27T12:30:00Z",
        "newSessionExpireTime": "2026-09-27T12:01:00Z",
        "bidiGenerateContentSetup": {"model": "models/gemini-3.8-live"},
        "fieldMask": "model",
    }
    assert result == {
        "token": "auth_tokens/abc123",
        "expires_at": "2026-09-27T12:30:00Z",
        "new_session_expires_at": "2026-09-27T12:01:00Z",
        "model": "gemini-3.8-live",
        "websocket_url": "wss://generativelanguage.googleapis.com/ws/"
        "google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContentConstrained",
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
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(api.MINT_LIMIT, api.MINT_WINDOW_S))
    monkeypatch.setattr(api, "_edge_limiter", api._MintLimiter(api._EDGE_LIMIT, api._EDGE_WINDOW_S))
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


def test_token_response_is_not_cacheable(client):
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.headers["cache-control"] == "no-store"


def test_requested_profile_fails_closed_when_it_cannot_be_scoped(client):
    # No hermes_cli in the test environment, so profile scoping is unavailable:
    # the request must not fall back to the default profile's key.
    for method, path in (("post", "token"), ("get", "status")):
        response = getattr(client, method)(f"/api/plugins/conduit_push/gemini-live/{path}?profile=coder")
        assert response.status_code == 503


def test_token_route_rate_limits_per_profile(client, monkeypatch):
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(2, 60.0))
    codes = [client.post("/api/plugins/conduit_push/gemini-live/token").status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_mint_limiter_frees_slots_after_the_window():
    now = [0.0]
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0])
    limiter.acquire("default")
    with pytest.raises(api.TokenError) as raised:
        limiter.acquire("default")
    assert raised.value.status == 429
    limiter.acquire("coder")
    now[0] = 60.0
    limiter.acquire("default")
    assert list(limiter._mints) == ["default"]


def test_mint_limiter_reports_retry_after_on_429():
    now = [10.0]
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0])
    limiter.acquire("k")
    now[0] = 25.0  # 15s into the 60s window
    with pytest.raises(api.TokenError) as raised:
        limiter.acquire("k")
    assert raised.value.retry_after_s == pytest.approx(45.0)


def test_client_id_uses_the_peer_host():
    class FakeClient:
        host = "203.0.113.5"

    class FakeRequest:
        client = FakeClient()

    assert api._client_id(FakeRequest()) == "203.0.113.5"


def test_client_id_falls_back_when_transport_has_no_peer_info():
    class FakeRequest:
        client = None

    assert api._client_id(FakeRequest()) == "unknown"


def test_edge_limiter_isolates_by_caller(client, monkeypatch):
    monkeypatch.setattr(api, "_edge_limiter", api._MintLimiter(1, 60.0))
    ids = iter(["client-a", "client-a", "client-b"])
    monkeypatch.setattr(api, "_client_id", lambda request: next(ids))

    first = client.get("/api/plugins/conduit_push/gemini-live/status")
    second = client.get("/api/plugins/conduit_push/gemini-live/status")
    third = client.get("/api/plugins/conduit_push/gemini-live/status")

    assert first.status_code == 200
    assert second.status_code == 429
    # A different caller is unaffected by client-a's exhausted budget.
    assert third.status_code == 200


def test_status_and_token_share_the_same_edge_budget(client, monkeypatch):
    monkeypatch.setattr(api, "_edge_limiter", api._MintLimiter(1, 60.0))
    monkeypatch.setattr(api, "_client_id", lambda request: "same-caller")

    status = client.get("/api/plugins/conduit_push/gemini-live/status")
    token = client.post("/api/plugins/conduit_push/gemini-live/token")

    assert status.status_code == 200
    # Same caller, same bucket: the /status call already spent the budget.
    assert token.status_code == 429


def test_edge_limit_429_sets_retry_after_header(client, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(api, "_edge_limiter", api._MintLimiter(1, 60.0, clock=lambda: now[0]))
    monkeypatch.setattr(api, "_client_id", lambda request: "same-caller")

    assert client.get("/api/plugins/conduit_push/gemini-live/status").status_code == 200

    now[0] = 10.0
    status_response = client.get("/api/plugins/conduit_push/gemini-live/status")
    assert status_response.status_code == 429
    assert status_response.headers["retry-after"] == "50"  # ceil(60 - 10)

    token_response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert token_response.status_code == 429
    assert token_response.headers["retry-after"] == "50"
    assert token_response.headers["cache-control"] == "no-store"


def test_edge_limit_configurable_via_env_vars(monkeypatch):
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_LIMIT", "5")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_WINDOW_S", "30")
    module = _load_plugin_api()
    assert module._EDGE_LIMIT == 5
    assert module._EDGE_WINDOW_S == 30.0


def test_edge_limit_env_vars_fall_back_to_defaults_on_invalid_value(monkeypatch):
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_LIMIT", "not-a-number")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_WINDOW_S", "-5")
    module = _load_plugin_api()
    assert module._EDGE_LIMIT == module.DEFAULT_EDGE_LIMIT
    assert module._EDGE_WINDOW_S == module.DEFAULT_EDGE_WINDOW_S


def test_requests_that_never_reach_google_do_not_use_the_mint_budget(client, monkeypatch):
    monkeypatch.setattr(api, "_mint_limiter", api._MintLimiter(1, 60.0))
    for _ in range(3):
        assert client.post("/api/plugins/conduit_push/gemini-live/token?profile=nope").status_code == 503
    assert api._mint_limiter._mints == {}
    assert client.post("/api/plugins/conduit_push/gemini-live/token").status_code == 200


def test_google_http_error_becomes_a_502_without_the_key(monkeypatch):
    import io
    import urllib.error

    def fail(request, timeout):
        body = io.BytesIO(json.dumps({"error": {"message": "API key not valid"}}).encode())
        raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, body)

    monkeypatch.setattr(api._opener, "open", fail)
    with pytest.raises(api.TokenError) as raised:
        api._post_json(api.token_url("v1alpha"), "secret", {})
    assert raised.value.status == 502
    assert "API key not valid" in str(raised.value)
    assert "secret" not in str(raised.value)


def test_redirects_are_not_followed():
    assert api._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example") is None


def test_token_route_maps_failures_to_http_status(client, monkeypatch):
    monkeypatch.setattr(api, "_env_value", lambda key: None)
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.status_code == 503
    assert "GEMINI_API_KEY" in response.json()["detail"]


def test_api_version_override_moves_both_urls():
    calls = []
    result = api.mint_gemini_live_token(
        env(GEMINI_API_KEY="secret", CONDUIT_GEMINI_LIVE_API_VERSION="v1beta"),
        lambda url, key, body: calls.append(url) or {"name": "auth_tokens/t"}, now=NOW)
    assert calls == ["https://generativelanguage.googleapis.com/v1beta/auth_tokens"]
    assert ".v1beta.GenerativeService." in result["websocket_url"]


def test_unknown_api_version_falls_back_to_v1alpha():
    assert api.resolve_api_version(env(CONDUIT_GEMINI_LIVE_API_VERSION="../evil")) == "v1alpha"
