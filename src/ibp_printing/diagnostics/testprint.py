"""``ibp-print-diag --test-print``: the 4x6 test label and printing it."""

import logging
import socket
from datetime import datetime
from typing import Any, Optional

from PIL import Image, ImageDraw, ImageFont

from ibp_printing.api import print_image, print_to_first_available
from ibp_printing.backends import PrintError
from ibp_printing.diagnostics.status import direct_layer
from ibp_printing.discovery import direct_candidates, is_direct_name
from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.models import JobOutcome, PrinterCandidate, PrintResult

logger = get_logger(__name__)

LABEL_SIZE = (1200, 1800)  # 4x6 inches at 300 DPI
LABEL_DPI = 300
TEST_PRINT_TIMEOUT_S = 60.0


def make_test_label(printer_name: str, when: Optional[datetime] = None) -> Image.Image:
    """Draw a 4x6 inch (1200x1800 at 300 DPI) test label.

    A border, a half-inch ruler grid and corner marks make scaling, cropping
    and rotation visible on the printed label.
    """
    when = when or datetime.now()
    width, height = LABEL_SIZE
    img = Image.new("L", LABEL_SIZE, 255)
    draw = ImageDraw.Draw(img)
    _draw_ruler(draw)
    _draw_frame(draw)

    lines = [
        ("IBP TEST PRINT", 110),
        (socket.gethostname(), 56),
        (when.isoformat(sep=" ", timespec="seconds"), 48),
        (printer_name or "(first available printer)", 44),
        (f'{width}x{height} px = 4x6" @ {LABEL_DPI} DPI', 40),
    ]
    box_top, box_bottom = height // 2 - 300, height // 2 + 260
    draw.rectangle(
        [(80, box_top), (width - 80, box_bottom)], fill=255, outline=0, width=6
    )
    y = box_top + 60
    for text, size in lines:
        draw.text((width // 2, y), text, fill=0, font=_font(size), anchor="ma")
        y += int(size * 1.6)

    img.info["dpi"] = (LABEL_DPI, LABEL_DPI)
    return img


def _draw_ruler(draw: ImageDraw.ImageDraw) -> None:
    """Half-inch grid with inch labels along the top and left edges."""
    width, height = LABEL_SIZE
    font = _font(28)
    step = LABEL_DPI // 2
    for x in range(step, width, step):
        major = x % LABEL_DPI == 0
        draw.line(
            [(x, 0), (x, height)], fill=120 if major else 200, width=3 if major else 1
        )
        draw.text((x + 4, 20), f'{x / LABEL_DPI:g}"', fill=0, font=font)
    for y in range(step, height, step):
        major = y % LABEL_DPI == 0
        draw.line(
            [(0, y), (width, y)], fill=120 if major else 200, width=3 if major else 1
        )
        draw.text((20, y + 4), f'{y / LABEL_DPI:g}"', fill=0, font=font)


def _draw_frame(draw: ImageDraw.ImageDraw) -> None:
    """Thick border plus a label in each corner, to reveal cropping and rotation."""
    width, height = LABEL_SIZE
    font = _font(36)
    draw.rectangle([(0, 0), (width - 1, height - 1)], outline=0, width=12)
    for text, anchor, pos in (
        ("TOP LEFT", "la", (30, 60)),
        ("TOP RIGHT", "ra", (width - 30, 60)),
        ("BOTTOM LEFT", "ld", (30, height - 30)),
        ("BOTTOM RIGHT", "rd", (width - 30, height - 30)),
    ):
        draw.text(pos, text, fill=0, font=font, anchor=anchor)


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except (OSError, TypeError, ImportError):  # Pillow without FreeType
        return ImageFont.load_default()


def run_test_print(
    printer_name: Optional[str], *, direct_only: bool = False
) -> tuple[Optional[PrintResult], str]:
    """Print a test label and return ``(result, error)``; error is "" on success.

    Args:
        printer_name: The queue to print to, or None/"" for the best usable one.
        direct_only: Print only to a direct USB printer; never to a print
            queue (see :func:`run_direct_test_print`).
    """
    if direct_only:
        return run_direct_test_print(printer_name)
    img = make_test_label(printer_name or "")
    try:
        if printer_name:
            result = print_image(
                img,
                printer_name,
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
        else:
            result = print_to_first_available(
                img, job_name="IBP Test Print", track_timeout_s=TEST_PRINT_TIMEOUT_S
            )
    except PrintError as exc:
        log_event(
            logger, logging.ERROR, "test print failed", error=describe_exception(exc)
        )
        return None, str(exc)
    return result, ""


def run_direct_test_print(
    printer_name: Optional[str],
) -> tuple[Optional[PrintResult], str]:
    """``--test-print --direct-only``: a direct USB print or nothing.

    The label goes to the named direct printer (or the best usable one) over
    USB only. When there is none, direct printing is off, or the print is
    refused before sending (``PrintError``), the error says so and nothing is
    sent anywhere: no Windows / CUPS queue is ever tried.
    """
    nothing = "nothing was printed and no print queue was tried"
    layer = direct_layer()
    if layer is None:
        return _direct_only_failed(
            f"the active backend has no direct USB layer; {nothing}"
        )
    enabled, why = layer.direct_mode()
    if not enabled:
        return _direct_only_failed(f"direct USB printing is OFF ({why}); {nothing}")
    if printer_name and not is_direct_name(printer_name):
        return _direct_only_failed(
            f"{printer_name!r} is not a direct USB printer name (it would print "
            "through a print queue); use the 'printer name:' from --direct-status, "
            f"e.g. 'PM2411BT (USB direct, serial ...)'; {nothing}"
        )
    try:
        if printer_name:
            img = make_test_label(printer_name)
            result = layer.print_image(
                img,
                printer_name,
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
        else:
            devices, _ = layer.list_devices()
            usable = sorted(
                (c for c in direct_candidates(devices) if c.usable),
                key=PrinterCandidate.rank_key,
            )
            if not usable:
                return _direct_only_failed(
                    "no usable direct USB printer found (run --direct-status to "
                    f"see why); {nothing}"
                )
            img = make_test_label(usable[0].name)
            result = layer.print_to_candidate(
                img,
                usable[0],
                job_name="IBP Test Print",
                track_timeout_s=TEST_PRINT_TIMEOUT_S,
            )
    except PrintError as exc:
        log_event(
            logger,
            logging.ERROR,
            "direct-only test print not sent",
            error=describe_exception(exc),
        )
        reason = getattr(exc, "reason", "")
        return _direct_only_failed(
            f"direct USB print not sent{f' ({reason})' if reason else ''}: "
            f"{str(exc).rstrip('.')}; {nothing}"
        )
    return result, ""


def _direct_only_failed(message: str) -> tuple[None, str]:
    text = f"--direct-only: {message}"
    log_event(logger, logging.ERROR, "direct-only test print failed", error=text)
    return None, text


def describe_result(result: Optional[PrintResult], error: str) -> str:
    """Human-readable summary of a test print."""
    if result is None:
        return f"-- Test print --\n  FAILED: {error}"
    lines = [
        "-- Test print --",
        f"  printer={result.printer_name!r}",
        f"  job_name={result.job_name!r} job_id={result.job_id}",
        f"  outcome={result.outcome.value} ({_outcome_text(result)})",
        f"  elapsed={result.elapsed_s}s",
    ]
    lines += [f"  history: {entry}" for entry in result.history]
    return "\n".join(lines)


def _outcome_text(result: PrintResult) -> str:
    if result.outcome.ok:
        return "label most likely printed"
    if result.outcome is JobOutcome.UNCERTAIN:
        return "PROBLEM: the label may or may not have printed; check the printer"
    if result.outcome is JobOutcome.TRACKING_FAILED:
        return "PROBLEM: spooled, but following the job failed; check the printer"
    if result.outcome is JobOutcome.TIMEOUT:
        return "PROBLEM: the printer did not finish in time; check the printer"
    return "PROBLEM: check the printer"


def result_data(result: Optional[PrintResult], error: str) -> dict[str, Any]:
    """A test print's result (or error) as JSON-friendly data."""
    if result is None:
        return {"ok": False, "error": error}
    return {
        "ok": result.outcome.ok,
        "printer": result.printer_name,
        "job_name": result.job_name,
        "job_id": result.job_id,
        "outcome": result.outcome.value,
        "history": result.history,
        "elapsed_s": result.elapsed_s,
    }
