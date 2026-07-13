"""Normalising the usage payload into something renderable.

The interesting part of the payload is the ``limits`` array, whose entries look
like::

    {"kind": "session",        "percent": 6,   "resets_at": "...", "severity": "normal"}
    {"kind": "weekly_all",     "percent": 65,  "resets_at": "...", "severity": "normal"}
    {"kind": "weekly_scoped",  "percent": 100, "resets_at": "...", "severity": "critical",
     "scope": {"model": {"display_name": "Fable"}}}

Model-scoped limits are read generically. No model name is hardcoded, so a
promotional model that goes away — or a new one that appears — needs no code
change here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

SESSION = "session"
WEEKLY = "weekly"

# The FREE IN column starts counting down once a binding (non-model-scoped)
# limit crosses this, not only when it is fully spent at 100%. A limit this
# close to its cap is worth a heads-up before it actually blocks you.
FREE_IN_THRESHOLD = 90.0


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Limit:
    """One quota bucket for one account."""

    label: str  # "Session", "Weekly", or a model name like "Fable"
    group: str  # SESSION or WEEKLY
    percent: float
    resets_at: Optional[datetime]
    is_model_scoped: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, 100.0 - self.percent)

    @property
    def exhausted_now(self) -> bool:
        """Whether this limit is spent, i.e. blocking whatever it scopes.

        Nothing else in the payload can be trusted for this. ``is_active``
        merely marks the account's most-constrained window — it is true on a
        3% session — and ``severity`` goes ``critical`` at 99%, when there is
        still headroom. A limit blocks you exactly when it is fully used.
        """
        return self.percent >= 100.0

    def resets_in_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        if self.resets_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (self.resets_at - now).total_seconds())


@dataclass
class AccountUsage:
    """Everything cclimits knows about one account."""

    slug: str
    config_dir: Path
    email: Optional[str] = None
    plan: Optional[str] = None
    limits: List[Limit] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def label(self) -> str:
        """How this account is named in output.

        The directory slug by default. ``email`` is only populated when the user
        opted in with ``--email``, so addresses never appear unasked — which
        matters when you screenshot this or pipe it into a status line.
        """
        return self.email or self.slug

    def find(self, label: str) -> Optional[Limit]:
        for limit in self.limits:
            if limit.label.lower() == label.lower():
                return limit
        return None

    @property
    def session(self) -> Optional[Limit]:
        return self.find("Session")

    @property
    def weekly(self) -> Optional[Limit]:
        return self.find("Weekly")

    @property
    def model_limits(self) -> List[Limit]:
        return [limit for limit in self.limits if limit.is_model_scoped]

    @property
    def blocked_until(self) -> Optional[datetime]:
        """When this account can be used again, or None if it is usable now.

        Two things this is careful about:

        * A model-scoped limit does not block the account. Fable at 100% stops
          you using Fable, not Sonnet, so it never lands here.
        * If both session and weekly are spent, you are free at the *later* of
          the two. The session resetting does not help while weekly is still
          exhausted — so this is a max(), not a min().
        """
        spent = [
            limit
            for limit in self.limits
            if not limit.is_model_scoped and limit.exhausted_now
        ]
        resets = [limit.resets_at for limit in spent if limit.resets_at]
        return max(resets) if resets else None

    @property
    def near_limit_reset(self) -> Optional[datetime]:
        """When the binding limit resets, once it is near its cap but not yet
        blocking — the early warning FREE IN shows before an account is spent.

        The binding limit is the most-consumed of the general (non-model-scoped)
        limits, the same one headroom is measured against. Model-scoped caps are
        excluded for the same reason they are everywhere else: a spent Fable cap
        does not stop you using Sonnet, so it must not light up FREE IN.
        """
        near = [
            limit
            for limit in self.limits
            if not limit.is_model_scoped
            and limit.resets_at is not None
            and limit.percent >= FREE_IN_THRESHOLD
        ]
        if not near:
            return None
        return max(near, key=lambda limit: limit.percent).resets_at

    def free_in_seconds(self, now: Optional[datetime] = None) -> Optional[float]:
        """Seconds to show in the FREE IN column, or None to leave it blank.

        When the account is genuinely blocked, this is the accurate usable-again
        time — the later of the spent limits' resets. Below 100% it becomes a
        heads-up: the binding limit's reset, shown once it crosses
        FREE_IN_THRESHOLD. The block time takes precedence, so a fully spent
        limit is always counted down accurately rather than as a warning.
        """
        until = self.blocked_until or self.near_limit_reset
        if until is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max(0.0, (until - now).total_seconds())

    @property
    def headroom(self) -> float:
        """Free capacity on the binding constraint, ignoring model-scoped caps.

        This is what ranks accounts: an account is only as usable as its most
        consumed of {session, weekly}. Model-scoped limits are excluded because
        an exhausted Fable cap does not stop you using Sonnet.
        """
        general = [limit.percent for limit in self.limits if not limit.is_model_scoped]
        if not general:
            return 0.0
        return max(0.0, 100.0 - max(general))


def _display_name(entry: dict) -> Optional[str]:
    scope = entry.get("scope") or {}
    model = scope.get("model") or {}
    return model.get("display_name") or model.get("id")


def parse_limits(payload: dict) -> List[Limit]:
    """Build the limit list, preferring the modern ``limits`` array."""
    entries = payload.get("limits")
    if isinstance(entries, list) and entries:
        return _parse_modern(entries)
    return _parse_legacy(payload)


def _parse_modern(entries: List[dict]) -> List[Limit]:
    limits: List[Limit] = []
    for entry in entries:
        kind = entry.get("kind")
        if kind == "session":
            label, scoped = "Session", False
        elif kind == "weekly_all":
            label, scoped = "Weekly", False
        elif kind == "weekly_scoped":
            name = _display_name(entry)
            if not name:
                continue  # a scoped limit we cannot name is not worth a column
            label, scoped = name, True
        else:
            continue

        limits.append(
            Limit(
                label=label,
                group=SESSION if kind == "session" else WEEKLY,
                percent=float(entry.get("percent") or 0.0),
                resets_at=_parse_time(entry.get("resets_at")),
                is_model_scoped=scoped,
            )
        )
    return limits


def _parse_legacy(payload: dict) -> List[Limit]:
    """Fallback for older payloads that only had the flat top-level windows."""
    limits: List[Limit] = []
    for key, label, group in (
        ("five_hour", "Session", SESSION),
        ("seven_day", "Weekly", WEEKLY),
    ):
        window = payload.get(key)
        if isinstance(window, dict) and window.get("utilization") is not None:
            limits.append(
                Limit(
                    label=label,
                    group=group,
                    percent=float(window["utilization"]),
                    resets_at=_parse_time(window.get("resets_at")),
                )
            )

    for key, label in (("seven_day_opus", "Opus"), ("seven_day_sonnet", "Sonnet")):
        window = payload.get(key)
        if isinstance(window, dict) and window.get("utilization") is not None:
            limits.append(
                Limit(
                    label=label,
                    group=WEEKLY,
                    percent=float(window["utilization"]),
                    resets_at=_parse_time(window.get("resets_at")),
                    is_model_scoped=True,
                )
            )
    return limits


def parse_profile(payload: Optional[dict]) -> tuple:
    """Pull (email, plan) out of a profile payload."""
    if not payload:
        return None, None
    account = payload.get("account") or {}
    organization = payload.get("organization") or {}
    email = account.get("email")
    plan = organization.get("organization_type") or None
    if account.get("has_claude_max"):
        plan = "max"
    elif account.get("has_claude_pro"):
        plan = "pro"
    return email, plan
