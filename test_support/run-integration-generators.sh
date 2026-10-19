#!/bin/bash
# Offline fixtures only; run all four workstreams against the same checked-out product.
set -euo pipefail
cd "$(dirname "$0")/.."
reports="$PWD/integration-evidence/generators"
mkdir -p "$reports"
t=$(cd "$(mktemp -d "$PWD/.tmp.XXXXXX")" && pwd -P)
trap 'rmdir "$t" 2>/dev/null || true' EXIT
export TMPDIR="$t" PYTHONDONTWRITEBYTECODE=1
export INVARIANT_REPORT="$reports/A1-base.json"
export DIMENSION_REPORT="$reports/A1-boundaries.json"
export TWO_SIDED_REPORT="$reports/A1-two-sided.json"
export AGIW_INVARIANT_REPORT="$reports/A2-linux.json"
export AGIW_VIEWS_REPORT="$reports/A2-two-sided-views.json"
export FIX10_REPORT="$reports/A3.json"
export AGIW_A4_REPORT="$reports/A4.json"
node --test ./test_pipeline_invariant.mjs ./test_linux_invariant.mjs ./test_two_sided_views.mjs ./test_fix9_invariant.mjs ./test_pipeline_invariants.mjs
