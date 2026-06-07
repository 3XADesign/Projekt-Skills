#!/usr/bin/env python3
"""workload_report.py — deterministic per-member workload + capacity report (READ-ONLY).

Pulls the four server-side aggregates and does ALL the math here (zero model tokens):
    GET /workload?date_from=&date_to=   per-member assigned / in-progress / done / hours
    GET /workload/capacity              utilization vs capacity target
    GET /capacity                       per-member open + estimated load
    GET /capacity/threshold             org overload threshold (utilization %)

Renders a Markdown table (default) or CSV (--csv). Flags over-allocated members
(utilization > threshold) and under-allocated ones (utilization < under-threshold).

Default window = current ISO week (Mon→Sun). Override with --from/--to (YYYY-MM-DD).

This script never writes. It needs no --apply; it is dry-run-safe by construction.
Run the `projekt` skill's auth_check.sh + context_sync.sh first so member names
resolve from .projekt-run/context.json (we never re-query the roster).

Usage:
    python3 workload_report.py                         # current week, Markdown
    python3 workload_report.py --from 2026-06-01 --to 2026-06-07
    python3 workload_report.py --csv > workload.csv
    python3 workload_report.py --under 40              # flag <40% as under-allocated
    python3 workload_report.py --json                  # machine-readable summary
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "projekt" / "scripts" / "lib"))
from projekt_api import Client, slim, eprint  # noqa: E402

# Default fallback when the org has no /capacity/threshold configured.
DEFAULT_OVER_THRESHOLD = 100.0   # utilization % above which a member is over-allocated
DEFAULT_UNDER_THRESHOLD = 50.0   # utilization % below which a member is under-allocated


# ── window ────────────────────────────────────────────────────────────────────
def current_week() -> tuple[str, str]:
    """Monday→Sunday of the current ISO week as (from, to) in YYYY-MM-DD."""
    today = dt.date.today()
    monday = today - dt.timedelta(days=today.weekday())
    sunday = monday + dt.timedelta(days=6)
    return monday.isoformat(), sunday.isoformat()


def valid_date(s: str) -> str:
    try:
        return dt.date.fromisoformat(s).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD, got %r" % s)


# ── response normalization (endpoints differ in envelope shape) ─────────────────
def _rows(data) -> list:
    """Extract a list of per-member rows from any plausible envelope."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in ("members", "data", "rows", "workload", "users", "entries", "capacity"):
            v = data.get(k)
            if isinstance(v, list):
                return v
        # Some aggregates nest under {workload:{members:[…]}} etc.
        for v in data.values():
            if isinstance(v, dict):
                for k in ("members", "data", "rows", "users"):
                    if isinstance(v.get(k), list):
                        return v[k]
    return []


