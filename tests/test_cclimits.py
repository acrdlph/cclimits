"""Offline tests. Nothing here touches the network or the Keychain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cclimits import api, cli, collect, creds, model, refresh, render, shell

# A trimmed copy of a real /api/oauth/usage payload.
USAGE = {
    "five_hour": {"utilization": 6.0, "resets_at": "2026-07-12T22:30:00+00:00"},
    "seven_day": {"utilization": 65.0, "resets_at": "2026-07-13T08:00:00+00:00"},
    "limits": [
        {
            "kind": "session",
            "percent": 6,
            "severity": "normal",
            "resets_at": "2026-07-12T22:30:00+00:00",
            "is_active": False,
        },
        {
            "kind": "weekly_all",
            "percent": 65,
            "severity": "normal",
            "resets_at": "2026-07-13T08:00:00+00:00",
            "is_active": False,
        },
        {
            "kind": "weekly_scoped",
            "percent": 100,
            "severity": "critical",
            "resets_at": "2026-07-13T08:00:00+00:00",
            "scope": {"model": {"id": None, "display_name": "Fable"}},
            "is_active": True,
        },
    ],
}


def test_parses_session_weekly_and_model_scoped_limits():
    limits = model.parse_limits(USAGE)
    assert [limit.label for limit in limits] == ["Session", "Weekly", "Fable"]
    assert limits[2].is_model_scoped
    assert limits[2].exhausted_now
    assert limits[0].is_model_scoped is False


def test_model_scoped_limits_are_read_generically():
    """A model we have never heard of still gets a limit, with no code change."""
    payload = {
        "limits": [
            {
                "kind": "weekly_scoped",
                "percent": 42,
                "scope": {"model": {"display_name": "Brand New Model"}},
            }
        ]
    }
    limits = model.parse_limits(payload)
    assert len(limits) == 1
    assert limits[0].label == "Brand New Model"
    assert limits[0].is_model_scoped


def test_unnameable_scoped_limit_is_skipped():
    payload = {"limits": [{"kind": "weekly_scoped", "percent": 10, "scope": {}}]}
    assert model.parse_limits(payload) == []


def test_falls_back_to_legacy_windows_when_limits_array_is_absent():
    payload = {
        "five_hour": {"utilization": 12.0, "resets_at": "2026-07-12T22:30:00+00:00"},
        "seven_day": {"utilization": 30.0, "resets_at": "2026-07-13T08:00:00+00:00"},
        "seven_day_opus": {"utilization": 55.0, "resets_at": "2026-07-13T08:00:00+00:00"},
    }
    limits = model.parse_limits(payload)
    assert [limit.label for limit in limits] == ["Session", "Weekly", "Opus"]
    assert limits[2].is_model_scoped


def _account(session: float, weekly: float, fable: float = 0.0) -> model.AccountUsage:
    return model.AccountUsage(
        slug="test",
        config_dir=Path("/tmp/x"),
        limits=[
            model.Limit("Session", model.SESSION, session, None),
            model.Limit("Weekly", model.WEEKLY, weekly, None),
            model.Limit("Fable", model.WEEKLY, fable, None, is_model_scoped=True),
        ],
    )


def test_accounts_are_labelled_by_directory_slug_by_default():
    """No email is shown unless the user opted in, so a screenshot or a status
    line never leaks an address."""
    account = _account(10, 20)
    assert account.email is None
    assert account.label == "test"


def test_label_stays_the_slug_even_once_the_email_is_known():
    """--email adds a column; it does not rename the row. The slug is what you
    type to switch accounts, so it has to stay on the row."""
    account = _account(10, 20)
    account.email = "someone@example.com"
    assert account.label == "test"


def test_table_shows_no_email_by_default():
    out = render.render_table([_account(10, 20)], color=False)
    assert "@" not in out
    assert "EMAIL" not in out


def test_table_gains_a_trailing_email_column_once_addresses_are_fetched():
    account = _account(10, 20)
    account.email = "someone@example.com"
    header, row = render.render_table([account], color=False).splitlines()[:2]
    assert header.rstrip().endswith("EMAIL")
    assert row.rstrip().endswith("someone@example.com")
    assert row.split()[0] == "test", "the slug still names the row"


def test_email_column_gives_a_cell_to_an_account_that_has_no_address():
    """One account's profile lookup failing must not knock the columns out of
    alignment for the accounts whose lookup worked."""
    known, unknown = _account(10, 20), _account(30, 40)
    known.email = "someone@example.com"
    rows = render.render_table([known, unknown], color=False).splitlines()[1:3]
    assert rows[1].rstrip().endswith("—")


def test_no_row_carries_trailing_whitespace():
    """The table is made to be copied out of a terminal, and a padded final
    column puts a tail of spaces on every row when it lands somewhere else."""
    account = _account(10, 20)
    account.email = "someone@example.com"
    for color in (True, False):
        out = render.render_table([account, _account(30, 40)], color=color)
        assert all(line == line.rstrip() for line in out.splitlines())


def test_email_column_is_never_painted():
    """An address is reference information, like the reset day: default color."""
    account = _account(10, 20)
    account.email = "someone@example.com"
    out = render.render_table([account], color=True)
    assert not re.search(r"\033\[[0-9;]*m" + re.escape("someone@example.com"), out)


def test_headroom_is_set_by_the_binding_general_limit():
    assert _account(session=10, weekly=65).headroom == 35
    assert _account(session=96, weekly=20).headroom == 4


def test_headroom_ignores_model_scoped_limits():
    """An exhausted Fable cap does not stop you using Sonnet, so it must not
    make an otherwise-free account look unusable."""
    assert _account(session=0, weekly=0, fable=100).headroom == 100


def test_best_account_picks_the_most_headroom():
    low, high = _account(90, 90), _account(5, 10)
    assert render.best_account([low, high]) is high


def test_best_account_skips_broken_accounts():
    broken = model.AccountUsage(slug="b", config_dir=Path("/tmp/b"), error="expired")
    good = _account(50, 50)
    assert render.best_account([broken, good]) is good


def test_best_account_returns_none_when_nothing_is_usable():
    broken = model.AccountUsage(slug="b", config_dir=Path("/tmp/b"), error="expired")
    assert render.best_account([broken]) is None


def test_keychain_service_is_derived_from_the_config_dir_path():
    path = Path("/Users/someone/.claude-work")
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    assert list(creds._keychain_services(path)) == [f"Claude Code-credentials-{digest}"]


def test_non_default_dir_never_falls_back_to_the_unsuffixed_entry():
    """Falling back would report the default account's usage under another
    account's name — a wrong answer, which is worse than no answer."""
    services = list(creds._keychain_services(Path("/Users/someone/.claude-work")))
    assert creds.KEYCHAIN_SERVICE not in services


