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
      cclimits
      return $?
      ;;
    -h | --help)
      printf '%s\n' \
        'cc              show the usage table' \
        'cc <n>          switch to account n (1 = default)' \
        'cc <name>       switch to ~/.claude-<name>' \
        'cc best         switch to the account with the most headroom' \
        'cc which        print the account in use'
      return 0
      ;;
    which)
      printf '%s\n' "${CLAUDE_CONFIG_DIR:-$HOME/.claude (default)}"
      return 0
      ;;
    best)
      dir="$(cclimits --best)" || return 1
      ;;
    1 | default)
      unset CLAUDE_CONFIG_DIR
      printf '→ default\n'
      return 0
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
}
"""

_ZSH_COMPLETION = r"""
_cc() {
  local -a accounts
  accounts=(best which default)
  local dir
  for dir in "$HOME"/.claude-*(N/); do
    accounts+=("${dir##*/.claude-}")
  done
  _describe 'account' accounts
}
compdef _cc cc
"""

_SNIPPETS = {
    "zsh": _COMMON + _ZSH_COMPLETION,
    "bash": _COMMON,
}

SUPPORTED = tuple(_SNIPPETS)


def shell_init(shell: str) -> str:
    return _SNIPPETS[shell].strip() + "\n"
