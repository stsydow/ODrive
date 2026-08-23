#!/usr/bin/env bash
# Static checks for the QtGUI: lint (ruff) + type-check (mypy) + compile +
# qmllint.
#
# This script does NOT install anything — it assumes `ruff`, `mypy` and
# `pyside6-qmllint` are already available (interpreter/venv on PATH). See
# ARCHITECTURE.md.
#
# Optional formatting check (not gated here - formatting is a separate,
# opt-in step that normalizes a large part of the codebase):
#   ruff format --check .     # then: ruff format .  to apply

set -uo pipefail
cd "$(dirname "$0")"

fail=0

echo "==> ruff check ."
ruff check . || fail=1

echo "==> mypy (main, backend, monitoring, status_backend, errors, eventlog)"
mypy --no-incremental --ignore-missing-imports \
     main.py backend.py monitoring.py status_backend.py errors.py eventlog.py || fail=1

echo "==> python -m py_compile (all modules)"
python3 -m py_compile main.py backend.py monitoring.py status_backend.py errors.py eventlog.py || fail=1

echo "==> qmllint (qml/)"
if command -v pyside6-qmllint >/dev/null 2>&1; then
    pyside6-qmllint qml/*.qml >/dev/null 2>&1 || fail=1
else
    echo "    (pyside6-qmllint not found - skipped)"
fi

echo "==> pytest (tests/, offscreen)"
if python3 -c "import pytest" >/dev/null 2>&1; then
    QT_QPA_PLATFORM=offscreen python3 -m pytest tests/ -q >/dev/null 2>&1 || fail=1
else
    echo "    (pytest not installed - skipped)"
fi

if [ "$fail" -ne 0 ]; then
    echo "==> FAILED"
    exit 1
fi
echo "==> OK"