def test_default_dir_may_use_the_unsuffixed_entry():
    services = list(creds._keychain_services(Path("/Users/someone/.claude")))
    assert services[-1] == creds.KEYCHAIN_SERVICE


@pytest.mark.parametrize(
    ("name", "expected"),
    [(".claude", "default"), (".claude-work", "work"), (".claude-account2", "account2")],
)
def test_slug(name, expected):
    assert creds.slug_for(Path("/Users/someone") / name) == expected


def test_discovery_ignores_directories_that_are_not_claude_config_dirs(tmp_path, monkeypatch):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    (tmp_path / ".claude-work").mkdir()
    (tmp_path / ".claude-work" / "projects").mkdir()
    (tmp_path / ".claude-flow").mkdir()  # a neighbour that is not a config dir
    (tmp_path / ".claude-flow" / "update-state.json").write_text("{}")

    monkeypatch.delenv("CLAUDE_CONFIG_DIRS", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    found = {path.name for path in creds.discover_config_dirs()}
    assert found == {".claude", ".claude-work"}


@pytest.mark.parametrize("name", shell.SUPPORTED)
def test_shell_init_defines_the_cc_function(name):
    snippet = shell.shell_init(name)
    assert "cc()" in snippet
    assert "CLAUDE_CONFIG_DIR" in snippet


def test_only_zsh_gets_completion():
    assert "compdef" in shell.shell_init("zsh")
    assert "compdef" not in shell.shell_init("bash")


def _run_cc(home: Path, *args: str) -> str:
    """Run the real `cc` function under bash, against a fake cclimits and a fake
    HOME. Nothing here reaches the network: the stub just echoes its arguments.
    """
    stub = home / "bin" / "cclimits"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text('#!/bin/sh\nprintf "cclimits[%s]\\n" "$*"\n')
    stub.chmod(0o755)
    env = {
        "HOME": str(home),
        "PATH": f"{stub.parent}:{os.environ['PATH']}",
    }
    script = shell.shell_init("bash") + "\ncc " + " ".join(args) + "\n"
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, check=False
    )
    return done.stdout


