"""The shipping-label workflow shared by shippy, shippy-gui and the label watcher.

* :mod:`.paths`: the Downloads folder and the ``to-print/`` / ``printed/`` /
  ``check-printer/`` queue under it, label sidecars, :func:`save_for_retry`.
* :mod:`.labels`: the per-user label journal (duplicate-purchase warnings).

Nothing in the printing core (:mod:`ibp_printing.api`, the backends) depends
on this; the intake stations' print server does not use it.
"""
