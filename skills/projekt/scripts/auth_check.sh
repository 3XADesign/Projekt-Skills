#!/usr/bin/env bash
# auth_check.sh — MANDATORY first call of every run. Resolves the token, calls
# /me ONCE, pins the org, and seeds .projekt-run/context.json with {org_id,user_id}.
# Prints a human summary + the token FINGERPRINT (never the token). Non-zero exit
# with a remediation hint if no token or no org resolves.
#
# Usage: auth_check.sh
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$HERE/lib/http.sh"

[ -n "$(pj_token)" ] || pj_die "No token found.
  Fix: export TREXA_API_TOKEN=\"pjk_live_…\"  (or create ~/.config/3xa-projekt/auth.json)
  Mint one at Projekt → Organization → Settings → General → Integraciones. See references/auth-setup.md"

echo "Token:    $(pj_fingerprint)"
echo "API base: $(pj_api_base)"

ME="$(pj_req GET /me)" || pj_die "GET /me failed (HTTP $PJ_LAST_STATUS): $(echo "$ME" | jq -r '.message // .error // .' 2>/dev/null). Token invalid/expired/revoked?"

# /me shape: { user:{id,name,email,…}, organization:{id,name,role,…} (current),
#              organizations:[…] (all) }. Env override wins for the org.
ORG_ID="${TREXA_ORG_ID:-$(echo "$ME" | jq -r '.organization.id // .current_organization.id // .organizations[0].id // empty')}"
ORG_NAME="$(echo "$ME" | jq -r '.organization.name // .current_organization.name // .organizations[0].name // empty')"
USER_ID="$(echo "$ME" | jq -r '.user.id // .id // .user_id // empty')"
USER_NAME="$(echo "$ME" | jq -r '.user.name // .user.email // .name // .email // empty')"
ROLE="$(echo "$ME" | jq -r '.organization.role // .current_organization.role // .organizations[0].role // empty')"

[ -n "$ORG_ID" ] || pj_die "Authenticated as $USER_NAME but no organization resolved.
  Fix: set TREXA_ORG_ID=<uuid>, or switch your current org in Projekt. (A PAT is bound to one org.)"

mkdir -p "$PJ_RUN_DIR"
tmp="$(mktemp)"
[ -f "$PJ_CONTEXT_FILE" ] && cp "$PJ_CONTEXT_FILE" "$tmp" || echo '{}' > "$tmp"
jq --arg o "$ORG_ID" --arg on "$ORG_NAME" --arg u "$USER_ID" --arg un "$USER_NAME" \
   --arg r "$ROLE" --arg b "$(pj_api_base)" \
   '.org_id=$o | .org_name=$on | .user_id=$u | .user_name=$un | .role=$r | .api_base=$b' \
   "$tmp" > "$PJ_CONTEXT_FILE" && rm -f "$tmp"

echo "User:     $USER_NAME ($USER_ID)"
echo "Org:      ${ORG_NAME:-?} ($ORG_ID)  role=${ROLE:-?}"
echo "✓ Connected. Context → $PJ_CONTEXT_FILE"
echo "Next: run context_sync.sh to cache projects + members."