def test_cc_forwards_flags_to_cclimits(tmp_path):
    """`cc --email` used to fall through to the account branch and hunt for
    ~/.claude---email. A flag is cclimits', never an account name."""
    assert _run_cc(tmp_path, "--email").strip() == "cclimits[--email]"


def test_cc_forwards_several_flags_at_once(tmp_path):
    assert _run_cc(tmp_path, "--detail", "--refresh").strip() == "cclimits[--detail --refresh]"


def test_bare_cc_still_shows_the_table(tmp_path):
    assert _run_cc(tmp_path).strip() == "cclimits[]"


def test_cc_ls_takes_flags_too(tmp_path):
    """`ls` names the table, so the flags after it are still the table's."""
    assert _run_cc(tmp_path, "ls", "--email").strip() == "cclimits[--email]"


def test_cc_help_is_ccs_own_not_cclimits(tmp_path):
    out = _run_cc(tmp_path, "--help")
    assert "switch to account n" in out
    assert "cclimits[" not in out


def test_cc_still_switches_accounts(tmp_path):
    (tmp_path / ".claude-work").mkdir()
    assert "→ work" in _run_cc(tmp_path, "work")


def test_shell_init_prints_nothing_else(capsys):
    """The output is eval'd, so a stray line would be executed as a command."""
    assert cli.main(["--shell-init", "bash"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("cc()")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(None, "—"), (0, "now"), (90, "1m"), (3660, "1h 1m"), (100000, "1d 3h")],
)
def test_humanize(seconds, expected):
    assert render.humanize(seconds) == expected


def test_resets_in_seconds_never_goes_negative():
    past = datetime.now(timezone.utc) - timedelta(hours=1)
    limit = model.Limit("Session", model.SESSION, 10, past)
    assert limit.resets_in_seconds() == 0.0


def test_expired_credentials_are_detected():
    stale = creds.Credentials("tok", expires_at=1.0, subscription_type="max", rate_limit_tier=None)
    fresh = creds.Credentials("tok", expires_at=None, subscription_type="max", rate_limit_tier=None)
    assert stale.is_expired
    assert not fresh.is_expired


def _account_with_resets(slug, session_pct, session_in, weekly_in):
    now = datetime.now(timezone.utc)
    return model.AccountUsage(
        slug=slug,
        config_dir=Path("/tmp") / slug,
        limits=[
            model.Limit("Session", model.SESSION, session_pct, now + timedelta(minutes=session_in)),
            model.Limit("Weekly", model.WEEKLY, 50, now + timedelta(minutes=weekly_in)),
        ],
    )


def test_footer_names_the_account_that_frees_up_first():
    # Durations are asserted in test_humanize; what matters here is *which*
    # account each reset is attributed to — the two are deliberately different.
    soon = _account_with_resets("soon", 100, session_in=10, weekly_in=999)
    later = _account_with_resets("later", 100, session_in=500, weekly_in=100)
    out = render.render_table([later, soon], color=False)
    assert "next session reset: soon in" in out
    assert "next weekly reset: later in" in out


