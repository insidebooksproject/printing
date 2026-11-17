"""``ibp-print-diag``: the command line (see the package docstring)."""

import argparse
import json
import logging
from pathlib import Path
from typing import Optional, Sequence

from ibp_printing.api import discover, get_backend
from ibp_printing.apps.diag.listen import run_listen
from ibp_printing.apps.diag.report import LOG_APP, build_report, report_data
from ibp_printing.apps.diag.status import probe_direct_devices, run_direct_status
from ibp_printing.apps.diag.testprint import (
    describe_result,
    result_data,
    run_test_print,
)
from ibp_printing.direct.config import set_direct_enabled
from ibp_printing.log import configure_logging, get_logger, log_event
from ibp_printing.models import PrintResult

logger = get_logger(__name__)

# Exit status of ``--test-print --direct-only`` when nothing was printed
# (no usable direct printer, direct printing off, or the direct print was
# refused before sending). 1 stays "printed, but not confirmed".
EXIT_DIRECT_NOT_SENT = 3


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ibp-print-diag",
        description="Explain which label printers are usable and why.",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="where to write printer-diag.log/.jsonl",
    )
    parser.add_argument(
        "--events",
        type=int,
        default=60,
        metavar="MINUTES",
        help="include PrintService events from the last MINUTES (0 to skip)",
    )
    parser.add_argument(
        "--json", action="store_true", help="print machine-readable JSON instead"
    )
    parser.add_argument(
        "--test-print",
        nargs="?",
        const="",
        default=None,
        metavar="PRINTER",
        help="print a 4x6 test label to PRINTER (default: best usable printer)",
    )
    parser.add_argument(
        "--direct-only",
        action="store_true",
        help="with --test-print: print only over direct USB; if no direct printer "
        "is usable or it refuses, fail (exit status 3) instead of using a print "
        "queue",
    )
    parser.add_argument(
        "--direct-status",
        action="store_true",
        help="only list direct USB printers and probe their cover/paper state "
        "(sends nothing that prints)",
    )
    parser.add_argument(
        "--listen",
        type=float,
        default=None,
        metavar="SECONDS",
        help="open the direct USB printer and show every line it sends for "
        "SECONDS (sends only SSSGETCAP, at the start and the end; no label); "
        "open and close the cover meanwhile. Ctrl+C stops early",
    )
    parser.add_argument(
        "--no-direct",
        action="store_true",
        help="turn direct USB printing off for this run (like IBP_PRINTING_DIRECT=0)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="also echo the log to stderr"
    )
    args = parser.parse_args(argv)
    if args.direct_only and args.test_print is None:
        parser.error("--direct-only only applies to --test-print")
    if args.listen is not None and args.listen <= 0:
        parser.error("--listen needs a number of seconds > 0")
    if args.listen is not None and (args.direct_status or args.test_print is not None):
        parser.error("--listen cannot be combined with --direct-status/--test-print")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point for ``ibp-print-diag``. Returns the process exit code."""
    args = _parse_args(argv)
    if args.no_direct:
        set_direct_enabled(False)
    log_dir = configure_logging(args.log_dir, app=LOG_APP, console=args.verbose)
    if args.direct_status:
        return run_direct_status(args.json)
    if args.listen is not None:
        return run_listen(args.listen, args.json)

    discovery = discover()
    probes = (
        probe_direct_devices(discovery.direct_devices)
        if discovery.direct_enabled
        else {}
    )
    events = (
        get_backend().recent_print_events(minutes=args.events)
        if args.events > 0
        else []
    )
    report = build_report(discovery, events, log_dir=log_dir, probes=probes)
    log_event(logger, logging.INFO, "diagnostics report\n" + report)

    exit_code = 0
    test_result: Optional[PrintResult] = None
    test_error = ""
    if args.test_print is not None:
        test_result, test_error = run_test_print(
            args.test_print or None, direct_only=args.direct_only
        )
        exit_code = 0 if test_result is not None and test_result.outcome.ok else 1
        if test_result is None and args.direct_only:
            exit_code = EXIT_DIRECT_NOT_SENT
    elif not discovery.usable:
        exit_code = 1

    if args.json:
        data = report_data(discovery, events, log_dir=log_dir, probes=probes)
        if args.test_print is not None:
            data["test_print"] = result_data(test_result, test_error)
        print(json.dumps(data, indent=2, default=repr))
    else:
        print(report)
        if args.test_print is not None:
            print()
            print(describe_result(test_result, test_error))
    return exit_code
