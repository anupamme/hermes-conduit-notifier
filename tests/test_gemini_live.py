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
    monkeypatch.setattr(
        api, "_edge_status_limiter", api._MintLimiter(api._EDGE_STATUS_LIMIT, api._EDGE_STATUS_WINDOW_S)
    )
    monkeypatch.setattr(
        api, "_edge_token_limiter", api._MintLimiter(api._EDGE_TOKEN_LIMIT, api._EDGE_TOKEN_WINDOW_S)
    )
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
    # Force eager sweeping (as if unconditional) so this test still exercises
    # the stale-bucket prune directly; gating behavior has its own test below.
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0], sweep_interval_s=0)
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


class FakeHeaders(dict):
    def get(self, key, default=None):
        return super().get(key.lower(), default)


class FakeClient:
    def __init__(self, host):
        self.host = host


class FakeRequest:
    def __init__(self, client_host=None, headers=None):
        self.client = FakeClient(client_host) if client_host is not None else None
        self.headers = FakeHeaders(headers or {})


def test_client_id_uses_the_peer_host():
    assert api._client_id(FakeRequest(client_host="203.0.113.5")) == "203.0.113.5"


def test_client_id_falls_back_when_transport_has_no_peer_info():
    assert api._client_id(FakeRequest(client_host=None)) == "unknown"


def test_client_id_ignores_forwarded_header_by_default(monkeypatch):
    monkeypatch.setattr(api, "TRUST_PROXY", False)
    request = FakeRequest(client_host="203.0.113.5", headers={"x-forwarded-for": "198.51.100.9"})
    assert api._client_id(request) == "203.0.113.5"


def test_client_id_trusts_forwarded_header_when_enabled(monkeypatch):
    monkeypatch.setattr(api, "TRUST_PROXY", True)
    request = FakeRequest(client_host="203.0.113.5", headers={"x-forwarded-for": "198.51.100.9, 203.0.113.5"})
    assert api._client_id(request) == "198.51.100.9"


def test_client_id_falls_back_when_forwarded_header_is_invalid(monkeypatch):
    monkeypatch.setattr(api, "TRUST_PROXY", True)
    request = FakeRequest(client_host="203.0.113.5", headers={"x-forwarded-for": "not-an-ip"})
    assert api._client_id(request) == "203.0.113.5"


def test_client_id_normalizes_ipv6_to_a_64_prefix():
    first = api._client_id(FakeRequest(client_host="2001:db8:1234:5678:aaaa::1"))
    second = api._client_id(FakeRequest(client_host="2001:db8:1234:5678:bbbb::2"))
    assert first == second == "2001:db8:1234:5678::"


def test_client_id_does_not_collapse_across_a_64_boundary_within_one_56():
    # Documents the accepted /64 tradeoff: a caller delegated a larger /56
    # (or /48) can still get a fresh bucket per /64 within it.
    first = api._client_id(FakeRequest(client_host="2001:db8:1234:5600::1"))
    second = api._client_id(FakeRequest(client_host="2001:db8:1234:5601::1"))
    assert first != second


def test_client_id_unwraps_ipv4_mapped_ipv6_addresses():
    mapped = api._client_id(FakeRequest(client_host="::ffff:203.0.113.5"))
    plain = api._client_id(FakeRequest(client_host="203.0.113.5"))
    assert mapped == plain == "203.0.113.5"


def test_client_id_distinguishes_ipv4_mapped_addresses_by_embedded_ip():
    first = api._client_id(FakeRequest(client_host="::ffff:203.0.113.5"))
    second = api._client_id(FakeRequest(client_host="::ffff:198.51.100.9"))
    assert first != second


def test_client_id_keeps_loopback_distinct_from_other_addresses():
    loopback = api._client_id(FakeRequest(client_host="::1"))
    other_all_zero_prefix = api._client_id(FakeRequest(client_host="::2"))
    assert loopback == "::1"
    assert loopback != other_all_zero_prefix


def test_client_id_warns_once_when_peer_is_loopback_and_trust_proxy_is_unset(monkeypatch):
    monkeypatch.setattr(api, "TRUST_PROXY", False)
    monkeypatch.setattr(api, "_logged_untrusted_proxy_warning", False)
    warnings = []
    monkeypatch.setattr(api.logger, "warning", lambda *args, **kwargs: warnings.append(args))

    api._client_id(FakeRequest(client_host="127.0.0.1"))
    api._client_id(FakeRequest(client_host="127.0.0.1"))

    assert len(warnings) == 1


def test_edge_limiter_isolates_by_caller(client, monkeypatch):
    monkeypatch.setattr(api, "_edge_status_limiter", api._MintLimiter(1, 60.0))
    ids = iter(["client-a", "client-a", "client-b"])
    monkeypatch.setattr(api, "_client_id", lambda request: next(ids))

    first = client.get("/api/plugins/conduit_push/gemini-live/status")
    second = client.get("/api/plugins/conduit_push/gemini-live/status")
    third = client.get("/api/plugins/conduit_push/gemini-live/status")

    assert first.status_code == 200
    assert second.status_code == 429
    # A different caller is unaffected by client-a's exhausted budget.
    assert third.status_code == 200


