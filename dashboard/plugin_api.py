"""Conduit routes on the Hermes dashboard, mounted at /api/plugins/conduit_push/.

Gemini Live: Conduit talks to Gemini Live directly from the phone, but the
Gemini API key stays on this host. Conduit asks for a short-lived ephemeral
token per Live connection; the token is locked to one model, one use, and
must open its session within a minute.

Routes sit behind the dashboard's own auth, the same as /api/audio/*.
The API key is never returned, logged, or written anywhere.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_MODEL = "gemini-3.8-live"
# Ephemeral tokens are served on v1alpha only (google-genai 2.25 pins it and
# warns on anything else); the env override covers a later v1beta rollout.
DEFAULT_API_VERSION = "v1alpha"
API_VERSION_ENV_VAR = "CONDUIT_GEMINI_LIVE_API_VERSION"
# Same lookup order as Hermes' Gemini TTS provider.
API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
MODEL_ENV_VAR = "CONDUIT_GEMINI_LIVE_MODEL"
TOKEN_LIFETIME = timedelta(minutes=30)
NEW_SESSION_WINDOW = timedelta(minutes=1)
REQUEST_TIMEOUT_S = 15.0
# Each Live connection (including every resume) needs its own token, so allow
# bursts, but stop a looping client from burning the host's Gemini quota.
MINT_LIMIT = 20
MINT_WINDOW_S = 60.0
EDGE_LIMIT_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_LIMIT"
EDGE_WINDOW_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_WINDOW_S"
DEFAULT_EDGE_LIMIT = MINT_LIMIT * 3
DEFAULT_EDGE_WINDOW_S = MINT_WINDOW_S


class TokenError(Exception):
    """A token request failed. ``status`` is the HTTP status to return to Conduit."""

    def __init__(self, status: int, message: str, retry_after_s: Optional[float] = None) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


def _env_value(key: str) -> Optional[str]:
    try:
        from hermes_cli.config import get_env_value
    except ImportError:
        logger.warning("hermes_cli.config unavailable; reading %s from the process environment", key)
        return os.environ.get(key)
    return get_env_value(key)


def resolve_api_key(get_env: Callable[[str], Optional[str]] = _env_value) -> Optional[str]:
    for name in API_KEY_ENV_VARS:
        value = str(get_env(name) or "").strip()
        if value:
            return value
    return None


def resolve_model(get_env: Callable[[str], Optional[str]] = _env_value) -> str:
    value = str(get_env(MODEL_ENV_VAR) or "").strip()
    return value.removeprefix("models/") or DEFAULT_MODEL


def resolve_api_version(get_env: Callable[[str], Optional[str]] = _env_value) -> str:
    value = str(get_env(API_VERSION_ENV_VAR) or "").strip()
    return value if value in ("v1alpha", "v1beta", "v1") else DEFAULT_API_VERSION


def token_url(api_version: str) -> str:
    return f"https://generativelanguage.googleapis.com/{api_version}/auth_tokens"


def websocket_url(api_version: str) -> str:
    return (
        "wss://generativelanguage.googleapis.com/ws/"
        f"google.ai.generativelanguage.{api_version}.GenerativeService.BidiGenerateContentConstrained"
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def token_request_body(model: str, now: datetime) -> Dict[str, Any]:
    return {
        "uses": 1,
        "expireTime": _timestamp(now + TOKEN_LIFETIME),
        "newSessionExpireTime": _timestamp(now + NEW_SESSION_WINDOW),
        # The auth-token service takes the Live setup under this name (the SDK's
        # live_connect_constraints). fieldMask locks only the model; without it
        # Google locks the whole setup and Conduit couldn't send its tools.
        "bidiGenerateContentSetup": {"model": f"models/{model}"},
        "fieldMask": "model",
    }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # A redirect would resend the x-goog-api-key header to wherever it points.
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _post_json(url: str, api_key: str, body: Dict[str, Any]) -> Dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
        method="POST",
    )
    try:
        with _opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google's error body names the problem (bad key, quota) without echoing the key.
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            pass
        raise TokenError(502, f"Google rejected the token request ({exc.code}){': ' + detail if detail else ''}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TokenError(502, f"Could not reach Google: {getattr(exc, 'reason', exc)}")
    except ValueError:
        raise TokenError(502, "Google returned an unreadable token response")


def gemini_live_status(get_env: Optional[Callable[[str], Optional[str]]] = None) -> Dict[str, Any]:
    get_env = get_env or _env_value
    model = resolve_model(get_env)
    if resolve_api_key(get_env) is None:
        return {"available": False, "reason": "no_api_key", "model": model}
    return {"available": True, "model": model}


def mint_gemini_live_token(
    get_env: Optional[Callable[[str], Optional[str]]] = None,
    post: Optional[Callable[[str, str, Dict[str, Any]], Dict[str, Any]]] = None,
    now: Optional[datetime] = None,
    limiter_key: Optional[str] = None,
) -> Dict[str, Any]:
    get_env = get_env or _env_value
    post = post or _post_json
    api_key = resolve_api_key(get_env)
    if api_key is None:
        raise TokenError(503, "GEMINI_API_KEY is not set on this Hermes host")
    if limiter_key is not None:
        # Counted only once the profile resolved and has a key, i.e. for requests that reach Google.
        _mint_limiter.acquire(limiter_key)
    model = resolve_model(get_env)
    now = now or datetime.now(timezone.utc)
    api_version = resolve_api_version(get_env)
    body = token_request_body(model, now)
    payload = post(token_url(api_version), api_key, body)
    token = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        raise TokenError(502, "Google's token response had no token")
    return {
        "token": token,
        "expires_at": body["expireTime"],
        "new_session_expires_at": body["newSessionExpireTime"],
        "model": model,
        "websocket_url": websocket_url(api_version),
    }


class _MintLimiter:
    """Sliding-window cap on requests, keyed by a caller-supplied key."""

    def __init__(self, limit: int, window_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self._mints: Dict[str, deque] = {}
        self._lock = threading.Lock()

    def acquire(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            # Drop buckets whose newest mint has aged out, so idle keys don't pile up.
            for stale in [k for k, q in self._mints.items() if now - q[-1] >= self.window_s]:
                del self._mints[stale]
            mints = self._mints.setdefault(key, deque())
            while mints and now - mints[0] >= self.window_s:
                mints.popleft()
            if len(mints) >= self.limit:
                # Pruning above guarantees mints is non-empty and mints[0] is
                # still inside the window, so this is always > 0.
                retry_after_s = self.window_s - (now - mints[0])
                raise TokenError(
                    429,
                    "Too many Gemini Live token requests; try again shortly",
                    retry_after_s=retry_after_s,
                )
            mints.append(now)


_mint_limiter = _MintLimiter(MINT_LIMIT, MINT_WINDOW_S)


def _positive_int_env(name: str, default: int) -> int:
    """Read a positive int policy value directly from the process environment.

    Edge-limiter policy is process-wide, set once at import time -- not a
    per-request/profile-scoped lookup -- so this intentionally bypasses
    ``_env_value`` (which exists so profile scoping can override a lookup on
    a per-request basis) and reads ``os.environ`` directly.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %d", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r; using default %d", name, raw, default)
        return default
    return value


