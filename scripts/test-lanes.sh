#!/bin/sh
# Run the pytest lanes, print their wall times, and enforce the budgets.
#
# Usage:
#   scripts/test-lanes.sh            # fast lane, then the full lane
#   scripts/test-lanes.sh fast       # fast lane only (seconds; safe in a loop)
#   scripts/test-lanes.sh full       # full lane through dolt sql-server (TETRAMARK_DRIVER=server)
#   scripts/test-lanes.sh embedded   # full lane through per-statement `dolt sql` subprocesses
#
# The repository has no CI: this script is the measurement and enforcement
# point before a merge/release. The default `full` lane is the *server* lane:
# every ledger test still runs against real Dolt, but through the opt-in
# `dolt sql-server` driver, so the suite is expected far below five minutes.
# `embedded` runs the same tests against the original one-`dolt sql`-process
# backend (~13 min on the reference machine); use it to prove the default
# backend is still green, not as the everyday lane.
#
# It fails (exit 1) when
#   * a lane fails, or
#   * the fast lane exceeds FAST_LANE_BUDGET_SECONDS (default 10 s), or
#   * the server full lane exceeds SERVER_LANE_BUDGET_SECONDS (default 300 s =
#     the user's hard five-minute ceiling), or
#   * EMBEDDED_LANE_BUDGET_SECONDS is set and the embedded lane exceeds it
#     (unset by default: that lane is expected to be slow), or
#   * the server lane leaves a `dolt sql-server` process behind
#     (LEFTOVER_CHECK=0 disables the check for a machine with unrelated servers).
#
# The server lane sets TETRAMARK_SERVER_STATS so each pytest worker records how
# many `dolt sql-server` processes it started; the script sums them and prints
# the total next to the wall time.
#
# Eligible environments: `uv run pytest` when uv is installed, otherwise
# $PYTEST (default `python3 -m pytest`). In sandboxes where `uv run` does not
# work, point $PYTEST at a venv pytest:
#   PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh full
#   PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh embedded
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$ROOT" || exit 1

LANE=${1:-all}
FAST_LANE_BUDGET_SECONDS=${FAST_LANE_BUDGET_SECONDS:-10}
# Hard ceiling for the default full lane: the real-Dolt server suite must stay
# under five minutes. An explicitly empty value disables the gate.
SERVER_LANE_BUDGET_SECONDS=${SERVER_LANE_BUDGET_SECONDS-300}
# The embedded lane is the historical slow path; only gate it when asked to.
EMBEDDED_LANE_BUDGET_SECONDS=${EMBEDDED_LANE_BUDGET_SECONDS-}
LEFTOVER_CHECK=${LEFTOVER_CHECK:-1}
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
server_seconds=0
embedded_seconds=0
STATS_FILE=$(mktemp "${TMPDIR:-/tmp}/tetramark-server-stats.XXXXXX") || exit 1
trap 'rm -f "$STATS_FILE"' EXIT INT TERM

run_lane() {
    name=$1
    mode=$2
    shift 2
    printf '== %s lane: %s %s\n' "$name" "$RUN_PYTEST" "$*"
    started=$(date +%s)
    # Word splitting of $RUN_PYTEST is intentional (e.g. `uv run pytest`).
    # shellcheck disable=SC2086
    if [ "$mode" = server ]; then
        TETRAMARK_DRIVER=server TETRAMARK_SERVER_STATS="$STATS_FILE" \
            $RUN_PYTEST "$@" || { echo "!! $name lane failed" >&2; failures=$((failures + 1)); }
    elif [ "$mode" = embedded ]; then
        TETRAMARK_DRIVER= $RUN_PYTEST "$@" \
            || { echo "!! $name lane failed" >&2; failures=$((failures + 1)); }
    else
        $RUN_PYTEST "$@" || { echo "!! $name lane failed" >&2; failures=$((failures + 1)); }
    fi
    finished=$(date +%s)
    printf '== %s lane wall time: %ss\n' "$name" "$((finished - started))"
    case $name in
        fast) fast_seconds=$((finished - started)) ;;
        full) server_seconds=$((finished - started)) ;;
        embedded) embedded_seconds=$((finished - started)) ;;
    esac
}

report_server_starts() {
    if [ -s "$STATS_FILE" ]; then
        starts=$(awk -F'=' '/starts=/{total += $2} END {print total + 0}' "$STATS_FILE")
        printf '== server lane dolt sql-server starts: %s\n' "$starts"
    else
        echo "== server lane dolt sql-server starts: (no worker reported; was the lane skipped?)"
    fi
}

check_leftover_servers() {
    if [ "$LEFTOVER_CHECK" != 1 ]; then
        echo "== leftover dolt sql-server check disabled (LEFTOVER_CHECK=$LEFTOVER_CHECK)"
        return
    fi
    if ! command -v pgrep >/dev/null 2>&1; then
        echo "== leftover dolt sql-server check skipped (pgrep not available)"
        return
    fi
    leftover=$(pgrep -f 'dolt sql-server' | wc -l | tr -d ' ')
    printf '== leftover dolt sql-server processes: %s\n' "$leftover"
    if [ "$leftover" -ne 0 ]; then
        echo "!! the server lane left ${leftover} dolt sql-server process(es) behind" >&2
        failures=$((failures + 1))
    fi
}

if [ "$LANE" = all ] || [ "$LANE" = fast ]; then
    run_lane fast plain -q -m fast --durations=10
    if [ "$fast_seconds" -gt "$FAST_LANE_BUDGET_SECONDS" ]; then
        echo "!! fast lane took ${fast_seconds}s > ${FAST_LANE_BUDGET_SECONDS}s budget:" \
             "a Dolt-free test got slow or a DB test was marked fast" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$LANE" = all ] || [ "$LANE" = full ]; then
    run_lane full server -q --durations="$DURATIONS"
    report_server_starts
    check_leftover_servers
    if [ -z "$SERVER_LANE_BUDGET_SECONDS" ]; then
        echo "== server full lane budget not enforced: set SERVER_LANE_BUDGET_SECONDS" \
             "to a number (300 = the five-minute ceiling)"
    elif [ "$server_seconds" -gt "$SERVER_LANE_BUDGET_SECONDS" ]; then
        echo "!! server full lane took ${server_seconds}s > ${SERVER_LANE_BUDGET_SECONDS}s" \
             "budget (TETRAMARK_DRIVER=server, the default full lane)" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$LANE" = embedded ] || [ "$LANE" = full-embedded ]; then
    run_lane embedded embedded -q --durations="$DURATIONS"
    if [ -n "$EMBEDDED_LANE_BUDGET_SECONDS" ] &&
       [ "$embedded_seconds" -gt "$EMBEDDED_LANE_BUDGET_SECONDS" ]; then
        echo "!! embedded full lane took ${embedded_seconds}s >" \
             "${EMBEDDED_LANE_BUDGET_SECONDS}s budget" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$failures" -ne 0 ]; then
    echo "test-lanes: FAILED ($failures)" >&2
    exit 1
fi
echo "test-lanes: OK"
