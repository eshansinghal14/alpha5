import email
import email.policy
import json
from types import SimpleNamespace

import pytest

from infra.config import ConfigError, param, section
from infra.runtime import report_replies as rr


@pytest.mark.parametrize("text,want", [
    ("1 yes, 2 no", {1: "yes", 2: "no"}),
    ("1: yes\n2 - no keep SOL", {1: "yes", 2: "no"}),
    ("yes to 1, reject #2", {1: "yes", 2: "no"}),
    ("approve 2", {2: "yes"}),
    ("1 yes. reject 1", {1: "unclear"}),
    ("SOL lost 3c at 60s, no idea", {}),
    ("Yes all", {rr.ALL: "yes"}),
])
def test_verdicts(text, want):
    assert rr.verdicts(text) == want


def test_quoted_original_is_not_read_as_votes():
    raw = "Subject: Re: x\nContent-Type: text/plain\n\n1 yes\n\nOn Thu, Nov 13, 2026 at 8:01 AM <a@b> wrote:\n> 2 no\n"
    msg = email.message_from_string(raw, policy=email.policy.default)
    assert rr.verdicts(rr._text(msg)) == {1: "yes"}


@section
class _Watch:
    exclude: list[str] = param("x")
    size: float = param("x", min=0, max=10)


@section
class _Cfg:
    watch: _Watch


@pytest.fixture
def series(tmp_path, monkeypatch):
    """Two shard processes, like ARBS and ARBS3."""
    procs = []
    for name, exclude in (("config.json", ["A"]), ("config_shard3.json", ["A", "B"])):
        path = tmp_path / name
        path.write_text(json.dumps({"watch": {"exclude": exclude, "size": 1.0}}))
        procs.append(SimpleNamespace(config_cls=_Cfg, config_path=str(path), config_key=None))
    monkeypatch.setattr(rr, "_series", lambda name: procs)
    return procs


def test_resolve_adds_to_each_shards_current_list(series):
    targets = rr.resolve({"series": "ARBS", "changes": [{"key": "watch.exclude", "add": ["C"]}]})
    assert [new for _, new, _ in targets] == [{"watch.exclude": ["A", "C"]}, {"watch.exclude": ["A", "B", "C"]}]


def test_resolve_skips_a_shard_that_already_has_it(series):
    targets = rr.resolve({"series": "ARBS", "changes": [{"key": "watch.exclude", "add": ["B"]}]})
    assert [s.config_path for s, _, _ in targets] == [series[0].config_path]


@pytest.mark.parametrize("change", [
    {"key": "watch.missing", "set": 1},               # no new keys
    {"key": "watch.size", "set": "big"},              # type change
    {"key": "watch.size", "set": 50.0},               # out of bounds
    {"key": "watch.exclude", "add": ["A"]},           # no change
])
def test_resolve_rejects(series, change):
    with pytest.raises(ConfigError):
        rr.resolve({"series": "ARBS", "changes": [change]})


def test_extract_strips_block_and_keeps_valid(series):
    body = "## Ideas\n1. **x**\n\n```proposals\n" + json.dumps([
        {"n": 1, "series": "ARBS", "title": "t", "changes": [{"key": "watch.size", "set": 2.0}]},
        {"n": 2, "series": "ARBS", "title": "bad", "changes": [{"key": "nope", "set": 1}]}]) + "\n```\n"
    out, props = rr.extract(body, "11-13")
    assert "proposals" not in out and [p["n"] for p in props] == [1]
    # The same change on both shards reads as one line.
    assert props[0]["summary"] == "ARBS watch.size 1.0 → 2.0"
