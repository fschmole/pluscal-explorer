#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  generate_diagrams.sh — Generate PlantUML diagrams from a PlusCal file
#
#  Usage:
#    ./generate_diagrams.sh <pcal_file> [output_dir]
#
#  Steps:
#    1. Wrap the PlusCal source for pcal.trans
#    2. Run pcal.trans -writeAST → produces AST.tla
#    3. Pipe AST.tla through ast_to_puml.py
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

TOOLS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TLA2TOOLS="$TOOLS_DIR/tla2tools.jar"

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <pcal_file> [output_dir]" >&2
    exit 1
fi

PCAL_FILE="$1"
OUTPUT_DIR="${2:-.}"

if [[ ! -f "$PCAL_FILE" ]]; then
    echo "ERROR: File not found: $PCAL_FILE" >&2
    exit 1
fi

if [[ ! -f "$TLA2TOOLS" ]]; then
    echo "ERROR: tla2tools.jar not found at $TLA2TOOLS" >&2
    exit 1
fi

# Find Java
if [[ -n "${JAVA_HOME:-}" ]] && [[ -x "$JAVA_HOME/bin/java" ]]; then
    JAVA="$JAVA_HOME/bin/java"
elif command -v java &>/dev/null; then
    JAVA="java"
else
    echo "ERROR: Java not found. Set JAVA_HOME or add java to PATH." >&2
    exit 1
fi

# Create temp working directory
TMPDIR_WORK="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_WORK"' EXIT

# Derive a module name from the filename (strip path and .pcal extension)
BASENAME="$(basename "$PCAL_FILE")"
MODULE="${BASENAME%.pcal}"

# Step 1: Read the PlusCal source and wrap for pcal.trans
# We run a small inline Python to reuse _wrap_pcal_for_trans from tlc_sweep
PYTHONPATH="$TOOLS_DIR" python -c "
import sys; from tlc_sweep import _wrap_pcal_for_trans
pcal_text = open(sys.argv[1], encoding='utf-8').read()
wrapped = _wrap_pcal_for_trans(pcal_text)
if '====' not in wrapped: wrapped += '\n====\n'
print(wrapped, end='')
" "$PCAL_FILE" > "$TMPDIR_WORK/$MODULE.tla"

echo "  Wrapped PlusCal -> $TMPDIR_WORK/$MODULE.tla"

# Step 2: Run pcal.trans -writeAST
# pcal.trans writes AST.tla to the current working directory, so we
# run it from the temp dir.  Note: pcal.trans returns a non-zero exit code
# even on success, so we check for the output file instead.
(cd "$TMPDIR_WORK" && "$JAVA" -XX:TieredStopAtLevel=1 -Xms32m -Xmx256m \
    -cp "$TLA2TOOLS" pcal.trans -writeAST "$TMPDIR_WORK/$MODULE.tla") || true

AST_FILE="$TMPDIR_WORK/AST.tla"
if [[ ! -f "$AST_FILE" ]]; then
    echo "ERROR: pcal.trans did not produce AST.tla" >&2
    exit 1
fi

echo "  pcal.trans -writeAST -> $AST_FILE"

# Step 3: Pipe AST.tla through ast_to_puml.py
cat "$AST_FILE" | python "$TOOLS_DIR/ast_to_puml.py" \
    --output-dir "$OUTPUT_DIR" \
    --name "$MODULE"

echo "Done — diagrams written to $OUTPUT_DIR"
