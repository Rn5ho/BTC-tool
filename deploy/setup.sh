#!/usr/bin/env bash
# ============================================================
# BTC Polymarket Edge Finder — Hetzner VPS Setup Script
# Run as root on a fresh Ubuntu 22.04+ / Debian 12+ server.
# ============================================================
set -euo pipefail

APP_USER="btcedge"
APP_DIR="/home/${APP_USER}/BTC-tool"
REPO_URL="https://github.com/Rn5ho/BTC-tool.git"
SERVICE_NAME="btc-edge"

echo "=== BTC Edge Finder — Server Setup ==="

# -------------------------------------------------------
# 1. System packages
# -------------------------------------------------------
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git

# -------------------------------------------------------
# 2. Create dedicated user (if not exists)
# -------------------------------------------------------
echo "[2/6] Creating user '${APP_USER}'..."
if id "${APP_USER}" &>/dev/null; then
    echo "  User '${APP_USER}' already exists, skipping."
else
    useradd -m -s /bin/bash "${APP_USER}"
    echo "  User '${APP_USER}' created."
fi

# -------------------------------------------------------
# 3. Clone / update repository
# -------------------------------------------------------
echo "[3/6] Setting up repository..."
if [ -d "${APP_DIR}/.git" ]; then
    echo "  Repo already exists, pulling latest..."
    sudo -u "${APP_USER}" git -C "${APP_DIR}" pull origin main
else
    sudo -u "${APP_USER}" git clone "${REPO_URL}" "${APP_DIR}"
fi

# -------------------------------------------------------
# 4. Python virtual environment + dependencies
# -------------------------------------------------------
echo "[4/6] Setting up Python environment..."
sudo -u "${APP_USER}" bash -c "
    cd ${APP_DIR}
    python3 -m venv venv
    source venv/bin/activate
    pip install --upgrade pip -q
    pip install -e . -q
"

# -------------------------------------------------------
# 5. Create .env if it doesn't exist
# -------------------------------------------------------
echo "[5/6] Checking .env configuration..."
ENV_FILE="${APP_DIR}/.env"
if [ -f "${ENV_FILE}" ]; then
    echo "  .env already exists, skipping."
else
    sudo -u "${APP_USER}" cp "${APP_DIR}/.env.example" "${ENV_FILE}"
    echo "  .env created from .env.example."
    echo "  >>> IMPORTANT: Edit ${ENV_FILE} to add your Telegram credentials! <<<"
fi

# -------------------------------------------------------
# 6. Install and start systemd service
# -------------------------------------------------------
echo "[6/6] Installing systemd service..."
cp "${APP_DIR}/deploy/btc-edge.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Useful commands:"
echo "  systemctl status ${SERVICE_NAME}    # Check service status"
echo "  journalctl -u ${SERVICE_NAME} -f    # Follow systemd logs"
echo "  tail -f ${APP_DIR}/btc_edge.log     # Follow app logs"
echo "  systemctl restart ${SERVICE_NAME}   # Restart the service"
echo "  systemctl stop ${SERVICE_NAME}      # Stop the service"
echo ""
echo "Config: ${ENV_FILE}"
echo "Logs:   ${APP_DIR}/btc_edge.log"
