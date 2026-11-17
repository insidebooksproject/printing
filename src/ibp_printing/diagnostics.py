"""Moved to :mod:`ibp_printing.apps.diag`; kept so old imports still work.

shippy uses ``build_report`` and shippy-gui's ``diagnose-printers`` script is
``ibp_printing.diagnostics:main``.
"""

import sys

# pylint: disable-next=wildcard-import,unused-wildcard-import
from ibp_printing.apps.diag import *  # noqa: F403
from ibp_printing.apps.diag import __all__, main

if __name__ == "__main__":
    sys.exit(main())
