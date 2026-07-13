"""Turning AccountUsage into something a human reads."""

from __future__ import annotations

import html as html_escape
import os
import sys
from datetime import datetime, timezone
from typing import List, Optional

from .model import SESSION, WEEKLY, AccountUsage, Limit

BAR_WIDTH = 12
FULL, EMPTY = "█", "░"

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
BRIGHT_RED = "\033[91m"
CYAN = "\033[36m"


def use_color(force: Optional[bool] = None) -> bool:
    if force is not None:
        return force
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _tint(percent: float) -> str:
    if percent >= 100:
        return BRIGHT_RED
    if percent >= 80:
        return RED
    if percent >= 50:
        return YELLOW
    return GREEN


class Painter:
    def __init__(self, color: bool):
        self.color = color

    def __call__(self, text: str, *codes: str) -> str:
        if not self.color or not codes:
            return text
        return f"{''.join(codes)}{text}{RESET}"


def bar(percent: float, paint: Painter, width: int = BAR_WIDTH) -> str:
    """Filled portion takes the severity tint; the remainder stays neutral.

    Tinting the whole bar makes a 6% bar read as a solid green block — the eye
    catches color long before it catches texture, so only the filled part is
    allowed to carry it.
    """
    filled = min(width, round(percent / 100 * width))
    return paint(FULL * filled, _tint(percent)) + paint(EMPTY * (width - filled), DIM)


def humanize(seconds: Optional[float]) -> str:
    """'2h 14m', '45m', '3d 4h' — the units that matter, never more than two."""
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "now"
    minutes = int(seconds // 60)
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _cell(limit: Optional[Limit], paint: Painter) -> str:
    if limit is None:
        return paint("       —    ", DIM)
    percent = limit.percent
    text = f"{bar(percent, paint, 8)} {percent:3.0f}%"
    return text


def _free_cell(account: AccountUsage, paint: Painter, now: datetime) -> str:
    """How long until a blocked account is usable again; blank if it is usable now."""
    seconds = account.blocked_for_seconds(now)
    if seconds is None:
        return ""
    return paint(humanize(seconds), BRIGHT_RED)


def _visible_len(text: str) -> int:
    """Length ignoring ANSI escapes, so columns line up when colored."""
    out, in_escape = 0, False
    for char in text:
        if in_escape:
            in_escape = char != "m"
        elif char == "\033":
            in_escape = True
        else:
            out += 1
    return out


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _visible_len(text))


