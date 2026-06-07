#!/usr/bin/env python3
"""assign_and_move.py — assign issues then move them to a target column, in bulk.

Uses POST /issues/bulk, which MUTATES existing issues only (it does NOT create —
use bulk_issue_create.py for that). Two server actions are sent in order:
  1. {issue_ids:[…], action:"assignee", value:<user_id>}   (only if --assignee given)
  2. {issue_ids:[…], action:"status",   value:<target>}

Assignee rule (references/errors.md): an issue cannot leave Backlog/To Do for a
working column (In Progress / In Review / Done) without an assignee_id (422 /
blocked_unassigned). So when the target is a working column we PRE-FILTER: any issue
that is still unassigned AFTER the optional assign step is held back and reported as
"needs owner" — it is never sent into the move, and the rest of the batch proceeds.

Issues are given as keys (e.g. WEB-12) or ids. Keys are resolved against a GET /issues
sweep of each referenced project (from context); ids are used as-is.

Examples:
  python3 assign_and_move.py --issues WEB-12,WEB-13 --assignee jane@acme.com --status "In Progress"
  python3 assign_and_move.py --issues WEB-12,WEB-13 --assignee jane@acme.com --status "In Progress" --apply
  python3 assign_and_move.py --issues a1b2,c3d4 --status Done --apply   # already-assigned only
"""
from __future__ import annotations
import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "projekt" / "scripts" / "lib"))
from projekt_api import Client, Ledger, slim, eprint  # noqa: E402

NON_WORKING = {"backlog", "to do", "todo", "to-do"}


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _is_working(status: str) -> bool:
    s = _norm(status)
    return bool(s) and s not in NON_WORKING


def resolve_assignee(ctx: dict, raw: str) -> str:
    raw = (raw or "").strip()
    for m in ctx.get("members", []):
        for key in (m.get("email"), m.get("name"), m.get("user_id")):
            if key and _norm(str(key)) == _norm(raw):
                return m.get("user_id")
    names = ", ".join(sorted(m.get("name") or m.get("email") or "?" for m in ctx.get("members", [])))
    raise SystemExit("✗ Assignee %r not in context members.\n  Known: %s" % (raw, names or "(none)"))


def looks_like_key(token: str) -> bool:
    """Project keys look like ABC-123; ids are long/uuid-ish."""
    return "-" in token and any(ch.isdigit() for ch in token.split("-")[-1])


def fetch_project_issues(c: Client, pid: str) -> list[dict]:
    out: list[dict] = []
    offset, page = 0, 200
    while True:
        data = c.get_json("/issues?project_id=%s&limit=%d&offset=%d" % (pid, page, offset))
        rows = data if isinstance(data, list) else (
            data.get("data") or data.get("issues") or [] if isinstance(data, dict) else [])
        if not rows:
            break
        out.extend(rows)
        if len(rows) < page:
            break
        offset += page
    return out


