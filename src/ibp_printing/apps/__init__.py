"""The programs built on the library, one per console script.

* :mod:`.server`: ``ibp-print-server``, the intake stations' HTTP print server.
* :mod:`.watcher`: ``ibp-label-watcher(w)``, prints the shipping laptops'
  ``Downloads/to-print/`` queue.
* :mod:`.diag`: ``ibp-print-diag``, printer diagnostics and test prints.

No library module imports these, except the ``ibp_printing.diagnostics``
alias kept for shippy and shippy-gui.
"""
