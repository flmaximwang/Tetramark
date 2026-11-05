"""Tetramark — a version-controlled inventory ledger on Dolt.

A ledger is one self-contained directory (``.dolt/`` plus ``ledger.yaml``) that
lives next to its docs or repository.  :func:`open` takes its path or discovers
it upward from the current directory; a 0.5.0 ledger has no workspace layer.
"""

from tetramark.core.ledger import Ledger, adopt_ledger, init_ledger, open

__version__ = "0.8.30"

__all__ = ["Ledger", "__version__", "adopt_ledger", "init_ledger", "open"]