def _positive_float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    if value <= 0:
        logger.warning("Ignoring non-positive %s=%r; using default %s", name, raw, default)
        return default
    return value


# Edge-level cap, keyed per calling client (see _client_id) rather than per
# profile, so it can't be bypassed by rotating profile names, and it also
# covers /status (which has no per-profile limiter at all) and requests that
# fail before ever reaching Google (e.g. bad profile, missing key).
#
# Unlike a single global bucket, keying by caller means one caller
# exhausting its budget cannot 429 unrelated callers. /status and /token
# intentionally share one bucket per caller (see the route handlers below),
# so this is one ceiling on a caller's overall use of the Gemini Live edge
# surface, not two independent ones.
#
# Like _mint_limiter, this state is in-process only: if this dashboard ever
# runs with multiple worker processes, each worker enforces its own
# independent per-caller budget rather than one budget shared across
# workers.
_EDGE_LIMIT = _positive_int_env(EDGE_LIMIT_ENV_VAR, DEFAULT_EDGE_LIMIT)
_EDGE_WINDOW_S = _positive_float_env(EDGE_WINDOW_ENV_VAR, DEFAULT_EDGE_WINDOW_S)
_edge_limiter = _MintLimiter(_EDGE_LIMIT, _EDGE_WINDOW_S)


