#!/usr/bin/env bash
# Install/refresh the TradeAI systemd units. Run as root from /opt/tradeai:
#   sudo APP_DIR=/opt/tradeai bash ./deploy/install.sh
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/tradeai}"
UNIT_DIR="/etc/systemd/system"

if [[ "$EUID" -ne 0 ]]; then
    echo "run as root: sudo APP_DIR=$APP_DIR $0" >&2
    exit 1
fi
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "missing venv: $APP_DIR/.venv/bin/python" >&2
    exit 1
fi

install -m 0644 "$APP_DIR"/deploy/tradeai.service "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-premarket.service "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-premarket.timer "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-daily-update.service "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-daily-update.timer "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-daily-review.service "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-daily-review.timer "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-backup.service "$UNIT_DIR"/
install -m 0644 "$APP_DIR"/deploy/tradeai-backup.timer "$UNIT_DIR"/
chmod 0755 "$APP_DIR"/deploy/backup.sh

systemctl daemon-reload
systemctl enable --now tradeai.service
systemctl enable --now tradeai-premarket.timer
systemctl enable --now tradeai-daily-update.timer
systemctl enable --now tradeai-daily-review.timer
systemctl enable --now tradeai-backup.timer

echo
systemctl --no-pager --lines=0 status tradeai.service || true
echo
systemctl list-timers 'tradeai-*' --no-pager || true
echo
echo "web:      systemctl status tradeai"
echo "logs:     journalctl -u tradeai -f"
echo "jobs:     journalctl -u tradeai-premarket -n 100 --no-pager"
