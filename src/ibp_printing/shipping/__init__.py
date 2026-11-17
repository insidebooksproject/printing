"""The shipping-label workflow shared by shippy, shippy-gui and the label watcher.

* :mod:`.paths`: the Downloads folder and the ``to-print/`` / ``printed/`` /
  ``check-printer/`` queue under it, label sidecars, :func:`save_for_retry`.
* :mod:`.labels`: the per-user label journal (duplicate-purchase warnings).

No core module (:mod:`ibp_printing.api`, the backends, ``direct``, ``jobs``)
imports this, and the intake stations' print server does not use it; the
package's top-level ``__init__`` only re-exports its public names.
"""