def resolve_issues(c: Client, ctx: dict, tokens: list[str]) -> tuple[list[dict], list[str]]:
    """Map key/id tokens -> issue dicts {id,key,status,assignee_id}. Returns (found, unresolved)."""
    keys = {t for t in tokens if looks_like_key(t)}
    ids = {t for t in tokens if t not in keys}
    found: dict[str, dict] = {}

    # Resolve keys: sweep only the projects whose key prefixes the tokens.
    prefixes = {k.rsplit("-", 1)[0].upper() for k in keys}
    for p in ctx.get("projects", []):
        if not keys:
            break
        if (p.get("key") or "").upper() in prefixes:
            for r in fetch_project_issues(c, p["id"]):
                rk = (r.get("key") or "").upper()
                if rk and rk in {k.upper() for k in keys}:
                    found[rk] = slim("issue", r)

    out: list[dict] = []
    unresolved: list[str] = []
    for t in tokens:
        if t in ids:
            # Trust the id; we don't have its current state cached, so fetch detail to
            # learn assignee (needed for the working-column pre-filter).
            st, data = c.request("GET", "/issues/%s" % t)
            if 200 <= st < 300 and data:
                out.append(slim("issue", data) if isinstance(data, dict) else {"id": t})
            else:
                unresolved.append(t)
        else:
            hit = found.get(t.upper())
            if hit:
                out.append(hit)
            else:
                unresolved.append(t)
    return out, unresolved


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bulk assign + move issues via /issues/bulk (dry-run by default). Does NOT create.")
    ap.add_argument("--issues", required=True, help="Comma-separated issue keys (WEB-12) or ids.")
    ap.add_argument("--assignee", help="Email/name/id of the owner to set first (optional).")
    ap.add_argument("--status", required=True, help="Target column, e.g. 'In Progress', Done, Backlog.")
    ap.add_argument("--apply", action="store_true", help="Execute. Without it: dry-run only.")
    args = ap.parse_args()

    tokens = [t.strip() for t in args.issues.split(",") if t.strip()]
    if not tokens:
        raise SystemExit("✗ No issues given.")

    c = Client()
    ctx = c.context()
    if not ctx.get("org_id"):
        raise SystemExit("✗ No context. Run the projekt skill's auth_check.sh + context_sync.sh first.")

    assignee_id = resolve_assignee(ctx, args.assignee) if args.assignee else None
    target = args.status.strip()
    target_working = _is_working(target)

    eprint("Resolving %d issue(s)…" % len(tokens))
    issues, unresolved = resolve_issues(c, ctx, tokens)

    # Pre-filter: who will still be unassigned at MOVE time?
    # If we're assigning, every resolved issue gets the owner -> none unassigned.
    movable: list[dict] = []
    needs_owner: list[dict] = []
    for it in issues:
        will_have_owner = bool(assignee_id) or bool(it.get("assignee_id"))
        if target_working and not will_have_owner:
            needs_owner.append(it)
        else:
            movable.append(it)

    move_ids = [it["id"] for it in movable]
    assign_ids = [it["id"] for it in movable] if assignee_id else []

    # ---- plan table ----
    print("Token:  %s | org %s" % (c.fingerprint(), c.org))
    print("Target: %s%s" % (target, "  (working column → assignee required)" if target_working else ""))
    print("Owner:  %s" % (("%s (%s)" % (args.assignee, assignee_id)) if assignee_id else "— (leave as-is)"))
    print("Plan:   %d move · %d need owner (held back) · %d unresolved\n"
          % (len(movable), len(needs_owner), len(unresolved)))
    print("  %-7s  %-12s  %-14s  %-12s  %s" % ("ACTION", "KEY/ID", "CUR.STATUS", "CUR.OWNER", "→"))
    print("  " + "-" * 78)
    for it in movable:
        ref = it.get("key") or it.get("id")
        acts = ("assign+" if assignee_id else "") + "move"
        print("  %-7s  %-12.12s  %-14.14s  %-12.12s  %s"
              % (acts, ref, it.get("status") or "?", (it.get("assignee_id") or "—"), target))
    for it in needs_owner:
        ref = it.get("key") or it.get("id")
        print("  %-7s  %-12.12s  %-14.14s  %-12.12s  %s"
              % ("HOLD", ref, it.get("status") or "?", "—", "needs owner"))
    for t in unresolved:
        print("  %-7s  %-12.12s  %-14s  %-12s  %s" % ("?", t, "", "", "unresolved (bad key/id?)"))

    if needs_owner:
        print("\n  ⚠ %d issue(s) can't enter %r unassigned — pass --assignee, or assign them first."
              % (len(needs_owner), target))

    if not args.apply:
        print("\nDRY-RUN. Re-run with --apply. No writes were made.")
        return 0
    if not move_ids:
        print("\nNothing to move. No writes made.")
        return 1 if needs_owner else 0

    ledger = Ledger()
    rc = 0

    # ---- step 1: assignee ----
    if assign_ids and assignee_id:
        key = "assignee:%s:%s" % (assignee_id, ",".join(sorted(assign_ids)))
        if ledger.seen("issue.bulk", key):
            print("\n[1/2] assignee already applied (ledger) — skipping.")
        else:
            print("\n[1/2] POST /issues/bulk action=assignee → %d issue(s)…" % len(assign_ids))
            st, data = c.request("POST", "/issues/bulk",
                                 {"issue_ids": assign_ids, "action": "assignee", "value": assignee_id})
            if 200 <= st < 300:
                ledger.add("assign", "issue.bulk", key, "ok", ref="assignee")
                print("  ✓ assigned.")
            else:
                ledger.add("assign", "issue.bulk", key, "error", ref=str(st))
                msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
                print("  ✗ assign failed HTTP %s: %s" % (st, msg))
                if st == 403:
                    raise SystemExit("✗ 403 cross-org — wrong token/org. Aborting before move.")
                return 1  # don't move into a working column on a failed assign

    # ---- step 2: status ----
    key = "status:%s:%s" % (_norm(target), ",".join(sorted(move_ids)))
    if ledger.seen("issue.bulk", key):
        print("[2/2] status already applied (ledger) — skipping.")
    else:
        print("[2/2] POST /issues/bulk action=status → %d issue(s)…" % len(move_ids))
        st, data = c.request("POST", "/issues/bulk",
                             {"issue_ids": move_ids, "action": "status", "value": target})
        if 200 <= st < 300:
            blocked = data.get("blocked_unassigned") if isinstance(data, dict) else None
            ledger.add("move", "issue.bulk", key, "ok", ref=target)
            print("  ✓ moved %d issue(s) to %s." % (len(move_ids), target))
            if blocked:
                rc = 1
                print("  ⚠ server reported blocked_unassigned: %s — assign then re-run." % blocked)
        elif st == 422:
            ledger.add("move", "issue.bulk", key, "blocked", ref="422")
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
            print("  ⚠ 422 assignee-required: %s — assign owners then re-run." % msg)
            rc = 1
        elif st == 403:
            ledger.add("move", "issue.bulk", key, "error", ref="403")
            raise SystemExit("✗ 403 cross-org — wrong token/org.")
        else:
            ledger.add("move", "issue.bulk", key, "error", ref=str(st))
            msg = data.get("message") or data.get("error") if isinstance(data, dict) else str(data)
            print("  ✗ move failed HTTP %s: %s" % (st, msg))
            rc = 1

    print("\nDone. ledger %s" % ledger.summary())
    if needs_owner:
        print("⚠ %d held back as needs-owner (not moved)." % len(needs_owner))
        rc = rc or 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
