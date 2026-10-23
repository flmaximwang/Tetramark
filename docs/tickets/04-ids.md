# 04 · ID strategies
Depends on: 01  · Spec: §6.1

`core/ids.py`:
- `date_code(month, day)` exactly as `~/.local/bin/date_code.py` (month A–M skipping I; day 1–9 then A–W skipping I);
  doctests: (6,24)→FP, (1,1)→A1, (12,31)→MW, (9,10)→JA.
- Strategy classes `UuidIds`, `DatecodeIds(prefix, reuse=True)`, `SequenceIds(prefix, width=5)` with
  `next_id(existing: Callable[[str], Iterable[str]], today: date)` — `existing(like_prefix)` returns IDs already used,
  so the strategy is DB-agnostic. Datecode: smallest unused NN 01–99 (reuse) or max+1; overflow → error.
- `validate(id)` per strategy (pattern) for explicit IDs; `aliquot_id(stock_id, seq)` and parser.
- `strategy_from_config(dict)`.

Tests: pure unit tests incl. reuse gaps (01,02,07 used → 03), reuse=False (→08), day/month edges, invalid explicit IDs.
