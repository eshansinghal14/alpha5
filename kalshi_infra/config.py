"""
Strategy config: dataclass sections whose fields have NO defaults, loaded
strictly from a strategy's config.json.

Every tunable value lives in the JSON file, never in code. A section is a
dataclass built from `param(...)` fields; a strategy's top-level config is
a dataclass of sections. Loading requires every field, rejects unknown
keys, type-checks and bounds-checks -- with no fallback value anywhere, a
missing or misspelled key has to fail loudly or it would silently vanish.

Field metadata drives the dashboard (help text, bounds, step) and says when
a change may take effect:
  apply="live"        -- setattr on the running object, effective next decision
  apply="next_event"  -- held as pending until the session rolls over (things
                         baked in at event start: feed scope, MC draw count)

Calibration notes ("measured against X on date Y") belong in the `help` or
a comment beside the field here; the number itself belongs in config.json.
"""
from __future__ import annotations

import dataclasses
import json
import os
import time
import types
import typing

APPLY_LIVE = "live"
APPLY_NEXT_EVENT = "next_event"


class ConfigError(ValueError):
    pass


def param(help: str, *, min: float | None = None, max: float | None = None,
          step: float | None = None, apply: str = APPLY_LIVE, editable: bool = True):
    """A required config field. `editable=False` hides it from the dashboard
    form (still loaded and validated)."""
    if apply not in (APPLY_LIVE, APPLY_NEXT_EVENT):
        raise ValueError(f"apply must be {APPLY_LIVE!r} or {APPLY_NEXT_EVENT!r}")
    return dataclasses.field(metadata={"help": help, "min": min, "max": max, "step": step,
                                       "apply": apply, "editable": editable})


def section(cls):
    """Decorator for a config section: a keyword-only dataclass, so fields
    with no defaults can be declared in any order."""
    return dataclasses.dataclass(kw_only=True)(cls)


# ---------------------------------------------------------------------------
# Type handling
# ---------------------------------------------------------------------------

def _hints(cls) -> dict:
    return typing.get_type_hints(cls)


