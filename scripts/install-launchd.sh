#!/bin/sh
# Install the package into Application Support and write a LaunchAgent.
# This does not load the agent. The plist points at that copy, not at a worktree.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)
UV=$(command -v uv || true)
if [ -z "$UV" ]; then
  echo "uv is not on PATH" >&2
  exit 1
fi
INSTALL_DIR=${INSTALL_DIR:-"$HOME/Library/LaunchAgents"}
LOG_DIR=${LOG_DIR:-"$HOME/Library/Logs"}
APP=${ISOBAR_APP_DIR:-"$HOME/Library/Application Support/isobar-data"}
APP_SRC="$APP/app"
case "$APP_SRC" in
  "$ROOT"|"$ROOT"/*)
    echo "refusing to install into the source tree" >&2
    exit 1
    ;;
esac
mkdir -p "$INSTALL_DIR" "$LOG_DIR" "$APP/bin" "$APP_SRC"
rm -rf "$APP_SRC/isobar_data" "$APP_SRC/config"
cp -R "$ROOT/isobar_data" "$APP_SRC/isobar_data"
cp -R "$ROOT/config" "$APP_SRC/config"
cp "$ROOT/pyproject.toml" "$ROOT/uv.lock" "$APP_SRC/"
(
  cd "$APP_SRC"
  "$UV" sync --frozen
)
python3 - "$ROOT/launchd/com.isobar.data.plist" \
  "$INSTALL_DIR/com.isobar.data.plist" \
  "$APP/bin/isobar-data-launchd" \
  "$APP_SRC" \
  "$LOG_DIR/isobar-data.log" \
  "$APP_SRC/.venv/bin/python" \
  "$APP_SRC/.venv/bin/isobar-data" << 'PY'
import pathlib, shlex, shutil, sys
source, dest, wrapper_path, app_src, log_file, python_bin, binary = sys.argv[1:]
package = pathlib.Path(app_src) / "isobar_data"
for cached in package.rglob("__pycache__"):
    shutil.rmtree(cached, ignore_errors=True)
text = pathlib.Path(source).read_text()

def esc(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

for key, value in (("__WRAPPER__", wrapper_path), ("__APP__", app_src)):
    text = text.replace(key, esc(value))
pathlib.Path(dest).write_text(text)
wrapper = """#!/bin/sh
set -eu
LOG={log}
PY={python_bin}
BIN={binary}
# uv's standalone Python has no system trust store; without this, HTTPS to hosts whose chain ends
# at a root outside OpenSSL's default paths (ECMWF's HARICA root) fails CERTIFICATE_VERIFY_FAILED.
SSL_CERT_FILE="$("$PY" -c 'import certifi; print(certifi.where())')"
export SSL_CERT_FILE
"$PY" -c 'import sys; from pathlib import Path; from isobar_data.retain import rotate_log; rotate_log(Path(sys.argv[1]))' "$LOG"
mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
exec "$BIN"
""".format(
    log=shlex.quote(log_file),
    python_bin=shlex.quote(python_bin),
    binary=shlex.quote(binary),
)
path = pathlib.Path(wrapper_path)
path.write_text(wrapper)
path.chmod(0o755)
PY

echo "wrote $INSTALL_DIR/com.isobar.data.plist"
echo "not loaded"
