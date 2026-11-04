#!/bin/sh
# Convenience runner around the ONE authoritative command: `uv run pytest`.
#
# Usage:
#   uv run pytest                  # the full real-Dolt suite (the requirement)
#   uv run pytest -q -m fast       # fast lane, no Dolt
#   scripts/test-lanes.sh          # fast lane, then the full suite, with stats
#   scripts/test-lanes.sh fast     # fast lane only
#   scripts/test-lanes.sh full     # the full suite via the same plain command
#   scripts/test-lanes.sh embedded # full suite on the default embedded backend
#
# The test suite enforces its own wall-clock budgets in tests/conftest.py:
#   * the full run fails when it takes more than 300 s
#     (TETRAMARK_SUITE_BUDGET_SECONDS=<seconds> overrides, empty disables);
#   * `-m fast` fails after 30 s.
# pytest-timeout fails a hung test after 60 s.  This script does not set
# TETRAMARK_DRIVER: the harness itself runs ledger tests through the real
# `dolt sql-server` backend and the `embedded_driver` budget tests through the
# embedded driver in the same session.  What the script adds is only:
#   * a second wall-clock report,
#   * the number of `dolt sql-server` processes the workers started,
#   * a check that none survived the run,
#   * an optional full-suite run on the embedded backend (`embedded`), to prove
#     the product's default driver is still green.
#
# The repository has no CI: this script is the manual measurement/release check.
# Eligible environments: `uv run pytest` when uv is installed, otherwise
# $PYTEST (default `python3 -m pytest`), e.g.
#   PYTEST=/path/to/.venv/bin/pytest scripts/test-lanes.sh full
set -u

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1
cd "$ROOT" || exit 1

LANE=${1:-all}
# The pytest-side guards are authoritative; these values only mirror them so
# this script's report uses the same numbers.  Override the pytest guard with
# TETRAMARK_SUITE_BUDGET_SECONDS, not with these.
FULL_LANE_BUDGET_SECONDS=${FULL_LANE_BUDGET_SECONDS-300}
FAST_LANE_BUDGET_SECONDS=${FAST_LANE_BUDGET_SECONDS-30}
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
# checkout's own src first so the run tests this checkout, not a global install.
PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH

failures=0
fast_seconds=0
full_seconds=0
STATS_FILE=$(mktemp "${TMPDIR:-/tmp}/tetramark-server-stats.XXXXXX") || exit 1
trap 'rm -f "$STATS_FILE"' EXIT INT TERM

# run_lane <name> <mode: plain|embedded> <pytest args...>
run_lane() {
    name=$1
    mode=$2
    shift 2
    printf '== %s lane: %s %s\n' "$name" "$RUN_PYTEST" "$*"
    started=$(date +%s)
    # Word splitting of $RUN_PYTEST is intentional (e.g. `uv run pytest`).
    # shellcheck disable=SC2086
    if [ "$mode" = embedded ]; then
        TETRAMARK_DRIVER=embedded TETRAMARK_SERVER_STATS="$STATS_FILE" \
            $RUN_PYTEST "$@" || { echo "!! $name lane failed" >&2; failures=$((failures + 1)); }
    else
        TETRAMARK_SERVER_STATS="$STATS_FILE" \
            $RUN_PYTEST "$@" || { echo "!! $name lane failed" >&2; failures=$((failures + 1)); }
    fi
    finished=$(date +%s)
    printf '== %s lane wall time: %ss\n' "$name" "$((finished - started))"
    case $name in
        fast) fast_seconds=$((finished - started)) ;;
        full) full_seconds=$((finished - started)) ;;
    esac
}

report_server_starts() {
    if [ -s "$STATS_FILE" ]; then
        starts=$(awk -F'=' '/starts=/{total += $2} END {print total + 0}' "$STATS_FILE")
        printf '== dolt sql-server starts reported by the workers: %s\n' "$starts"
    else
        echo "== dolt sql-server starts: (no worker reported; server backend not used?)"
    fi
}

check_leftover_servers() {
    if [ "$LEFTOVER_CHECK" != 1 ]; then
        echo "== leftover dolt sql-server check disabled (LEFTOVER_CHECK=$LEFTOVER_CHECK)"
        return
    fi
    # Match only a real `dolt sql-server` process: argv[0] the dolt binary,
    # argv[1] the sql-server subcommand.  `pgrep -f 'dolt sql-server'` also
    # matches every shell whose command line merely mentions the words.
    leftover=$(ps -axo command= 2>/dev/null | awk '
        $1 ~ /(^|\/)dolt$/ && $2 == "sql-server" { count += 1 }
        END { print count + 0 }
    ')
    printf '== leftover dolt sql-server processes: %s\n' "$leftover"
    if [ "$leftover" -ne 0 ]; then
        echo "!! the run left ${leftover} dolt sql-server process(es) behind" >&2
        failures=$((failures + 1))
    fi
}

if [ "$LANE" = all ] || [ "$LANE" = fast ]; then
    run_lane fast plain -q -m fast --durations=10
    if [ "$fast_seconds" -gt "$FAST_LANE_BUDGET_SECONDS" ]; then
        echo "!! fast lane took ${fast_seconds}s > ${FAST_LANE_BUDGET_SECONDS}s" \
             "(pytest also enforces this; a fast test may have became a DB test)" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$LANE" = all ] || [ "$LANE" = full ]; then
    run_lane full plain -q --durations="$DURATIONS"
    report_server_starts
    check_leftover_servers
    if [ "$full_seconds" -gt "$FULL_LANE_BUDGET_SECONDS" ]; then
        echo "!! full suite took ${full_seconds}s > ${FULL_LANE_BUDGET_SECONDS}s" \
             "(pytest also enforces this; see the budget report above)" >&2
        failures=$((failures + 1))
    fi
fi

if [ "$LANE" = embedded ] || [ "$LANE" = full-embedded ]; then
    run_lane embedded embedded -q --durations="$DURATIONS"
    check_leftover_servers
fi

if [ "$failures" -ne 0 ]; then
    echo "test-lanes: FAILED ($failures)" >&2
    exit 1
fi
echo "test-lanes: OK"
