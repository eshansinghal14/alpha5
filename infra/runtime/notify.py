"""Email the owner through the report SNS topic (the one the daily report and
shard funding use)."""
from __future__ import annotations

import subprocess
import threading

REGION = "us-east-2"
TOPIC_PATH = "/etc/alpha5/report-topic-arn"


def email(subject: str, body: str) -> str | None:
    """Blocking. None on success, else the error."""
    try:
        topic = open(TOPIC_PATH).read().strip()
        subprocess.run(["aws", "sns", "publish", "--region", REGION, "--topic-arn", topic,
                        "--subject", subject[:99], "--message", body, "--output", "text"],
                       capture_output=True, timeout=30, check=True)
        return None
    except Exception as e:
        return repr(e)


def email_async(subject: str, body: str, on_error=None):
    """email() on a daemon thread, for callers on an event loop."""
    def run():
        err = email(subject, body)
        if err and on_error:
            on_error(err)
    threading.Thread(target=run, daemon=True).start()
