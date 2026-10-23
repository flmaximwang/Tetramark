# DoltLedger

A general-purpose, version-controlled inventory ledger on [Dolt](https://www.dolthub.com/).

Four core objects — **entity**, **stock**, **aliquot**, **box** — are extended per domain by
declarative YAML **schemas** bundled into **presets**. See `docs/spec.md` for the design and
`docs/tickets/` for the implementation plan.

## Development

```sh
uv sync
uv run pytest
uv run ledger --help
```

Requires Python >= 3.11 and a `dolt` binary (>= 2.3.5) on `PATH` for the test suite.
