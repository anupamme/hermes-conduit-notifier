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
import ipaddress
import json
import logging
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, deque
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional, TypeVar

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
EDGE_STATUS_LIMIT_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_STATUS_LIMIT"
EDGE_STATUS_WINDOW_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_STATUS_WINDOW_S"
EDGE_TOKEN_LIMIT_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_TOKEN_LIMIT"
EDGE_TOKEN_WINDOW_ENV_VAR = "CONDUIT_GEMINI_LIVE_EDGE_TOKEN_WINDOW_S"
TRUST_PROXY_ENV_VAR = "CONDUIT_GEMINI_LIVE_TRUST_PROXY"
# /status is a cheap, side-effect-free read with no per-profile limiter of its
# own, so its edge budget is generous -- sized to comfortably exceed normal
# dashboard polling cadence rather than to closely ration usage.
DEFAULT_EDGE_STATUS_LIMIT = 60
DEFAULT_EDGE_STATUS_WINDOW_S = 60.0
# /token feeds Google quota, so its edge budget (enforced before profile
# resolution) matches the per-profile MINT_LIMIT/MINT_WINDOW_S it backstops,
# rather than being loosened just because it shares infrastructure with
# /status. This is per caller address, not per profile, so a legitimate
# client using several profiles -- or several users behind one NAT/CGNAT
# address -- share one budget by default; operators in that situation
# should raise CONDUIT_GEMINI_LIVE_EDGE_TOKEN_LIMIT.
DEFAULT_EDGE_TOKEN_LIMIT = MINT_LIMIT
DEFAULT_EDGE_TOKEN_WINDOW_S = MINT_WINDOW_S
# Same strict "1" check as relay's TRUST_PROXY (relay/src/server.mjs):
# X-Forwarded-For is only trusted when explicitly told this host sits behind
# a reverse proxy that overwrites/strips any client-supplied copy of it.
# Deliberately its own setting, prefixed like every other var in this file,
# rather than sharing the relay's TRUST_PROXY: the relay is a separate,
# independently-deployed service (a standalone push-notification relay) with
# no guarantee it sits behind the same proxy as this dashboard. An operator
# running both behind one proxy needs to set both flags.
TRUST_PROXY = os.environ.get(TRUST_PROXY_ENV_VAR) == "1"


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

    def __init__(
        self,
        limit: int,
        window_s: float,
        clock: Callable[[], float] = time.monotonic,
        max_buckets: int = 10_000,
        sweep_interval_s: float = 30.0,
    ) -> None:
        self.limit = limit
        self.window_s = window_s
        self.clock = clock
        self.max_buckets = max_buckets
        self.sweep_interval_s = sweep_interval_s
        self._mints: "OrderedDict[str, deque]" = OrderedDict()
        self._lock = threading.Lock()
        self._last_sweep_at = float("-inf")

    def acquire(self, key: str) -> None:
        now = self.clock()
        with self._lock:
            # Time-gated sweep, independent of size (like relay's
            # enforceRateLimit), so keeping the dict artificially small
            # can't be used to dodge the gate.
            if now - self._last_sweep_at > self.sweep_interval_s:
                self._last_sweep_at = now
                for stale in [k for k, q in self._mints.items() if now - q[-1] >= self.window_s]:
                    del self._mints[stale]
            if key not in self._mints and len(self._mints) >= self.max_buckets:
                # Hard cap on tracked buckets bounds memory even within one
                # sweep interval, when keys are attacker-influenced
                # addresses rather than bounded profile names. Only evict the
                # least-recently-used bucket if it's actually idle (its own
                # newest mint has aged out of the window) -- otherwise a
                # caller could flood distinct filler keys to force its own
                # live, still-in-window counter to be evicted early,
                # resetting its budget ahead of schedule. If the LRU bucket
                # is still live, skip eviction for this call; the map
                # temporarily exceeds max_buckets rather than discarding a
                # live counter, and the time-gated sweep above still bounds
                # long-term growth.
                lru_key, lru_mints = next(iter(self._mints.items()))
                if now - lru_mints[-1] >= self.window_s:
                    del self._mints[lru_key]
            mints = self._mints.setdefault(key, deque())
            self._mints.move_to_end(key)
            while mints and now - mints[0] >= self.window_s:
                mints.popleft()
            if len(mints) >= self.limit:
                # Pruning above guarantees mints is non-empty and mints[0] is
                # still inside the window, so this is always > 0.
                retry_after_s = self.window_s - (now - mints[0])
                raise TokenError(
                    429,
                    "Too many Gemini Live requests; try again shortly",
                    retry_after_s=retry_after_s,
                )
            mints.append(now)


