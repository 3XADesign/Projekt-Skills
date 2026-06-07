#!/usr/bin/env bash
# spec_index.sh — build a tiny TSV index of the OpenAPI spec (path, methods,
# summary) so the skill can DISCOVER any of the 800+ endpoints by grepping a few
# KB instead of loading the 1.3 MB YAML. Pure awk — no yq/python needed.
#
# Output: $PJ_SPEC_DIR/.index.tsv  (one line: <path>\t<METHODS>\t<summary>)
set -uo pipefail
PJ_SPEC_DIR="${PJ_SPEC_DIR:-$HOME/.cache/3xa-projekt}"
SPEC="${PROJEKT_SPEC:-$PJ_SPEC_DIR/projekt.yaml}"
INDEX="$PJ_SPEC_DIR/.index.tsv"
[ -f "$SPEC" ] || { echo "✗ Spec not found at $SPEC — run fetch_spec.sh first." >&2; exit 1; }

awk '
  # enter the paths: section (top-level key, col 0)
  /^paths:[[:space:]]*$/ { inpaths=1; next }
  inpaths && /^[^[:space:]]/ { inpaths=0 }          # left the section
  !inpaths { next }

  # a path key: exactly two leading spaces then "/...:"
  /^  \/[^:]*:[[:space:]]*$/ {
    if (path != "") print path "\t" methods "\t" summary
    line=$0; sub(/:[[:space:]]*$/,"",line); sub(/^  /,"",line)
    path=line; methods=""; summary=""; next
  }
  # a method under the current path: four spaces then verb:
  /^    (get|post|put|patch|delete|options|head):[[:space:]]*$/ {
    v=$1; sub(/:.*/,"",v); methods = (methods=="" ? toupper(v) : methods "," toupper(v)); next
  }
  # first summary seen for the path (method-level 6sp or path-level 4sp)
  summary=="" && /^[[:space:]]+summary:[[:space:]]*/ {
    s=$0; sub(/^[[:space:]]+summary:[[:space:]]*/,"",s); gsub(/^"|"$/,"",s); summary=s; next
  }
  END { if (path != "") print path "\t" methods "\t" summary }
' "$SPEC" > "$INDEX"

echo "✓ Indexed $(wc -l < "$INDEX" | tr -d ' ') paths → $INDEX"
