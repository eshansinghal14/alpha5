"""
Owner replies to the daily review email (deploy/claude_report.sh) approve or
reject its ideas, and approved config changes are applied to the live
config.json.

    extract  (alpha5-report, after Claude): pull the ```proposals block out of
             the reply, validate each change against the strategy's config
             class, save the valid ones under PROPOSALS_DIR, write the body
             without the block.
    poll     (ubuntu, alpha5-report-replies.timer): read the owner's replies
             from Gmail over IMAP, apply approved proposals through
             kalshi_infra.config.update_file (the dashboard's validated write;
             the running strategies hot-reload it), log every decision to
             DECISIONS, and email a confirmation.

Only the stored proposal is ever applied; the reply only says yes or no to
its number. Replies are read from the Sent Mail folder, which only the account
holder can write to, so mail spoofed into the inbox can't approve anything.
"""
from __future__ import annotations

import argparse
import email
import email.policy
import imaplib
import importlib
import json
import os
import re
import sys
import time
from email.mime.text import MIMEText

from kalshi_infra.config import ConfigError, build, read_json, update_file

PROPOSALS_DIR = "/var/lib/alpha5-proposals"
STATE_DIR = "/var/lib/alpha5-replies"
DECISIONS = f"{STATE_DIR}/decisions.jsonl"
PASSWORD_FILE = "/etc/alpha5/gmail-app-password"
OWNER_FILE = "/etc/alpha5/report-email"
# Series a proposal may change: {name: strategy package}.
SERIES = {"ARBS": "ARBS", "KXCRYPTOLEAD15M": "KXCRYPTOLEAD15M"}
BLOCK = re.compile(r"```proposals\s*\n(.*?)\n```\s*", re.S)
SUBJECT_DAY = re.compile(r"\[alpha5(?: ALERT)?\] Daily (\d\d-\d\d)")
YES = {"yes", "y", "approve", "approved", "apply", "ok", "do"}
NO = {"no", "n", "reject", "rejected", "skip", "keep", "don't", "dont"}
_WORDS = "|".join(sorted(YES | NO, key=len, reverse=True))
# "1 yes", "#2: no", and the other way round: "yes to 1", "reject #2".
VERDICT = re.compile(r"(?<![\d.$])#?(\d)\s*[:.)\-=]?\s*(" + _WORDS + r")\b", re.I)
VERDICT_REV = re.compile(r"\b(" + _WORDS + r")[ \t]+(?:to[ \t]+)?(?:idea[ \t]+)?#?(\d)(?![\d.%c$])", re.I)
ALL = -1


def _series(name: str) -> list:
    """The processes a proposal for `name` changes: ARBS is one strategy run
    as one process per Kalshi shard, each with its own config.json."""
    if name not in SERIES:
        raise ConfigError(f"series {name!r} isn't one a reply may change ({', '.join(SERIES)})")
    procs = importlib.import_module(f"kalshi_strategies.{SERIES[name]}").SERIES
    return list(procs.values()) if name == "ARBS" else [procs[name]]


def _node(raw: dict, dotted: str):
    node = raw
    parts = dotted.split(".")
    for p in parts[:-1]:
        if not isinstance(node, dict) or p not in node:
            raise ConfigError(f"unknown field {dotted}")
        node = node[p]
    if not isinstance(node, dict) or parts[-1] not in node:
        raise ConfigError(f"unknown field {dotted}")
    return node, parts[-1]


def resolve(proposal: dict) -> list[tuple[object, dict[str, object], dict[str, object]]]:
    """[(process, {key: new value}, {key: current value})] for each process
    whose config the proposal changes, against the configs as they are NOW,
    each validated as a whole. "add" and "remove" act on the current list, so
    edits made since the proposal stay. Raises if any config rejects it or
    none would change."""
    out = []
    for s in _series(proposal["series"]):
        try:
            out.append(_resolve_one(s, proposal["changes"]))
        except _NoChange:
            pass
    if not out:
        raise ConfigError("no change against the current config")
    return out


