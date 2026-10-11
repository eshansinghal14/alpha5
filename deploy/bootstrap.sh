#!/usr/bin/env bash
# One-time server setup on Ubuntu 24.04 (arm64). Run as the default `ubuntu`
# user: bash bootstrap.sh <git-ref>
# Idempotent: re-running updates the code and venv and reloads the units.
set -euo pipefail

REF="${1:-main}"
APP=/opt/alpha5
REPO=https://github.com/eshansinghal14/alpha5.git

sudo timedatectl set-timezone UTC
# chrony keeps the clock to ~ms; this box's clock was 178 ms off NTP and every
# exchange-timestamp lag we measured carried that error.
sudo apt-get update -y
sudo apt-get install -y python3.12 python3.12-venv git chrony
sudo systemctl enable --now chrony

if [ ! -d "$APP/.git" ]; then
  sudo mkdir -p "$APP" && sudo chown ubuntu:ubuntu "$APP"
  git clone "$REPO" "$APP"
fi
cd "$APP"
git fetch --all --tags
git checkout "$REF"
git pull --ff-only || true

python3.12 -m venv "$APP/.venv"
"$APP/.venv/bin/pip" install --upgrade pip
"$APP/.venv/bin/pip" install -r deploy/requirements-live.txt

mkdir -p "$APP/data/live" "$APP/data/kalshi_crypto" "$APP/secrets"
chmod 700 "$APP/secrets"

sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo systemctl daemon-reload
echo "bootstrap done at $(git rev-parse --short HEAD). Copy secrets, then: sudo systemctl enable --now alpha5-crypto"
