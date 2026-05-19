#!/usr/bin/env bash
# Install the `vimax` CLI wrapper into ~/.local/bin (or $VIMAX_BIN_DIR for
# testing). Idempotent. Refuses to clobber a vimax binary that doesn't
# already point at this repo's wrapper.
#
# Usage:
#   ./scripts/install-cli.sh            # install or refresh
#   ./scripts/install-cli.sh status     # show which vimax / vimax health
#   ./scripts/install-cli.sh remove     # remove only our own symlink

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WRAPPER_SRC="$REPO_DIR/scripts/vimax"
BIN_DIR="${VIMAX_BIN_DIR:-$HOME/.local/bin}"
TARGET="$BIN_DIR/vimax"

ensure_prereqs() {
  if [[ ! -x "$WRAPPER_SRC" ]]; then
    echo "wrapper not executable: $WRAPPER_SRC" >&2
    echo "run: chmod +x $WRAPPER_SRC" >&2
    exit 1
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not on PATH; install from https://docs.astral.sh/uv/ first" >&2
    exit 1
  fi
  mkdir -p "$BIN_DIR"
}

# Returns 0 if $TARGET is a symlink owned by us (points inside this repo).
target_is_ours() {
  if [[ ! -L "$TARGET" ]]; then
    return 1
  fi
  local resolved
  resolved="$(readlink "$TARGET")"
  # Accept either relative-to-bindir or absolute, as long as it lands here.
  case "$resolved" in
    "$WRAPPER_SRC") return 0 ;;
    /*) return 1 ;;
    *)
      # relative — resolve against $BIN_DIR
      local abs="$BIN_DIR/$resolved"
      abs="$(cd "$(dirname "$abs")" 2>/dev/null && pwd)/$(basename "$abs")" || return 1
      [[ "$abs" == "$WRAPPER_SRC" ]] && return 0
      return 1
      ;;
  esac
}

path_warn_if_missing() {
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *)
      echo
      echo "warn: $BIN_DIR is not on PATH"
      echo "add to your shell rc:  export PATH=\"$BIN_DIR:\$PATH\""
      ;;
  esac
}

cmd_install() {
  ensure_prereqs
  if [[ -e "$TARGET" || -L "$TARGET" ]]; then
    if target_is_ours; then
      echo "already installed: $TARGET -> $WRAPPER_SRC"
    else
      echo "refusing to overwrite existing $TARGET (not pointing at this repo)" >&2
      echo "remove it manually if you want this install to take it over" >&2
      exit 1
    fi
  else
    ln -s "$WRAPPER_SRC" "$TARGET"
    echo "installed: $TARGET -> $WRAPPER_SRC"
  fi
  path_warn_if_missing
}

cmd_status() {
  echo "wrapper:    $WRAPPER_SRC"
  echo "bin dir:    $BIN_DIR"
  echo "symlink:    $TARGET"
  if [[ -L "$TARGET" ]]; then
    echo "points to:  $(readlink "$TARGET")"
    if target_is_ours; then
      echo "ownership:  ours"
    else
      echo "ownership:  foreign (will not be touched by remove)"
    fi
  elif [[ -e "$TARGET" ]]; then
    echo "(file exists but is not a symlink — will not be touched)"
  else
    echo "(not installed)"
    return 0
  fi
  echo
  if command -v vimax >/dev/null 2>&1; then
    echo "--- vimax health ---"
    vimax health || true
  fi
}

cmd_remove() {
  if [[ ! -L "$TARGET" ]]; then
    echo "nothing to remove at $TARGET"
    return 0
  fi
  if ! target_is_ours; then
    echo "$TARGET is not our symlink; leaving it alone" >&2
    exit 1
  fi
  rm "$TARGET"
  echo "removed: $TARGET"
}

case "${1:-install}" in
  install) cmd_install ;;
  status)  cmd_status ;;
  remove)  cmd_remove ;;
  *)       echo "usage: $0 [install|status|remove]" >&2; exit 2 ;;
esac
