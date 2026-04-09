#!/usr/bin/env bash
# install.sh — Install gs232-bridge onto the deployment machine
# Usage: ./install.sh [--user USERNAME]
# Defaults to the current user if --user is not specified.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "[INFO]  $*"; }
warn()  { echo "[WARN]  $*" >&2; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Args
# ---------------------------------------------------------------------------

DEPLOY_USER="${1:-$USER}"
INSTALL_DIR="/opt/gs232_bridge"
SERVICE="gs232-bridge@${DEPLOY_USER}.service"

# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

[[ $EUID -eq 0 ]] || die "run as root: sudo ./install.sh"
id "$DEPLOY_USER" &>/dev/null || die "user '$DEPLOY_USER' does not exist"
python3 -c "import select, termios, os, configparser, threading, signal" 2>/dev/null \
    || die "Python 3 with standard library required"

# ---------------------------------------------------------------------------
# Install files
# ---------------------------------------------------------------------------

info "Installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp main.py gpio_backend.py controller.py watchdog.py serial_port.py gs232_parser.py "$INSTALL_DIR/"

# Only copy config if it doesn't already exist — preserve live config on upgrade
if [[ ! -f "$INSTALL_DIR/config.ini" ]]; then
    info "Installing default config.ini"
    cp config.ini "$INSTALL_DIR/"
else
    info "Existing config.ini preserved (diff below)"
    diff "$INSTALL_DIR/config.ini" config.ini || true
fi

chown -R "$DEPLOY_USER:$DEPLOY_USER" "$INSTALL_DIR"

info "Installing gs232-mklink helper"
cp gs232-mklink /usr/local/bin/gs232-mklink
chmod 755 /usr/local/bin/gs232-mklink
chown root:root /usr/local/bin/gs232-mklink
chmod 755 "$INSTALL_DIR"
chmod 644 "$INSTALL_DIR"/*.py "$INSTALL_DIR/config.ini"

# ---------------------------------------------------------------------------
# systemd
# ---------------------------------------------------------------------------

info "Installing systemd service"
cp gs232-bridge@.service /etc/systemd/system/
systemctl daemon-reload

# ---------------------------------------------------------------------------
# Enable + start (or restart if already running)
# ---------------------------------------------------------------------------

if systemctl is-active --quiet "$SERVICE"; then
    info "Restarting $SERVICE"
    systemctl restart "$SERVICE"
else
    info "Enabling and starting $SERVICE"
    systemctl enable "$SERVICE"
    systemctl start  "$SERVICE"
fi

systemctl status "$SERVICE" --no-pager --lines=10

info "Done. Reload config at any time with: systemctl reload $SERVICE"