class _NoChange(Exception):
    pass


def _resolve_one(s, changes: list[dict]):
    doc = read_json(s.config_path)
    raw = doc if s.config_key is None else doc[s.config_key]
    new, old = {}, {}
    for ch in changes:
        key = ch["key"]
        node, leaf = _node(raw, key)
        cur = node[leaf]
        if "set" in ch:
            val = ch["set"]
        elif "add" in ch or "remove" in ch:
            if not isinstance(cur, list):
                raise ConfigError(f"{key} is not a list; use set")
            val = list(cur) + [x for x in ch.get("add", []) if x not in cur]
            val = [x for x in val if x not in ch.get("remove", [])]
        else:
            raise ConfigError(f"{key}: change needs set, add or remove")
        if cur is not None and val is not None and type(val) is not type(cur) \
                and not (isinstance(cur, (int, float)) and isinstance(val, (int, float))):
            raise ConfigError(f"{key}: {type(cur).__name__} -> {type(val).__name__}")
        old[key], new[key] = cur, val
        node[leaf] = val
    if not new or new == old:
        raise _NoChange
    build(s.config_cls, raw, s.config_key or "")
    return s, new, old


def summarize(name: str, targets: list) -> str:
    """One line per distinct change, e.g. "ARBS watch.exclude + X"."""
    lines = list(dict.fromkeys(describe(new, old) for _, new, old in targets))
    return f"{name} " + " | ".join(lines)


def describe(new: dict, old: dict) -> str:
    out = []
    for k, v in new.items():
        o = old[k]
        if isinstance(o, list) and isinstance(v, list):
            add, rem = [x for x in v if x not in o], [x for x in o if x not in v]
            out.append(f"{k}" + (f" + {', '.join(map(str, add))}" if add else "")
                       + (f" − {', '.join(map(str, rem))}" if rem else ""))
        else:
            out.append(f"{k} {json.dumps(o)} → {json.dumps(v)}")
    return "; ".join(out)


# ---------------------------------------------------------------- extract

def extract(reply_body: str, day: str) -> tuple[str, list[dict]]:
    """(body without the block, valid proposals with a "summary" of the change)."""
    m = BLOCK.search(reply_body)
    if not m:
        return reply_body, []
    body = (reply_body[:m.start()] + reply_body[m.end():]).rstrip() + "\n"
    try:
        props = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"proposals block isn't JSON: {e}", file=sys.stderr)
        return body, []
    valid = []
    for p in props if isinstance(props, list) else []:
        try:
            targets = resolve(p)
        except (ConfigError, KeyError, TypeError, ImportError) as e:
            print(f"proposal {p.get('n')} dropped: {e}", file=sys.stderr)
            continue
        valid.append({"day": day, "n": int(p["n"]), "series": p["series"], "title": p.get("title", ""),
                      "changes": p["changes"], "summary": summarize(p["series"], targets)})
    return body, valid


# ---------------------------------------------------------------- poll

