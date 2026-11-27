"""Private regional analytics and persistent visit counter for Project Plan Health Check.

The module is called by the existing app.py through render_footer(st). It:
- keeps the MVP1-style public footer and cumulative Visits number;
- keeps regional country/state aggregates private in Upstash Redis;
- estimates country and broad region from the connection IP using FreeIPAPI;
- never stores the raw IP address, city, coordinates, ISP, or ASN in this app;
- counts each browser ID at most once in a rolling 24-hour period;
- excludes explicitly tagged wake-up and uptime-monitor requests.

The browser ID is a random identifier stored in localStorage. A SHA-256 digest
is used as the Redis deduplication key. Redis regional hashes contain only
aggregate counters (country and country/region labels), not visitor-level data.

Secrets required in Streamlit Community Cloud:
    UPSTASH_REDIS_REST_URL
    UPSTASH_REDIS_REST_TOKEN

No additional Python package is required. This uses Streamlit Components v2
(available in the application's pinned Streamlit version) and the Python
standard library.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


_COUNTER_KEY = "project-plan-health-check:visits:total"
_COUNTRY_COUNTS_KEY = "project-plan-health-check:analytics:countries"
_REGION_COUNTS_KEY = "project-plan-health-check:analytics:regions"
_BROWSER_KEY_PREFIX = "project-plan-health-check:visits:browser:"
_BROWSER_ID_STORAGE_KEY = "pphc_anonymous_browser_id_v1"
_COMPONENT_SESSION_KEY = "_pphc_browser_identity_component"
_DEDUP_TTL_SECONDS = 24 * 60 * 60
_HTTP_TIMEOUT_SECONDS = 3
_GEO_API_BASE = "https://free.freeipapi.com/api/v1/json/"

_IDENTITY_COMPONENT_RENDERER: Any | None = None
_IDENTITY_COMPONENT_UNAVAILABLE = False

# Atomically mark this random browser as counted for 24 hours, increment the
# public total and private country/region aggregate hashes. No raw IP is passed
# to or stored by Redis.
_INCREMENT_VISIT_LUA = r"""
local added = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
if added then
    local total = redis.call('INCR', KEYS[2])
    if ARGV[2] ~= '' then
        redis.call('HINCRBY', KEYS[3], ARGV[2], 1)
    end
    if ARGV[3] ~= '' then
        redis.call('HINCRBY', KEYS[4], ARGV[3], 1)
    end
    return total
end
return tonumber(redis.call('GET', KEYS[2]) or '0')
""".strip()

# A hard refresh creates a new Streamlit session. Check the Redis dedup key
# before calling the external geolocation provider so ordinary refreshes do
# not repeatedly disclose the visitor IP or consume geolocation requests.
_CHECK_ALREADY_COUNTED_LUA = r"""
if redis.call('EXISTS', KEYS[1]) == 1 then
    return tonumber(redis.call('GET', KEYS[2]) or '0')