def test_status_and_token_have_independent_edge_budgets(client, monkeypatch):
    monkeypatch.setattr(api, "_edge_status_limiter", api._MintLimiter(1, 60.0))
    monkeypatch.setattr(api, "_edge_token_limiter", api._MintLimiter(1, 60.0))
    monkeypatch.setattr(api, "_client_id", lambda request: "same-caller")

    status = client.get("/api/plugins/conduit_push/gemini-live/status")
    token = client.post("/api/plugins/conduit_push/gemini-live/token")

    # Exhausting /status's budget doesn't touch /token's, and vice versa.
    assert status.status_code == 200
    assert token.status_code == 200
    assert client.get("/api/plugins/conduit_push/gemini-live/status").status_code == 429
    assert client.post("/api/plugins/conduit_push/gemini-live/token").status_code == 429


def test_status_429_message_does_not_mention_tokens(client, monkeypatch):
    monkeypatch.setattr(api, "_edge_status_limiter", api._MintLimiter(1, 60.0))
    client.get("/api/plugins/conduit_push/gemini-live/status")
    response = client.get("/api/plugins/conduit_push/gemini-live/status")
    assert response.status_code == 429
    assert "token" not in response.json()["detail"].lower()


def test_edge_status_limit_429_sets_retry_after_header(client, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(api, "_edge_status_limiter", api._MintLimiter(1, 60.0, clock=lambda: now[0]))

    assert client.get("/api/plugins/conduit_push/gemini-live/status").status_code == 200

    now[0] = 10.0
    response = client.get("/api/plugins/conduit_push/gemini-live/status")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "50"  # ceil(60 - 10)


def test_edge_token_limit_429_sets_retry_after_and_no_store_header(client, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(api, "_edge_token_limiter", api._MintLimiter(1, 60.0, clock=lambda: now[0]))

    assert client.post("/api/plugins/conduit_push/gemini-live/token").status_code == 200

    now[0] = 10.0
    response = client.post("/api/plugins/conduit_push/gemini-live/token")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "50"
    assert response.headers["cache-control"] == "no-store"


def test_edge_limits_configurable_via_env_vars(monkeypatch):
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_STATUS_LIMIT", "5")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_STATUS_WINDOW_S", "30")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_TOKEN_LIMIT", "7")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_TOKEN_WINDOW_S", "45")
    module = _load_plugin_api()
    assert module._EDGE_STATUS_LIMIT == 5
    assert module._EDGE_STATUS_WINDOW_S == 30.0
    assert module._EDGE_TOKEN_LIMIT == 7
    assert module._EDGE_TOKEN_WINDOW_S == 45.0


def test_edge_limit_env_vars_fall_back_to_defaults_on_invalid_value(monkeypatch):
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_STATUS_LIMIT", "not-a-number")
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_STATUS_WINDOW_S", "-5")
    module = _load_plugin_api()
    assert module._EDGE_STATUS_LIMIT == module.DEFAULT_EDGE_STATUS_LIMIT
    assert module._EDGE_STATUS_WINDOW_S == module.DEFAULT_EDGE_STATUS_WINDOW_S


@pytest.mark.parametrize("bad_window", ["nan", "inf", "-inf"])
def test_edge_limit_env_vars_reject_non_finite_window(monkeypatch, bad_window):
    # nan/inf both pass a bare `value <= 0` check in Python, so without an
    # explicit isfinite guard a typo'd env value would silently produce a
    # limiter whose window comparisons never behave sanely.
    monkeypatch.setenv("CONDUIT_GEMINI_LIVE_EDGE_STATUS_WINDOW_S", bad_window)
    module = _load_plugin_api()
    assert module._EDGE_STATUS_WINDOW_S == module.DEFAULT_EDGE_STATUS_WINDOW_S


def test_mint_limiter_sweep_waits_for_the_interval_even_once_entries_are_stale():
    now = [0.0]
    limiter = api._MintLimiter(1, 60.0, clock=lambda: now[0], sweep_interval_s=120.0)
    limiter.acquire("a")
    now[0] = 61.0  # "a" is stale (61 >= window 60), but only 61s since the
    # last sweep (61 <= sweep_interval_s=120): the gate stays closed.
    limiter.acquire("b")
    assert set(limiter._mints) == {"a", "b"}
    now[0] = 130.0  # 130s since the last sweep: the gate opens and both
    # "a" and "b" are stale by now (age 130 and 69 respectively).
    limiter.acquire("c")
    assert set(limiter._mints) == {"c"}


def test_mint_limiter_lru_eviction_skips_a_still_live_bucket():
    now = [0.0]
    limiter = api._MintLimiter(5, 60.0, clock=lambda: now[0], max_buckets=2, sweep_interval_s=1e9)
    limiter.acquire("a")
    now[0] = 1.0
    limiter.acquire("b")
    # The map is at max_buckets(2); "a" is the LRU entry but still live
    # (age 1 < window 60).
    now[0] = 2.0
    limiter.acquire("c")
    # Eviction is skipped since the LRU bucket ("a") isn't stale yet, so the
    # map temporarily holds more than max_buckets rather than discarding a
    # live counter -- a caller can't force its own budget to reset early by
    # flooding filler keys.
    assert set(limiter._mints) == {"a", "b", "c"}
    now[0] = 100.0  # "a" (age 99) is now genuinely idle; it's still the
    # LRU-most entry since it was never touched again.
    limiter.acquire("d")
    assert "a" not in limiter._mints


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
