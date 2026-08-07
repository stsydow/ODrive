#!/usr/bin/env bash
# Static checks for the QtGUI: lint (ruff) + type-check (mypy) + compile.
#
# This script does NOT install anything — it assumes `ruff` and `mypy` are
# already available (interpreter/venv on PATH). See ARCHITECTURE.md.
#
# Optional formatting check (not gated here - formatting is a separate,
# opt-in step that normalizes a large part of the codebase):
#   ruff format --check .     # then: ruff format .  to apply

set -uo pipefail
cd "$(dirname "$0")"

fail=0

echo "==> ruff check ."
ruff check . || fail=1

echo "==> mypy (main, controls, errors, eventlog, util)"
mypy --no-incremental --ignore-missing-imports \
     main.py controls.py errors.py eventlog.py util.py || fail=1

echo "==> python -m py_compile (all modules)"
python3 -m py_compile main.py controls.py errors.py eventlog.py util.py || fail=1

if [ "$fail" -ne 0 ]; then
    echo "==> FAILED"
    exit 1
fi
echo "==> OK"
