#!/usr/bin/env python3
"""Render the README's terminal screenshot to docs/screenshot.svg.

GitHub strips ANSI escapes from fenced code blocks, so a colored table can only
be shown as an image. Rather than paste in a photo that drifts from the code,
this drives the real renderer and converts its ANSI output to SVG: the picture
in the README cannot disagree with what the tool actually prints.

The data is fictional on purpose — no real account is published.

    python3 scripts/make_screenshot.py
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cclimits.model import SESSION, WEEKLY, AccountUsage, Limit  # noqa: E402
from cclimits.render import render_table  # noqa: E402

# The palette the SVG paints ANSI codes with. Chosen to stay legible on GitHub
# in both light and dark themes, against the dark terminal plate below.
PALETTE = {
    "32": "#3fb950",  # green   — under 50%
    "33": "#d29922",  # yellow  — 50-79%
    "31": "#f85149",  # red     — 80-99%
    "91": "#ff7b72",  # bright  — at the limit
    "36": "#79c0ff",  # cyan    — the recommendation
    "2": "#8b949e",  # dim     — headers, reset times
}
FOREGROUND = "#e6edf3"
BACKGROUND = "#0d1117"
CHAR_W, LINE_H, PAD = 8.4, 21.0, 22.0

TOKEN = re.compile(r"\033\[([0-9;]*)m")


def demo_accounts() -> list:
    # Reset times are anchored to "now" so the footer renders the same relative
    # phrasing a real run would produce.
    now = datetime.now(timezone.utc)
    session_reset = now + timedelta(hours=1, minutes=2)
    weekly_reset = now + timedelta(hours=12, minutes=32)

    def account(slug, session, weekly, fable):
        return AccountUsage(
            slug=slug,
            config_dir=Path("/Users/you") / (".claude" if slug == "default" else f".claude-{slug}"),
            plan="max",
            limits=[
                Limit("Session", SESSION, session, session_reset),
                Limit("Weekly", WEEKLY, weekly, weekly_reset),
                Limit("Fable", WEEKLY, fable, weekly_reset, is_model_scoped=True),
            ],
        )

    return [
        account("default", 6, 65, 100),
        account("work", 96, 98, 69),
        account("spare", 7, 83, 100),
        account("account4", 100, 21, 16),
        account("account5", 0, 100, 65),
        account("account6", 100, 59, 77),
    ]


def spans(line: str):
    """Split an ANSI line into (text, color, bold) runs."""
    color, bold, pos = None, False, 0
    for match in TOKEN.finditer(line):
        text = line[pos : match.start()]
        if text:
            yield text, color, bold
        for code in match.group(1).split(";"):
            if code in ("", "0"):
                color, bold = None, False
            elif code == "1":
                bold = True
            elif code in PALETTE:
                color = PALETTE[code]
        pos = match.end()
    if line[pos:]:
        yield line[pos:], color, bold


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_elements(ansi: str, x0: float, y0: float, char_w: float, line_h: float) -> list:
    """Lay an ANSI block out as absolutely-positioned <text> runs."""
    out = []
    for row, line in enumerate(ansi.split("\n")):
        y = y0 + (row + 0.8) * line_h
        column = 0
        for text, color, bold in spans(line):
            if text.strip():
                x = x0 + column * char_w
                attrs = f' fill="{color or FOREGROUND}"'
                if bold:
                    attrs += ' font-weight="600"'
                out.append(
                    f'<text x="{x:.1f}" y="{y:.1f}"{attrs} '
                    f'xml:space="preserve">{escape(text)}</text>'
                )
            column += len(text)
    return out


MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
SANS = "ui-sans-serif,system-ui,-apple-system,Helvetica,Arial,sans-serif"


def to_svg(ansi: str) -> str:
    """The README screenshot: a terminal plate sized to its content."""
    lines = ansi.split("\n")
    width = PAD * 2 + max(len(TOKEN.sub("", line)) for line in lines) * CHAR_W
    height = PAD * 2 + len(lines) * LINE_H
    body = text_elements(ansi, PAD, PAD, CHAR_W, LINE_H)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" \
viewBox="0 0 {width:.0f} {height:.0f}" role="img" aria-label="cclimits terminal output">
  <rect width="{width:.0f}" height="{height:.0f}" rx="10" fill="{BACKGROUND}"/>
  <g font-family="{MONO}" font-size="14">
    {chr(10).join('    ' + span for span in body).strip()}
  </g>
</svg>
"""


# GitHub's social preview, and every scraper that reads it, wants 2:1.
SOCIAL_W, SOCIAL_H = 1280, 640
# char_w must track the font's advance (0.6 em for these monospace faces) or the
# runs drift apart and words collide.
SOCIAL_FONT = 24
SOCIAL_CHAR_W, SOCIAL_LINE_H = SOCIAL_FONT * 0.6, 33.0


def to_social_svg(ansi: str) -> str:
    """The 1280x640 card shown when the repo is linked on Twitter, WhatsApp, Slack."""
    # The export= hint is useful in a terminal and just noise on a share card.
    ansi = re.sub(r"export CLAUDE_CONFIG_DIR=\S+", "", ansi)
    body = text_elements(ansi, 72, 236, SOCIAL_CHAR_W, SOCIAL_LINE_H)

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{SOCIAL_W}" height="{SOCIAL_H}" \
viewBox="0 0 {SOCIAL_W} {SOCIAL_H}" role="img" \
aria-label="cclimits — see the usage limits of every Claude Code account you own, at once">
  <rect width="{SOCIAL_W}" height="{SOCIAL_H}" fill="{BACKGROUND}"/>
  <rect x="0" y="0" width="{SOCIAL_W}" height="4" fill="#3fb950"/>
  <g font-family="{SANS}">
    <text x="72" y="118" fill="{FOREGROUND}" font-size="58" font-weight="700">cclimits</text>
    <text x="72" y="170" fill="#8b949e" font-size="25">See the usage limits of every Claude \
Code account you own, at once.</text>
    <text x="72" y="600" fill="#8b949e" font-size="20">github.com/acrdlph/cclimits</text>
  </g>
  <g font-family="{MONO}" font-size="{SOCIAL_FONT}">
    {chr(10).join('    ' + span for span in body).strip()}
  </g>
</svg>
"""


def main() -> None:
    accounts = demo_accounts()
    ansi = render_table(accounts, color=True)
    docs = Path(__file__).resolve().parent.parent / "docs"
    docs.mkdir(exist_ok=True)

    (docs / "screenshot.svg").write_text(to_svg(ansi))
    (docs / "social.svg").write_text(to_social_svg(ansi))
    print(f"wrote {docs / 'screenshot.svg'}")
    print(f"wrote {docs / 'social.svg'}")
    print("\nnow rasterise the social card (GitHub rejects SVG):")
    print(f"  rsvg-convert -w {SOCIAL_W} -h {SOCIAL_H} {docs / 'social.svg'} "
          f"-o {docs / 'social.png'}")


if __name__ == "__main__":
    main()