def test_footer_tiebreak_is_deterministic():
    """Two accounts whose limits reset at the same instant must not swap places
    in the footer between runs. Each render measures every countdown against a
    single clock sample, so an exact tie stays a tie and goes to the account
    listed first — not to whichever account's now() happened to be called last."""
    reset = datetime.now(timezone.utc) + timedelta(hours=2)

    def account(slug):
        return model.AccountUsage(
            slug=slug,
            config_dir=Path("/tmp") / slug,
            limits=[
                model.Limit("Session", model.SESSION, 50, reset),
                model.Limit("Weekly", model.WEEKLY, 50, reset),
            ],
        )

    accounts = [account("first"), account("second")]
    names = {
        re.search(r"next session reset: (\w+)", render.render_table(accounts, color=False))[1]
        for _ in range(20)
    }
    assert names == {"first"}


def test_footer_ignores_broken_accounts():
    broken = model.AccountUsage(slug="broken", config_dir=Path("/tmp/b"), error="expired")
    good = _account_with_resets("good", 50, session_in=30, weekly_in=60)
    out = render.render_table([broken, good], color=False)
    assert "next session reset: good in" in out


def _limits(session, weekly, fable=0):
    now = datetime.now(timezone.utc)
    return [
        model.Limit("Session", model.SESSION, session, now + timedelta(hours=1)),
        model.Limit("Weekly", model.WEEKLY, weekly, now + timedelta(days=3)),
        model.Limit("Fable", model.WEEKLY, fable, now + timedelta(days=3), is_model_scoped=True),
    ]


def _usage(**kwargs) -> model.AccountUsage:
    return model.AccountUsage(slug="t", config_dir=Path("/tmp/t"), limits=_limits(**kwargs))


def test_usable_account_is_not_blocked():
    assert _usage(session=10, weekly=20).blocked_until is None


def test_spent_session_blocks_until_the_session_resets():
    account = _usage(session=100, weekly=20)
    assert account.blocked_until == account.session.resets_at


def test_both_spent_means_free_only_when_the_LATER_one_resets():
    """Session resetting does not help while weekly is still exhausted."""
    account = _usage(session=100, weekly=100)
    assert account.blocked_until == account.weekly.resets_at


def test_a_spent_model_limit_does_not_block_the_account():
    """Fable at 100% stops you using Fable, not Sonnet."""
    assert _usage(session=5, weekly=5, fable=100).blocked_until is None


def test_is_active_below_100_percent_does_not_block():
    """Observed in the wild: ``is_active`` marks the account's most-constrained
    window, not an exhausted one — a weekly at 60% carries the flag while the
    account is perfectly usable. Only a fully spent limit may block."""
    payload = {
        "limits": [
            {"kind": "session", "percent": 0, "is_active": False, "resets_at": None},
            {
                "kind": "weekly_all",
                "percent": 60,
                "is_active": True,
                "resets_at": "2026-07-15T11:59:59+00:00",
            },
        ]
    }
    account = model.AccountUsage(
        slug="t", config_dir=Path("/tmp/t"), limits=model.parse_limits(payload)
    )
    assert not account.weekly.exhausted_now
    assert account.blocked_until is None


def test_free_in_column_is_filled_for_blocked_and_near_cap_accounts():
    blocked = model.AccountUsage(
        slug="blocked", config_dir=Path("/tmp/b"), limits=_limits(session=100, weekly=20)
    )
    nearcap = model.AccountUsage(
        slug="nearcap", config_dir=Path("/tmp/n"), limits=_limits(session=10, weekly=95)
    )
    usable = model.AccountUsage(
        slug="usable", config_dir=Path("/tmp/u"), limits=_limits(session=10, weekly=20)
    )
    rows = {
        line.split()[0]: line
        for line in render.render_table([blocked, nearcap, usable], color=False).splitlines()
        if line[:1].isalpha()
    }
    assert re.search(r"\d+[hm]\s*$", rows["blocked"]), "blocked account should show a wait"
    assert re.search(r"\d+[hm]\s*$", rows["nearcap"]), "near-cap account should show a wait"
    assert not re.search(r"\d+[hm]\s*$", rows["usable"]), "usable account should show nothing"


