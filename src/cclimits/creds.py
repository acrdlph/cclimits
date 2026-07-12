"""Discovery of Claude Code config directories and their OAuth credentials.

Claude Code isolates accounts by config directory (``CLAUDE_CONFIG_DIR``). Each
directory carries its own credentials:

* **macOS** — the system Keychain, under a service name derived from the config
  directory path: ``Claude Code-credentials-<first 8 hex of sha256(path)>``. The
  default ``~/.claude`` historically uses the unsuffixed ``Claude Code-credentials``.
* **Linux / Windows** — a ``.credentials.json`` file inside the config directory.

Everything here is read-only. We never write, refresh, or rotate a token: a
refresh token that the server rotated but we failed to persist would silently
break the user's real Claude Code login.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

KEYCHAIN_SERVICE = "Claude Code-credentials"

# A directory only counts as a Claude Code config dir if it holds one of these.
# This is what keeps unrelated neighbours like ~/.claude-flow out of the list.
CONFIG_DIR_MARKERS = ("settings.json", "projects", "history.jsonl", ".credentials.json")


class CredentialError(Exception):
    """Credentials for an account could not be read."""


@dataclass
class Credentials:
    access_token: str
    expires_at: Optional[float]  # epoch seconds
    subscription_type: Optional[str]
    rate_limit_tier: Optional[str]

    @property
    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at <= time.time()


def slug_for(config_dir: Path) -> str:
    """Short stable handle: ``.claude`` -> ``default``, ``.claude-work`` -> ``work``."""
    name = config_dir.name
    if name == ".claude":
        return "default"
    prefix = ".claude-"
    return name[len(prefix) :] if name.startswith(prefix) and len(name) > len(prefix) else name


def _is_config_dir(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any((path / marker).exists() for marker in CONFIG_DIR_MARKERS)


def discover_config_dirs(explicit: Optional[List[str]] = None) -> List[Path]:
    """Find every Claude Code config directory on this machine.

    Precedence: explicit paths > ``$CLAUDE_CONFIG_DIRS`` > autodiscovery of
    ``~/.claude`` and ``~/.claude-*``.
    """
    if explicit:
        return [Path(p).expanduser().resolve() for p in explicit]

    # Opt-in override so users with dirs outside $HOME can still be found.
    from_env = os.environ.get("CLAUDE_CONFIG_DIRS")
    if from_env:
        return [Path(p).expanduser().resolve() for p in from_env.split(os.pathsep) if p]

    home = Path.home()
    found = [p for p in [home / ".claude", *sorted(home.glob(".claude-*"))] if _is_config_dir(p)]
    return [p.resolve() for p in found]


def _keychain_services(config_dir: Path) -> Iterator[str]:
    """Keychain service names to try, most specific first.

    The path-derived suffix is tried first and is the *only* candidate for a
    non-default directory. Falling back to the unsuffixed entry for, say,
    ``~/.claude-work`` would silently report the default account's usage under
    the wrong name — a wrong answer is worse than no answer.
    """
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:8]
    yield f"{KEYCHAIN_SERVICE}-{digest}"
    if config_dir.name == ".claude":
        yield KEYCHAIN_SERVICE


def _read_keychain(config_dir: Path) -> Optional[dict]:
    for service in _keychain_services(config_dir):
        proc = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                return json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                raise CredentialError(f"Keychain entry {service!r} is not valid JSON") from exc
    return None


def _read_file(config_dir: Path) -> Optional[dict]:
    path = config_dir / ".credentials.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CredentialError(f"{path} is not valid JSON") from exc


def load_credentials(config_dir: Path) -> Credentials:
    """Read credentials for one config directory, or raise CredentialError."""
    blob = None
    if sys.platform == "darwin":
        blob = _read_keychain(config_dir)
    if blob is None:
        blob = _read_file(config_dir)

    if blob is None:
        raise CredentialError(
            "no credentials found — log in with "
            f"`CLAUDE_CONFIG_DIR={config_dir} claude`"
        )

    oauth = blob.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise CredentialError(
            "credentials hold no OAuth token — this account may use an API key "
            "(ANTHROPIC_API_KEY) rather than a Pro/Max subscription"
        )

    expires_at = oauth.get("expiresAt")
    return Credentials(
        access_token=token,
        expires_at=expires_at / 1000 if expires_at else None,  # ms -> s
        subscription_type=oauth.get("subscriptionType"),
        rate_limit_tier=oauth.get("rateLimitTier"),
    )
