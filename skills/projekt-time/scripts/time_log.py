#!/usr/bin/env python3
"""time_log.py — batch time logging, timers and roll-ups for Projekt issues.

Modes (subcommands):
  log     Batch-log time entries from a CSV/JSON sheet of
          {issue, date, minutes, note?} rows.
  timer   Start or stop a timer on a single issue (timer-start / timer-stop).
  summary Roll-up totals for an issue via GET .../time-summary (server math).

DRY-RUN BY DEFAULT for every write. `log` and `timer` print a plan and write
nothing until you pass --apply. `summary` is read-only.

Endpoints (see ../projekt/references/endpoints.md → "Time tracking"):
  POST /projects/{pid}/issues/{iid}/time-entries
  POST /projects/{pid}/issues/{iid}/time-entries/timer-start | timer-stop
  GET  /projects/{pid}/issues/{iid}/time-summary

Issue resolution: a row's `issue` may be a UUID id OR a human key (PRJ-123).
context.json caches projects+members only (not issues), so keys are resolved
with GET /issues?q=<key> (exact `key` match), which also yields project_id.
Resolutions are memoised so each unique issue is queried at most once per run.

Idempotency: each posted entry is deduped on (issue_id, date, note) via the
shared Ledger; re-runs create 0. Timers are idempotent server-side
(timer-start returns 200 "Timer already running"); timer-stop rounds elapsed
to the nearest minute, minimum 1 min.

stdlib only · Python 3.10+ · never prints the token (fingerprint only).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import sys
import pathlib

# ── shared client (path is install-location independent) ──
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "projekt" / "scripts" / "lib"))
from projekt_api import Client, Ledger, slim, eprint  # noqa: E402

PHASE = "time"
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# ───────────────────────── helpers ─────────────────────────
def _is_uuid(s: str) -> bool:
    return bool(_UUID_RE.match(s.strip()))


def _today() -> dt.date:
    return dt.datetime.now().date()


def _parse_date(raw: str) -> dt.date | None:
    raw = (raw or "").strip()
    if not _DATE_RE.match(raw):
        return None
    try:
        return dt.date.fromisoformat(raw)
    except ValueError:
        return None


def _parse_minutes(raw) -> int | None:
    """Accept int-like minutes. Returns None if unparseable (caller flags it)."""
    try:
        # tolerate "30", "30.0", 30, 30.0 — but reject fractions that don't round clean
        f = float(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if f != int(f):
        # round to nearest minute, matching the server's minimum-1-minute rule
        f = round(f)
    return int(f)


def _dedupe_key(issue_id: str, date: str, note: str) -> str:
    return "%s|%s|%s" % (issue_id, date, (note or "").strip())


def _truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ───────────────────────── row loading ─────────────────────────
def load_rows(path: pathlib.Path) -> list[dict]:
    """Read CSV or JSON. Normalize to {issue,date,minutes,note} dicts."""
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json" or (suffix not in (".csv", ".tsv") and text.lstrip()[:1] in "[{"):
        data = json.loads(text)
        if isinstance(data, dict):
            for k in ("rows", "data", "entries"):
                if isinstance(data.get(k), list):
                    data = data[k]
                    break
        if not isinstance(data, list):
            raise SystemExit("✗ JSON must be a list of row objects (or {rows:[…]}).")
        raw_rows = data
    else:
        delim = "\t" if suffix == ".tsv" else ","
        raw_rows = list(csv.DictReader(text.splitlines(), delimiter=delim))

    out: list[dict] = []
    for r in raw_rows:
        if not isinstance(r, dict):
            raise SystemExit("✗ Each row must be an object with issue/date/minutes.")
        out.append({
            "issue": str(r.get("issue") or r.get("issue_id") or r.get("key") or "").strip(),
            "date": str(r.get("date") or "").strip(),
            "minutes": r.get("minutes", r.get("duration_minutes", r.get("mins"))),
            "note": str(r.get("note") or r.get("description") or r.get("comment") or "").strip(),
        })
    return out


# ───────────────────────── issue resolution ─────────────────────────
class Resolver:
    """Resolve an issue ref (UUID or key) → (issue_id, project_id), memoised."""

    def __init__(self, client: Client):
        self.c = client
        self._cache: dict[str, tuple[str, str] | None] = {}
        # pre-warm key→project from any cached issues (context caches none today,
        # but tolerate a future shape without extra queries).
        self._ctx_projects = {p.get("id"): p for p in client.context().get("projects", [])}

    def resolve(self, ref: str) -> tuple[str, str] | None:
        ref = (ref or "").strip()
        if not ref:
            return None
        if ref in self._cache:
            return self._cache[ref]
        result = self._resolve_uuid(ref) if _is_uuid(ref) else self._resolve_key(ref)
        self._cache[ref] = result
        return result

    def _resolve_uuid(self, iid: str) -> tuple[str, str] | None:
        try:
            st, data = self.c.request("GET", "/issues/%s" % iid)
        except SystemExit as e:  # network error after retries — don't kill the batch
            eprint("  ! lookup failed for %s: %s" % (iid, e))
            return None
        if st == 404:
            return None
        if st == 403:
            raise SystemExit("✗ 403 on issue %s — cross-org token. Switch org / token "
                             "(see references/errors.md)." % iid)
        if not (200 <= st < 300) or not isinstance(data, dict):
            eprint("  ! GET /issues/%s → %s" % (iid, st))
            return None
        pid = data.get("project_id")
        return (iid, pid) if pid else None

    def _resolve_key(self, key: str) -> tuple[str, str] | None:
        try:
            st, data = self.c.request("GET", "/issues?q=%s&limit=200" % key)
        except SystemExit as e:  # network error after retries — don't kill the batch
            eprint("  ! lookup failed for %s: %s" % (key, e))
            return None
        if st == 403:
            raise SystemExit("✗ 403 searching for %s — cross-org token. Switch org / token." % key)
        if not (200 <= st < 300):
            eprint("  ! GET /issues?q=%s → %s" % (key, st))
            return None
        rows = data if isinstance(data, list) else (data.get("data") or data.get("issues") or [])
        norm = key.lower()
        for o in rows:
            if isinstance(o, dict) and str(o.get("key", "")).lower() == norm:
                iid, pid = o.get("id"), o.get("project_id")
                if iid and pid:
                    return (iid, pid)
        return None


# ───────────────────────── log mode ─────────────────────────
def cmd_log(args, c: Client) -> int:
    rows = load_rows(pathlib.Path(args.sheet))
    if not rows:
        eprint("✗ No rows found in %s" % args.sheet)
        return 1

    led = Ledger()
    resolver = Resolver(c)
    today = _today()

    plan: list[dict] = []   # rows we'd post (action=create)
    skips: list[dict] = []  # rows blocked, deduped, or unresolved
    batch_keys: set[str] = set()  # in-sheet dedupe (Ledger.seen only sees committed lines)

    for i, r in enumerate(rows, 1):
        issue_ref, date_raw, note = r["issue"], r["date"], r["note"]
        minutes = _parse_minutes(r["minutes"])
        d = _parse_date(date_raw)

        # ── validation (collect reasons, never silently drop) ──
        if not issue_ref:
            skips.append({"row": i, "issue": "—", "reason": "missing issue ref"}); continue
        if minutes is None:
            skips.append({"row": i, "issue": issue_ref, "reason": "minutes not a number"}); continue
        if minutes <= 0:
            skips.append({"row": i, "issue": issue_ref, "reason": "minutes<=0 (%s)" % minutes}); continue
        if d is None:
            skips.append({"row": i, "issue": issue_ref, "reason": "bad date '%s' (want YYYY-MM-DD)" % date_raw}); continue
        if d > today:
            skips.append({"row": i, "issue": issue_ref, "reason": "future date %s" % d}); continue

        resolved = resolver.resolve(issue_ref)
        if not resolved:
            skips.append({"row": i, "issue": issue_ref, "reason": "issue not found in this org"}); continue
        iid, pid = resolved

        dk = _dedupe_key(iid, str(d), note)
        if led.seen("time", dk):
            skips.append({"row": i, "issue": issue_ref, "reason": "already logged (dedupe)"}); continue
        if dk in batch_keys:
            skips.append({"row": i, "issue": issue_ref, "reason": "duplicate row in sheet (dedupe)"}); continue
        batch_keys.add(dk)

        plan.append({
            "row": i, "issue": issue_ref, "issue_id": iid, "project_id": pid,
            "date": str(d), "minutes": minutes, "note": note, "dk": dk,
        })

    _print_log_plan(plan, skips, c, applying=args.apply)

    if not args.apply:
        eprint("\nDry-run only. Re-run with --apply to post %d entr%s."
               % (len(plan), "y" if len(plan) == 1 else "ies"))
        return 0
    if not plan:
        eprint("\nNothing to post.")
        return 0

    posted = failed = 0
    for p in plan:
        body = {"duration_minutes": p["minutes"], "date": p["date"]}
        if p["note"]:
            body["description"] = p["note"]
        path = "/projects/%s/issues/%s/time-entries" % (p["project_id"], p["issue_id"])
        st, data = c.request("POST", path, body)
        if st == 201 or 200 <= st < 300:
            ref = data.get("id") if isinstance(data, dict) else None
            led.add(PHASE, "time", p["dk"], "created", ref=ref)
            posted += 1
            print("  ✓ %-12s %s  %dm" % (p["issue"], p["date"], p["minutes"]))
        elif st == 400:
            led.add(PHASE, "time", p["dk"], "skipped", ref="400 duration<=0")
            failed += 1
            eprint("  ✗ %-12s rejected (400: duration_minutes<=0)" % p["issue"])
        elif st == 422:
            led.add(PHASE, "time", p["dk"], "skipped", ref="422 validation")
            failed += 1
            eprint("  ✗ %-12s 422 validation: %s" % (p["issue"], _msg(data)))
        elif st == 403:
            eprint("  ✗ %-12s 403 cross-org — stopping." % p["issue"])
            return 2
        else:
            led.add(PHASE, "time", p["dk"], "error", ref=str(st))
            failed += 1
            eprint("  ✗ %-12s HTTP %s: %s" % (p["issue"], st, _msg(data)))

    print("\n%s  posted=%d  failed=%d  skipped=%d  (ledger: %s)"
          % ("done" if not failed else "done with errors", posted, failed, len(skips), led.summary()))
    return 0 if not failed else 1


def _print_log_plan(plan, skips, c: Client, applying: bool) -> None:
    print("Projekt time — batch log   org=%s   token=%s" % (c.org or "?", c.fingerprint()))
    print("Mode: %s\n" % ("APPLY (writing)" if applying else "DRY-RUN (no writes)"))
    if plan:
        print("Will log %d entr%s:" % (len(plan), "y" if len(plan) == 1 else "ies"))
        print("  %-12s %-10s %7s  %-30s %s" % ("ISSUE", "DATE", "MINUTES", "NOTE", "ACTION"))
        print("  " + "-" * 74)
        total = 0
        for p in plan:
            total += p["minutes"]
            print("  %-12s %-10s %7d  %-30s %s"
                  % (_truncate(p["issue"], 12), p["date"], p["minutes"],
                     _truncate(p["note"], 30), "create"))
        print("  " + "-" * 74)
        print("  %-12s %-10s %7d  (%d entries)" % ("TOTAL", "", total, len(plan)))
    else:
        print("Will log 0 entries.")
    if skips:
        print("\nSkipped / blocked (%d) — NOT logged:" % len(skips))
        for s in skips:
            print("  · row %-3s %-12s — %s" % (s["row"], _truncate(s["issue"], 12), s["reason"]))


# ───────────────────────── timer mode ─────────────────────────
def cmd_timer(args, c: Client) -> int:
    resolver = Resolver(c)
    resolved = resolver.resolve(args.issue)
    if not resolved:
        eprint("✗ Issue '%s' not found in this org." % args.issue)
        return 1
    iid, pid = resolved
    verb = args.action  # start | stop
    led = Ledger()
    base = "/projects/%s/issues/%s/time-entries" % (pid, iid)
    path = "%s/timer-%s" % (base, verb)

    print("Projekt time — timer %s   org=%s   token=%s" % (verb, c.org or "?", c.fingerprint()))
    print("Issue %s  (id=%s)" % (args.issue, iid))
    print("Mode: %s\n" % ("APPLY (writing)" if args.apply else "DRY-RUN (no writes)"))
    print("  POST %s%s" % (path, "  body={description}" if (verb == "stop" and args.note) else ""))

    if not args.apply:
        if verb == "start":
            eprint("\nDry-run. timer-start is idempotent (200 'already running'). Re-run with --apply.")
        else:
            eprint("\nDry-run. timer-stop rounds elapsed to ≥1 min and creates one entry. Re-run with --apply.")
        return 0

    body = None
    if verb == "stop" and args.note:
        body = {"description": args.note}
    st, data = c.request("POST", path, body)

    if verb == "start":
        if st == 201:
            tid = data.get("timer_id") if isinstance(data, dict) else None
            led.add(PHASE, "timer-start", iid, "created", ref=tid)
            print("  ✓ timer started (id=%s, started_at=%s)"
                  % (tid, (data or {}).get("started_at")))
            return 0
        if st == 200:
            # idempotent: already running — not an error
            led.add(PHASE, "timer-start", iid, "ok", ref="already-running")
            print("  • timer already running: %s" % _msg(data))
            return 0
    else:  # stop
        if 200 <= st < 300:
            mins = (data or {}).get("duration_minutes")
            entry = (data or {}).get("entry") if isinstance(data, dict) else None
            eid = entry.get("id") if isinstance(entry, dict) else None
            led.add(PHASE, "timer-stop", iid, "created", ref=eid)
            print("  ✓ timer stopped → entry %s  (%s min, rounded ≥1)" % (eid, mins))
            return 0
        if st == 404:
            eprint("  • no active timer to stop for this issue (404).")
            return 0

    if st == 403:
        eprint("  ✗ 403 cross-org — switch org / token (references/errors.md).")
        return 2
    eprint("  ✗ timer-%s → HTTP %s: %s" % (verb, st, _msg(data)))
    return 1


# ───────────────────────── summary mode ─────────────────────────
def cmd_summary(args, c: Client) -> int:
    resolver = Resolver(c)
    resolved = resolver.resolve(args.issue)
    if not resolved:
        eprint("✗ Issue '%s' not found in this org." % args.issue)
        return 1
    iid, pid = resolved
    data = c.get_json("/projects/%s/issues/%s/time-summary" % (pid, iid))
    if not isinstance(data, dict):
        eprint("✗ Unexpected time-summary payload.")
        return 1

    total = data.get("total_minutes", 0) or 0
    count = data.get("entry_count", 0) or 0
    print("Time summary — issue %s  (id=%s)   org=%s" % (args.issue, iid, c.org or "?"))
    print("  total: %d min (%.2f h)   entries: %d" % (total, total / 60.0, count))
    by_user = data.get("by_user") or []
    if by_user:
        print("  by user:")
        for u in by_user:
            um = u.get("total_minutes", 0) or 0
            print("    · %-24s %6d min (%.2f h)  [%d]"
                  % (_truncate(u.get("user_name") or u.get("user_id") or "?", 24),
                     um, um / 60.0, u.get("entry_count", 0) or 0))
    return 0


def _msg(data) -> str:
    if isinstance(data, dict):
        return data.get("message") or data.get("error") or json.dumps(data)
    return str(data)


# ───────────────────────── CLI ─────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="time_log.py",
        description="Batch-log time, drive timers, and roll up time-summary for Projekt issues. "
                    "Dry-run by default; pass --apply to write.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("log", help="batch-log entries from a CSV/JSON sheet "
                                    "{issue,date,minutes,note?}")
    pl.add_argument("sheet", help="path to .csv/.tsv/.json with rows {issue,date,minutes,note?}")
    pl.add_argument("--apply", action="store_true", help="execute writes (default: dry-run)")
    pl.set_defaults(func=cmd_log)

    pt = sub.add_parser("timer", help="start/stop a timer on one issue")
    pt.add_argument("action", choices=["start", "stop"])
    pt.add_argument("issue", help="issue key (PRJ-123) or UUID id")
    pt.add_argument("--note", default="", help="description for the entry (stop only)")
    pt.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    pt.set_defaults(func=cmd_timer)

    ps = sub.add_parser("summary", help="print server-side time roll-up for one issue (read-only)")
    ps.add_argument("issue", help="issue key (PRJ-123) or UUID id")
    ps.set_defaults(func=cmd_summary)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        c = Client()
    except SystemExit as e:
        eprint(str(e))
        return 1
    return args.func(args, c)


if __name__ == "__main__":
    sys.exit(main())
