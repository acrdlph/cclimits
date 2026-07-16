"""Fetching usage for every account, in parallel, with a polite cache.

The usage endpoint is rate limited. cclimits caches each account's payload on
disk and refuses to refetch inside the TTL, so that a status line calling
``cclimits`` on every prompt costs nothing.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List, Optional

from . import api, creds, model, refresh

DEFAULT_TTL = 60.0  # seconds
MAX_PARALLEL = 8


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    path = Path(base) / "cclimits"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cache_file(config_dir: Path) -> Path:
    # The directory name alone can collide across parents; hash the full path.
    import hashlib

    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:12]
    return _cache_dir() / f"{digest}.json"


def _read_cache(config_dir: Path, ttl: float) -> Optional[dict]:
    path = _cache_file(config_dir)
    try:
        blob = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if time.time() - blob.get("fetched_at", 0) > ttl:
        return None
    return blob


def _write_cache(
    config_dir: Path, usage: dict, profile: Optional[dict], plan: Optional[str]
) -> None:
    blob = {"fetched_at": time.time(), "usage": usage, "profile": profile, "plan": plan}
    try:
        _cache_file(config_dir).write_text(json.dumps(blob))
    except OSError:
        pass  # a cache we cannot write is not an error worth failing on


def collect_one(
    config_dir: Path,
    ttl: float = DEFAULT_TTL,
    want_email: bool = False,
    renew_logins: bool = True,
) -> model.AccountUsage:
    """Usage for a single account. Never raises; failures land in ``.error``.

    ``want_email`` also decides whether the profile endpoint is called at all.
    ``renew_logins`` lets an expired or rejected token be refreshed in place,
    exactly as Claude Code would refresh it, instead of being reported.
    """
    result = model.AccountUsage(slug=creds.slug_for(config_dir), config_dir=config_dir)

    cached = _read_cache(config_dir, ttl)
    # An account is identified by its directory. The email is only ever fetched
    # when the user explicitly asks to see it, so the default run neither
    # requests nor stores an address anywhere.
    if cached and (cached.get("profile") or not want_email):
        usage = cached["usage"]
        profile = cached.get("profile")
        result.plan = cached.get("plan")
    else:
        try:
            credentials = creds.load_credentials(config_dir)
        except creds.CredentialError as exc:
            result.error = str(exc)
            return result

        if credentials.is_expired:
            if not renew_logins:
                result.error = (
                    "access token expired — run `CLAUDE_CONFIG_DIR="
                    f"{config_dir} claude` once to refresh it"
                )
                return result
            try:
                credentials = refresh.refresh_credentials(
                    config_dir, stale_token=credentials.access_token
                )
            except refresh.RefreshError as exc:
                result.error = str(exc)
                return result

        try:
            usage = api.fetch_usage(credentials.access_token)
        except api.ApiError as exc:
            # Expiry is checked locally, but revocation is the server's call:
            # an auth rejection on a fresh-looking token gets one renewal and
            # one retry, never a loop.
            if not (renew_logins and exc.status in (401, 403)):
                result.error = str(exc)
                return result
            try:
                credentials = refresh.refresh_credentials(
                    config_dir, stale_token=credentials.access_token
                )
                usage = api.fetch_usage(credentials.access_token)
            except (refresh.RefreshError, api.ApiError) as retry_exc:
                result.error = str(retry_exc)
                return result

        result.plan = credentials.subscription_type
        profile = api.fetch_profile(credentials.access_token) if want_email else None
        _write_cache(config_dir, usage, profile, result.plan)

    if want_email:
        email, plan = model.parse_profile(profile)
        result.email = email
        result.plan = plan or result.plan
    result.limits = model.parse_limits(usage)
    return result


def collect_all(
    config_dirs: List[Path],
    ttl: float = DEFAULT_TTL,
    want_email: bool = False,
    renew_logins: bool = True,
) -> List[model.AccountUsage]:
    """Usage for every account, fetched concurrently, order preserved."""
    if not config_dirs:
        return []
    workers = min(MAX_PARALLEL, len(config_dirs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(
            pool.map(lambda d: collect_one(d, ttl, want_email, renew_logins), config_dirs)
        )
