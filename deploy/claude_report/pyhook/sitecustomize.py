"""Loaded by every python the scheduled reviews run (PYTHONPATH in
alpha5-report-*.service). alpha5-report can't read kalshi_infra/keys.py, which
is owner-only, yet importing the strategies reaches it through auth.py. A
stand-in with empty values lets them import; nothing a review runs signs a
request, and an authenticated call fails at Kalshi instead."""
import os
import sys
import types

if not os.access("/opt/alpha5/kalshi_infra/keys.py", os.R_OK):
    stub = types.ModuleType("kalshi_infra.keys")
    stub.__getattr__ = lambda name: ""
    sys.modules["kalshi_infra.keys"] = stub