def _text(msg) -> str:
    part = msg.get_body(preferencelist=("plain",)) if msg.is_multipart() else msg
    text = part.get_content() if part is not None else ""
    lines = []
    for line in text.splitlines():
        # The quoted original starts here; its idea list would read as votes.
        if re.match(r"^On .+wrote:\s*$", line) or line.startswith(">") or line.startswith("-----"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def verdicts(text: str) -> dict[int, str]:
    """{idea number: "yes" | "no" | "unclear"}; {ALL: "yes"} for "yes to all"."""
    out = {}
    for n, word in VERDICT.findall(text) + [(n, w) for w, n in VERDICT_REV.findall(text)]:
        v = "yes" if word.lower() in YES else "no"
        # Both readings of one number ("1 yes ... reject 1"): apply nothing.
        out[int(n)] = v if out.get(int(n), v) == v else "unclear"
    if not out and re.search(r"\b(all yes|yes (to )?all|approve all)\b", text, re.I):
        out = {ALL: "yes"}
    return out


def _load_json(path: str, default):
    try:
        return read_json(path)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _send(owner: str, subject: str, body: str):
    from kalshi_infra.runtime.report_email import send
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"], msg["From"], msg["To"] = subject, owner, owner
    send(msg)


def poll(days: int = 7, dry_run: bool = False):
    owner = open(OWNER_FILE).read().strip()
    password = open(PASSWORD_FILE).read().strip().replace(" ", "")
    seen_path = f"{STATE_DIR}/seen.json"
    seen = set(_load_json(seen_path, []))
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(owner, password)
    try:
        imap.select('"[Gmail]/Sent Mail"', readonly=True)
        since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - days * 86400))
        _, ids = imap.search(None, f'(SINCE {since} SUBJECT "[alpha5" SUBJECT "Daily")')
        msgs = []
        for i in ids[0].split():
            _, data = imap.fetch(i, "(BODY.PEEK[])")
            msgs.append(email.message_from_bytes(data[0][1], policy=email.policy.default))
    finally:
        imap.logout()
    for msg in msgs:
        mid = msg["Message-ID"] or ""
        subject = str(msg["Subject"] or "")
        m = SUBJECT_DAY.search(subject)
        if mid in seen or not m or not subject.lower().startswith("re:"):
            continue
        day = m.group(1)
        props = {p["n"]: p for p in _load_json(f"{PROPOSALS_DIR}/{day}.json", [])}
        text = _text(msg)
        votes = verdicts(text)
        if ALL in votes:
            votes = {n: "yes" for n in props}
        report = []
        for n, verdict in sorted(votes.items()):
            p = props.get(n)
            rec = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "day": day, "n": n,
                   "verdict": verdict, "title": p["title"] if p else None, "note": text[:2000]}
            if verdict == "yes" and p:
                try:
                    # Every process's config is validated before any is written.
                    targets = resolve(p)
                    rec["applied"] = summarize(p["series"], targets)
                    if not dry_run:
                        for s, new, _ in targets:
                            update_file(s.config_cls, s.config_path, s.config_key, new)
                    report.append(f"#{n} APPLIED: {rec['applied']}")
                except (ConfigError, KeyError, TypeError) as e:
                    rec["error"] = str(e)
                    report.append(f"#{n} NOT applied ({e}): {p['title']}")
            elif verdict == "unclear":
                report.append(f"#{n} read as both yes and no; nothing applied. Reply again with just '{n} yes' or '{n} no'.")
            elif verdict == "yes":
                report.append(f"#{n} approved, recorded (no automatic change; needs code or a manual edit)")
            else:
                report.append(f"#{n} rejected, recorded: future reports won't propose it again without new evidence")
            if not dry_run:
                with open(DECISIONS, "a") as fh:
                    fh.write(json.dumps(rec) + "\n")
        if not votes:
            # Kept as a note so the next review sees it.
            if not dry_run:
                with open(DECISIONS, "a") as fh:
                    fh.write(json.dumps({"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "day": day,
                                         "n": None, "verdict": "note", "note": text[:2000]}) + "\n")
            report.append("No '1 yes' / '2 no' found; your reply was saved as a note for the next review.")
        print(f"{day} {mid}: " + " | ".join(report))
        if not dry_run:
            _send(owner, f"Re: [alpha5] Daily {day}: reply processed", "\n".join(report) + "\n")
            seen.add(mid)
            from kalshi_infra.config import atomic_write_json
            atomic_write_json(seen_path, sorted(seen))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("extract")
    e.add_argument("--reply", required=True, help="Markdown body with the proposals block")
    e.add_argument("--out", required=True, help="body without the block")
    e.add_argument("--day", required=True, help="MM-DD in the email subject")
    p = sub.add_parser("poll")
    p.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "extract":
        with open(args.reply, encoding="utf-8") as fh:
            body, props = extract(fh.read(), args.day)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(body)
        with open(f"{PROPOSALS_DIR}/{args.day}.json", "w") as fh:
            json.dump(props, fh, indent=1)
        print(f"{len(props)} proposals")
    else:
        poll(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
