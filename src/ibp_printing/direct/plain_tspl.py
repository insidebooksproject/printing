"""Direct USB printing for plain-TSPL label printers (the iDPRT SP310).

The job is what the vendor's CUPS filter (``rastertolabeltspl``) sends,
verified on hardware (``docs/printers/sp310.md``)::

    \\r\\nSIZE 36 mm,89 mm\\r\\nGAP 3 mm,0 mm\\r\\nDIRECTION 0,0\\r\\n
    REFERENCE 0,0\\r\\nCLS\\r\\nBITMAP 0,0,36,712,0,<raster>\\r\\nPRINT 1,1\\r\\n

``<raster>`` is the label uncompressed, 8 dots per byte, MSB first, a 0 bit
printing a black dot (BITMAP mode 0). The label size comes from the model's
:class:`~ibp_printing.printers.Media`: TSPL printers cannot report it.

Status comes from TSPL's real-time query ``ESC ! ?`` (``1B 21 3F``), which
the printer answers with one byte at any time, even mid-print; it never
prints or feeds. Seen on the SP310: ``0x00`` ready, ``0x20`` printing,
``0x40`` cover open. The other bits are TSPL's documented ones.

:func:`print_label` runs one print over an open ``Transport``:

1. drain anything stale, then ask the status (twice before giving up). Not
   ready -> ``DirectPrintError`` (nothing sent): cover open, out of labels,
   jammed, paused, in error, no reply, or still printing a previous label
   after ``realign_wait_s``;
2. write the job once (``job_write_s``);
3. poll the status every ``poll_s``: the printing bit (``0x20``) then a
   ready ``0x00`` -> ``COMPLETED``; still printing after ``done_s`` ->
   ``TIMEOUT``; an error bit -> ``ERROR`` (a part label may have come out);
   never printing within ``doing_s``, a stalled write or a lost printer ->
   ``UNCERTAIN``.

The job is never resent. Every status byte is in the result's history.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from PIL import Image

from ibp_printing.direct import tspl
from ibp_printing.direct.session import (
    ImageOrRaster,
    PrintSession,
    SessionOutcome,
    SessionResult,
    SessionTimeouts,
    StatusProbe,
    guarded_build,
)
from ibp_printing.log import describe_exception, get_logger, log_event
from ibp_printing.printers import Media
from ibp_printing.render import flatten_for_print, orient_portrait

if TYPE_CHECKING:
    from ibp_printing.direct.transport import Transport

logger = get_logger(__name__)

QUERY_STATUS = b"\x1b!?"

# ESC ! ? status bits (TSPL); 0x00 is ready.
HEAD_OPEN = 0x01
PAPER_JAM = 0x02
PAPER_OUT = 0x04
RIBBON_OUT = 0x08
PAUSED = 0x10
PRINTING = 0x20
COVER_OPEN = 0x40
OTHER_ERROR = 0x80

STATUS_FLAGS = (
    (HEAD_OPEN, "head open"),
    (PAPER_JAM, "paper jam"),
    (PAPER_OUT, "out of labels"),
    (RIBBON_OUT, "ribbon out"),
    (PAUSED, "paused"),
    (PRINTING, "printing"),
    (COVER_OPEN, "cover open"),
    (OTHER_ERROR, "other error"),
)

# Not-ready bits, most actionable first, and the DirectPrintError reason each
# gives before the job is sent.
_BLOCKING = (
    (HEAD_OPEN | COVER_OPEN, "cover_open"),
    (PAPER_OUT, "paper_out"),
    (PAPER_JAM, "paper_jam"),
    (PAUSED, "paused"),
    (RIBBON_OUT | OTHER_ERROR, "printer_error"),
)
ERROR_BITS = 0xFF & ~PRINTING


def status_flags(status: int) -> list[str]:
    """Names of the set status bits (``[]`` when ready)."""
    return [name for bit, name in STATUS_FLAGS if status & bit]


def describe_status(status: int) -> str:
    """``"0x20 (printing)"`` / ``"0x00 (ready)"``."""
    return f"0x{status:02x} ({', '.join(status_flags(status)) or 'ready'})"


def blocking_reason(status: int) -> Optional[str]:
    """The pre-check failure reason for an error ``status``, or None.

    None when ready or only printing.
    """
    for bits, reason in _BLOCKING:
        if status & bits:
            return reason
    return "printer_error" if status & ERROR_BITS else None


# -- the job ------------------------------------------------------------------


def fit_rect(size: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """The largest ``(width, height)`` with ``size``'s aspect ratio inside ``box``."""
    width, height = size
    box_w, box_h = box
    if width <= 0 or height <= 0:
        raise ValueError(f"image has no area: {size}")
    if box_w <= 0 or box_h <= 0:
        raise ValueError(f"label has no printable area: {box}")
    if width * box_h >= height * box_w:
        return box_w, min(box_h, max(1, round(height * box_w / width)))
    return min(box_w, max(1, round(width * box_h / height))), box_h