def _first(d: dict, *keys, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _uid(row: dict) -> str | None:
    v = _first(row, "user_id", "userId", "id", "member_id", "memberId")
    return str(v) if v is not None else None


# ── threshold ───────────────────────────────────────────────────────────────────
def parse_threshold(data) -> float | None:
    """Pull the org overload threshold (utilization %) from /capacity/threshold."""
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        v = _first(data, "threshold", "overload_threshold", "value", "utilization_threshold",
                   "max_utilization", "percent", "threshold_percent")
        if v is not None:
            return _num(v, None)
    return None


# ── merge ─────────────────────────────────────────────────────────────────────
def build_rows(workload, capacity_util, capacity, ctx) -> list[dict]:
    """Merge the per-member aggregates into one deterministic row per user_id."""
    names: dict[str, str] = {}
    roles: dict[str, str] = {}
    for m in ctx.get("members", []):
        uid = str(_first(m, "user_id", "id", default=""))
        if uid:
            names[uid] = _first(m, "name", "email", default=uid)
            roles[uid] = _first(m, "role", default="")

    merged: dict[str, dict] = {}

    def slot(uid: str, row: dict) -> dict:
        cur = merged.setdefault(uid, {
            "user_id": uid,
            "name": None, "role": "",
            "assigned": 0.0, "in_progress": 0.0, "done": 0.0,
            "hours_logged": 0.0, "hours_estimated": 0.0,
            "capacity_hours": 0.0, "utilization": None,
        })
        # Adopt a name/role if this row carries one (server may label rows directly).
        nm = _first(row, "name", "user_name", "member_name", "email")
        if nm and not cur["name"]:
            cur["name"] = nm
        rl = _first(row, "role")
        if rl and not cur["role"]:
            cur["role"] = rl
        return cur

    # 1) /workload — counts + logged hours over the window.
    for row in _rows(workload):
        uid = _uid(row)
        if not uid:
            continue
        s = slot(uid, row)
        s["assigned"] += _num(_first(row, "assigned", "assigned_count", "to_do", "todo", "open", default=0))
        s["in_progress"] += _num(_first(row, "in_progress", "in_progress_count", "inProgress",
                                         "active", "wip", default=0))
        s["done"] += _num(_first(row, "done", "done_count", "completed", "closed", default=0))
        s["hours_logged"] += _num(_first(row, "hours_logged", "logged_hours", "hours",
                                          "total_hours", "logged", default=0))
        s["hours_estimated"] += _num(_first(row, "estimated_hours", "hours_estimated",
                                            "estimate", default=0))

    # 2) /capacity — open + estimated load and (often) the capacity target in hours.
    for row in _rows(capacity):
        uid = _uid(row)
        if not uid:
            continue
        s = slot(uid, row)
        if not s["hours_estimated"]:
            s["hours_estimated"] = _num(_first(row, "estimated_hours", "hours_estimated",
                                               "open_hours", "load_hours", "estimate", default=0))
        s["capacity_hours"] = max(s["capacity_hours"], _num(_first(
            row, "capacity_hours", "capacity", "target_hours", "available_hours", "weekly_hours",
            default=0)))
        if not s["assigned"]:
            s["assigned"] = _num(_first(row, "open", "open_count", "assigned", default=0))

    # 3) /workload/capacity — the server's own utilization %, if it gives one.
    for row in _rows(capacity_util):
        uid = _uid(row)
        if not uid:
            continue
        s = slot(uid, row)
        util = _first(row, "utilization", "utilization_percent", "utilisation",
                      "percent", "usage", "load_percent")
        if util is not None:
            s["utilization"] = _num(util, None)
        s["capacity_hours"] = max(s["capacity_hours"], _num(_first(
            row, "capacity_hours", "capacity", "target_hours", "available_hours", default=0)))

    # Resolve names from context; finalize utilization deterministically.
    for uid, s in merged.items():
        s["name"] = names.get(uid) or s["name"] or uid
        if not s["role"]:
            s["role"] = roles.get(uid, "")
        if s["utilization"] is None:
            # Derive from estimated/logged vs capacity when the server didn't supply it.
            load = s["hours_estimated"] or s["hours_logged"]
            s["utilization"] = round(100.0 * load / s["capacity_hours"], 1) if s["capacity_hours"] else None

    return [merged[k] for k in sorted(merged, key=lambda u: (merged[u]["name"] or "").lower())]


def classify(rows: list[dict], over: float, under: float) -> None:
    for r in rows:
        u = r["utilization"]
        if u is None:
            r["flag"] = "no-capacity"
        elif u > over:
            r["flag"] = "OVER"
        elif u < under:
            r["flag"] = "under"
        else:
            r["flag"] = "ok"


# ── rendering ───────────────────────────────────────────────────────────────────
def _fnum(v) -> str:
    if v is None:
        return "—"
    f = float(v)
    return str(int(f)) if f.is_integer() else ("%.1f" % f)


def _util(v) -> str:
    return "—" if v is None else ("%g%%" % round(float(v), 1))


HEADERS = ["Member", "Role", "Assigned", "In progress", "Done",
           "Hours logged", "Est. hours", "Capacity", "Utilization", "Flag"]


def render_markdown(rows, frm, to, over, under, threshold_src) -> str:
    out: list[str] = []
    out.append("# Workload report — %s → %s" % (frm, to))
    out.append("")
    over_n = sum(1 for r in rows if r["flag"] == "OVER")
    under_n = sum(1 for r in rows if r["flag"] == "under")
    nocap_n = sum(1 for r in rows if r["flag"] == "no-capacity")
    out.append("- Members: **%d** · over-allocated: **%d** · under-allocated: **%d**%s"
               % (len(rows), over_n, under_n,
                  (" · no capacity set: **%d**" % nocap_n) if nocap_n else ""))
    out.append("- Over threshold: **%g%%** (%s) · Under threshold: **%g%%**"
               % (over, threshold_src, under))
    out.append("")
    out.append("| " + " | ".join(HEADERS) + " |")
    out.append("|" + "|".join(["---"] * len(HEADERS)) + "|")
    flag_label = {"OVER": "⚠️ OVER", "under": "↓ under", "ok": "ok", "no-capacity": "— n/a"}
    for r in rows:
        out.append("| " + " | ".join([
            r["name"], r["role"] or "—",
            _fnum(r["assigned"]), _fnum(r["in_progress"]), _fnum(r["done"]),
            _fnum(r["hours_logged"]), _fnum(r["hours_estimated"]),
            _fnum(r["capacity_hours"]) if r["capacity_hours"] else "—",
            _util(r["utilization"]), flag_label.get(r["flag"], r["flag"]),
        ]) + " |")
    if not rows:
        out.append("")
        out.append("_No workload data for this window._")
    if over_n:
        out.append("")
        out.append("**Over-allocated:** " + ", ".join(
            "%s (%s)" % (r["name"], _util(r["utilization"])) for r in rows if r["flag"] == "OVER"))
    if under_n:
        out.append("**Under-allocated:** " + ", ".join(
            "%s (%s)" % (r["name"], _util(r["utilization"])) for r in rows if r["flag"] == "under"))
    return "\n".join(out) + "\n"


def render_csv(rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "name", "role", "assigned", "in_progress", "done",
                "hours_logged", "hours_estimated", "capacity_hours", "utilization_pct", "flag"])
    for r in rows:
        w.writerow([r["user_id"], r["name"], r["role"],
                    _fnum(r["assigned"]), _fnum(r["in_progress"]), _fnum(r["done"]),
                    _fnum(r["hours_logged"]), _fnum(r["hours_estimated"]),
                    _fnum(r["capacity_hours"]) if r["capacity_hours"] else "",
                    "" if r["utilization"] is None else round(float(r["utilization"]), 1),
                    r["flag"]])
    return buf.getvalue()


