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

from . import api, creds, model

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


def _write_cache(config_dir: Path, usage: dict, profile: Optional[dict]) -> None:
    blob = {"fetched_at": time.time(), "usage": usage, "profile": profile}
    try:
        _cache_file(config_dir).write_text(json.dumps(blob))
    except OSError:
        pass  # a cache we cannot write is not an error worth failing on


def collect_one(config_dir: Path, ttl: float = DEFAULT_TTL) -> model.AccountUsage:
    """Usage for a single account. Never raises; failures land in ``.error``."""
    result = model.AccountUsage(slug=creds.slug_for(config_dir), config_dir=config_dir)

    cached = _read_cache(config_dir, ttl)
    if cached:
        usage, profile = cached["usage"], cached.get("profile")
    else:
        try:
            credentials = creds.load_credentials(config_dir)
        except creds.CredentialError as exc:
            result.error = str(exc)
            return result

        if credentials.is_expired:
            result.error = (
                "access token expired — run `CLAUDE_CONFIG_DIR="
                f"{config_dir} claude` once to refresh it"
            )
            return result

        try:
            usage = api.fetch_usage(credentials.access_token)
        except api.ApiError as exc:
            result.error = str(exc)
            return result

        profile = api.fetch_profile(credentials.access_token)
        _write_cache(config_dir, usage, profile)
        result.plan = credentials.subscription_type

    email, plan = model.parse_profile(profile)
    result.email = email
    result.plan = plan or result.plan
    result.limits = model.parse_limits(usage)
    return result


def collect_all(config_dirs: List[Path], ttl: float = DEFAULT_TTL) -> List[model.AccountUsage]:
    """Usage for every account, fetched concurrently, order preserved."""
    if not config_dirs:
        return []
    workers = min(MAX_PARALLEL, len(config_dirs))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda d: collect_one(d, ttl), config_dirs))
