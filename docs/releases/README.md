# Release notes

One file per release, named after the tag: `v0.5.1.md` for `v0.5.1`.

[`.github/workflows/release.yml`](../../.github/workflows/release.yml) reads the file when it publishes:

| Part of the file | Where it goes |
|---|---|
| the first `#` heading | the GitHub release title **and** the annotated tag's message |
| everything below it | the GitHub release body |

A missing file does not block a release: the workflow falls back to GitHub's generated notes and flags
the run with a warning. Missing notes are never a failure — a missing *tag* is the failure worth
automating away.

## The releases

| Version | Notes | What changed |
|---|---|---|
| [0.8.7](v0.8.7.md) | `v0.8.7.md` | `tm alias add box` accepts the box's name |
| [0.8.4](v0.8.4.md) | `v0.8.4.md` | `tm aliquot show` carries the tube's events; `tm aliquot label` |
| [0.8.3](v0.8.3.md) | `v0.8.3.md` | explicit purge for entities, stocks and boxes; aliquot aliases/references |
| [0.8.2](v0.8.2.md) | `v0.8.2.md` | box ids in `box list`/`box show`; aliquot help points at `move`/`use`/`status` |
| [0.8.1](v0.8.1.md) | `v0.8.1.md` | stock show cards in wrapping rows; regex find, shared filters, rewrite rules |
| [0.8.0](v0.8.0.md) | `v0.8.0.md` | one fixed status set; `use`/`move` require `-m`; manual spec 5→6 upgrade |
| [0.7.2](v0.7.2.md) | `v0.7.2.md` | `tm aliquot list`: find the usable tubes |
| [0.7.1](v0.7.1.md) | `v0.7.1.md` | `tm aliquot show`: read one tube |
| [0.7.0](v0.7.0.md) | `v0.7.0.md` | batched Dolt access: commands ~10× faster |
| [0.6.1](v0.6.1.md) | `v0.6.1.md` | `tm find` matches part of an alias |
| [0.6.0](v0.6.0.md) | `v0.6.0.md` | interactive `add-form` |
| [0.5.1](v0.5.1.md) | `v0.5.1.md` | full-word subcommands (`ls` → `list`, `rm` → `remove`) |

## Template

```markdown
# 0.5.2 — one line that names the change

Two or three sentences: what changed, whether it breaks anything, what has to migrate.

## The rename / the shape of the change

A table or a short list, if the release is a mechanical change.

## Highlights

- One bullet per user-visible fact. Name the command, the flag, the exit code.
- Say what was deliberately left alone; that is what readers ask about first.

## Upgrade

A link to the upgrade guide, or "nothing to migrate" plus the reason.

## Verification

- Fast lane: **N passed** (~N s).
- Full lane (`-n auto`, N workers): **N passed / 0 failed / N skipped** in N s.
- The manual check that the tests cannot make: a demo against a real ledger.
```

## Rules

- **The title carries the version**, because it is also the tag message: `0.5.1 — full-word subcommands`.
  The workflow strips the `#`; keep the line short enough to read in `git tag -n`.
- **Link to the released tree, not to `main`**: `https://github.com/flmaximwang/Tetramark/blob/v0.5.1/...`.
  A `main` link silently starts describing the next release.
- **State the hard breaks and the non-breaks** explicitly. "CLI-only, no ledger step" saves a reader an
  afternoon.
- **Numbers come from the run that shipped**, not from a later re-run.
