# Upgrading from 0.5.0 to 0.5.1

0.5.1 renames the abbreviated subcommands to their full words. It is a **CLI-only** change: ledgers,
the schema, presets and data are untouched, so nothing has to migrate and no ledger step is needed.
Only the spelling of the commands you type changes.

## Command mapping

| 0.5.0 | 0.5.1 |
|---|---|
| `tm entity ls` | `tm entity list` |
| `tm stock ls` | `tm stock list` |
| `tm box ls` | `tm box list` |
| `tm branch ls` | `tm branch list` |
| `tm remote ls` | `tm remote list` |
| `tm alias rm` | `tm alias remove` |
| `tm remote rm` | `tm remote remove` |
| `tm backup ls` | `tm backup list` |
| `tm backup --ls` | `tm backup --list` |

## It is a hard break

No compatibility aliases are shipped. The seven short subcommands above — `tm entity ls`,
`tm stock ls`, `tm box ls`, `tm branch ls`, `tm remote ls`, `tm alias rm` and `tm remote rm` — are
unknown commands, and `tm backup --ls` is an unknown option; all eight exit 2. A script, shell
alias, cron entry, Makefile or CI step that still uses one of them fails at its next run; so does
muscle memory at the prompt. There is no deprecation period — 0.5.1 has exactly one spelling per
command.

`tm backup ls` is the one row above that does **not** exit 2: `tm backup` takes a positional
directory, so a bare `ls` is now read as a directory name and would sync a backup into `./ls`.
Write `tm backup list` to list the configured backups.

**Subcommand names are full words.** `ls` and `rm` are not abbreviations of anything: a listing
subcommand is `list`, a removal subcommand is `remove`, and a new subcommand is named with its full
word from the start.

## `tm backup` and a directory named `list`

`tm backup DIR` takes a positional directory, so `tm backup list` now lists the configured backups
instead of syncing a directory literally called `list`. Pass the escaped path so the directory is
not read as a subcommand:

```sh
tm backup ./list
```

## How to update

Find the old spellings in scripts and notes (`.` is the checkout or your script directory):

```sh
grep -rnE 'tm (entity|stock|box|branch) ls|tm (alias|remote) rm|tm backup (--ls|ls)' .
```

Rewrite a file — macOS `sed -i ''`, GNU sed `sed -i`:

```sh
sed -i '' -E 's/tm (entity|stock|box|branch) ls/tm \1 list/g;
              s/tm (alias|remote) rm/tm \1 remove/g;
              s/tm backup --ls/tm backup --list/g;
              s/tm backup ls/tm backup list/g' FILE
```

Then run each command once: a 0.5.1 command that exits 2 with an unknown-command error is an old
spelling. Also check shell aliases and history, cron entries, Makefiles and saved notes.

## Verify the upgrade

```sh
tm <group> --help             # the group help lists the full-word subcommands
tm status                     # the ledger is unchanged: same spec_version, presets, author
tm check                      # same exit code as under 0.5.0
```

`docs/spec/06-cli.md` is the command specification and
`skills/tetramark/references/commands.md` is the day-to-day reference with every flag.
