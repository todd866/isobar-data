#!/bin/sh
# Remove the Application Support copy and the LaunchAgent plist.
# This does not boot out a loaded agent.
set -eu

APP=${ISOBAR_APP_DIR:-"$HOME/Library/Application Support/isobar-data"}
INSTALL_DIR=${INSTALL_DIR:-"$HOME/Library/LaunchAgents"}
rm -rf "$APP"
rm -f "$INSTALL_DIR/com.isobar.data.plist"
echo "removed $APP"
echo "not loaded"
