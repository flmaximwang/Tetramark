# 13 · Remote, backup, author config
Depends on: 06  · Spec: §6, §1.1

- `ops/remote.py` + CLI: `ledger remote add NAME URL`, `remote ls`, `push [NAME] [--branch]`, `pull`;
  `ledger backup [DIR]` (dolt backup sync to a file:// dir, default `<data_dir>/backups/<ledger>`), `backup ls`.
  Use Dolt stored procedures (DOLT_REMOTE, DOLT_PUSH, DOLT_PULL, DOLT_BACKUP) via the driver.
- Author resolution helper used by commits (ledger.yaml → workspace.yaml → `git config user.name/email`), with
  `ledger config get|set author`.

Tests: file:// remote in tmp_path — push from ledger A, clone/pull into B, data equal; backup creates restorable copy.
