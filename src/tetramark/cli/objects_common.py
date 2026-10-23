"""Shared output helpers for the object command groups (spec §6).

Reads are delegated to :mod:`tetramark.cli.output` (table/json/csv); writes
print their SQL under ``--dry-run`` and the created/changed objects under
``--json``.  This module stays as the write-side facade so the command groups
import one name.
"""

from __future__ import annotations

from tetramark.cli.output import emit_write

__all__ = ["emit_write"]