end
return -1
""".strip()

_IDENTITY_COMPONENT_HTML = '<span aria-hidden="true"></span>'
_IDENTITY_COMPONENT_JS = r"""
export default function (component) {
  const { data, setStateValue } = component;
  const storageKey = data.storage_key;
  const currentId = data.current_visitor_id || "";

  function makeId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    const bytes = new Uint8Array(16);
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
      throw new Error("Secure browser random generator unavailable");
    }
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, "0"));
    return [
      hex.slice(0, 4).join("") + hex.slice(4, 6).join(""),
      hex.slice(6, 8).join(""),
      hex.slice(8, 10).join(""),
      hex.slice(10, 12).join(""),
      hex.slice(12, 16).join("")
    ].join("-");
  }

  try {
    let visitorId = window.localStorage.getItem(storageKey);
    if (!visitorId) {
      visitorId = makeId();
      window.localStorage.setItem(storageKey, visitorId);
    }
    if (visitorId !== currentId) {
      setStateValue("visitor_id", visitorId);
    }
    if (data.storage_available === false) {
      setStateValue("storage_available", true);
    }
  } catch (error) {
    if (data.storage_available !== false) {
      setStateValue("storage_available", false);
    }
  }

  return () => {};
}
"""


def _is_true(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_automated_request(st: Any) -> bool:
    """Exclude explicitly tagged wake-up, health-check and uptime requests."""
    try:
        params = st.query_params
        if str(params.get("bot", "")).lower() == "wake":
            return True
        for key in ("uptime_check", "healthcheck", "monitor"):
            if _is_true(params.get(key, "")):
                return True
    except Exception:
        pass
    return False


def _analytics_opted_out(st: Any) -> bool:
    """Allow a visitor to opt out by opening the app with ?analytics=off."""
    try:
        value = str(st.query_params.get("analytics", "")).strip().lower()
        return value in {"off", "0", "false", "no"}
    except Exception:
        return False


def _read_upstash_secrets(st: Any) -> tuple[str, str] | None:
    """Read Upstash REST credentials from Streamlit Secrets."""
    try:
        url = str(st.secrets["UPSTASH_REDIS_REST_URL"]).strip().rstrip("/")
        token = str(st.secrets["UPSTASH_REDIS_REST_TOKEN"]).strip()
    except Exception:
        return None

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not token:
        return None
    return url, token


def _post_upstash(rest_url: str, rest_token: str, command: list[Any]) -> Any | None:
    """Execute one Redis REST command and return its result, or None on error."""
    request = urllib.request.Request(
        rest_url,
        data=json.dumps(command).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {rest_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "project-plan-health-check-analytics/3.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if isinstance(payload, dict) and payload.get("error"):
            return None
        return payload.get("result") if isinstance(payload, dict) else None
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return None


def _get_browser_identity(st: Any) -> tuple[str | None, bool]:
    """Get a browser-stable random ID from localStorage using Components v2."""
    global _IDENTITY_COMPONENT_RENDERER, _IDENTITY_COMPONENT_UNAVAILABLE

    components = getattr(st, "components", None)
    components_v2 = getattr(components, "v2", None) if components else None
    register_component = getattr(components_v2, "component", None)
    if not callable(register_component):
        _IDENTITY_COMPONENT_UNAVAILABLE = True
        return None, False

    if _IDENTITY_COMPONENT_RENDERER is None:
        try:
            _IDENTITY_COMPONENT_RENDERER = register_component(
                name="pphc_anonymous_browser_identity",
                html=_IDENTITY_COMPONENT_HTML,
                js=_IDENTITY_COMPONENT_JS,
            )
        except Exception:
            _IDENTITY_COMPONENT_UNAVAILABLE = True
            return None, False

    state = st.session_state
    saved = state.get(_COMPONENT_SESSION_KEY, {})

    def state_value(name: str, default: Any) -> Any:
        if isinstance(saved, dict):
            return saved.get(name, default)
        return getattr(saved, name, default)

    current_id = str(state_value("visitor_id", "") or "")
    storage_available = bool(state_value("storage_available", True))

    try:
        result = _IDENTITY_COMPONENT_RENDERER(
            key=_COMPONENT_SESSION_KEY,
            data={
                "storage_key": _BROWSER_ID_STORAGE_KEY,
                "current_visitor_id": current_id,
                "storage_available": storage_available,
            },
            default={
                "visitor_id": current_id,
                "storage_available": storage_available,
            },
            on_visitor_id_change=lambda: None,
            on_storage_available_change=lambda: None,
        )
        visitor_id = getattr(result, "visitor_id", None)
        if not visitor_id:
            visitor_id = state_value("visitor_id", None)
        storage_available = getattr(
            result, "storage_available", state_value("storage_available", True)
        )
        if visitor_id:
            visitor_id = str(visitor_id)
            state["_pphc_browser_visitor_id"] = visitor_id
        return visitor_id, bool(storage_available)
    except Exception:
        return None, False


def _get_public_client_ip(st: Any) -> str | None:
    """Return a public client IP for coarse geolocation, never for security."""
    try:
        raw_ip = st.context.ip_address
        if not raw_ip:
            return None
        parsed = ipaddress.ip_address(str(raw_ip))
        if not parsed.is_global:
            return None
        return str(parsed)
    except Exception:
        return None


def _safe_label(value: Any, max_length: int = 80) -> str:
    """Sanitize provider labels before using them as Redis hash fields."""
    raw = str(value or "")
    cleaned = "".join(
        char for char in raw
        if char.isprintable() and char not in {"|", "\\", "\n", "\r"}
    )
    return " ".join(cleaned.split())[:max_length]


def _lookup_country_region(client_ip: str | None) -> tuple[str, str]:
    """Resolve country and broad region using HTTPS; return empty labels on failure.

    FreeIPAPI receives the visitor's IP address for this lookup. This app does
    not save the IP address or the provider's detailed location/network fields.
    Only the country and region labels are returned to the aggregate counter.
    """
    if not client_ip:
        return "", ""

    endpoint = _GEO_API_BASE + urllib.parse.quote(client_ip, safe=":")
    request = urllib.request.Request(
        endpoint,
        headers={
            "Accept": "application/json",
            "User-Agent": "project-plan-health-check-regional-analytics/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            return "", ""

        country_code = _safe_label(payload.get("countryCode"), 3).upper()
        country_name = _safe_label(payload.get("countryName"), 80)
        region_name = _safe_label(payload.get("regionName"), 80)

        # Do not store arbitrary or incomplete provider output.
        if not re.fullmatch(r"[A-Z]{2}", country_code) or not country_name:
            return "", ""

        country_field = f"{country_code}|{country_name}"
        region_field = (
            f"{country_code}|{country_name}|{region_name}" if region_name else ""
        )
        return country_field, region_field
    except (
        urllib.error.URLError,
        TimeoutError,
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ):
        return "", ""


def _browser_redis_key(visitor_id: str) -> str:
    visitor_hash = hashlib.sha256(visitor_id.encode("utf-8")).hexdigest()
    return f"{_BROWSER_KEY_PREFIX}{visitor_hash}"


def _count_if_already_counted(
    rest_url: str, rest_token: str, visitor_id: str
) -> int | None:
    """Return current total if this browser was counted during the last 24h."""
    result = _post_upstash(
        rest_url,
        rest_token,
        [
            "EVAL",
            _CHECK_ALREADY_COUNTED_LUA,
            2,
            _browser_redis_key(visitor_id),
            _COUNTER_KEY,
        ],
    )
    if result is None:
        return None
    try:
        value = int(result)
        return value if value >= 0 else None
    except (TypeError, ValueError):
        return None


def _increment_once(
    rest_url: str,
    rest_token: str,
    visitor_id: str,
    country_field: str = "",
    region_field: str = "",
) -> int | None:
    """Increment public total and private geo aggregates once per 24h/browser."""
    result = _post_upstash(
        rest_url,
        rest_token,
        [
            "EVAL",
            _INCREMENT_VISIT_LUA,
            4,
            _browser_redis_key(visitor_id),
            _COUNTER_KEY,
            _COUNTRY_COUNTS_KEY,
            _REGION_COUNTS_KEY,
            _DEDUP_TTL_SECONDS,
            country_field,
            region_field,
        ],
    )
    if result is None:
        return None
    try:
        return int(result)
    except (TypeError, ValueError):
        return None


def get_persistent_visit_count(st: Any) -> int | None:
    """Count once per browser/24h and save private country/region aggregates."""
    if _is_automated_request(st) or _analytics_opted_out(st):
        return None

    visitor_id, storage_available = _get_browser_identity(st)
    if not visitor_id or not storage_available:
        return None

    state = st.session_state
    if (
        state.get("_pphc_counted_browser_id") == visitor_id
        and isinstance(state.get("_pphc_visit_count"), int)
    ):
        return state["_pphc_visit_count"]

    if state.get("_pphc_visit_counter_attempted_for") == visitor_id:
        return None

    credentials = _read_upstash_secrets(st)
    if credentials is None:
        return None

    state["_pphc_visit_counter_attempted_for"] = visitor_id
    rest_url, rest_token = credentials

    # If this browser was counted already, read the current total and do not
    # send its IP to the geolocation provider again on a hard refresh.
    existing_count = _count_if_already_counted(rest_url, rest_token, visitor_id)
    if existing_count is not None:
        state["_pphc_counted_browser_id"] = visitor_id
        state["_pphc_visit_count"] = existing_count
        return existing_count

    client_ip = _get_public_client_ip(st)
    country_field, region_field = _lookup_country_region(client_ip)
    count = _increment_once(
        rest_url, rest_token, visitor_id, country_field, region_field
    )
    if count is not None:
        state["_pphc_counted_browser_id"] = visitor_id
        state["_pphc_visit_count"] = count
    return count


def render_footer(st: Any) -> None:
    """Render MVP1 footer; keep country/region statistics private."""
    visit_count = get_persistent_visit_count(st)

    st.markdown("---")
    st.markdown("**Projects**")
    st.markdown(
        "**Project Plan Health Check** · "
        "[SOW → Project Planner](https://sow-to-project-planner.streamlit.app/) · "
        "[GxP AI Readiness & Governance Assessment](https://gxp-ai-readiness-governance.streamlit.app/) · "
        "[AI Risk & Issue Dashboard](https://ai-risk-issue-dashboard.streamlit.app/)"
    )
    st.markdown("© 2026 Sriram Sampath. All rights reserved.")
    st.markdown("[LinkedIn](https://www.linkedin.com/in/sriramsampath81/)")

    if _analytics_opted_out(st):
        st.caption("👁️ Visits: not counted for this session (analytics opted out)")
    elif visit_count is None:
        st.caption("👁️ Visits: temporarily unavailable")
    else:
        st.caption(f"👁️ Visits: {visit_count:,}")

    st.caption(
        "Regional analytics use an approximate country/region lookup. "
        "The connection IP is sent securely to FreeIPAPI for the lookup; "
        "this app stores aggregate country/region counts, not the IP address. "
        "[Opt out of analytics](?analytics=off)."
    )
