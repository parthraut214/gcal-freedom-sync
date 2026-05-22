#!/bin/bash
# Run on vpn-node (192.168.1.115) as root or with sudo.
# Installs dependencies and sets up the systemd service.

set -euo pipefail

INSTALL_DIR=/opt/gcal-freedom-sync
SERVICE=gcal-freedom-sync

echo "=== Installing system dependencies ==="
apt-get update -q
apt-get install -y python3 python3-venv python3-pip \
    chromium-browser chromium-driver \
    libglib2.0-0 libnss3 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2

echo "=== Creating install directory ==="
mkdir -p "$INSTALL_DIR"

echo "=== Copying project files ==="
# Run this from your local machine instead:
#   rsync -av --exclude venv --exclude __pycache__ --exclude '*.pyc' \
#     ./ pi@192.168.1.115:/opt/gcal-freedom-sync/
# Or if running on the Pi directly after cloning:
cp -r ./* "$INSTALL_DIR/" 2>/dev/null || true

echo "=== Creating Python virtualenv ==="
cd "$INSTALL_DIR"
python3 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt

echo "=== Installing Playwright (using system Chromium) ==="
# Tell Playwright to use the system chromium instead of downloading its own
# (Playwright's downloaded Chromium may not have an ARM64 build for older Pi OS)
PLAYWRIGHT_BROWSERS_PATH=0 venv/bin/playwright install-deps chromium 2>/dev/null || true

# If system chromium works, set the path; otherwise let Playwright download
if command -v chromium-browser &>/dev/null; then
    echo "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=$(command -v chromium-browser)" >> "$INSTALL_DIR/.env"
    echo "Using system chromium: $(command -v chromium-browser)"
else
    venv/bin/playwright install chromium
fi

echo "=== Setting up log file ==="
touch /var/log/gcal-freedom-sync.log
chown pi:pi /var/log/gcal-freedom-sync.log

echo "=== Installing systemd service ==="
cp "$INSTALL_DIR/systemd/$SERVICE.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE"

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit $INSTALL_DIR/config.yaml — set your calendar_id"
echo "  2. Create $INSTALL_DIR/.env with FREEDOM_EMAIL and FREEDOM_PASSWORD"
echo "  3. Copy token.json to $INSTALL_DIR/ (run auth_google.py on your Mac first)"
echo "  4. Start the service: systemctl start $SERVICE"
echo "  5. Watch logs: journalctl -u $SERVICE -f"
echo "     or: tail -f /var/log/gcal-freedom-sync.log"
echo ""
echo "To test Freedom automation manually before enabling the service:"
echo "  cd $INSTALL_DIR && venv/bin/python freedom_client.py --debug start test"
