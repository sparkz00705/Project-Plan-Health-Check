"""MVP1-style footer and persistent visit counter for Project Plan Health Check.

Uses Upstash Redis REST API through the Python standard library only.
Counts one visit per Streamlit browser session. Automated health/wake requests
are excluded. Redis failures never prevent the main application from running.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4
from typing import Any


_COUNTER_KEY = "project-plan-health-check:visits:total"
_SESSION_KEY_PREFIX = "project-plan-health-check:visits:session:"
_SESSION_TTL_SECONDS = 24 * 60 * 60
_HTTP_TIMEOUT_SECONDS = 3

# Atomic: mark this browser session once and increment the total only when the
# marker is newly created. The Lua operation prevents double increments if two
# Streamlit reruns race for the same session marker.
_INCREMENT_ONCE_LUA = """
local added = redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1])
if added then
    return redis.call('INCR', KEYS[2])
end
return tonumber(redis.call('GET', KEYS[2]) or '0')
""".strip()


def _is_automated_request(st: Any) -> bool:
    """Return True for explicitly marked uptime/wake requests."""
    try:
        params = st.query_params
        if str(params.get("bot", "")).lower() == "wake":
            return True
        if str(params.get("uptime_check", "")).lower() in {"1", "true", "yes"}:
            return True
    except Exception:
        # Older Streamlit versions may not expose query_params in the same way.
        pass
    return False


def _read_upstash_secrets(st: Any) -> tuple[str, str] | None:
    """Read credentials from Streamlit Secrets; never use hard-coded values."""
    try:
        url = str(st.secrets["UPSTASH_REDIS_REST_URL"]).strip().rstrip("/")
        token = str(st.secrets["UPSTASH_REDIS_REST_TOKEN"]).strip()
    except Exception:
        return None

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc or not token:
        return None
    return url, token


def _increment_once(
    rest_url: str,
    rest_token: str,
    session_id: str,
) -> int | None:
    """Atomically increment the total once for this random session ID."""
    session_key = f"{_SESSION_KEY_PREFIX}{session_id}"
    command = [
        "EVAL",
        _INCREMENT_ONCE_LUA,
        2,
        session_key,
        _COUNTER_KEY,
        _SESSION_TTL_SECONDS,
    ]
    body = json.dumps(command).encode("utf-8")
    request = urllib.request.Request(
        rest_url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {rest_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "project-plan-health-check-visitor-counter/1.0",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
        result = payload.get("result")
        if result is None:
            return None
        return int(result)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def get_persistent_visit_count(st: Any) -> int | None:
    """Return the persistent visit total, incrementing once per app session.

    A Streamlit session_state marker prevents normal widget reruns from making
    more REST calls. Redis provides an atomic second layer of deduplication.
    Returns None when secrets are missing or Redis is unavailable.
    """
    if _is_automated_request(st):
        return None

    state = st.session_state
    cached = state.get("_pphc_visit_count")
    if isinstance(cached, int):
        return cached
    if state.get("_pphc_visit_counter_attempted"):
        return None

    credentials = _read_upstash_secrets(st)
    if credentials is None:
        return None

    session_id = state.get("_pphc_visit_session_id")
    if not session_id:
        session_id = uuid4().hex
        state["_pphc_visit_session_id"] = session_id

    # Avoid retrying on every widget rerun if Redis is temporarily unavailable.
    state["_pphc_visit_counter_attempted"] = True
    count = _increment_once(credentials[0], credentials[1], session_id)
    if count is not None:
        state["_pphc_visit_count"] = count
    return count


def render_footer(st: Any) -> None:
    """Render the Project Plan Health Check footer using MVP1 branding."""
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

    if visit_count is None:
        st.caption("👁️ Visits: temporarily unavailable")
    else:
        st.caption(f"👁️ Visits: {visit_count:,}")
