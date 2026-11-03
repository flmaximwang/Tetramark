#!/bin/sh
# Run the two pytest lanes, print their wall times, and enforce the budget.
#
# Usage:
#   scripts/test-lanes.sh          # fast lane, then the full lane
#   scripts/test-lanes.sh fast     # fast lane only (seconds; safe in a loop)
#   scripts/test-lanes.sh full     # full lane only
#
# The repository has no CI: this script is the measurement and enforcement
# point before a merge/release. It fails (exit 1) when
#   * a lane fails, or
#   * the fast lane exceeds FAST_LANE_BUDGET_SECONDS (default 10 s), or
#   * the full lane exceeds the recorded 0.5.0 baseline +20 %.
#
# FULL_LANE_BASELINE_SECONDS defaults to the measured 0.5.0 baseline (1643 s,
# measured on the 10-CPU developer machine via ticket .11 with `-n auto`;
# override it on other machines). Set it to an empty value to measure the full
# lane without gating it -- the script then says so.
#
# Eligible environments: `uv run pytest` when uv is installed, otherwise
# $PYTEST (default `python3 -m pytest`). In sandboxes where `uv run` does not
# work, point $PYTEST at a venv pytest:
#   PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh fast
#   PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh full
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$ROOT" || exit 1

LANE=${1:-all}
FAST_LANE_BUDGET_SECONDS=${FAST_LANE_BUDGET_SECONDS:-10}
# Measured 0.5.0 baseline (ticket .11, `-n auto`, 10 workers); override on
# other machines. An explicitly empty value disables the gate.
FULL_LANE_BASELINE_SECONDS=${FULL_LANE_BASELINE_SECONDS-1643}
FULL_LANE_FACTOR_PERCENT=120
DURATIONS=${DURATIONS:-25}

if [ -n "${PYTEST:-}" ]; then
    RUN_PYTEST=$PYTEST
elif command -v uv >/dev/null 2>&1; then
    RUN_PYTEST="uv run pytest"
else
    RUN_PYTEST="${PYTHON:-python3} -m pytest"
fi

# A bare `python -m pytest`/venv pytest does not install the project; put the
# worktree's own src first so the lanes test this checkout, not a global install.
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

failures=0
fast_seconds=0
full_seconds=0

run_lane() {
    name=$1
    shift
    printf '== %s lane: %s %s\n' "$name" "$RUN_PYTEST" "$*"
    started=$(date +%s)
    # Word splitting of $RUN_PYTEST is intentional (e.g. `uv run pytest`).
    # shellcheck disable=SC2086
    if ! $RUN_PYTEST "$@"; then
        echo "!! $name lane failed" >&2
        failures=$((failures + 1))
    fi
    finished=$(date +%s)
    printf '== %s lane wall time: %ss\n' "$name" "$((finished - started))"
    if [ "$name" = fast ]; then
        fast_seconds=$((finished - started))
    else
        full_seconds=$((finished - started))
    fi
}

if [ "$LANE" = all ] || [ "$LANE" = fast ]; then
    run_lane fast -q -m fast --durations=10
    if [ "$fast_seconds" -gt "$FAST_LANE_BUDGET_SECONDS" ]; then
        echo "!! fast lane took ${fast_seconds}s > ${FAST_LANE_BUDGET_SECONDS}s budget:" \
             "a Dolt-free test got slow or a DB test was marked fast" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$LANE" = all ] || [ "$LANE" = full ]; then
    run_lane full -q --durations="$DURATIONS"
    if [ -z "$FULL_LANE_BASELINE_SECONDS" ]; then
        echo "== full lane budget not enforced: set FULL_LANE_BASELINE_SECONDS to the" \
             "measured 0.5.0 baseline (see docs/guides/test-harness.md)"
    else
        allowed=$((FULL_LANE_BASELINE_SECONDS * FULL_LANE_FACTOR_PERCENT / 100))
        if [ "$full_seconds" -gt "$allowed" ]; then
            echo "!! full lane took ${full_seconds}s > ${allowed}s" \
                 "(baseline ${FULL_LANE_BASELINE_SECONDS}s +20 %)" >&2
            failures=$((failures + 1))
        fi
    fi
fi

if [ "$failures" -ne 0 ]; then
    echo "test-lanes: FAILED ($failures)" >&2
    exit 1
fi
echo "test-lanes: OK"
