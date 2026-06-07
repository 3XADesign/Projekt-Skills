#!/usr/bin/env bash
# context_sync.sh — fetch projects + team members ONCE and cache slim copies into
# .projekt-run/context.json. Everything downstream resolves name→uuid from this
# file instead of hitting the API again. Run after auth_check.sh.
#
# Usage: context_sync.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/http.sh"

[ -f "$PJ_CONTEXT_FILE" ] || pj_die "Run auth_check.sh first (no $PJ_CONTEXT_FILE)."

echo "Syncing projects + members…"

PROJECTS="$(pj_req GET '/projects?limit=200')" || pj_die "GET /projects failed (HTTP $PJ_LAST_STATUS)"
# Members: /team is the org roster. Tolerate array OR {data:[]}/{members:[]} envelopes.
MEMBERS="$(pj_req GET '/team')"
# Note: check type=="array" FIRST — `.data` on a top-level array throws in jq.
proj_slim="$(echo "$PROJECTS" | jq -c '[ (if type=="array" then . elif (.data|type=="array") then .data elif (.projects|type=="array") then .projects else [] end)[] | {id, key, name} ]' 2>/dev/null || echo '[]')"
mem_slim="$(echo "$MEMBERS"  | jq -c '[ (if type=="array" then . elif (.data|type=="array") then .data elif (.members|type=="array") then .members else [] end)[] | {user_id: (.user_id // .id), name: (.name // .email), email, role} ]' 2>/dev/null || echo '[]')"

tmp="$(mktemp)"; cp "$PJ_CONTEXT_FILE" "$tmp"
jq --argjson p "$proj_slim" --argjson m "$mem_slim" --arg ts "$(date -u +%FT%TZ)" \
   '.projects=$p | .members=$m | .synced_at=$ts' "$tmp" > "$PJ_CONTEXT_FILE" && rm -f "$tmp"

echo "✓ Cached $(echo "$proj_slim" | jq length) projects, $(echo "$mem_slim" | jq length) members → $PJ_CONTEXT_FILE"
echo "$proj_slim" | jq -r '.[] | "  · \(.key // "—")  \(.name)  (\(.id))"' | head -50
