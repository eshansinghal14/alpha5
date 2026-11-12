#!/bin/bash
# One-time setup of the scheduled Claude reviews (deploy/claude_report.sh).
# Idempotent. Needs /etc/alpha5/claude-token (claude setup-token, owner's
# account) and /etc/alpha5/report-topic-arn (the SNS topic) in place.
set -euo pipefail
U=alpha5-report
H=/var/lib/$U
id $U >/dev/null 2>&1 || sudo useradd --system --home-dir $H --create-home --shell /bin/bash $U
# Reads system logs; not in group ubuntu, so it can't write data/ or control files.
sudo usermod -aG systemd-journal $U
sudo mkdir -p /etc/alpha5
for f in claude-token report-topic-arn; do
  [ -f /etc/alpha5/$f ] || { echo "missing /etc/alpha5/$f"; exit 1; }
done
sudo chown $U:$U /etc/alpha5/claude-token && sudo chmod 600 /etc/alpha5/claude-token
[ -x $H/.local/bin/claude ] || sudo -u $U -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash'
# Email replies: the daily run (alpha5-report) writes proposals that the reply
# poller (ubuntu, which owns config.json) reads; the poller's decision log is
# read back by the daily run. Gmail app password: root:ubuntu 640.
sudo install -d -o $U -g ubuntu -m 750 /var/lib/alpha5-proposals
sudo install -d -o ubuntu -g $U -m 750 /var/lib/alpha5-replies
if [ -f /etc/alpha5/gmail-app-password ]; then
  sudo chown root:ubuntu /etc/alpha5/gmail-app-password && sudo chmod 640 /etc/alpha5/gmail-app-password
fi
sudo cp /opt/alpha5/deploy/systemd/alpha5-report-*.service /opt/alpha5/deploy/systemd/alpha5-report-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now alpha5-report-daily.timer alpha5-report-hourly.timer alpha5-report-replies.timer
systemctl list-timers 'alpha5-report-*' --no-pager
