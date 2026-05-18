#!/usr/bin/env bash
# Install vimax-mcp as a per-user launchd agent.
#
# Idempotent: safe to re-run. Substitutes user-specific paths into the
# template, drops the result into ~/Library/LaunchAgents, then bootstraps
# and kickstarts the agent.
#
# Usage:
#   ./scripts/install-launchd.sh          # install or refresh
#   ./scripts/install-launchd.sh status   # show launchctl print + log tails
#   ./scripts/install-launchd.sh remove   # bootout and delete the plist

set -euo pipefail

LABEL="com.zcdeng.vimax-mcp"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEMPLATE="$REPO_DIR/launchd/$LABEL.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET="$TARGET_DIR/$LABEL.plist"
GUI_TARGET="gui/$(id -u)"
LOG_DIR="$HOME/projects/ViMax/.working_dir/logs"
ENV_FILE="$HOME/projects/ViMax/.env"

ensure_prereqs() {
  if [[ ! -f "$TEMPLATE" ]]; then
    echo "missing template: $TEMPLATE" >&2; exit 1
  fi
  if ! command -v uv >/dev/null 2>&1; then
    echo "uv not on PATH; please install it first (https://docs.astral.sh/uv/)" >&2
    exit 1
  fi
  if [[ ! -d "$HOME/projects/ViMax" ]]; then
    echo "warn: ~/projects/ViMax not found; the server will fail when running jobs until ViMax is cloned there" >&2
  fi
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "warn: $ENV_FILE not found; secrets won't load" >&2
  fi
  mkdir -p "$LOG_DIR" "$TARGET_DIR"
}

render_plist() {
  local uv_path
  uv_path="$(command -v uv)"
  sed \
    -e "s|__USER_HOME__|$HOME|g" \
    -e "s|__UV_PATH__|$uv_path|g" \
    "$TEMPLATE" > "$TARGET"
  # Validate before doing anything more drastic.
  plutil -lint "$TARGET" >/dev/null
  echo "rendered: $TARGET"
}

bootstrap_agent() {
  # bootout is best-effort; the first install has no prior agent to remove.
  launchctl bootout "$GUI_TARGET/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$GUI_TARGET" "$TARGET"
  launchctl kickstart -k "$GUI_TARGET/$LABEL"
  echo "agent bootstrapped: $LABEL"
}

cmd_install() {
  ensure_prereqs
  render_plist
  bootstrap_agent
  echo
  echo "tail -f $LOG_DIR/mcp.out.log $LOG_DIR/mcp.err.log to inspect"
  echo "verify with: curl -sI http://127.0.0.1:7801/sse"
}

cmd_status() {
  launchctl print "$GUI_TARGET/$LABEL" 2>&1 | sed -n '1,30p' || true
  echo
  echo "--- mcp.out.log (tail) ---"
  tail -n 20 "$LOG_DIR/mcp.out.log" 2>/dev/null || echo "(no stdout log yet)"
  echo
  echo "--- mcp.err.log (tail) ---"
  tail -n 20 "$LOG_DIR/mcp.err.log" 2>/dev/null || echo "(no stderr log yet)"
}

cmd_remove() {
  launchctl bootout "$GUI_TARGET/$LABEL" 2>/dev/null || true
  rm -f "$TARGET"
  echo "removed agent and $TARGET"
}

case "${1:-install}" in
  install) cmd_install ;;
  status)  cmd_status ;;
  remove)  cmd_remove ;;
  *)       echo "usage: $0 [install|status|remove]" >&2; exit 2 ;;
esac