def place_on_label(img: Image.Image, media: Media) -> Image.Image:
    """Return a grey ("L") image of the whole label holding ``img``.

    The label is portrait (``width_mm`` across the head): a landscape image
    is rotated a quarter turn counter-clockwise. An image already the label's
    size in dots is used dot for dot. Anything else is thresholded and scaled
    to fit inside the margins, centered, the way the PM2411BT path does it
    (:func:`ibp_printing.direct.tspl.scale_dark_mask`: nearest-neighbour,
    keeping 1-pixel rules).
    """
    size = (media.width_dots, media.height_dots)
    img = flatten_for_print(orient_portrait(img)).convert("L")
    if img.size == size:
        return img
    margin = media.margin_dots
    box = (size[0] - 2 * margin, size[1] - 2 * margin)
    new_w, new_h = fit_rect(img.size, box)
    mask = img.point(
        [0 if value <= tspl.DARK_THRESHOLD else 255 for value in range(256)]
    )
    scaled = tspl.scale_dark_mask(mask, (new_w, new_h))
    canvas = Image.new("L", size, 255)
    canvas.paste(scaled, ((size[0] - new_w) // 2, (size[1] - new_h) // 2))
    return canvas


def bytes_per_row(media: Media) -> int:
    """Raster bytes per label row."""
    return (media.width_dots + 7) // 8


def raster_size(media: Media) -> int:
    """Raster bytes for one label."""
    return bytes_per_row(media) * media.height_dots


def rasterize(img: Image.Image, media: Media) -> bytes:
    """Place ``img`` on the label and pack it (0 bit = black dot)."""
    return tspl.pack_bits(place_on_label(img, media))


def job_header(media: Media) -> bytes:
    """Everything before the BITMAP raster."""
    lines = (
        f"SIZE {media.width_mm:g} mm,{media.height_mm:g} mm",
        f"GAP {media.gap_mm:g} mm,0 mm",
        "DIRECTION 0,0",
        "REFERENCE 0,0",
        "CLS",
    )
    text = "\r\n" + "".join(line + "\r\n" for line in lines)
    text += f"BITMAP 0,0,{bytes_per_row(media)},{media.height_dots},0,"
    return text.encode("ascii")


def build_job(raster: bytes, media: Media) -> bytes:
    """The complete one-label job for a packed raster."""
    expected = raster_size(media)
    if len(raster) != expected:
        raise ValueError(f"raster must be {expected} bytes, got {len(raster)}")
    return job_header(media) + bytes(raster) + tspl.JOB_TRAILER


def parse_job(job: bytes, media: Media) -> bytes:
    """Check a job's framing and return its raster (tests, tools)."""
    header = job_header(media)
    if not job.startswith(header) or not job.endswith(tspl.JOB_TRAILER):
        raise ValueError("job does not have the expected header and trailer")
    raster = job[len(header) : len(job) - len(tspl.JOB_TRAILER)]
    if len(raster) != raster_size(media):
        raise ValueError(f"raster is {len(raster)} bytes")
    return raster


def prepare_job(img_or_raster: ImageOrRaster, media: Media) -> bytes:
    """Rasterize (if needed) and build the job.

    Raises:
        DirectPrintError: (``bad_image``) if the input cannot be printed.
    """
    started = time.monotonic()

    def build() -> bytes:
        if isinstance(img_or_raster, Image.Image):
            return build_job(rasterize(img_or_raster, media), media)
        return build_job(bytes(img_or_raster), media)

    job = guarded_build(build)
    log_event(
        logger,
        logging.DEBUG,
        "job built",
        media=media.describe(),
        job_bytes=len(job),
        build_s=round(time.monotonic() - started, 3),
    )
    return job


# -- the session ----------------------------------------------------------------


class TsplSession(PrintSession):
    """Runs one print (or probe) over ``transport``; not reusable.

    Shares the job write (``send``: a stall is UNCERTAIN, nothing accepted
    a ``DirectPrintError``) and the history bookkeeping with the PM2411BT's
    :class:`~ibp_printing.direct.session.PrintSession`; the pre-check, the
    watch and the probe are its own, from the status byte.

    Uses these :class:`SessionTimeouts` fields: ``drain_s``,
    ``query_write_s``, ``query_reply_s``, ``cover_query_attempts`` (status
    attempts), ``realign_wait_s`` (waiting out a previous label still
    printing), ``job_write_s``, ``doing_s`` (to see the printing bit),
    ``done_s`` (to see it clear) and ``poll_s``.
    """

    def __init__(
        self,
        transport: "Transport",
        *,
        job_name: str,
        timeouts: Optional[SessionTimeouts] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(transport, job_name=job_name, timeouts=timeouts, clock=clock)
        # The last status byte received.
        self.status: Optional[int] = None

    # ------------------------------------------------------------- helpers

    def _record(self, status: int) -> None:
        if status != self.status:
            self._note(f"status {describe_status(status)}")
        self.status = status
        self.result.cover = "OPEN" if status & (HEAD_OPEN | COVER_OPEN) else "CLOSE"
        self.result.paper = "NO" if status & PAPER_OUT else "YES"
        self._status_event(status, describe_status(status))

    def _wait(self, seconds: float) -> None:
        """Let ``seconds`` pass, logging anything unexpected that arrives."""
        data = self.transport.read_bytes(max(0.0, seconds))
        if data:
            log_event(
                logger, logging.DEBUG, "late or unasked bytes", data=data.hex(" ")
            )

    def _write_status_query(self) -> bool:
        """Send ``ESC ! ?``; False if the printer did not take all of it.

        EAGAIN-style refusals (the printer is busy taking a label) are not
        errors. A dead device raises.
        """
        sent = 0
        for _ in range(2):
            try:
                sent += self.transport.write_all(
                    QUERY_STATUS[sent:], self.timeouts.query_write_s
                )
            except OSError as exc:
                log_event(
                    logger,
                    logging.DEBUG,
                    "status query not taken",
                    error=describe_exception(exc),
                )
            if sent >= len(QUERY_STATUS):
                return True
        if sent:
            self._note(
                f"status query cut short ({sent}/{len(QUERY_STATUS)} bytes taken)",
                logging.WARNING,
            )
        return False

    def ask_status(self) -> Optional[int]:
        """Ask ``ESC ! ?`` once; the status byte, or None (busy / no reply)."""
        stale = self.transport.read_bytes(0)
        if stale:
            log_event(logger, logging.DEBUG, "stale bytes", data=stale.hex(" "))
        if not self._write_status_query():
            return None
        reply = self.transport.read_bytes(self.timeouts.query_reply_s)
        if not reply:
            return None
        if len(reply) > 1:
            log_event(logger, logging.DEBUG, "extra reply bytes", data=reply.hex(" "))
        status = reply[-1]
        self._record(status)
        return status

    def _ask_with_retries(self) -> Optional[int]:
        for _ in range(max(1, self.timeouts.cover_query_attempts)):
            status = self.ask_status()
            if status is not None:
                return status
        return None

    # ------------------------------------------------------------- phases

    def _precheck_steps(self) -> None:
        """Pass only when the printer is ready (see the module docstring).

        Raises:
            DirectPrintError: if printing must not start (nothing was sent).
        """
        self._wait(self.timeouts.drain_s)
        status = self._ask_with_retries()
        if status is None:
            raise self._fail("no_status_reply", "printer did not answer ESC ! ?")
        deadline = self.clock() + self.timeouts.realign_wait_s
        while status is None or status == PRINTING:
            if self.clock() >= deadline:
                raise self._fail(
                    "printer_busy",
                    f"still printing after {self.timeouts.realign_wait_s:g} s",
                )
            self._wait(self.timeouts.poll_s)
            status = self.ask_status()
        reason = blocking_reason(status)
        if reason is not None:
            raise self._fail(reason, f"printer status {describe_status(status)}")
        self._note("printer ready")

    def watch(self) -> SessionOutcome:
        """Poll the status after a complete write until the label is done."""
        started = self.clock()
        printing_since: Optional[float] = None
        unanswered = 0
        while True:
            status = self.ask_status()
            now = self.clock()
            if status is None:
                unanswered += 1
            elif status & ERROR_BITS:
                self._note(
                    f"printer reports {describe_status(status)} after the job "
                    "(a part label may have come out)",
                    logging.ERROR,
                )
                return SessionOutcome.ERROR
            elif status & PRINTING:
                if printing_since is None:
                    printing_since = now
                    self.result.saw_doing = True
                    self.result.timings["doing_after_s"] = round(now - started, 3)
            elif printing_since is not None:
                self.result.saw_done = True
                self.result.timings["print_s"] = round(now - printing_since, 3)
                self._note(f"label printed in {self.result.timings['print_s']} s")
                return SessionOutcome.COMPLETED
            if printing_since is None and now - started >= self.timeouts.doing_s:
                self._note(
                    f"printer never reported printing within "
                    f"{self.timeouts.doing_s:g} s ({unanswered} status queries "
                    "unanswered; the label may or may not have printed)",
                    logging.ERROR,
                )
                return SessionOutcome.UNCERTAIN
            if printing_since is not None and now - printing_since >= (
                self.timeouts.done_s
            ):
                self._note(
                    f"printer still printing after {self.timeouts.done_s:g} s "
                    "(jam? labels ran out mid-label?)",
                    logging.ERROR,
                )
                return SessionOutcome.TIMEOUT
            self._wait(self.timeouts.poll_s)

    def _probe_queries(self, probe: StatusProbe) -> None:
        """Drain, then ask ``ESC ! ?`` only (it cannot feed or print)."""
        self._wait(self.timeouts.drain_s)
        status = self._probe_status_byte(probe, self._ask_with_retries(), status_flags)
        if status is not None:
            probe.printing = "DOING" if status & PRINTING else None


def probe_status(
    transport: "Transport",
    *,
    timeouts: Optional[SessionTimeouts] = None,
    clock: Callable[[], float] = time.monotonic,
) -> StatusProbe:
    """Ask a plain-TSPL printer its status without printing; never raises."""
    session = TsplSession(
        transport, job_name="status probe", timeouts=timeouts, clock=clock
    )
    return session.probe()


def print_label(
    transport: "Transport",
    img_or_raster: ImageOrRaster,
    *,
    media: Media,
    job_name: str,
    timeouts: Optional[SessionTimeouts] = None,
    clock: Callable[[], float] = time.monotonic,
) -> SessionResult:
    """Print one label on ``media`` over an open transport.

    Returns:
        The session result (COMPLETED, UNCERTAIN, TIMEOUT or ERROR).

    Raises:
        DirectPrintError: (a ``PrintError``) when nothing was sent.
    """
    job = prepare_job(img_or_raster, media)
    session = TsplSession(transport, job_name=job_name, timeouts=timeouts, clock=clock)
    return session.run(job)


__all__ = [
    "QUERY_STATUS",
    "TsplSession",
    "blocking_reason",
    "build_job",
    "describe_status",
    "parse_job",
    "place_on_label",
    "prepare_job",
    "print_label",
    "probe_status",
    "rasterize",
    "status_flags",
]
