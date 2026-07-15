"""The shell integration printed by ``cclimits --shell-init``.

Switching accounts means setting ``CLAUDE_CONFIG_DIR`` in the *calling* shell, so
this has to be a shell function that the user sources. A binary cannot do it: a
child process cannot export a variable into its parent.
"""

from __future__ import annotations

# `cc 1` is the default ~/.claude, `cc 4` is ~/.claude-account4, and any other
# word is treated as a suffix, so ~/.claude-work is reachable as `cc work`.
_COMMON = r"""
cc() {
  local target dir
  target="${1-}"

  case "$target" in
    '' | ls | status)
      if [ "$#" -gt 0 ]; then shift; fi
      cclimits "$@"
      return $?
      ;;
    -h | --help)
      printf '%s\n' \
        'cc                    show the usage table' \
        'cc --email            show the table with an EMAIL column' \
        'cc <n>                switch to account n (1 = default)' \
        'cc <name>             switch to ~/.claude-<name>' \
        'cc best               switch to the account with the most headroom' \
        'cc which              print the account in use' \
        'cc <target> <cmd>...  switch, then run cmd on that account' \
        '' \
        'e.g.  cc 4 claude --dangerously-skip-permissions' \
        '      cc best claude' \
        '      cc --detail --refresh'
      return 0
      ;;
    -*)
      # Every other flag is cclimits', not cc's. Without this a `cc --email`
      # reaches the account branch below and looks for ~/.claude---email.
      cclimits "$@"
      return $?
      ;;
    which)
      printf '%s\n' "${CLAUDE_CONFIG_DIR:-$HOME/.claude (default)}"
      return 0
      ;;
    best)
      dir="$(cclimits --best)" || return 1
      ;;
    1 | default)
      dir="$HOME/.claude"
      ;;
    [0-9] | [0-9][0-9])
      dir="$HOME/.claude-account$target"
      ;;
    *)
      dir="$HOME/.claude-$target"
      ;;
  esac

  if [ ! -d "$dir" ]; then
    printf 'cc: no account at %s\n' "$dir" >&2
    return 1
  fi

  # The default account is the absence of the variable, not a value for it.
  if [ "$dir" = "$HOME/.claude" ]; then
    unset CLAUDE_CONFIG_DIR
    printf '→ default\n'
  else
    export CLAUDE_CONFIG_DIR="$dir"
    printf '→ %s\n' "${dir##*/.claude-}"
  fi

  # Anything after the target is a command to run on the account we just switched
  # to: `cc 4 claude --dangerously-skip-permissions`. Args are passed through
  # untouched, so every claude flag works without cc knowing about any of them.
  if [ "$#" -gt 1 ]; then
    shift
    "$@"
  fi
}
"""

_ZSH_COMPLETION = r"""
_cc() {
  local -a accounts flags
  accounts=(best which default)
  local dir
  for dir in "$HOME"/.claude-*(N/); do
    accounts+=("${dir##*/.claude-}")
  done
  _describe 'account' accounts
  # The table flags are forwarded to cclimits; offering them here is how you
  # find out `cc --email` exists without reading the help.
  flags=(--email --detail --watch --json --refresh --help)
  _describe 'flag' flags
}

# compdef only exists once compinit has run. Guard it, or shells without
# completion set up print an error on every startup.
if whence compdef >/dev/null 2>&1; then
  compdef _cc cc
fi
"""

_SNIPPETS = {
    "zsh": _COMMON + _ZSH_COMPLETION,
    "bash": _COMMON,
}

SUPPORTED = tuple(_SNIPPETS)


def shell_init(shell: str) -> str:
    return _SNIPPETS[shell].strip() + "\n"
