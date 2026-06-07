---
name: projekt-issues
description: >-
  Bulk-create and triage Projekt issues (projekt.3xa.es) from a CSV/JSON backlog, and
  assign-then-move issues across board columns safely. Use when the user wants to import
  a backlog, mass-create tasks, assign owners, or move issues to In Progress/Done. Soporta
  español: crear incidencias en lote, importar backlog, asignar responsables, mover tareas
  de columna, triaje. Enforces the assignee-required rule so unassigned issues never enter
  a working column.
allowed-tools: Read, Grep, Bash(python3:*), Bash(bash:*), Bash(jq:*)
---

# Projekt — issues (bulk create + assign/move)

Create issues in bulk from a file, and assign+move existing issues, with dry-run safety and
idempotent resume. Part of the `projekt` pipeline (phases CREATE + ASSIGN).

`SK="${CLAUDE_SKILL_DIR}/scripts"` — use it for every command below.

## Prerequisite (connect once)

Run the **`projekt`** skill's setup first; these scripts read `.projekt-run/context.json` and
never re-query identity:

```bash
bash "${CLAUDE_SKILL_DIR}/../projekt/scripts/auth_check.sh"
bash "${CLAUDE_SKILL_DIR}/../projekt/scripts/context_sync.sh"
```

If there's no token, see `skills/projekt/references/auth-setup.md`.

## 1. Bulk create — `bulk_issue_create.py`

Reads the columns of `skills/projekt/assets/import_template.csv`
(`title,description,status,assignee,estimated_hours,priority,type,labels,external_ref`) or a JSON
list. Resolves the project by key/name and each `assignee` (email or name) → `user_id` from context.
Dedupes against a live `GET /issues` sweep (by `title` and `external_ref`) **and** the Ledger.

```bash
# DRY-RUN: prints a create/skip table, writes nothing
python3 "$SK/bulk_issue_create.py" --project WEB --file backlog.csv

# APPLY: sequential POST /issues, ≤3 in flight, every create logged for resume
python3 "$SK/bulk_issue_create.py" --project WEB --file backlog.csv --apply
```

There is **no bulk-create endpoint** — `/issues/bulk` only *mutates* existing issues. So creation is
one POST per row at concurrency ≤3 (`--concurrency`, capped at 3). Re-running creates 0 (idempotent).

Flags: `--strict-status` skips (instead of demoting) rows that target a working column with no owner.

## 2. Assign + move — `assign_and_move.py`

Assigns an owner then moves issues to a target column via `POST /issues/bulk` — two actions in order:
`{action:"assignee",value}` then `{action:"status",value}`.

```bash
# DRY-RUN
python3 "$SK/assign_and_move.py" --issues WEB-12,WEB-13 --assignee jane@acme.com --status "In Progress"

# APPLY
python3 "$SK/assign_and_move.py" --issues WEB-12,WEB-13 --assignee jane@acme.com --status "In Progress" --apply
```

Issue tokens are keys (`WEB-12`) or ids. `--assignee` is optional (omit it when the issues already have
owners and you only need to move them).

## The assignee-required rule (critical)

An issue **cannot leave Backlog/To Do for a working column** (In Progress / In Review / Done) without an
`assignee_id` — the API returns **422** (`blocked_unassigned`). See `skills/projekt/references/errors.md`.

- **create**: a row targeting a working column with no resolvable assignee is **demoted to To Do** and
  flagged **"needs owner"** (never dropped). `--strict-status` skips it instead.
- **assign/move**: any issue still unassigned after the optional assign step is **pre-filtered out of the
  move** and reported as "needs owner"; the rest of the batch still moves. Creating/parking in
  `Backlog`/`To Do` without an assignee is always fine.

## Gotchas

- `/issues/bulk` **does NOT create** — it only mutates existing issues (assignee/status/priority/labels).
  Use `bulk_issue_create.py` for creation.
- **Dedupe keys**: `(project_id,title)` + `external_ref`. Give every import row a stable `external_ref`
  so re-imports are safe even if a title is edited.
- Status names are per-project (`project.columns`); the server normalizes localized inputs ("En revisión").
  Defaults: `Backlog`, `To Do`, `In Progress`, `In Review`, `Done` (`errors.md`).
- **403 cross-org** is fatal and not retried — a PAT is bound to one org; fix the token/org. **429** is
  auto-backed-off by the client; keep concurrency ≤3. (errors.md)
- Re-run after fixing owners — both scripts dedupe via the Ledger and resume cleanly.

## What it does NOT do

No estimation (→ `projekt-estimate`), no time logging (→ `projekt-time`), no docs (→ `projekt-docs`), no
workload reports (→ `projekt-workload`). No hard delete (use `/issues/:iid/archive`). No CSV editing — it
only reads the import file. It never touches the 1.3 MB spec.
