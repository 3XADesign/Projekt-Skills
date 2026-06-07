#!/usr/bin/env python3
"""bulk_issue_create.py — create Projekt issues in bulk from a CSV or JSON file.

There is NO bulk-create endpoint: /issues/bulk only MUTATES existing issues. So we
create one-at-a-time with sequential POST /issues (concurrency capped at 3).

Pipeline:
  1. Resolve the target project by --project (key or name) from .projekt-run/context.json.
  2. Read rows (CSV columns of assets/import_template.csv, or a JSON list).
  3. Resolve each row's assignee (email or name) -> user_id from context.members.
  4. Dedupe: sweep existing GET /issues for the project, skip any (project_id,title)
     or external_ref already present; also skip anything the Ledger has already created.
  5. DRY-RUN (default): print a create/skip table. Nothing is written.
  6. --apply: POST /issues for each create row, ≤3 in flight. Every create is logged
     to the Ledger so re-runs are idempotent and resumable.

Assignee rule (references/errors.md): an issue cannot LEAVE Backlog/To Do without an
assignee_id (422). Creating directly into a working column (In Progress / In Review /
Done) without an assignee is therefore unsafe — such rows are demoted to "needs owner"
and created in To Do instead (or skipped with --strict-status), never silently dropped.

Examples:
  python3 bulk_issue_create.py --project WEB --file backlog.csv          # dry-run
  python3 bulk_issue_create.py --project WEB --file backlog.csv --apply  # execute
"""
from __future__ import annotations
import argparse
import csv
import json
import pathlib
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "projekt" / "scripts" / "lib"))
from projekt_api import Client, Ledger, slim, eprint  # noqa: E402

# Canonical board columns. "Backlog"/"To Do" are the only columns an issue may be
# created into without an assignee per the assignee-required rule (errors.md).
NON_WORKING = {"backlog", "to do", "todo", "to-do"}
DEFAULT_STATUS = "Backlog"
SAFE_STATUS = "To Do"  # where unassigned working-column rows get parked
CSV_COLS = ("title", "description", "status", "assignee", "estimated_hours",
            "priority", "type", "labels", "external_ref")


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _is_working(status: str) -> bool:
    """True for any column an issue can't sit in without an assignee."""
    s = _norm(status)
    return bool(s) and s not in NON_WORKING


def load_rows(path: pathlib.Path) -> list[dict]:
    """Read a CSV (import_template columns) or a JSON list of row objects."""
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".json":
        data = json.loads(text)
        rows = data.get("issues", data) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise SystemExit("✗ JSON must be a list of issue objects (or {issues:[…]}).")
        return [dict(r) for r in rows]
    rdr = csv.DictReader(text.splitlines())
    return [{k: (v or "").strip() for k, v in row.items()} for row in rdr]


def resolve_project(ctx: dict, ref: str) -> dict:
    ref_n = _norm(ref)
    for p in ctx.get("projects", []):
        if _norm(p.get("key")) == ref_n or _norm(p.get("name")) == ref_n or str(p.get("id")) == ref:
            return p
    keys = ", ".join(sorted(f"{p.get('key')}" for p in ctx.get("projects", []) if p.get("key"))) or "(none)"
    raise SystemExit("✗ Project %r not in context. Known keys: %s\n"
                     "  Run the projekt skill's context_sync.sh first." % (ref, keys))


def build_member_index(ctx: dict) -> dict[str, dict]:
    """Map lowercased email AND name -> member, for assignee resolution."""
    idx: dict[str, dict] = {}
    for m in ctx.get("members", []):
        for key in (m.get("email"), m.get("name"), m.get("user_id")):
            if key:
                idx.setdefault(_norm(str(key)), m)
    return idx


def resolve_assignee(idx: dict[str, dict], raw: str) -> tuple[str | None, str | None]:
    """Return (user_id|None, error|None). Empty input -> (None, None) = unassigned."""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    m = idx.get(_norm(raw))
    if not m:
        return None, "unknown assignee %r" % raw
    return m.get("user_id"), None