def test_free_in_counts_down_the_binding_limit_before_100_percent():
    """Above the warning threshold, FREE IN surfaces the binding limit's reset
    even though the account is not yet blocked."""
    now = datetime.now(timezone.utc)
    account = _usage(session=10, weekly=95)
    assert account.blocked_until is None  # not actually blocked
    assert account.free_in_seconds(now) == account.weekly.resets_in_seconds(now)


def test_free_in_is_blank_with_comfortable_headroom():
    assert _usage(session=10, weekly=88).free_in_seconds() is None


def test_free_in_ignores_a_near_cap_model_limit():
    """A near-full Fable cap must not light up FREE IN — it blocks Fable, not
    the account, exactly like a fully spent one."""
    assert _usage(session=10, weekly=10, fable=95).free_in_seconds() is None


def test_free_in_uses_the_later_reset_when_both_limits_are_spent():
    now = datetime.now(timezone.utc)
    account = _usage(session=100, weekly=100)
    assert account.free_in_seconds(now) == account.weekly.resets_in_seconds(now)


def test_table_shows_the_local_weekly_reset_day():
    reset = datetime.now(timezone.utc) + timedelta(days=2)
    account = model.AccountUsage(
        slug="t",
        config_dir=Path("/tmp/t"),
        limits=[model.Limit("Weekly", model.WEEKLY, 50, reset)],
    )
    out = render.render_table([account], color=False)
    local = reset.astimezone()  # the reader plans in local days, not UTC ones
    assert "WEEKLY RESET" in out
    assert f"{local:%a} {local.day}, {local:%H:%M}" in out


def test_weekly_reset_day_is_never_painted():
    """The reset day is reference information: regular weight, default color,
    even in a colored table."""
    reset = datetime.now(timezone.utc) + timedelta(days=2)
    account = model.AccountUsage(
        slug="t",
        config_dir=Path("/tmp/t"),
        limits=[model.Limit("Weekly", model.WEEKLY, 50, reset)],
    )
    out = render.render_table([account], color=True)
    local = reset.astimezone()
    day = f"{local:%a} {local.day}, {local:%H:%M}"
    assert day in out
    assert f"{day}\033" not in out
    assert not re.search(r"\033\[[0-9;]*m" + re.escape(day), out)


def test_table_renders_a_column_per_model_scoped_limit():
    out = render.render_table([_account(10, 20, fable=30)], color=False)
    assert "SESSION" in out and "WEEKLY" in out and "FABLE" in out


def test_broken_account_does_not_widen_the_table():
    """One long error message must not blow out the columns for healthy rows."""
    broken = model.AccountUsage(
        slug="b", config_dir=Path("/tmp/b"), error="x" * 200
    )
    with_broken = render.render_table([_account(10, 20), broken], color=False)
    without = render.render_table([_account(10, 20)], color=False)
    assert len(with_broken.splitlines()[0]) == len(without.splitlines()[0])


# --- token renewal -------------------------------------------------------


def _blob(access="old-token", refresh_token="old-refresh", expires_ms=1):
    """A store blob shaped like Claude Code's, with a neighbour key that must
    survive any rewrite untouched."""
    return {
        "claudeAiOauth": {
            "accessToken": access,
            "refreshToken": refresh_token,
            "expiresAt": expires_ms,
            "subscriptionType": "max",
            "scopes": ["user:inference"],
        },
        "mcpOAuth": {"some-server": {"accessToken": "mcp-token"}},
    }


