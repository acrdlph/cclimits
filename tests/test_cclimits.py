"""Offline tests. Nothing here touches the network or the Keychain."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from cclimits import cli, creds, model, render, shell

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


def test_label_uses_the_email_once_it_has_been_fetched():
    account = _account(10, 20)
    account.email = "someone@example.com"
    assert account.label == "someone@example.com"


def test_table_shows_no_email_by_default():
    out = render.render_table([_account(10, 20)], color=False)
    assert "@" not in out


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
