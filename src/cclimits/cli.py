"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List, Optional

from . import __version__
from .collect import DEFAULT_TTL, collect_all
from .creds import discover_config_dirs
from .model import AccountUsage
from .render import (
    best_account,
    render_detail,
    render_html,
    render_table,
    to_dict,
    use_color,
)
from .shell import SUPPORTED, shell_init

# The usage endpoint is rate limited; refusing to poll faster than this is what
# keeps a --watch loop from getting the user 429'd.
MIN_WATCH_INTERVAL = 60


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cclimits",
        description="See the usage limits of every Claude Code account you own, at once.",
        epilog="Reading usage is strictly read-only; an expired login is renewed in place, "
        "exactly as Claude Code itself would renew it (disable with --no-token-refresh).",
    )
    parser.add_argument("--version", action="version", version=f"cclimits {__version__}")
    parser.add_argument(
        "-d",
        "--dir",
        action="append",
        metavar="PATH",
        help="config dir to inspect; repeatable. Default: ~/.claude and ~/.claude-*",
    )
    parser.add_argument(
        "--detail", action="store_true", help="expand every limit instead of a compact table"
    )
    parser.add_argument(
        "--email",
        action="store_true",
        help="add an EMAIL column with each account's address; without this, "
        "no address is fetched or stored at all",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--html", metavar="FILE", help="write a self-contained dashboard file")
    parser.add_argument(
        "--best",
        action="store_true",
        help="print only the config dir with the most headroom (for shell use)",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        type=int,
        const=180,
        metavar="SEC",
        help=f"refresh every SEC seconds (default 180, minimum {MIN_WATCH_INTERVAL})",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="bypass the cache and refetch now"
    )
    parser.add_argument(
        "--no-token-refresh",
        action="store_true",
        help="leave expired logins alone instead of renewing them automatically",
    )
    parser.add_argument(
        "--shell-init",
        choices=SUPPORTED,
        metavar="SHELL",
        help="print the `cc` account-switcher function for zsh or bash; "
        'add `eval "$(cclimits --shell-init zsh)"` to your rc file',
    )
    parser.add_argument("--no-color", action="store_true", help="disable ANSI color")
    return parser


def _gather(args: argparse.Namespace) -> List[AccountUsage]:
    config_dirs = discover_config_dirs(args.dir)
    ttl = 0.0 if args.refresh else DEFAULT_TTL
    return collect_all(
        config_dirs,
        ttl=ttl,
        want_email=args.email,
        renew_logins=not args.no_token_refresh,
    )


def _emit(accounts: List[AccountUsage], args: argparse.Namespace, color: bool) -> int:
    if args.json:
        print(json.dumps(to_dict(accounts), indent=2))
        return 0

    if args.best:
        best = best_account(accounts)
        if best is None:
            print("no usable account found", file=sys.stderr)
            return 1
        print(best.config_dir)
        return 0

    if args.html:
        path = Path(args.html).expanduser()
        path.write_text(render_html(accounts))
        print(f"wrote {path}")
        return 0

    print(render_detail(accounts, color) if args.detail else render_table(accounts, color))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Printed for `eval`, so it must not touch the network or emit anything else.
    if args.shell_init:
        print(shell_init(args.shell_init), end="")
        return 0

    color = use_color(False if args.no_color else None)

    if args.watch is None:
        accounts = _gather(args)
        return _emit(accounts, args, color)

    interval = max(MIN_WATCH_INTERVAL, args.watch)
    args.refresh = True  # a watch loop that served cache would show stale numbers
    try:
        while True:
            accounts = _gather(args)
            # Clear screen and home the cursor, so the table redraws in place.
            sys.stdout.write("\033[2J\033[H")
            print(render_detail(accounts, color) if args.detail else render_table(accounts, color))
            print(f"\nrefreshing every {interval}s · ctrl-c to stop")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
