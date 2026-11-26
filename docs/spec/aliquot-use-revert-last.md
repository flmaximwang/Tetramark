# Confirming and reverting aliquot use

This specification defines the behavior to ship for `tm aliquot use`. It adds a confirmation step to inventory deductions and a way to correct a mistaken deduction while retaining its audit history.

## Command forms

```text
tm aliquot use REF [AMOUNT UNIT] -m MESSAGE [--yes]
tm aliquot use REF --revert-last -m MESSAGE [--yes]
```

`REF` accepts an aliquot ID or alias. `--revert-last` is mutually exclusive with both amount and unit positionals. Both forms require a nonblank `-m` / `--message`, trimmed before it is stored. Omitting both amount positionals in ordinary `use` still consumes the whole remainder. Existing rules for quantities, units and availability continue to apply.

## Preview and confirmation

- Validate the request before prompting. Show the canonical aliquot ID, operation, reason, and quantity before and after. Show any status or placement change, including the box and slot being freed or restored, where applicable. For a revert, also show the target event ID, timestamp and original message if available. Render an unknown quantity as unknown.
- In an interactive terminal, print the preview and ask exactly once with `[y/N]`. An affirmative answer authorizes the displayed operation. Declining, pressing Enter without an answer, or ending input cancels without writing a row, event, or commit. Report `Cancelled` and exit nonzero.
- `--yes` provides explicit confirmation without a prompt, including in scripts and agents. A real write with non-interactive stdin and without `--yes` exits nonzero with a hint to pass `--yes`. `--dry-run` validates and previews the requested operation without prompting or writing, with or without `--yes`. `--no-commit` still needs confirmation for a real write and leaves the row and event together in the working set.
- Human previews, prompts and cancellation messages use stderr. With `--json`, stdout is reserved for the structured result of the selected mode, including any dry-run output under the CLI's existing dry-run contract. Ordinary `use` retains its existing result fields.
- Before applying a write, revalidate the target and relevant state in the write session. If the operation would now differ from its preview, or a conflict has appeared, abort and ask the caller to rerun the command. A confirmation never applies to a different `use` event.

## What `--revert-last` restores

1. Select the most recent original `use` event for this aliquot on the resolved ledger branch. If there is no `use`, or the latest one already has a reversal, report that condition. Repeated invocations never step back to an earlier `use`. A later new `use` can itself be reverted once.
2. Check for subsequent changes to the aliquot's quantity, unit, status, or placement. A later move, status change, or edit of those fields blocks the revert. Label and alias changes do not block it and are preserved. Where historical records cannot establish whether one of the affected fields changed, fail with a diagnostic. Apply this rule even if the current values happen to match the earlier post-use state.
3. Restore the pre-use quantity, unit, status and placement. A partial deduction restores the amount consumed while preserving unchanged state. A whole-tube deduction restores the prior quantity and status plus any position cleared by the deduction. A quantity that was unknown before use remains unknown. Historical use records created before the feature are eligible when the ledger has been upgraded and the event and available history establish the values to restore and the absence of conflicting changes. Missing or ambiguous evidence is an error. The command must not guess values, including a historical quantity or old status.
4. Validate the resulting row using the ledger's placement and integrity rules. If the old location no longer exists, cannot accept the tube, or is now occupied, abort the entire operation before any write. When another tube occupies the position, identify it and the position in the error. The caller can retry the same revert once the position is available.
5. Update the aliquot and append its reversal event atomically. By default a successful operation creates one Dolt commit. If anything fails, leave the aliquot and its event history as they were.

## Audit event and ledger upgrade

Add a distinct `aliquot_event` kind named `revert_use`. The event references the exact original `use` by event ID, retains the operator's reason, actor and timestamp, and records the restored amount and changed status/placement using the existing event fields. The original event remains intact. A reversal references a `use` for the same aliquot, and each `use` can have at most one reversal. Enforce these invariants against retries and concurrent writes. A correction that restores an old status is recognized as such by `tm check`, even when the ordinary forward transition rules would reject that status change.

`tm history` and `tm aliquot show` display the reversal and its target event ID in human and JSON output. Add the event kind and a nullable reversal reference to the generated schema with a spec version bump and a supported migration from the preceding format. Upgrading preserves existing events and IDs. On a ledger that lacks the required schema, `--revert-last` reports the upgrade requirement before writing.

## Acceptance examples

| Starting condition | Expected result |
| --- | --- |
| `5 ml` before a `2 ml` deduction | Revert restores `5 ml` and links to that `use`. |
| A stored `5 ml` tube emptied by `use` | Revert restores `5 ml`, `stored`, and its original box and slot. |
| A whole-use event for a tube with unknown quantity | Revert restores its prior status and placement with quantity still unknown. |
| The released slot is now occupied | Error identifies the occupant. No quantity, status, placement, or event changes. |
| The latest use was already reverted and an older use exists | Error identifies that the latest use was already reverted. The earlier use is untouched. |
| An amount or unit edit, status change, or move followed the use | Error identifies the conflicting change. A label or alias change alone still permits the revert. |
| The operator declines, or the confirmed state changes concurrently | No row update, event insertion, or commit. |
| An existing, unambiguous `use` predates the upgrade | The upgraded ledger can revert it without rewriting its original event. |
