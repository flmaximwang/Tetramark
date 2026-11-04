# Releasing

Tagging is not a step any more. **Merging a version bump is the release**: the merge triggers
[`.github/workflows/release.yml`](../../.github/workflows/release.yml), which puts the annotated tag on the
merge commit and publishes the GitHub release. An agent that forgets `git tag` can no longer produce an
untagged release, because the agent never tags anything.

## The version lives in four locations

| File | What it holds | Why it matters |
|---|---|---|
| `pyproject.toml` | `[project] version` | the canonical value; the wheel and the trigger |
| `src/tetramark/__init__.py` | `__version__` | what `tm --version` prints |
| `uv.lock` | the `tetramark` package entry | what `uv run` and `uv tool install` resolve |
| `skills/tetramark/SKILL.md`, `skills/tetramark-schema/SKILL.md` | quoted frontmatter `version:` plus the version in the visible first body line | the manual an agent reads; it claims to describe exactly this CLI |

Bumping some but not all is the classic release bug: the wheel says 0.5.2, `tm --version` says
0.5.1, or the shipped skill still describes the previous release.
[`scripts/release_check.py`](../../scripts/release_check.py) proves they agree — including that each
`SKILL.md` carries a version and that it equals `pyproject.toml`; the fast lane runs it and the
workflow gates on it, so a mismatch blocks the tag instead of shipping.

```console
$ python3 scripts/release_check.py
version    0.8.14
  pyproject    0.8.14   pyproject.toml
  __init__     0.8.14   src/tetramark/__init__.py
  uv.lock      0.8.14   uv.lock
  skill        0.8.14   skills/tetramark/SKILL.md
  skill-schema 0.8.14   skills/tetramark-schema/SKILL.md
previous   0.8.13  -> moved
tag        v0.8.14  absent -- the workflow will create it
notes      docs/releases/v0.8.14.md  present
checkout   main @ efd79e9  clean
ready
```

## Cutting a release

1. **Bump the version** in the hand-edited files and let uv rewrite the lock:

   ```console
   $EDITOR pyproject.toml src/tetramark/__init__.py \
       skills/tetramark/SKILL.md skills/tetramark-schema/SKILL.md
   uv lock
   python3 scripts/release_check.py        # must say "ready"
   ```

   In both `SKILL.md` files bump the quoted `version:`, bump the number in the visible first body
   line, and set `updated:` to the release date. The frontmatter is the machine-readable claim; the
   first body line is the copy a reader actually sees.

2. **Write the notes** as `docs/releases/v<version>.md` — see [the notes README](../releases/README.md).
   The first `#` heading becomes the release title *and* the tag message; the body becomes the release
   body.

3. **Commit and merge** the PR as usual (`chore(release): 0.5.2`). The merge is what releases.

4. **Verify** (seconds later):

   ```console
   gh release view v0.5.2
   git ls-remote --tags origin v0.5.2
   ```

Never run `git tag` or `gh release create` by hand. A hand tag pushed from a feature branch is the one
mistake the workflow cannot undo: it does not move tags.

## What the workflow does

| Step | Detail |
|---|---|
| Trigger | a push to `main` that touches `pyproject.toml` or `src/tetramark/__init__.py`, or a manual `workflow_dispatch` |
| Gate | `scripts/release_check.py` must report `ready`; every version source must agree, both `SKILL.md` frontmatters included |
| Change check | the version at the previous tip of `main` must differ from the version being merged; a dependency-only edit to `pyproject.toml` does not release |
| Tag | annotated `v<version>` at `github.sha` — the merge commit for a PR merge — pushed to origin |
| Release | `gh release create` with the notes file, or GitHub generated notes when it is missing |
| Re-runs | idempotent: an existing tag is never touched, an existing release is left alone, a tag without a release gets its release |

The repository convention is that **tags sit on the merge commit** (`v0.5.0` → `d69b4d6`, `v0.4.2` →
`c89365f`). Tagging `github.sha` reproduces that automatically; doing it by hand is how a tag ends up on
a branch commit that later gets deleted.

The workflow does **not** run the test suite. The commit it tags is the tip of `main`, which should
already be green — `uv run pytest -q -m fast` before merging and the full lane per
[the test-harness guide](test-harness.md).

## When something is wrong

**The tag is missing** — backfill it:

```console
gh workflow run release.yml -f ref=<commit-or-branch>      # add -f dry_run=true to only check
```

The tree at `ref` decides the version and the notes, so this only works for commits that already
contain this workflow and `scripts/release_check.py`. All ten tags from v0.1.0 through v0.5.1 were
created by hand before the workflow existed; v0.5.1 is the last one that can be.

**The tag is on the wrong commit** — a release already points at it, so remove both, then backfill:

```console
gh release delete v0.5.2 --yes
git tag -d v0.5.2 && git push origin :refs/tags/v0.5.2
gh workflow run release.yml -f ref=<good-commit>
```

**A release shipped without notes** — edit the release body in place; the notes file for the future is
`docs/releases/v<version>.md`.

**You only want to know what would happen** — `python3 scripts/release_check.py --since origin/main`
says whether the version moved, and `-f dry_run=true` runs the workflow without creating anything.

**The run fails with `403` pushing the tag** — the workflow's token is read-only on this repository
(the default is *read*). The workflow already declares `permissions: contents: write`, so this means the
repository refuses the elevation: Settings → Actions → General → Workflow permissions → *Read and write*.
