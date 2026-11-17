"""``ibp-print-diag``: explain which printers are usable and why, and test them.

The default action runs a full discovery pass and prints a report showing
whether direct USB printing is on, every direct USB printer (path, serial,
1284 ID, and a label-safe status probe: cover and paper), then for every print
queue each detection gate and its result, every USB device that has a
VID:PID, the final verdict, recent PrintService events, and where the logs
live. ``--test-print`` sends a 4x6 test label (directly over USB when a
supported printer is plugged in; with ``--direct-only`` only directly, never
through a print queue), ``--direct-status`` runs only the direct probe and
names each direct printer as ``--test-print`` takes it, ``--listen SECONDS``
shows every line the direct printer pushes (cover open/close, realign), and
``--no-direct`` turns the direct path off for this run.

Modules: :mod:`.report` (the report), :mod:`.testprint` (the test label),
:mod:`.status` (``--direct-status``), :mod:`.listen` (``--listen``) and
:mod:`.cli` (arguments and ``main``).
"""

from ibp_printing.diagnostics.cli import EXIT_DIRECT_NOT_SENT, main
from ibp_printing.diagnostics.report import LOG_APP, build_report, report_data
from ibp_printing.diagnostics.testprint import (
    LABEL_SIZE,
    describe_result,
    make_test_label,
    run_test_print,
)

__all__ = [
    "EXIT_DIRECT_NOT_SENT",
    "LABEL_SIZE",
    "LOG_APP",
    "build_report",
    "describe_result",
    "main",
    "make_test_label",
    "report_data",
    "run_test_print",
]
