#!/usr/bin/env bash
# Fetch the upstream data contracts (JSON Schema) into contracts/original/.
# Linux/macOS counterpart of fetch_contracts.ps1 (same file list, same mirror order).
#
# Usage:
#   bash contracts/fetch_contracts.sh
#   OUT_DIR=.contracts-test bash contracts/fetch_contracts.sh
#
# Mirror order: gh-proxy -> jsDelivr -> raw (raw.githubusercontent is unreliable here).
set -euo pipefail

REPO="${REPO:-jlcglobal/jlc-global-ozon-auto-listing}"
REF="${REF:-main}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${OUT_DIR:-$SCRIPT_DIR/original}"

FILES=(
  "templates/collector-capture.schema.json"
  "templates/source.schema.json"
  "templates/source-manifest.schema.json"
  "templates/product-analysis.schema.json"
  "templates/product-positioning.schema.json"
  "templates/category-selection.schema.json"
  "templates/ozon-ecommerce-design.schema.json"
  "templates/title-ru.schema.json"
  "templates/description-ru.schema.json"
  "templates/copy-ru.schema.json"
  "templates/keywords-ru.schema.json"
  "templates/keyword-research-ru.schema.json"
  "templates/ozon-tags.schema.json"
  "templates/rich-content.schema.json"
  "templates/image-plan.schema.json"
  "templates/image-asset-contract.schema.json"
  "templates/image-qc-report.schema.json"
  "templates/ozon-images.schema.json"
  "templates/visual-reference-analysis.schema.json"
  "templates/ozon-category.schema.json"
  "templates/ozon-category-tree.schema.json"
  "templates/ozon-category-attributes.schema.json"
  "templates/ozon-attributes.schema.json"
  "templates/ozon-attributes-final.schema.json"
  "templates/ozon-upload-config.schema.json"
  "templates/ozon-upload-payload.schema.json"
  "templates/ozon-upload-preflight.schema.json"
  "templates/ozon-preflight.schema.json"
  "templates/ozon-draft.schema.json"
  "templates/ozon-result.schema.json"
  "templates/store-publications.schema.json"
  "templates/status.schema.json"
  "templates/batch.schema.json"
  "templates/batch-result.schema.json"
  "templates/pricing-result.schema.json"
  "templates/cost-analysis.schema.json"
  "templates/profit-analysis.schema.json"
  "templates/variant-grouping-result.schema.json"
  "templates/variant-decision.schema.json"
  "templates/platform-grouping-result.schema.json"
)

mkdir -p "$OUT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
downloaded=0
failed=()

for file in "${FILES[@]}"; do
  name="$(basename "$file")"
  target="$OUT_DIR/$name"
  ok=0
  for url in \
    "https://gh-proxy.com/https://raw.githubusercontent.com/$REPO/$REF/$file" \
    "https://cdn.jsdelivr.net/gh/$REPO@$REF/$file" \
    "https://raw.githubusercontent.com/$REPO/$REF/$file"
  do
    rm -f "$target"
    if curl -fsSL --max-time 90 -o "$target" "$url"; then
      # keep it only if it really parses as JSON
      if "$PYTHON_BIN" -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$target" 2>/dev/null; then
        ok=1
        break
      fi
    fi
  done
  if [ "$ok" = "1" ]; then
    downloaded=$((downloaded + 1))
  else
    rm -f "$target"
    failed+=("$file")
  fi
done

echo "out dir   : $OUT_DIR"
echo "downloaded: $downloaded"
if [ "${#failed[@]}" -gt 0 ]; then
  echo "failed:"
  printf '  %s\n' "${failed[@]}"
  echo "Retry the failed ones later, or copy them from GitHub by hand."
  exit 1
fi
echo "OK"
