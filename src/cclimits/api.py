"""The two Anthropic OAuth endpoints cclimits reads.

Both are undocumented — they are what Claude Code's own ``/usage`` command calls.
There is no stability guarantee; if a schema changes, cclimits degrades to
reporting what it can still parse rather than crashing.

Only GETs. cclimits never sends a request that changes account state.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Optional

from . import __version__

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"

# An honest User-Agent; verified to be accepted. cclimits does not impersonate
# the Claude Code client.
USER_AGENT = f"cclimits/{__version__} (+https://github.com/acrdlph/cclimits)"
OAUTH_BETA = "oauth-2025-04-20"


class ApiError(Exception):
    """The API call failed."""


def _get(url: str, token: str, timeout: float) -> dict:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ApiError("token rejected (expired or revoked) — log in again") from exc
        if exc.code == 429:
            raise ApiError("rate limited by the usage endpoint — poll less often") from exc
        raise ApiError(f"HTTP {exc.code} from {url}") from exc
    except urllib.error.URLError as exc:
        raise ApiError(f"network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ApiError("response was not valid JSON") from exc


def fetch_usage(token: str, timeout: float = 15.0) -> dict:
    """Raw usage payload: session/weekly/model-scoped limits, and credit spend."""
    return _get(USAGE_URL, token, timeout)


def fetch_profile(token: str, timeout: float = 15.0) -> Optional[dict]:
    """Raw profile payload, used only to label an account by email.

    Best-effort: a missing profile costs a nice label, not the usage numbers.
    """
    try:
        return _get(PROFILE_URL, token, timeout)
    except ApiError:
        return None
