#!/bin/bash
# Run python for the review agent on the live box, inside a 1 GiB memory cap.
# Uncapped review queries read full-day journals (~2.5 GB) and got OOM-killed
# 16 times on 2026-10-12 on a 3.8 GB box that trading needs ~1.4 GB of; the
# cap makes such a query fail by itself instead of crowding out trading.
# Usage: deploy/review_py.sh -m kalshi_infra.runtime.trade_review --event ...
set -e
cd /opt/alpha5
exec sudo -n systemd-run --scope --quiet --collect -p MemoryMax=1G -p MemorySwapMax=0 \
    --uid=ubuntu --gid=ubuntu --working-directory=/opt/alpha5 \
    -- /bin/bash -c 'echo 1000 > /proc/self/oom_score_adj; exec nice -n 19 /opt/alpha5/.venv/bin/python "$@"' _ "$@"
