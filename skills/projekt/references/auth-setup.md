# Auth setup — your Personal Access Token

The plugin acts **as you** in **one** organization using a Projekt PAT. Never bundled, never committed.

## 1. Mint a key
Projekt → **Organization → Settings → General → Integraciones** → **Create API key**.
- Format: `pjk_live_` + 32 chars. Shown once — copy it.
- Carries your full role (owner/admin/manager/member/viewer). **No per-endpoint scoping** — treat it
  like a password. Max 20 active keys/user/org; revoke instantly from the same screen.
- Docs: <https://projekt.3xa.es/developers/auth.html#pat>

## 2. Provide it (precedence: env > file)
```bash
# Option A — environment (best for CI / multiple accounts)
export TREXA_API_TOKEN="pjk_live_…"
export TREXA_API_BASE="https://projekt.3xa.es/api"   # optional, this is the default
export TREXA_ORG_ID="<uuid>"                           # optional; else current org from /me
```
```jsonc
// Option B — ~/.config/3xa-projekt/auth.json  (shared with the Projekt MCP)
{ "token": "pjk_live_…", "api_base": "https://projekt.3xa.es/api" }
```

## 3. Verify
```bash
bash "${CLAUDE_SKILL_DIR}/scripts/auth_check.sh"
```
Prints your user, org and role, writes `.projekt-run/context.json`, and shows only a token
**fingerprint** (`pjk_live_…abcd`) — never the secret.

## Headers (handled for you by `lib/http.sh`)
`Authorization: Bearer <pat>` + `X-Auth-Token: <pat>` (LiteSpeed/proxy fallback) + `X-Org-Id: <org>`.

## If it fails
- *No token* → set env or create the file above.
- *No org resolved* → set `TREXA_ORG_ID`, or switch your current org in Projekt.
- *401/403* → token invalid/expired/revoked, or wrong org. Mint a fresh key.
