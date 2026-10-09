import json
import os
import time

import pytest

from kalshi_infra.config import (APPLY_NEXT_EVENT, ConfigError, ConfigStore, build, describe,
                                 load, param, section, update_file)


@section
class Inner:
    size: float = param("size", min=0)
    cap: int | None = param("cap")
    scope: int = param("scope", apply=APPLY_NEXT_EVENT)


@section
class Top:
    inner: Inner
    enabled: bool = param("on")


RAW = {"inner": {"size": 1.0, "cap": None, "scope": 5}, "enabled": True}


def test_build_ok():
    cfg = build(Top, RAW)
    assert cfg.inner.size == 1.0 and cfg.inner.cap is None and cfg.enabled is True


def test_ints_accepted_for_floats():
    cfg = build(Top, {**RAW, "inner": {**RAW["inner"], "size": 2}})
    assert isinstance(cfg.inner.size, float)


@pytest.mark.parametrize("mutate,msg", [
    (lambda r: r["inner"].pop("size"), "missing size"),
    (lambda r: r["inner"].update(sizee=1), "unknown sizee"),
    (lambda r: r["inner"].update(size=-1), "< min"),
    (lambda r: r.update(enabled=1), "expected bool"),
    (lambda r: r["inner"].update(size=True), "expected number"),
    (lambda r: r["inner"].update(scope=None), "null not allowed"),
])
def test_build_rejects(mutate, msg):
    raw = json.loads(json.dumps(RAW))
    mutate(raw)
    with pytest.raises(ConfigError, match=msg):
        build(Top, raw)


def test_no_defaults_anywhere():
    with pytest.raises(TypeError):
        Inner()


def test_describe():
    names = {d["name"]: d for d in describe(Top)}
    assert set(names) == {"inner.size", "inner.cap", "inner.scope", "enabled"}
    assert names["inner.cap"]["nullable"] and names["inner.scope"]["apply"] == APPLY_NEXT_EVENT


def _write(path, doc):
    with open(path, "w") as fh:
        json.dump(doc, fh)
    # Filesystem mtime resolution: make sure the store sees a change.
    t = time.time() + 2
    os.utime(path, (t, t))


def test_store_live_pending_and_reject(tmp_path):
    p = str(tmp_path / "config.json")
    _write(p, {"S": RAW})
    store = ConfigStore(Top, p, key="S")
    held = store.config.inner           # sections mutate in place

    doc = {"S": json.loads(json.dumps(RAW))}
    doc["S"]["inner"]["size"] = 3.0
    doc["S"]["inner"]["scope"] = 9
    _write(p, doc)
    changes = store.poll()
    assert held.size == 3.0
    assert held.scope == 5 and store.pending == {"inner.scope": 9}
    assert any("pending" in c for c in changes)
    store.apply_pending()
    assert held.scope == 9 and store.pending == {}

    doc["S"]["inner"]["size"] = -5
    _write(p, doc)
    out = store.poll()
    assert out and out[0].startswith("REJECTED") and held.size == 3.0
    assert store.last_error


def test_update_file_validates_before_write(tmp_path):
    p = str(tmp_path / "config.json")
    _write(p, {"S": RAW})
    update_file(Top, p, "S", {"inner.size": 4.0})
    assert load(Top, p, "S").inner.size == 4.0
    with pytest.raises(ConfigError):
        update_file(Top, p, "S", {"inner.size": -1})
    assert load(Top, p, "S").inner.size == 4.0
