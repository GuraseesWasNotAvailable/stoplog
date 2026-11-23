#!/usr/bin/env bash
# Install stoplog on a Raspberry Pi (Raspberry Pi OS Bookworm or newer; 64-bit recommended).
# Run from the repo:  bash deploy/install-rpi.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="${SUDO_USER:-$USER}"

echo "Installing system packages"
sudo apt-get update
sudo apt-get install -y python3-venv python3-pip libjpeg-dev zlib1g-dev
# Camera tools: rpicam-apps for the Pi camera module, fswebcam for a USB webcam.
sudo apt-get install -y rpicam-apps || sudo apt-get install -y rpicam-apps-lite || \
  echo "rpicam-apps not installed (fine if you use a USB webcam or phone photos only)"
sudo apt-get install -y fswebcam || echo "fswebcam not installed"
sudo usermod -aG video "$USER_NAME"

echo "Creating the Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

[ -f "$APP_DIR/.env" ] || cp "$APP_DIR/config.example.env.txt" "$APP_DIR/.env"
[ -f "$APP_DIR/stoplog.yaml" ] || cp "$APP_DIR/stoplog.example.yaml" "$APP_DIR/stoplog.yaml"
chmod 600 "$APP_DIR/.env"

echo "Installing the systemd service"
sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@USER@|$USER_NAME|g" "$APP_DIR/deploy/stoplog.service" \
  | sudo tee /etc/systemd/system/stoplog.service >/dev/null
sudo systemctl daemon-reload

cat <<EOF

Done. Next:
  1. Edit $APP_DIR/.env (API key, sheet id, credentials file) and $APP_DIR/stoplog.yaml (shifts).
  2. Copy your Google service-account JSON next to .env.
  3. $APP_DIR/.venv/bin/stoplog init
  4. $APP_DIR/.venv/bin/stoplog capture-test   (check the framing of capture-test.jpg)
  5. sudo systemctl enable --now stoplog
     journalctl -u stoplog -f                   (live log)
EOF
