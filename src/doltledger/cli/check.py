"""``ledger check``: run the generated Dolt tests (spec §4, §6)."""

from __future__ import annotations

import csv
import io
import json

import typer

from doltledger.cli.options import resolve_ledger
from doltledger.errors import EXIT_ERROR, UsageError
from doltledger.schema.tests_gen import CheckReport, TestResult, run_check

FORMATS = ("table", "json", "csv")


def check_command(
    ctx: typer.Context,
    verbose: bool = typer.Option(
        False,
        "-v",
        "--verbose",
        help="Print the offending rows of every failing test.",
    ),
    group: str | None = typer.Option(
        None,
        "--group",
        "-g",
        help="Only run a group (e.g. 'core', 'schema', 'schema:plasmid', 'rules').",
    ),
    format: str = typer.Option(
        "table",
        "--format",
        "-f",
        help="Output format: table, json or csv.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Shorthand for --format json."),
) -> None:
    """Run dolt_tests; exit 1 when a non-info test fails."""
    output_format = "json" if as_json else format.lower()
    if output_format not in FORMATS:
        raise UsageError(
            f"invalid --format {format!r}: expected one of {', '.join(FORMATS)}"
        )

    ledger = resolve_ledger(ctx)
    try:
        report = run_check(
            ledger.driver, ledger.schema, group=group, verbose=verbose
        )
    finally:
        ledger.close()

    _render(report, output_format, verbose=verbose)
    if not report.passed:
        raise typer.Exit(EXIT_ERROR)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render(report: CheckReport, output_format: str, *, verbose: bool) -> None:
    if output_format == "json":
        typer.echo(
            json.dumps(report.as_dict(), indent=2, ensure_ascii=False, default=str)
        )
    elif output_format == "csv":
        typer.echo(_csv(report, verbose=verbose))
    else:
        typer.echo(_table(report, verbose=verbose))


def _table(report: CheckReport, *, verbose: bool) -> str:
    rows = [
        (result.status.upper(), result.group, result.name, result.message)
        for result in report.results
    ]
    headers = ("STATUS", "GROUP", "NAME", "MESSAGE")
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in rows))
        if rows
        else len(headers[index])
        for index in range(len(headers))
    ]

    def line(values: tuple[str, ...]) -> str:
        cells = [
            value.ljust(widths[index]) if index < len(widths) - 1 else value
            for index, value in enumerate(values)
        ]
        return "  ".join(cells).rstrip()

    lines = [line(headers)]
    lines.extend(line(row) for row in rows)
    if verbose:
        for result in report.failures:
            lines.extend(_detail_lines(result))
    lines.append(_summary(report))
    return "\n".join(lines)


def _detail_lines(result: TestResult) -> list[str]:
    lines = [f"  {result.name}:"]
    if result.detail_error:
        lines.append(f"    ! detail query failed: {result.detail_error}")
        return lines
    if not result.detail_rows:
        lines.append("    (no offending rows)")
        return lines
    for row in result.detail_rows:
        rendered = ", ".join(f"{key}={value}" for key, value in row.items())
        lines.append(f"    {rendered}")
    return lines


def _csv(report: CheckReport, *, verbose: bool = False) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    header = ("status", "group", "name", "message")
    if verbose:
        header = (*header, "details")
    writer.writerow(header)
    for result in report.results:
        row: tuple[str, ...] = (
            result.status.upper(),
            result.group,
            result.name,
            result.message,
        )
        if verbose:
            details = json.dumps(result.detail_rows, ensure_ascii=False, default=str)
            row = (*row, details)
        writer.writerow(row)
    return buffer.getvalue().rstrip("\n")


def _summary(report: CheckReport) -> str:
    info = len(report.info_failures)
    if report.passed:
        suffix = f", {info} info failure(s)" if info else ""
        return f"OK: {report.count} test(s) passed{suffix}"
    failed = len(report.blocking_failures)
    suffix = f" ({info} info failure(s))" if info else ""
    return f"FAILED: {failed} of {report.count} test(s) failed{suffix}"
