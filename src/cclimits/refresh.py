"""Renewing an expired login, the way Claude Code itself does.

``creds.py`` stays strictly read-only; every credential write in cclimits
lives here instead. When an access token has expired, cclimits performs the
same ``refresh_token`` grant Claude Code runs on launch, under Claude Code's
own OAuth client id, and persists the response to the same store the original
came from (Keychain on macOS, ``.credentials.json`` elsewhere) — so a login
renewed by cclimits is indistinguishable from one renewed by Claude Code.

Two properties matter more than anything else here:

* **The rotated refresh token must not be lost.** The server rotates the
  refresh token on every renewal and the old one dies; a tool that dropped
  the new one would silently break the real Claude Code login. So the write
  is verified by reading the store back, and a persist failure is reported
  loudly with the recovery step.
* **Two processes must not race the rotation.** A status line can run
  cclimits on every shell prompt. An exclusive per-account file lock makes
  the second process wait; by the time it looks again, the store already
  holds a fresh login and it skips its own renewal entirely.
"""

from __future__ import annotations

import contextlib
import getpass
import hashlib
import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from copy import deepcopy
from pathlib import Path
from typing import Iterator, Optional

from . import creds
from .api import USER_AGENT

try:
    import fcntl
except ImportError:  # Windows: renewals proceed, just without serialization
    fcntl = None  # type: ignore[assignment]

TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"

# Claude Code's own (public) OAuth client id. Renewing under it is what makes
# the result identical to the login the real client would have produced.
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class RefreshError(Exception):
    """The login could not be renewed; the message says what to do instead."""


def _login_hint(config_dir: Path) -> str:
    return f"log in with `CLAUDE_CONFIG_DIR={config_dir} claude`"


def refresh_credentials(
    config_dir: Path, stale_token: Optional[str] = None
) -> creds.Credentials:
    """Renew ``config_dir``'s login and return the fresh credentials.

    ``stale_token`` is the access token the caller knows to be bad. If, once
    the lock is ours, the store holds a *different* unexpired token, another
    process renewed the login while we waited — that token is returned as-is
    and no request is made.
    """
    with _account_lock(config_dir):
        blob, service = creds.locate_store(config_dir)
        if blob is None:
            raise RefreshError(f"no credentials found — {_login_hint(config_dir)}")
        try:
            current = creds.parse_blob(blob)
        except creds.CredentialError as exc:
            raise RefreshError(str(exc)) from exc
        if not current.is_expired and current.access_token != stale_token:
            return current

        refresh_token = (blob.get("claudeAiOauth") or {}).get("refreshToken")
        if not refresh_token:
            raise RefreshError(
                f"login expired and no refresh token is stored — {_login_hint(config_dir)}"
            )

        payload = _post_refresh(refresh_token, config_dir)
        renewed = merge_response(blob, payload, time.time())
        _persist(config_dir, service, renewed)
        return creds.parse_blob(renewed)


def merge_response(blob: dict, payload: dict, now: float) -> dict:
    """A copy of ``blob`` with the token response folded into ``claudeAiOauth``.

    Only the fields the response speaks to are touched. Everything else —
    ``mcpOAuth``, scopes, subscription metadata, keys this code has never
    heard of — rides along byte-for-byte, because Claude Code owns this store
    and cclimits is only a guest in it.
    """
    merged = deepcopy(blob)
    oauth = merged.setdefault("claudeAiOauth", {})
    oauth["accessToken"] = payload["access_token"]
    oauth["expiresAt"] = int((now + payload["expires_in"]) * 1000)
    if payload.get("refresh_token"):
        oauth["refreshToken"] = payload["refresh_token"]
        if payload.get("refresh_token_expires_in"):
            oauth["refreshTokenExpiresAt"] = int(
                (now + payload["refresh_token_expires_in"]) * 1000
            )
    return merged


def _post_refresh(refresh_token: str, config_dir: Path, timeout: float = 30.0) -> dict:
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLIENT_ID,
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise RefreshError(
                f"stored refresh token was rejected — {_login_hint(config_dir)}"
            ) from exc
        raise RefreshError(
            f"HTTP {exc.code} from the token endpoint — try again, or {_login_hint(config_dir)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RefreshError(f"network error while renewing the login: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise RefreshError("token endpoint returned something that is not JSON") from exc

    if not payload.get("access_token") or not payload.get("expires_in"):
        raise RefreshError("token endpoint returned an unexpected response")
    return payload


def _persist(config_dir: Path, service: Optional[str], blob: dict) -> None:
    data = json.dumps(blob)
    if service is not None:
        _write_keychain(service, data, config_dir)
    else:
        _write_file(config_dir, data)

    # Read back through the exact path Claude Code will read. Anything short
    # of a perfect match means the rotated refresh token may not have landed,
    # which is the one failure this module must never be quiet about.
    readback, _ = creds.locate_store(config_dir)
    if readback != blob:
        raise RefreshError(
            "login was renewed but saving it could not be verified — run "
            f"`CLAUDE_CONFIG_DIR={config_dir} claude` and log in again if it asks"
        )


def _write_keychain(service: str, data: str, config_dir: Path) -> None:
    proc = subprocess.run(
        [
            "security",
            "add-generic-password",
            "-U",
            "-s",
            service,
            "-a",
            _keychain_account(service),
            "-w",
            data,
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RefreshError(
            f"could not write Keychain entry {service!r} ({proc.stderr.strip()}) — "
            + _login_hint(config_dir)
        )


def _keychain_account(service: str) -> str:
    """The existing item's account attribute, so the update targets the same
    item Claude Code created rather than growing a sibling."""
    proc = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if '"acct"<blob>=' in line:
            value = line.split("=", 1)[1].strip()
            if value.startswith('"') and value.endswith('"'):
                return value[1:-1]
    return getpass.getuser()


def _write_file(config_dir: Path, data: str) -> None:
    """Atomic replace: a crash mid-write must leave the old login intact."""
    target = config_dir / ".credentials.json"
    handle, tmp = tempfile.mkstemp(dir=str(config_dir), prefix=".credentials.", suffix=".tmp")
    try:
        try:
            os.write(handle, data.encode())
        finally:
            os.close(handle)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise RefreshError(f"could not write {target}: {exc}") from exc


@contextlib.contextmanager
def _account_lock(config_dir: Path) -> Iterator[None]:
    if fcntl is None:
        yield
        return
    path = _lock_path(config_dir)
    with open(path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _lock_path(config_dir: Path) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cclimits"
    base.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(str(config_dir).encode()).hexdigest()[:12]
    return base / f"{digest}.refresh.lock"
