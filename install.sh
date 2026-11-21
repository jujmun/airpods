#!/bin/sh
# Install the hands-free scroller for this user.
#
#   ./install.sh              build the app, install the Python bits, add `airpods-scroll` to ~/.local/bin
#   ./install.sh --uninstall  remove what this script created (the clone and the app bundle stay)
#
# Everything lives outside the checkout: the venv in ~/.local/share/airpods-scroll, the command in
# ~/.local/bin. Nothing is installed system-wide and nothing needs sudo.
set -eu

REPO=$(cd "$(dirname "$0")" && pwd)
DATA="${XDG_DATA_HOME:-$HOME/.local/share}/airpods-scroll"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/airpods-scroll"

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  rm -f "$LAUNCHER"
  rm -rf "$DATA"
  say "removed $LAUNCHER and $DATA"
  say "the app bundle in $REPO/airpods.app is untouched; delete the clone to remove it"
  exit 0
fi

# ---- checks -------------------------------------------------------------------------------------
[ "$(uname -s)" = "Darwin" ] || die "macOS only: the app uses CoreMotion, Vision and Quartz"
sw_vers -productVersion | awk -F. '{ exit ($1 >= 14) ? 0 : 1 }' || die "macOS 14 or later is required"
command -v swift >/dev/null 2>&1 || die "install Xcode Command Line Tools first: xcode-select --install"
command -v python3 >/dev/null 2>&1 || die "python3 not found: install it with 'brew install python'"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || die "python 3.10 or later is required"

# ---- build the app ------------------------------------------------------------------------------
if [ ! -x "$REPO/airpods.app/Contents/MacOS/airpods" ]; then
  say "building airpods.app (first time only)…"
  (cd "$REPO" && ./build.sh)
else
  say "airpods.app already built"
fi

# ---- python environment -------------------------------------------------------------------------
say "creating a virtualenv in $DATA"
mkdir -p "$DATA" "$BIN_DIR"
[ -x "$DATA/venv/bin/python" ] || python3 -m venv "$DATA/venv"
"$DATA/venv/bin/pip" install --quiet --upgrade pip
"$DATA/venv/bin/pip" install --quiet pyobjc-framework-Quartz

# ---- launcher -----------------------------------------------------------------------------------
cat > "$LAUNCHER" <<EOF
#!/bin/sh
# Start the motion stream if it is not already running, then scroll hands-free.
#
#   airpods-scroll            start
#   airpods-scroll --toggle   pause or resume gestures, without stopping the stream
#   airpods-scroll --stop     stop it
#   airpods-scroll --demo     pass anything else through to scroll.py
#
# Control-C also stops it, and the stream this script started is stopped with it.
set -eu
APP="$REPO/airpods.app/Contents/MacOS/airpods"
PYTHON="$DATA/venv/bin/python"
PORT=\${AIRPODS_PORT:-9999}
PIDFILE="$DATA/scroll.pid"

case "\${1:-}" in
  --toggle)
    [ -f "\$PIDFILE" ] || { echo "airpods-scroll is not running" >&2; exit 1; }
    kill -USR1 "\$(cat "\$PIDFILE")"
    exit 0 ;;
  --stop)
    [ -f "\$PIDFILE" ] || { echo "airpods-scroll is not running" >&2; exit 1; }
    kill "\$(cat "\$PIDFILE")"
    exit 0 ;;
esac

started=""
child=""
cleanup() {
  rm -f "\$PIDFILE"
  [ -n "\$child" ] && kill "\$child" 2>/dev/null || true
  [ -n "\$started" ] && kill "\$started" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

if ! "$DATA/venv/bin/python" - "\$PORT" <<'PY'
import socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)      # something is already streaming
PY
then :; else
  "\$APP" --stream-only 127.0.0.1 "\$PORT" >/dev/null 2>&1 &
  started=\$!
  sleep 1
fi

"\$PYTHON" "$REPO/game/scroll.py" --port "\$PORT" "\$@" &
child=\$!
echo \$child > "\$PIDFILE"
status=0
wait "\$child" || status=\$?
exit "\$status"
EOF
chmod +x "$LAUNCHER"

say ""
say "installed: $LAUNCHER"
case ":$PATH:" in
  *":$BIN_DIR:"*) say "run it with: airpods-scroll" ;;
  *) say "add $BIN_DIR to PATH, then run: airpods-scroll" ;;
esac
say ""
say "two permissions, both one-time:"
say "  1. Accessibility, for the terminal that runs airpods-scroll (so it can send scroll events)"
say "  2. Motion & Fitness for airpods.app, which macOS asks for on first launch"
say ""
say "opening the Accessibility pane now"
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility" >/dev/null 2>&1 || true