def test_merge_touches_only_what_the_token_response_speaks_to():
    payload = {
        "access_token": "new-token",
        "expires_in": 28800,
        "refresh_token": "new-refresh",
        "refresh_token_expires_in": 500000,
    }
    blob = _blob()
    merged = refresh.merge_response(blob, payload, now=1000.0)
    oauth = merged["claudeAiOauth"]
    assert oauth["accessToken"] == "new-token"
    assert oauth["refreshToken"] == "new-refresh"
    assert oauth["expiresAt"] == int((1000.0 + 28800) * 1000)
    assert oauth["refreshTokenExpiresAt"] == int((1000.0 + 500000) * 1000)
    assert oauth["subscriptionType"] == "max", "fields the response is silent on stay put"
    assert oauth["scopes"] == ["user:inference"]
    assert merged["mcpOAuth"] == blob["mcpOAuth"], "Claude Code's other keys ride along"
    assert blob["claudeAiOauth"]["accessToken"] == "old-token", "input blob is not mutated"


def test_merge_keeps_the_old_refresh_token_when_none_is_returned():
    merged = refresh.merge_response(_blob(), {"access_token": "n", "expires_in": 1}, now=0.0)
    assert merged["claudeAiOauth"]["refreshToken"] == "old-refresh"


def _file_store(tmp_path, monkeypatch, blob) -> Path:
    """A config dir whose credentials live in .credentials.json, plus the env
    that keeps locks and caches inside tmp_path. Forces the file store so no
    test ever touches a real Keychain."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    config = tmp_path / ".claude-work"
    config.mkdir(exist_ok=True)
    path = config / ".credentials.json"
    path.write_text(json.dumps(blob))
    path.chmod(0o600)
    return config


def test_renewal_persists_the_rotated_tokens_to_the_store(tmp_path, monkeypatch):
    """The server rotates the refresh token on every renewal; losing the new
    one would break the real Claude Code login. The rotated pair must land in
    the store, with the file's private permissions intact."""
    config = _file_store(tmp_path, monkeypatch, _blob())
    monkeypatch.setattr(
        refresh,
        "_post_refresh",
        lambda token, config_dir: {
            "access_token": "new-token",
            "expires_in": 28800,
            "refresh_token": "new-refresh",
        },
    )

    renewed = refresh.refresh_credentials(config, stale_token="old-token")

    assert renewed.access_token == "new-token"
    assert not renewed.is_expired
    stored = json.loads((config / ".credentials.json").read_text())
    assert stored["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert stored["mcpOAuth"] == _blob()["mcpOAuth"]
    assert ((config / ".credentials.json").stat().st_mode & 0o777) == 0o600


def test_renewal_skips_the_request_when_another_process_already_renewed(tmp_path, monkeypatch):
    """A second process waiting on the account lock must notice the store
    already holds a token that is fresh and *different* from the one it saw
    fail, and use that instead of spending another rotation."""
    fresh_ms = int((time.time() + 3600) * 1000)
    config = _file_store(
        tmp_path, monkeypatch, _blob(access="already-fresh", expires_ms=fresh_ms)
    )

    def boom(token, config_dir):
        raise AssertionError("no request should be made")

    monkeypatch.setattr(refresh, "_post_refresh", boom)
    renewed = refresh.refresh_credentials(config, stale_token="the-token-that-401d")
    assert renewed.access_token == "already-fresh"


def test_renewal_without_a_stored_refresh_token_asks_for_a_login(tmp_path, monkeypatch):
    config = _file_store(tmp_path, monkeypatch, _blob(refresh_token=None))
    with pytest.raises(refresh.RefreshError, match=re.escape(f"CLAUDE_CONFIG_DIR={config}")):
        refresh.refresh_credentials(config, stale_token="old-token")


def test_collect_renews_an_expired_login_and_fetches_with_the_new_token(tmp_path, monkeypatch):
    config = _file_store(tmp_path, monkeypatch, _blob())
    monkeypatch.setattr(
        refresh,
        "_post_refresh",
        lambda token, config_dir: {"access_token": "new-token", "expires_in": 28800},
    )
    tokens_used = []

    def fake_fetch(token):
        tokens_used.append(token)
        return USAGE

    monkeypatch.setattr(collect.api, "fetch_usage", fake_fetch)

    account = collect.collect_one(config)

    assert account.error is None
    assert tokens_used == ["new-token"], "the fetch must use the renewed token"
    assert [limit.label for limit in account.limits] == ["Session", "Weekly", "Fable"]


def test_no_token_refresh_reports_the_expiry_instead(tmp_path, monkeypatch):
    config = _file_store(tmp_path, monkeypatch, _blob())

    def boom(config_dir, stale_token=None):
        raise AssertionError("renewal must not run when the user opted out")

    monkeypatch.setattr(collect.refresh, "refresh_credentials", boom)
    account = collect.collect_one(config, renew_logins=False)
    assert account.error is not None and "expired" in account.error


def test_a_rejected_token_is_renewed_once_and_retried(tmp_path, monkeypatch):
    """Expiry is checked locally, but revocation is the server's call: a 401
    on a fresh-looking token gets exactly one renewal and one retry."""
    fresh_ms = int((time.time() + 3600) * 1000)
    config = _file_store(tmp_path, monkeypatch, _blob(access="revoked", expires_ms=fresh_ms))
    monkeypatch.setattr(
        refresh,
        "_post_refresh",
        lambda token, config_dir: {"access_token": "new-token", "expires_in": 28800},
    )
    tokens_used = []

    def fake_fetch(token):
        tokens_used.append(token)
        if token == "revoked":
            raise api.ApiError("token rejected", status=401)
        return USAGE

    monkeypatch.setattr(collect.api, "fetch_usage", fake_fetch)

    account = collect.collect_one(config)

    assert account.error is None
    assert tokens_used == ["revoked", "new-token"]


def test_a_non_auth_failure_is_not_treated_as_a_login_problem(tmp_path, monkeypatch):
    fresh_ms = int((time.time() + 3600) * 1000)
    config = _file_store(tmp_path, monkeypatch, _blob(expires_ms=fresh_ms))

    def rate_limited(token):
        raise api.ApiError("rate limited by the usage endpoint — poll less often", status=429)

    def boom(config_dir, stale_token=None):
        raise AssertionError("a 429 is not cured by a new token")

    monkeypatch.setattr(collect.api, "fetch_usage", rate_limited)
    monkeypatch.setattr(collect.refresh, "refresh_credentials", boom)
    account = collect.collect_one(config)
    assert account.error is not None and "rate limited" in account.error


# --- empty cells that used to mislead -------------------------------------


def test_weekly_reset_names_the_unstarted_window_instead_of_a_dash():
    """At 0% the API reports no reset time at all: the 7-day window is rolling
    and only starts — reset included — with the first message. A bare dash
    reads as missing data, so the cell states the fact instead."""
    idle = model.AccountUsage(
        slug="idle",
        config_dir=Path("/tmp/idle"),
        limits=[
            model.Limit("Session", model.SESSION, 0, None),
            model.Limit("Weekly", model.WEEKLY, 0, None),
        ],
    )
    assert "starts on use" in render.render_table([idle], color=False)


def test_a_used_weekly_without_a_reset_time_still_gets_a_dash():
    """'starts on use' is only true at 0%. A payload that omits the reset on a
    window that *is* running gets the honest dash, not a wrong explanation."""
    used = model.AccountUsage(
        slug="used",
        config_dir=Path("/tmp/used"),
        limits=[model.Limit("Weekly", model.WEEKLY, 20, None)],
    )
    out = render.render_table([used], color=False)
    assert "starts on use" not in out


def test_free_in_column_appears_only_when_some_row_needs_it():
    """A column of empty FREE IN cells says nothing; it only earns its header
    once an account is blocked or near its cap."""
    comfortable = _account(10, 20)
    assert "FREE IN" not in render.render_table([comfortable], color=False)

    blocked = model.AccountUsage(
        slug="blocked", config_dir=Path("/tmp/b"), limits=_limits(session=100, weekly=20)
    )
    assert "FREE IN" in render.render_table([comfortable, blocked], color=False)