def render_json(rows, frm, to, over, under) -> str:
    import json
    payload = {
        "window": {"from": frm, "to": to},
        "thresholds": {"over": over, "under": under},
        "summary": {
            "members": len(rows),
            "over_allocated": sum(1 for r in rows if r["flag"] == "OVER"),
            "under_allocated": sum(1 for r in rows if r["flag"] == "under"),
            "no_capacity": sum(1 for r in rows if r["flag"] == "no-capacity"),
        },
        "rows": rows,
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


# ── main ────────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Deterministic per-member workload + capacity report (READ-ONLY).")
    d_from, d_to = current_week()
    ap.add_argument("--from", dest="frm", type=valid_date, default=d_from,
                    help="window start YYYY-MM-DD (default: Monday of current week)")
    ap.add_argument("--to", dest="to", type=valid_date, default=d_to,
                    help="window end YYYY-MM-DD (default: Sunday of current week)")
    # Pre-format then escape '%' so argparse (3.14 validates help templates) won't re-parse it.
    over_help = ("over-allocation threshold %% (default: org /capacity/threshold or %g)"
                 % DEFAULT_OVER_THRESHOLD).replace("%", "%%")
    under_help = ("under-allocation threshold %% (default: %g)"
                  % DEFAULT_UNDER_THRESHOLD).replace("%", "%%")
    ap.add_argument("--over", type=float, default=None, help=over_help)
    ap.add_argument("--under", type=float, default=DEFAULT_UNDER_THRESHOLD, help=under_help)
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument("--csv", action="store_true", help="emit CSV instead of Markdown")
    fmt.add_argument("--json", action="store_true", help="emit JSON summary instead of Markdown")
    args = ap.parse_args()

    if args.frm > args.to:
        eprint("✗ --from (%s) is after --to (%s)." % (args.frm, args.to))
        return 2

    c = Client()
    eprint("Token: %s · org: %s · window: %s → %s"
           % (c.fingerprint(), c.org or "(none)", args.frm, args.to))
    ctx = c.context()
    if not ctx.get("members"):
        eprint("  ⚠️  No cached members in .projekt-run/context.json — names may show as ids. "
               "Run the projekt skill's auth_check.sh + context_sync.sh first.")

    # READ-ONLY fetches. get_json raises SystemExit with a friendly message on non-2xx.
    qs = "?date_from=%s&date_to=%s" % (args.frm, args.to)
    workload = c.get_json("/workload" + qs)
    capacity_util = c.get_json("/workload/capacity")
    capacity = c.get_json("/capacity")
    try:
        threshold_raw = c.get_json("/capacity/threshold")
    except SystemExit:
        threshold_raw = None  # endpoint may not be configured for every org

    # slim the raw payloads so nothing bulky is surfaced if the caller inspects stderr.
    _ = (slim("member", workload), slim("member", capacity))

    org_threshold = parse_threshold(threshold_raw)
    if args.over is not None:
        over, src = args.over, "flag"
    elif org_threshold is not None:
        over, src = org_threshold, "org"
    else:
        over, src = DEFAULT_OVER_THRESHOLD, "default"
    under = args.under

    rows = build_rows(workload, capacity_util, capacity, ctx)
    classify(rows, over, under)

    if args.csv:
        sys.stdout.write(render_csv(rows))
    elif args.json:
        sys.stdout.write(render_json(rows, args.frm, args.to, over, under) + "\n")
    else:
        sys.stdout.write(render_markdown(rows, args.frm, args.to, over, under, src))
    return 0


if __name__ == "__main__":
    sys.exit(main())
