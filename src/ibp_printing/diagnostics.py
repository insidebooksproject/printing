"""Moved to :mod:`ibp_printing.apps.diag`; kept so old imports still work.

shippy uses ``build_report`` and shippy-gui's ``diagnose-printers`` script is
``ibp_printing.diagnostics:main``. Every public name of the old single-module
``ibp_printing.diagnostics`` is still importable from here.
"""

import sys

# pylint: disable=unused-import
from ibp_printing.apps.diag.cli import EXIT_DIRECT_NOT_SENT, main
from ibp_printing.apps.diag.listen import (
    LISTEN_FINAL_S,
    LISTEN_POLL_S,
    describe_line,
    run_listen,
)
from ibp_printing.apps.diag.report import (
    LOG_APP,
    NO_PRINTER_MESSAGE,
    PROBABLY_PM2411BT,
    RULE,
    build_report,
    describe_direct_devices,
    direct_section,
    identify_device,
    open_problem,
    report_data,
)
from ibp_printing.apps.diag.status import probe_direct_devices, run_direct_status
from ibp_printing.apps.diag.testprint import (
    LABEL_DPI,
    LABEL_SIZE,
    TEST_PRINT_TIMEOUT_S,
    describe_result,
    make_test_label,
    run_direct_test_print,
    run_test_print,
)

if __name__ == "__main__":
    sys.exit(main())