_mint_limiter = _MintLimiter(MINT_LIMIT, MINT_WINDOW_S)


_T = TypeVar("_T", int, float)


def _positive_env(name: str, default: _T, cast: Callable[[str], _T]) -> _T:
    """Read a positive policy value directly from the process environment.

    Edge-limiter policy is process-wide, set once at import time -- not a
    per-request/profile-scoped lookup -- so this intentionally bypasses
    ``_env_value`` (which exists so profile scoping can override a lookup on
    a per-request basis) and reads ``os.environ`` directly.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %s", name, raw, default)
        return default
    # math.isfinite rejects "nan"/"inf": both pass a bare `value <= 0` check
    # (nan compares False to everything; inf compares False to <= 0), and a
    # non-finite window/limit would silently break every comparison in
    # _MintLimiter (a caller could get stuck 429'd forever, or never limited
    # at all).
    if not math.isfinite(value) or value <= 0:
        logger.warning("Ignoring non-finite/non-positive %s=%r; using default %s", name, raw, default)
        return default
    return value


# Edge-level caps, keyed per calling client (see _client_id) rather than per
# profile, so they can't be bypassed by rotating profile names, and they also
# cover requests that fail before ever reaching Google (e.g. bad profile,
# missing key). Keying by caller means one caller exhausting its budget
# cannot 429 unrelated callers. /status and /token get independent budgets
# (rather than sharing one) so routine status polling can't starve token
# minting.
#
# Like _mint_limiter, this state is in-process only: if this dashboard ever
# runs with multiple worker processes, each worker enforces its own
# independent per-caller budget rather than one budget shared across
# workers.
_EDGE_STATUS_LIMIT = _positive_env(EDGE_STATUS_LIMIT_ENV_VAR, DEFAULT_EDGE_STATUS_LIMIT, int)
_EDGE_STATUS_WINDOW_S = _positive_env(EDGE_STATUS_WINDOW_ENV_VAR, DEFAULT_EDGE_STATUS_WINDOW_S, float)
_edge_status_limiter = _MintLimiter(_EDGE_STATUS_LIMIT, _EDGE_STATUS_WINDOW_S)

_EDGE_TOKEN_LIMIT = _positive_env(EDGE_TOKEN_LIMIT_ENV_VAR, DEFAULT_EDGE_TOKEN_LIMIT, int)
_EDGE_TOKEN_WINDOW_S = _positive_env(EDGE_TOKEN_WINDOW_ENV_VAR, DEFAULT_EDGE_TOKEN_WINDOW_S, float)
_edge_token_limiter = _MintLimiter(_EDGE_TOKEN_LIMIT, _EDGE_TOKEN_WINDOW_S)


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


def _bucket_key_for_ip(addr: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> str:
    if addr.version == 6:
        mapped = addr.ipv4_mapped
        if mapped is not None:
            # ::ffff:a.b.c.d -- an IPv4 caller seen through a dual-stack
            # socket. Its first 64 bits are always zero regardless of the
            # embedded address, so without this every IPv4(-mapped) caller
            # would collapse into one "::" bucket.
            return str(mapped)
        if addr.is_loopback:
            # ::1's first 64 bits are also all zero; keep it out of the "::"
            # bucket other all-zero-prefix addresses would otherwise share.
            return str(addr)
        # Collapse to the routed /64 -- a deliberate accuracy/abuse
        # tradeoff: this narrows, but (for a caller routinely delegated a
        # larger /56 or /48) doesn't eliminate, rotation within a caller's
        # prefix, in exchange for not bucketing together unrelated
        # customers who merely share a larger upstream allocation.
        return str(ipaddress.ip_network(f"{addr}/64", strict=False).network_address)
    return str(addr)


_logged_untrusted_proxy_warning = False


def _warn_about_shared_edge_bucket_once(host: str) -> None:
    global _logged_untrusted_proxy_warning
    if _logged_untrusted_proxy_warning:
        return
    _logged_untrusted_proxy_warning = True
    logger.warning(
        "Gemini Live edge rate limiter saw a loopback/private peer address (%s) with "
        "%s unset. If this dashboard is reached through a reverse proxy, SSH tunnel, or "
        "similar, every caller may appear as this one address and share a single "
        "rate-limit bucket. Set %s=1 only if that proxy overwrites (not appends) "
        "X-Forwarded-For with the real client address.",
        host,
        TRUST_PROXY_ENV_VAR,
        TRUST_PROXY_ENV_VAR,
    )


def _client_id(request: Request) -> str:
    """Best-effort per-caller key for the edge limiters.

    No authenticated identity reaches this module (auth happens in the host
    package that mounts these routes -- see the module docstring), so the
    caller's address is the only signal available here.

    X-Forwarded-For is trusted only when CONDUIT_GEMINI_LIVE_TRUST_PROXY=1 is
    set -- i.e. only when this dashboard is known to sit behind a reverse
    proxy that OVERWRITES any client-supplied copy of that header with the
    real connecting address (same trust model as relay's TRUST_PROXY).
    Taking the first hop is only safe under that assumption: an
    append-style config (e.g. nginx's default $proxy_add_x_forwarded_for)
    leaves an attacker-supplied value first and fully defeats the limiter,
    so TRUST_PROXY=1 must never be paired with an append-style proxy.
    Without TRUST_PROXY, a caller could vary the header per request to mint
    an unlimited number of fresh buckets, defeating the limiter entirely.
    """
    if TRUST_PROXY:
        forwarded = request.headers.get("x-forwarded-for", "")
        first_hop = forwarded.split(",", 1)[0].strip()
        if first_hop:
            try:
                return _bucket_key_for_ip(ipaddress.ip_address(first_hop))
            except ValueError:
                pass  # not a valid address; fall through to the raw peer
    client = request.client
    if client is None or not client.host:
        return "unknown"
    try:
        addr = ipaddress.ip_address(client.host)
    except ValueError:
        return client.host
    if not TRUST_PROXY and (addr.is_loopback or addr.is_private):
        _warn_about_shared_edge_bucket_once(client.host)
    return _bucket_key_for_ip(addr)


def _retry_after_header(retry_after_s: Optional[float]) -> Dict[str, str]:
    if retry_after_s is None:
        return {}
    return {"Retry-After": str(max(1, math.ceil(retry_after_s)))}


@router.get("/gemini-live/status")
async def get_gemini_live_status(request: Request, profile: Optional[str] = None) -> Dict[str, Any]:
    try:
        _edge_status_limiter.acquire(_client_id(request))
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
        # by probing with invalid/rotating profile names. Uses its own
        # per-caller budget, independent of /status's, so routine status
        # polling can't consume the allowance a client needs to mint tokens.
        _edge_token_limiter.acquire(_client_id(request))
        result = await _run_scoped(profile, lambda: mint_gemini_live_token(limiter_key=profile or ""))
    except TokenError as exc:
        logger.warning("Gemini Live token request failed: %s", exc)
        raise HTTPException(
            status_code=exc.status,
            detail=str(exc),
            headers={"Cache-Control": "no-store", **_retry_after_header(exc.retry_after_s)},
        )
    return {"ok": True, **result}