def _profile_scope(profile: Optional[str]):
    """Resolve .env/config for ``profile`` the way /api/audio/* does.

    Fails closed: a requested profile that can't be scoped must never fall back
    to the default profile's key.
    """
    if not profile:
        return nullcontext()
    try:
        from hermes_cli.web_server_profiles import _config_profile_scope
    except ImportError:
        logger.warning("Cannot scope Gemini Live request to profile %r: profile scoping unavailable", profile)
        raise TokenError(503, "This Hermes version can't resolve per-profile keys")
    return _config_profile_scope(profile)


async def _run_scoped(profile: Optional[str], fn: Callable[[], Dict[str, Any]]) -> Dict[str, Any]:
    def scoped() -> Dict[str, Any]:
        with _profile_scope(profile):
            return fn()

    return await asyncio.get_running_loop().run_in_executor(None, scoped)


def _client_id(request: Request) -> str:
    """Best-effort per-caller key for the edge limiter.

    No authenticated identity reaches this module (auth happens in the host
    package that mounts these routes -- see the module docstring), so the
    peer address Starlette resolved for the connection is the only signal
    available here. Deliberately does not trust X-Forwarded-For/X-Real-IP:
    this repo has no known trusted-proxy configuration, and trusting a
    client-supplied header would let a caller mint an unlimited number of
    fresh buckets just by varying it -- the exact bypass this limiter exists
    to close for ``profile``.
    """
    client = request.client
    if client is None or not client.host:
        return "unknown"
    return client.host


def _retry_after_header(retry_after_s: Optional[float]) -> Dict[str, str]:
    if retry_after_s is None:
        return {}
    return {"Retry-After": str(max(1, math.ceil(retry_after_s)))}


@router.get("/gemini-live/status")
async def get_gemini_live_status(request: Request, profile: Optional[str] = None) -> Dict[str, Any]:
    try:
        _edge_limiter.acquire(_client_id(request))
        return {"ok": True, **(await _run_scoped(profile, gemini_live_status))}
    except TokenError as exc:
        raise HTTPException(
            status_code=exc.status,
            detail=str(exc),
            headers=_retry_after_header(exc.retry_after_s),
        )


@router.post("/gemini-live/token")
async def create_gemini_live_token(
    request: Request, response: Response, profile: Optional[str] = None
) -> Dict[str, Any]:
    # The body is a usable credential: keep it out of any cache on the way.
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    try:
        # Counted up front, before profile resolution, so it can't be dodged
        # by probing with invalid/rotating profile names. Shares one bucket
        # with /status per caller (_client_id) rather than a separate
        # bucket per route, capping a caller's overall use of the Gemini
        # Live edge surface, not each endpoint independently.
        _edge_limiter.acquire(_client_id(request))
        result = await _run_scoped(profile, lambda: mint_gemini_live_token(limiter_key=profile or ""))
    except TokenError as exc:
        logger.warning("Gemini Live token request failed: %s", exc)
        raise HTTPException(
            status_code=exc.status,
            detail=str(exc),
            headers={"Cache-Control": "no-store", **_retry_after_header(exc.retry_after_s)},
        )
    return {"ok": True, **result}