def existing_sweep(c: Client, pid: str) -> tuple[set[str], set[str]]:
    """Sweep current issues of the project; return (titles_lower, external_refs)."""
    titles: set[str] = set()
    refs: set[str] = set()
    offset, page = 0, 200
    while True:
        data = c.get_json("/issues?project_id=%s&limit=%d&offset=%d" % (pid, page, offset))
        rows = data if isinstance(data, list) else (
            data.get("data") or data.get("issues") or [] if isinstance(data, dict) else [])
        if not rows:
            break
        for r in rows:
            if r.get("title"):
                titles.add(_norm(r["title"]))
            xr = r.get("external_ref") or r.get("externalRef")
            if xr:
                refs.add(str(xr).strip())
        if len(rows) < page:
            break
        offset += page
    return titles, refs


def plan_row(row: dict, pid: str, midx: dict[str, dict], ledger: Ledger,
             have_titles: set[str], have_refs: set[str], strict_status: bool) -> dict:
    """Classify one row into a create/skip plan entry (no writes)."""
    title = (row.get("title") or "").strip()
    if not title:
        return {"action": "skip", "title": "(blank)", "reason": "missing title"}

    ext = (row.get("external_ref") or "").strip()
    dedupe_key = ext or "%s|%s" % (pid, _norm(title))

    if _norm(title) in have_titles:
        return {"action": "skip", "title": title, "reason": "title exists in project"}
    if ext and ext in have_refs:
        return {"action": "skip", "title": title, "reason": "external_ref exists: %s" % ext}
    if ledger.seen("issue.create", dedupe_key):
        return {"action": "skip", "title": title, "reason": "already created (ledger)"}

    uid, aerr = resolve_assignee(midx, row.get("assignee", ""))
    status = (row.get("status") or "").strip() or DEFAULT_STATUS
    needs_owner = False
    note = None

    if aerr:  # unknown assignee -> create unassigned, surface the mismatch
        note = aerr
        uid = None

    # Assignee rule: a working column without an assignee is invalid. Park in To Do
    # and flag "needs owner" rather than failing or dropping the row.
    if _is_working(status) and not uid:
        if strict_status:
            return {"action": "skip", "title": title,
                    "reason": "working status %r without assignee (strict)" % status}
        needs_owner = True
        note = (note + "; " if note else "") + "working status %r demoted to %s (needs owner)" % (status, SAFE_STATUS)
        status = SAFE_STATUS

    payload: dict = {"project_id": pid, "title": title, "status": status}
    if row.get("description"):
        payload["description"] = row["description"]
    if uid:
        payload["assignee_id"] = uid
    if row.get("priority"):
        payload["priority"] = row["priority"].strip()
    if row.get("type"):
        payload["type"] = row["type"].strip()
    eh = (row.get("estimated_hours") or "").strip()
    if eh:
        try:
            payload["estimated_hours"] = float(eh) if "." in eh else int(eh)
        except ValueError:
            note = (note + "; " if note else "") + "bad estimated_hours %r ignored" % eh
    labels = (row.get("labels") or "").strip()
    if labels:
        payload["labels"] = [t.strip() for t in labels.replace(",", ";").split(";") if t.strip()]
    if ext:
        payload["external_ref"] = ext

    return {"action": "create", "title": title, "payload": payload,
            "dedupe_key": dedupe_key, "needs_owner": needs_owner, "note": note}


def print_plan(plan: list[dict], project: dict, c: Client) -> None:
    creates = [p for p in plan if p["action"] == "create"]
    skips = [p for p in plan if p["action"] == "skip"]
    owners = [p for p in creates if p.get("needs_owner")]
    print("Project: %s — %s (%s)" % (project.get("key"), project.get("name"), project.get("id")))
    print("Token:   %s | org %s" % (c.fingerprint(), c.org))
    print("Plan:    %d create · %d skip · %d need owner\n" % (len(creates), len(skips), len(owners)))
    print("  %-7s  %-40s  %-12s  %-10s  %s" % ("ACTION", "TITLE", "STATUS", "ASSIGNEE", "NOTE"))
    print("  " + "-" * 96)
    for p in plan:
        if p["action"] == "create":
            pl = p["payload"]
            print("  %-7s  %-40.40s  %-12s  %-10s  %s" % (
                "CREATE", p["title"], pl.get("status", ""),
                (pl.get("assignee_id") or "—")[:10], p.get("note") or ""))
        else:
            print("  %-7s  %-40.40s  %-12s  %-10s  %s" % ("skip", p["title"], "", "", p["reason"]))
    if owners:
        print("\n  ⚠ %d issue(s) need an owner (parked in %s, can't advance until assigned)."
              % (len(owners), SAFE_STATUS))