def render_table(accounts: List[AccountUsage], color: bool = True) -> str:
    """One row per account; session + weekly + a column per model-scoped limit."""
    paint = Painter(color)
    if not accounts:
        return "No Claude Code accounts found. See `cclimits --help`."

    # One clock sample per render. Every countdown is measured against the same
    # instant, or durations that are equal in fact compare unequal by the
    # microseconds between two now() calls — and ties break arbitrarily.
    now = datetime.now(timezone.utc)

    # Model columns are the union across accounts, so a limit that only exists
    # on some plans (a promo model, say) still gets its own column.
    model_names: List[str] = []
    for account in accounts:
        for limit in account.model_limits:
            if limit.label not in model_names:
                model_names.append(limit.label)
    model_names.sort()

    headers = [
        "ACCOUNT",
        "PLAN",
        "SESSION",
        "WEEKLY",
        *(name.upper() for name in model_names),
        "FREE IN",
    ]

    # Broken accounts are rendered as a name plus a free-text reason. They are
    # kept out of the width computation so one long error message cannot blow
    # out the columns for every healthy account.
    healthy = [account for account in accounts if account.ok]
    rows = {
        id(account): [
            paint(account.label, BOLD),
            account.plan or "—",
            _cell(account.session, paint),
            _cell(account.weekly, paint),
            *(_cell(account.find(name), paint) for name in model_names),
            _free_cell(account, paint, now),
        ]
        for account in healthy
    }

    name_width = max(
        _visible_len(headers[0]),
        *(_visible_len(account.label) for account in accounts),
    )
    widths = [name_width] + [
        max(_visible_len(header), *(_visible_len(rows[id(a)][i]) for a in healthy))
        if healthy
        else _visible_len(header)
        for i, header in enumerate(headers[1:], start=1)
    ]

    lines = ["  ".join(paint(_pad(h, widths[i]), DIM) for i, h in enumerate(headers))]
    for account in accounts:
        if account.ok:
            row = rows[id(account)]
            lines.append("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
        else:
            name = _pad(paint(account.label, DIM), widths[0])
            lines.append(f"{name}  {paint(account.error or 'error', RED)}")

    lines.append("")
    lines.append(_reset_footer(accounts, paint, now))
    advice = recommend(accounts, paint)
    if advice:
        lines.append(advice)
    return "\n".join(lines)


def _soonest_reset(accounts: List[AccountUsage], group: str, now: datetime):
    """The (account, seconds) pair whose limit in this group resets first.

    Exact ties go to the account listed first — discovery order, the same
    order the table shows.
    """
    candidates = []
    for account in accounts:
        if not account.ok:
            continue
        limit = account.session if group == SESSION else account.weekly
        if limit is None:
            continue
        seconds = limit.resets_in_seconds(now)
        if seconds is not None:
            candidates.append((account, seconds))
    return min(candidates, key=lambda pair: pair[1]) if candidates else None


def _reset_footer(accounts: List[AccountUsage], paint: Painter, now: datetime) -> str:
    """Which account frees up next, and when."""
    parts = []
    for name, group in (("session", SESSION), ("weekly", WEEKLY)):
        soonest = _soonest_reset(accounts, group, now)
        if soonest is None:
            continue
        account, seconds = soonest
        parts.append(
            paint(f"next {name} reset: ", DIM)
            + paint(account.label, BOLD)
            + paint(f" in {humanize(seconds)}", DIM)
        )
    return "   ".join(parts)


def recommend(accounts: List[AccountUsage], paint: Painter) -> str:
    best = best_account(accounts)
    if best is None:
        return ""
    return (
        paint("→ most headroom: ", DIM)
        + paint(best.label, BOLD, CYAN)
        + paint(f"  ({best.headroom:.0f}% free)", DIM)
    )


def best_account(accounts: List[AccountUsage]) -> Optional[AccountUsage]:
    """The healthy account with the most room on its binding limit."""
    healthy = [account for account in accounts if account.ok and account.limits]
    if not healthy:
        return None
    return max(healthy, key=lambda account: account.headroom)


def render_detail(accounts: List[AccountUsage], color: bool = True) -> str:
    paint = Painter(color)
    now = datetime.now(timezone.utc)
    blocks: List[str] = []
    for account in accounts:
        title = paint(account.label, BOLD)
        header = f"{title}  {paint(str(account.config_dir), DIM)}"
        if not account.ok:
            blocks.append(f"{header}\n  {paint(account.error or 'error', RED)}")
            continue
        lines = [header]
        for limit in account.limits:
            flag = paint("  ← blocking you now", BRIGHT_RED) if limit.exhausted_now else ""
            lines.append(
                f"  {_pad(limit.label, 10)} {bar(limit.percent, paint)} "
                f"{limit.percent:3.0f}%  {paint('resets in ' + humanize(limit.resets_in_seconds(now)), DIM)}{flag}"
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def to_dict(accounts: List[AccountUsage]) -> dict:
    """The --json shape. Stable; script against this rather than the table."""
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "accounts": [
            {
                "slug": account.slug,
                "email": account.email,
                "plan": account.plan,
                "config_dir": str(account.config_dir),
                "ok": account.ok,
                "error": account.error,
                "headroom_percent": round(account.headroom, 1) if account.ok else None,
                "limits": [
                    {
                        "label": limit.label,
                        "group": limit.group,
                        "percent": limit.percent,
                        "remaining_percent": limit.remaining,
                        "model_scoped": limit.is_model_scoped,
                        "exhausted_now": limit.exhausted_now,
                        "resets_at": limit.resets_at.isoformat() if limit.resets_at else None,
                        "resets_in_seconds": limit.resets_in_seconds(now),
                    }
                    for limit in account.limits
                ],
            }
            for account in accounts
        ],
    }


def render_html(accounts: List[AccountUsage]) -> str:
    """A self-contained dashboard file. No network, no fonts, no scripts."""
    esc = html_escape.escape
    now = datetime.now(timezone.utc)

    def card(account: AccountUsage) -> str:
        name = esc(account.label)
        if not account.ok:
            return (
                f'<article class="card err"><h2>{name}</h2>'
                f'<p class="error">{esc(account.error or "error")}</p></article>'
            )
        rows = "".join(
            f'<div class="row"><span class="lbl">{esc(limit.label)}</span>'
            f'<span class="track"><span class="fill t{_tier(limit.percent)}"'
            f' style="width:{min(100, limit.percent):.0f}%"></span></span>'
            f'<span class="pct">{limit.percent:.0f}%</span>'
            f'<span class="rst">{esc(humanize(limit.resets_in_seconds(now)))}</span></div>'
            for limit in account.limits
        )
        plan = esc(account.plan or "")
        return (
            f'<article class="card"><h2>{name} <span class="plan">{plan}</span></h2>{rows}</article>'
        )

    cards = "\n".join(card(account) for account in accounts)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>cclimits</title>
<style>
:root {{ color-scheme: light dark; --bg:#fff; --fg:#111; --dim:#6b7280; --line:#e5e7eb; --card:#fff; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0b0d10; --fg:#e8eaed; --dim:#9aa3af; --line:#232830; --card:#12151a; }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; padding:2rem 1rem; background:var(--bg); color:var(--fg);
  font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:820px; margin:0 auto; }}
h1 {{ font-size:1.1rem; margin:0 0 .25rem; }}
.sub {{ color:var(--dim); font-size:.85rem; margin:0 0 1.5rem; }}
.card {{ border:1px solid var(--line); border-radius:12px; padding:1rem 1.15rem;
  margin-bottom:.85rem; background:var(--card); }}
h2 {{ font-size:.95rem; margin:0 0 .75rem; display:flex; gap:.5rem; align-items:center; }}
.plan {{ font-size:.7rem; font-weight:500; color:var(--dim); border:1px solid var(--line);
  padding:.1rem .4rem; border-radius:999px; text-transform:uppercase; letter-spacing:.03em; }}
.row {{ display:grid; grid-template-columns:5.5rem 1fr 3rem 4.5rem; gap:.6rem;
  align-items:center; padding:.2rem 0; }}
.lbl {{ font-size:.85rem; }}
.track {{ height:8px; border-radius:999px; background:var(--line); overflow:hidden; }}
.fill {{ display:block; height:100%; border-radius:999px; }}
.t0 {{ background:#10b981; }} .t1 {{ background:#f59e0b; }}
.t2 {{ background:#ef4444; }} .t3 {{ background:#b91c1c; }}
.pct {{ font-variant-numeric:tabular-nums; font-size:.85rem; text-align:right; }}
.rst {{ color:var(--dim); font-size:.75rem; text-align:right; }}
.err {{ border-color:#ef4444; }} .error {{ color:#ef4444; margin:0; font-size:.85rem; }}
@media (max-width:520px) {{ .row {{ grid-template-columns:4.5rem 1fr 2.5rem; }} .rst {{ display:none; }} }}
</style>
<main>
<h1>Claude usage</h1>
<p class="sub">{len(accounts)} account(s) · generated {stamp}</p>
{cards}
</main>
"""


def _tier(percent: float) -> int:
    if percent >= 100:
        return 3
    if percent >= 80:
        return 2
    if percent >= 50:
        return 1
    return 0