def _is_section(tp) -> bool:
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def _unwrap_optional(tp):
    """(inner_type, allows_none)."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        if len(args) == 1 and len(typing.get_args(tp)) == 2:
            return args[0], True
        raise ConfigError(f"unsupported union type {tp}")
    return tp, False


def _coerce(value, tp, where: str):
    inner, nullable = _unwrap_optional(tp)
    if value is None:
        if nullable:
            return None
        raise ConfigError(f"{where}: null not allowed")
    # bool is an int subclass; never let true/false pass as a number or vice versa.
    if inner is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{where}: expected bool, got {value!r}")
        return value
    if inner is int:
        if isinstance(value, bool) or not isinstance(value, int):
            if isinstance(value, float) and value.is_integer():
                return int(value)
            raise ConfigError(f"{where}: expected int, got {value!r}")
        return value
    if inner is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{where}: expected number, got {value!r}")
        return float(value)
    if inner is str:
        if not isinstance(value, str):
            raise ConfigError(f"{where}: expected string, got {value!r}")
        return value
    origin = typing.get_origin(inner)
    if origin in (dict, list):
        expected = origin
        if not isinstance(value, expected):
            raise ConfigError(f"{where}: expected {expected.__name__}, got {value!r}")
        if origin is list and typing.get_args(inner) == (str,) and not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{where}: expected a list of strings, got {value!r}")
        return value
    raise ConfigError(f"{where}: unsupported field type {tp}")


def _check_bounds(value, meta: dict, where: str):
    if value is None or isinstance(value, (bool, str, dict, list)):
        return
    lo, hi = meta.get("min"), meta.get("max")
    if lo is not None and value < lo:
        raise ConfigError(f"{where}: {value} < min {lo}")
    if hi is not None and value > hi:
        raise ConfigError(f"{where}: {value} > max {hi}")


def build(cls, raw: dict, where: str = ""):
    """Construct `cls` from `raw`, strictly. Nested sections recurse."""
    if not isinstance(raw, dict):
        raise ConfigError(f"{where or cls.__name__}: expected object, got {type(raw).__name__}")
    hints = _hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    missing = [n for n in fields if n not in raw]
    unknown = [k for k in raw if k not in fields]
    errs = []
    if missing:
        errs.append(f"missing {', '.join(missing)}")
    if unknown:
        errs.append(f"unknown {', '.join(unknown)}")
    if errs:
        raise ConfigError(f"{where or cls.__name__}: " + "; ".join(errs))
    kwargs = {}
    for name, f in fields.items():
        path = f"{where}.{name}" if where else name
        tp = hints[name]
        if _is_section(tp):
            kwargs[name] = build(tp, raw[name], path)
        else:
            v = _coerce(raw[name], tp, path)
            _check_bounds(v, f.metadata, path)
            kwargs[name] = v
    return cls(**kwargs)


def to_dict(cfg) -> dict:
    return dataclasses.asdict(cfg)


def describe(cls, prefix: str = "") -> list[dict]:
    """Flat field list for UIs: dotted name, type name, nullable, metadata."""
    out = []
    hints = _hints(cls)
    for f in dataclasses.fields(cls):
        path = f"{prefix}.{f.name}" if prefix else f.name
        tp = hints[f.name]
        if _is_section(tp):
            out.extend(describe(tp, path))
            continue
        inner, nullable = _unwrap_optional(tp)
        out.append({"name": path, "type": getattr(inner, "__name__", str(inner)),
                    "nullable": nullable, **f.metadata})
    return out


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and not k.startswith("_"):
            nested = _flatten(v, path)
            # Leaf dict-typed params (rare) are flattened too; set_dotted
            # handles both shapes, so this stays a pure diff aid.
            out.update(nested if nested else {path: v})
        else:
            out[path] = v
    return out


def get_dotted(cfg, dotted: str):
    obj = cfg
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def set_dotted(cfg, dotted: str, value):
    parts = dotted.split(".")
    obj = cfg
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def field_meta(cls, dotted: str) -> dict:
    parts = dotted.split(".")
    tp = cls
    for part in parts[:-1]:
        tp = _hints(tp)[part]
    return {f.name: f for f in dataclasses.fields(tp)}[parts[-1]].metadata


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def _replace_with_retry(tmp: str, path: str, attempts: int = 10, delay_s: float = 0.05):
    # Windows refuses os.replace while another process holds the target open,
    # and the runner and dashboard both poll these files every second.
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay_s)


def atomic_write_json(path: str, obj, indent: int | None = 2):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    # dumps, not dump: dump always streams through the pure-Python encoder, and
    # ARBS's 2 MB quote-id file took ~12% of its event loop that way (10-31).
    text = json.dumps(obj, indent=indent)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.write("\n")
    _replace_with_retry(tmp, path)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _section_raw(doc: dict, key: str | None, path: str) -> dict:
    if key is None:
        return doc
    if key not in doc:
        raise ConfigError(f"{path}: no section {key!r} (have {', '.join(doc)})")
    return doc[key]


def load(cls, path: str, key: str | None = None):
    """Strictly load `cls` from `path` (optionally the top-level `key`
    section, for files that hold several series)."""
    return build(cls, _section_raw(read_json(path), key, path), key or "")


def update_file(cls, path: str, key: str | None, changes: dict[str, object]):
    """Apply dotted-name `changes` to the file, validating the WHOLE result
    before writing. Used by the dashboard so an invalid edit never reaches
    disk. Returns the validated config."""
    doc = read_json(path)
    raw = _section_raw(doc, key, path)
    for dotted, value in changes.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        if parts[-1] not in node:
            raise ConfigError(f"unknown field {dotted}")
        node[parts[-1]] = value
    cfg = build(cls, raw, key or "")
    atomic_write_json(path, doc)
    return cfg


# ---------------------------------------------------------------------------
# Hot reload
# ---------------------------------------------------------------------------

class ConfigStore:
    """Owns one running config object and keeps it in sync with its file.

    The object is mutated IN PLACE, so code holding a reference to a section
    (the quoter's `cfg.quoting`, say) sees live changes without re-wiring.
    """

    def __init__(self, cls, path: str, key: str | None = None):
        self.cls = cls
        self.path = path
        self.key = key
        self.config = load(cls, path, key)
        self._mtime = os.path.getmtime(path)
        self.pending: dict[str, object] = {}
        self.last_error: str | None = None
        self.last_applied: dict[str, tuple] = {}

    def poll(self) -> list[str]:
        """Re-read the file if it changed. Live fields are applied now,
        next_event fields are queued. An invalid file is rejected whole and
        the running config is left untouched. Returns change descriptions."""
        try:
            mtime = os.path.getmtime(self.path)
        except OSError as e:
            self.last_error = f"cannot stat config: {e}"
            return []
        if mtime <= self._mtime:
            return []
        try:
            new = load(self.cls, self.path, self.key)
        except (ConfigError, json.JSONDecodeError, OSError) as e:
            # A half-written file looks like a JSONDecodeError; don't advance
            # mtime so the next poll retries it.
            if isinstance(e, json.JSONDecodeError):
                return []
            self._mtime = mtime
            self.last_error = str(e)
            return [f"REJECTED config change: {e}"]
        self._mtime = mtime
        self.last_error = None
        old_flat, new_flat = _flatten(to_dict(self.config)), _flatten(to_dict(new))
        changes = []
        self.pending = {}
        for name, v in new_flat.items():
            cur = old_flat.get(name)
            if v == cur:
                continue
            if field_meta(self.cls, name)["apply"] == APPLY_NEXT_EVENT:
                self.pending[name] = v
                changes.append(f"{name} {cur} -> {v} (pending next event)")
            else:
                set_dotted(self.config, name, v)
                self.last_applied[name] = (cur, v)
                changes.append(f"{name} {cur} -> {v}")
        return changes

    def apply_pending(self) -> list[str]:
        """Apply queued next_event changes. Call between events."""
        out = []
        for name, v in self.pending.items():
            cur = get_dotted(self.config, name)
            set_dotted(self.config, name, v)
            out.append(f"{name} {cur} -> {v}")
        self.pending = {}
        return out

    def status(self) -> dict:
        return {"path": self.path, "key": self.key, "error": self.last_error,
                "pending": dict(self.pending)}
