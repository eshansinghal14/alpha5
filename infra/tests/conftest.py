

import pytest as _pytest


@_pytest.fixture(autouse=True)
def _cme_open(monkeypatch):
    """CME touches count whatever the wall clock says (cme_closed is tested on its own)."""
    from strategies.KXCRYPTOLEAD15M import perp_nowcast
    monkeypatch.setattr(perp_nowcast, "cme_closed", lambda t: False)