def do_create(c: Client, ledger: Ledger, entry: dict) -> tuple[dict, int, str]:
    st, data = c.request("POST", "/issues", entry["payload"])
    key = entry["dedupe_key"]
    if 200 <= st < 300:
        new = slim("issue", data)
        iid = (new[0] if isinstance(new, list) and new else new) if new else {}
        ref = iid.get("key") or iid.get("id") if isinstance(iid, dict) else None
        ledger.add("create", "issue.create", key, "created", ref=ref)
        return entry, st, "created %s" % (ref or "")
    if st == 422:
        ledger.add("create", "issue.create", key, "blocked", ref="422")
        msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
        return entry, st, "422 needs owner / validation: %s" % msg
    if st == 403:
        ledger.add("create", "issue.create", key, "error", ref="403")
        return entry, st, "403 cross-org — stop, wrong token/org"
    ledger.add("create", "issue.create", key, "error", ref=str(st))
    msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
    return entry, st, "HTTP %s: %s" % (st, msg)


def main() -> int:
    ap = argparse.ArgumentParser(description="Bulk-create Projekt issues from CSV/JSON (dry-run by default).")
    ap.add_argument("--project", required=True, help="Project key, name, or id (resolved from context).")
    ap.add_argument("--file", required=True, help="CSV (import_template columns) or .json list of rows.")
    ap.add_argument("--apply", action="store_true", help="Execute writes. Without it: dry-run only.")
    ap.add_argument("--strict-status", action="store_true",
                    help="Skip (don't demote) rows targeting a working column with no assignee.")
    ap.add_argument("--concurrency", type=int, default=3, help="Parallel POSTs (capped at 3).")
    args = ap.parse_args()

    path = pathlib.Path(args.file)
    if not path.exists():
        raise SystemExit("✗ File not found: %s" % path)

    c = Client()
    ctx = c.context()
    if not ctx.get("projects"):
        raise SystemExit("✗ No context. Run the projekt skill's auth_check.sh + context_sync.sh first.")

    project = resolve_project(ctx, args.project)
    pid = project["id"]
    midx = build_member_index(ctx)
    ledger = Ledger()
    rows = load_rows(path)
    if not rows:
        print("Nothing to do: file has 0 rows.")
        return 0

    eprint("Sweeping existing issues for dedupe…")
    have_titles, have_refs = existing_sweep(c, pid)
    eprint("  found %d titles, %d external_refs already in project." % (len(have_titles), len(have_refs)))

    plan = [plan_row(r, pid, midx, ledger, have_titles, have_refs, args.strict_status) for r in rows]
    print_plan(plan, project, c)

    creates = [p for p in plan if p["action"] == "create"]
    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply to create %d issue(s). No writes were made." % len(creates))
        return 0
    if not creates:
        print("\nNothing to create (all skipped). Ledger: %s" % ledger.summary())
        return 0

    conc = max(1, min(args.concurrency, 3))
    print("\nApplying: creating %d issue(s) at concurrency %d…" % (len(creates), conc))
    ok = blocked = err = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(do_create, c, ledger, e): e for e in creates}
        for fut in as_completed(futs):
            entry, st, msg = fut.result()
            if 200 <= st < 300:
                ok += 1
                tag = "✓"
            elif st == 422:
                blocked += 1
                tag = "⚠"
            else:
                err += 1
                tag = "✗"
            print("  %s %-40.40s %s" % (tag, entry["title"], msg))
            if st == 403:  # cross-org is fatal for the whole batch
                eprint("✗ 403 cross-org — aborting. Use the token bound to org %s." % c.org)
                break

    print("\nDone. created=%d blocked(needs owner)=%d error=%d | ledger %s"
          % (ok, blocked, err, ledger.summary()))
    if blocked:
        print("⚠ %d issue(s) were blocked as needs-owner — assign them, then re-run (idempotent)." % blocked)
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
