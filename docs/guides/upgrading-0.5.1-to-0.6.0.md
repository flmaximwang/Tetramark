# Upgrading from 0.5.1 to 0.6.0

0.6.0 adds the **interactive form** (`tm entity|stock|aliquot|box add-form`) for people, and one
read-only **preset key** (`long: true`). It is an **additive** change: ledgers, the schema, the
presets, the data and the history are untouched, `spec_version` does not move, and **no migration
step ships with this release**. Nothing has to be converted or re-imported.

## What is new

| | 0.5.1 | 0.6.0 |
|---|---|---|
| Writing an object | `tm <object> add …` with flags | the same command, plus `tm <object> add-form` for a person at a terminal |
| Long free-text fields | ordinary text fields | marked `long: true` in the preset ([§2 of the schema language](../spec/02-schema-language.md)) |
| Runtime dependencies | `typer`, `pyyaml`, `pydantic`, `rich` | plus `prompt_toolkit` (the form's input layer) |

The old commands are **unchanged**: `tm entity add KIND --name N --set f=v` still works exactly as
before, and writes exactly the same row. The form is an addition, not a replacement.

## The form, for people

```sh
tm entity add-form
tm stock add-form
tm aliquot add-form
tm box add-form
```

The form takes no positional argument and no flags: it asks for the kind, the fields, the box and
the slots, driven by the ledger's own schema, and it checks each value as it is typed. The write it
performs is the same `ledger.<objects>.add(...)` call that `add` makes.

**Agents keep using `add`.** The form is interactive by design: it needs a terminal, and **without a
TTY it refuses with exit 2** and an error that points at `tm <object> add`. Scripts, cron jobs and
CI steps are therefore unaffected by this release — they never used the form in the first place.

The full behaviour — steps, keys, the review screen, long values — is in
[`../spec/06-cli.md`](../spec/06-cli.md) §6.3.

## The `long` preset key

`long: true` marks a `string`, `text` or `sequence` field as a long free-text value (a sequence, a
free-text note, a description). It is **read-only metadata** about how a field is filled in, and it
is documented in [§2 of the schema language](../spec/02-schema-language.md):

- it changes **no DDL** and no stored shape — the column is the same column;
- it is **excluded from the schema fingerprint** (like `description`), so adding it to a preset does
  not change `ledger_meta.fingerprint` and needs no `tm schema apply`;
- it needs **no `version:` bump** of the preset and **no migration step** (§5 of the schema
  evolution chapter);
- it appears in `tm schema show`, so you can see which fields are marked.

The bundled presets now mark their long fields this way.

**An existing ledger keeps the preset copies it was created with** (`<ledger>/presets/`), so it does
not get the key for free. To use it:

- add `long: true` to the field in the ledger's own copy (`<ledger>/presets/*.yaml`), or
- re-copy the preset as a preset update does — see
  [`../spec/05-schema-evolution.md`](../spec/05-schema-evolution.md).

Until then the field is asked and shown like any other text field; nothing breaks and no data
changes.

## The new dependency

`prompt_toolkit` is a new **runtime** dependency, pulled in by the form. It is pure Python and is
installed with the wheel:

```sh
uv tool install --upgrade tetramark      # or: pip install -U tetramark
```

`dolt` (>= 2.3.5) is still required on `PATH`, unchanged, and nothing else about the environment
changes. A source checkout picks it up with `uv sync`.

## Verify the upgrade

```sh
tm --version                 # 0.6.0
tm status                    # the ledger is unchanged: same spec_version, presets, author
tm check                     # same exit code as under 0.5.1
tm entity add-form           # the form opens (needs a terminal)
```

`tm status` and `tm check` are the useful checks: they prove that the ledger, its schema and its
data are the same ones as before the upgrade.

`docs/spec/06-cli.md` is the command specification and
`skills/tetramark/references/commands.md` is the day-to-day reference with every flag.
