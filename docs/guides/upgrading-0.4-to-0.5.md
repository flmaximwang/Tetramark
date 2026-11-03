# Upgrading from 0.4.2 to 0.5.0

0.5.0 removes the **workspace layer**: a ledger is now one self-contained directory (the Dolt
database `.dolt/`, `ledger.yaml` and the `presets/` copy). The ledger **format is unchanged** —
`ledger.yaml`, `spec_version` and the copied presets are untouched, so an 0.4.2 ledger opens as-is
(P1). Everything below is about the CLI/API surface.

## Quick version

```sh
cd /path/to/workspace/zsqlab_10          # cd into the ledger directory itself
tm status                                # path, name, branch, spec_version, presets, author
tm check                                 # same result as under 0.4.2
```

From anywhere else: `tm --ledger /path/to/workspace/zsqlab_10 status` (`-l` for short). No
`workspace.yaml`, no `-w/--workspace`, no `tm workspace` and no `tm server` are needed or read.

## The old layouts

### A ledger inside a 0.4 workspace

A 0.4 data directory holds `workspace.yaml` plus one subdirectory per ledger. In 0.5.0:

- `cd` into the **ledger directory** (the subdirectory holding `.dolt/` and `ledger.yaml`), or pass
  `-l/--ledger <that path>`; the upward search then finds it.
- The old `workspace.yaml` is not read. If the search meets it **before** any ledger (for example
  you run `tm` from the data-directory root), the command exits 2 and says:
  *"found the 0.4 workspace '…' … cd into the ledger directory itself and re-run the command"*,
  naming a ledger directory and the `tm --ledger <path>` form. This is the P4 migration guard, not
  an error in the ledger.
- Nothing has to move between files: `ledger.yaml` already carries `author:`, and the per-ledger
  `presets/` copy already made the ledger self-contained. You may keep or delete the stale
  `workspace.yaml`; it is never used as configuration again.

### A bare Dolt database (the legacy lab databases)

Directories with `.dolt/` but no `ledger.yaml` are **not** ledgers in 0.5.0. Format them in place
with the new top-level `tm adopt`, which keeps the whole existing history:

```sh
tm adopt /path/to/Dolt/zsqlab_05 --preset molbio --id-prefix 05 \
  --author "Name <you@example.com>"
tm --ledger /path/to/Dolt/zsqlab_05 check
```

`tm adopt PATH` needs an empty schema on the selected branch (no user tables, no views, no
`dolt_tests` rows), a clean working set and no `ledger.yaml` (use `--force-config` to overwrite a
stray one). It commits `adopt(ledger): NAME` on top of the existing history and records
`adopted_from_commit`; nothing is deleted, and a failed adopt rolls back without removing the
database directory (spec §3.1). The old spelling `tm init NAME --adopt` is gone.

### Command mapping

| 0.4.2 | 0.5.0 |
|---|---|
| `tm -w <data_dir> -l <name> <cmd>` | `cd <data_dir>/<name> && tm <cmd>`, or `tm -l <data_dir>/<name> <cmd>` |
| `tm workspace init …` | gone — `tm init PATH` creates one ledger directory |
| `tm server start/stop/status` | gone — the embedded driver runs `dolt sql` in the ledger directory; `tm status` describes the ledger |
| `tm init NAME --adopt …` | `tm adopt PATH --preset …` |
| `tm config get\|set presets_dir` | gone — bare preset names resolve through `$TETRAMARK_PRESETS_DIR`, then the bundled `tetramark/presets/`; `--preset PATH` always works |
| `tm backup` (default `<data_dir>/backups/<ledger>`) | `tm backup DIR` (or `tm backup` when a backup is already configured; `--name` selects one) |

## What else changes

- **Author (P6).** `ledger.yaml` → Git `user.name`/`user.email`. The workspace author is gone.
  An existing `ledger.yaml` keeps its author; change it with `tm config set author "Name <email>"`.
- **No default backup (P10).** A backup next to the ledger shares its disk and dies with it, so a
  directory is required unless the ledger already has a configured backup. `tm backup DIR` configures
  a Dolt backup and syncs it; `tm backup --ls` lists configured destinations.
- **One writer per ledger (P11).** Writes take an `fcntl.flock` on
  `<ledger>/.dolt/tetramark.lock` for the whole read → validate → write → commit window. A second
  writer waits up to 10 s (override with `TETRAMARK_LOCK_TIMEOUT=<seconds>`) and then exits 2 with
  `ledger busy: pid … (tm …) is writing …`. Reads never lock.
- **Sharing and backups (P12).** Copy the directory (`cp -a`, `rsync`, `dolt clone`, `dolt backup`),
  like a git repository. Two machines writing one ledger over a network share is **not supported**.
  Keep `kinds_dir`/`rules_dir` relative inside the ledger so the copy still opens; absolute
  `file://` remote/backup URLs are reported by `tm check`/`tm status` as portability warnings.
- **Presets (P8).** The wheel bundles the presets (`tetramark/presets/`), so `tm init --preset molbio`
  works from a fresh install; `$TETRAMARK_PRESETS_DIR` overrides the source for bare names. Each
  ledger still opens only its own `<ledger>/presets/` copy, exactly as in 0.4.2.
- **Server mode (R4).** Out of scope for 0.5.0 and not promised. The `Driver` interface is kept so a
  future server driver can be added, but nothing in 0.5.0 starts, stops or manages a
  `dolt sql-server`. If you run one yourself on the directory, `dolt sql` auto-routes through it;
  that is Dolt's behaviour and is not a supported multi-writer mode.

## Verify the upgrade

```sh
cd <ledger>
tm status                    # name/branch/spec_version/presets/author as before
tm check                     # same exit code as 0.4.2 (info lines allowed)
tm find <known-id-or-alias>  # spot-check data and box locations
```

Your old data directory is untouched by the upgrade; keep it (or a copy) until the 0.5.0 working
copy has been in use for a while.
