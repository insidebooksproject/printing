"""Direct USB printing for the DYMO LabelWriter 450 Turbo (raster protocol).

From DYMO's *LabelWriter 450 Series Technical Reference*, cross-checked
against DYMO's CUPS driver (``raster2dymolw``) and CUPS's
``rastertolabel.c``; ``docs/printers/dymo-lw450.md`` has the details and
what is still to be checked on the printer. A job is::

    ESC x 156                 sync: ends any half-received raster line
    ESC L hi lo               label length in dot lines
    ESC h | ESC i             text 300x300 | graphics 300x600 (rows sent twice)
    ESC c|d|e|g               density light / medium / normal / dark
    ESC B 0                   dot tab 0
    ESC D n                   n bytes per raster line
    SYN <n bytes>             one raster line (MSB first, 1 = black dot)
    ESC f 1 k                 or: skip k (<= 255) blank lines
    ESC E                     form feed to the next label

``ESC @`` (reset) is never sent: it throws away a label still printing.
Every setting is sent with each job instead.

Status comes from ``ESC A`` (``1B 41``), answered at once with one byte,
also mid-print: bit 0 ready, bit 1 top of form, bit 5 out of paper, bit 6
jam, bit 7 error (also set when out of paper). An idle printer answers
``0x03``, and ``0x01`` while it feeds a label. It cannot tell that the roll
is empty until it tries to feed: an empty printer answers ``0x03``, takes
the job and then reports ``0xa1`` (seen on the station, 2026-11-26), so
running out shows as an ``ERROR`` outcome, and the next print's pre-check
refuses with ``paper_out`` until the roll is reloaded. Nothing but status
bytes is ever read during a print.

:func:`print_label` runs one print over an open ``Transport``:

1. drain anything stale, then ask the status (twice). No reply: send the
   sync run (a job cut off mid line leaves the printer reading ``ESC A`` as
   dots) and ask again. Ready but off the top of form (a cut-off job's part
   label): ``ESC E`` to feed it out, and wait for the top of form. Out of
   labels, jammed, in error, still no reply, or not ready after
   ``realign_wait_s`` -> ``DirectPrintError`` (no label sent);
2. write the job once (``job_write_s``);
3. poll the status every ``poll_s``: busy (not ready, or off the top of
   form) then idle again -> ``COMPLETED``; idle without ever looking busy for
   ``idle_settle_s`` -> ``COMPLETED`` too, but weak (the printer took the
   label and reports no fault; it has no job id or printed
   acknowledgement); busy after ``done_s`` -> ``TIMEOUT``; a fault bit ->
   ``ERROR`` (a part label may have come out); no reply within ``doing_s``,
   a stalled write or a lost printer -> ``UNCERTAIN``.

The job is never resent. Every status byte is in the result's history.
"""

from __future__ import annotations

import logging
import time
from enum import Enum
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
from ibp_printing.render import fill_page, flatten_for_print

if TYPE_CHECKING:
    from ibp_printing.direct.transport import Transport

logger = get_logger(__name__)

ESC = 0x1B
SYN = 0x16

# 156 ESCs, as raster2dymolw sends (the manual asks for at least 85): a line
# cut short by an earlier job is filled up and ignored.
SYNC = bytes([ESC]) * 156
QUERY_STATUS = b"\x1bA"
# ESC V: 10 ASCII bytes of model and firmware ("93089v0K"-style).
QUERY_VERSION = b"\x1bV"
VERSION_REPLY_BYTES = 10
RESET = b"\x1b@"
FORM_FEED = b"\x1bE"

# The head has 672 dots (84 bytes) across.
MAX_BYTES_PER_ROW = 84
MAX_SKIP = 255

# ESC A status bits.
READY = 0x01
TOP_OF_FORM = 0x02
PAPER_OUT = 0x20
PAPER_JAM = 0x40
ERROR = 0x80
FAULT_BITS = PAPER_OUT | PAPER_JAM | ERROR
IDLE = READY | TOP_OF_FORM

STATUS_FLAGS = (
    (READY, "ready"),
    (TOP_OF_FORM, "top of form"),
    (PAPER_OUT, "out of labels"),
    (PAPER_JAM, "paper jam"),
    (ERROR, "error"),
)

# Fault bits, most actionable first (ERROR comes with PAPER_OUT), and the
# DirectPrintError reason each gives before the job is sent.
_BLOCKING = (
    (PAPER_OUT, "paper_out"),
    (PAPER_JAM, "paper_jam"),
    (ERROR, "printer_error"),
)


class Quality(str, Enum):
    """Print mode: the head's vertical resolution."""

    # ESC h: 300 x 300 dpi, the Turbo's fast mode.
    TEXT = "text"
    # ESC i: 300 x 600 dpi, slower; what the CUPS queue uses today.
    GRAPHICS = "graphics"


class Density(str, Enum):
    """Print density (head heat)."""

    LIGHT = "light"
    MEDIUM = "medium"
    NORMAL = "normal"
    DARK = "dark"


_QUALITY_COMMAND = {Quality.TEXT: b"\x1bh", Quality.GRAPHICS: b"\x1bi"}
_DENSITY_COMMAND = {
    Density.LIGHT: b"\x1bc",
    Density.MEDIUM: b"\x1bd",
    Density.NORMAL: b"\x1be",
    Density.DARK: b"\x1bg",
}

# Match the CUPS queue (DymoPrintQuality Graphics, DymoPrintDensity Normal)
# until text mode is checked against the scanners (docs/printers/dymo-lw450.md).
DEFAULT_QUALITY = Quality.GRAPHICS
DEFAULT_DENSITY = Density.NORMAL


def status_flags(status: int) -> list[str]:
    """Names of the set status bits."""
    return [name for bit, name in STATUS_FLAGS if status & bit]


def describe_status(status: int) -> str:
    """``"0x03 (ready, top of form)"`` / ``"0x00 (busy)"``."""
    return f"0x{status:02x} ({', '.join(status_flags(status)) or 'busy'})"


def blocking_reason(status: int) -> Optional[str]:
    """The pre-check failure reason for a fault ``status``, or None."""
    for bit, reason in _BLOCKING:
        if status & bit:
            return reason
    return None


def is_idle(status: int) -> bool:
    """Ready, at the top of a label and no fault: nothing printing."""
    return status & (IDLE | FAULT_BITS) == IDLE


# -- the job ------------------------------------------------------------------


def place_on_label(img: Image.Image, media: Media) -> Image.Image:
    """Return a grey ("L") image of the printed area holding ``img``.

    What the CUPS queue did, so the label looks as it did through CUPS:
    :func:`ibp_printing.render.fill_page` onto the whole page (a landscape
    image turned a quarter turn counter-clockwise and scaled, aspect kept, to
    fill it; an image already the page's size used dot for dot), then that
    page scaled to fit ``media.printable_dots`` (the PPD's ImageableArea),
    which starts at the head's first dot and the label's first line.
    ``media.margin_mm`` is kept white on every side of the page.
    """
    size = (media.width_dots, media.height_dots)
    img = flatten_for_print(img).convert("L")
    margin = media.margin_dots
    if margin:
        inner = fill_page(img, (size[0] - 2 * margin, size[1] - 2 * margin))
        page = Image.new("L", size, 255)
        page.paste(inner, (margin, margin))
    else:
        page = fill_page(img, size)
    return fill_page(page, media.printable_dots)


def bytes_per_row(media: Media) -> int:
    """Raster bytes per printed row."""
    return (media.printable_dots[0] + 7) // 8


def printed_rows(media: Media) -> int:
    """Raster rows in the printed area."""
    return media.printable_dots[1]


def rows(img: Image.Image, media: Media) -> list[bytes]:
    """Place ``img`` on the label and pack each row (1 bit = black dot).

    Raises:
        ValueError: if the label is wider than the print head.
    """
    width = bytes_per_row(media)
    if width > MAX_BYTES_PER_ROW:
        raise ValueError(
            f"label is {media.printable_dots[0]} dots wide; the head has "
            f"{MAX_BYTES_PER_ROW * 8}"
        )
    # tspl.pack_bits: same threshold, MSB first, but 0 bit = black.
    packed = bytes(byte ^ 0xFF for byte in tspl.pack_bits(place_on_label(img, media)))
    return [packed[i : i + width] for i in range(0, len(packed), width)]


def job_header(
    media: Media,
    *,
    quality: Quality = DEFAULT_QUALITY,
    density: Density = DEFAULT_DENSITY,
) -> bytes:
    """Everything before the first raster line.

    ESC L is the whole label's length (``media.height_dots``), in the mode's
    dot lines.
    """
    lines = media.height_dots * (2 if quality is Quality.GRAPHICS else 1)
    if not 0 < lines <= 0xFFFF:
        raise ValueError(f"label length {lines} dot lines is out of range")
    width = bytes_per_row(media)
    if width > MAX_BYTES_PER_ROW:
        raise ValueError(
            f"label is {media.printable_dots[0]} dots wide; the head has "
            f"{MAX_BYTES_PER_ROW * 8}"
        )
    return (
        SYNC
        + bytes([ESC, ord("L"), lines >> 8, lines & 0xFF])
        + _QUALITY_COMMAND[quality]
        + _DENSITY_COMMAND[density]
        + bytes([ESC, ord("B"), 0])
        + bytes([ESC, ord("D"), width])
    )


def _skip(count: int) -> bytes:
    out = bytearray()
    while count > 0:
        step = min(count, MAX_SKIP)
        out += bytes([ESC, ord("f"), 1, step])
        count -= step
    return bytes(out)


def build_job(
    label_rows: list[bytes],
    media: Media,
    *,
    quality: Quality = DEFAULT_QUALITY,
    density: Density = DEFAULT_DENSITY,
) -> bytes:
    """The complete one-label job for packed rows (one per printed row)."""
    width = bytes_per_row(media)
    if len(label_rows) != printed_rows(media):
        raise ValueError(f"need {printed_rows(media)} rows, got {len(label_rows)}")
    repeat = 2 if quality is Quality.GRAPHICS else 1
    out = bytearray(job_header(media, quality=quality, density=density))
    blank = 0
    for row in label_rows:
        if len(row) != width:
            raise ValueError(f"rows must be {width} bytes, got {len(row)}")
        if not any(row):
            blank += repeat
            continue
        out += _skip(blank)
        blank = 0
        out += (bytes([SYN]) + bytes(row)) * repeat
    out += _skip(blank)
    out += FORM_FEED
    return bytes(out)


def parse_job(
    job: bytes,
    media: Media,
    *,
    quality: Quality = DEFAULT_QUALITY,
    density: Density = DEFAULT_DENSITY,
) -> list[bytes]:
    """Check a job's framing and return its dot lines (tests, tools).

    A skipped line comes back as zero bytes. In graphics mode every line is
    returned, so each label row appears twice.
    """
    header = job_header(media, quality=quality, density=density)
    if not job.startswith(header) or not job.endswith(FORM_FEED):
        raise ValueError("job does not have the expected header and trailer")
    width = bytes_per_row(media)
    body = job[len(header) : len(job) - len(FORM_FEED)]
    lines: list[bytes] = []
    pos = 0
    while pos < len(body):
        if body[pos] == SYN:
            line = body[pos + 1 : pos + 1 + width]
            if len(line) != width:
                raise ValueError(f"short raster line at byte {pos}")
            lines.append(line)
            pos += 1 + width
        elif body[pos : pos + 3] == bytes([ESC, ord("f"), 1]) and pos + 3 < len(body):
            lines.extend([bytes(width)] * body[pos + 3])
            pos += 4
        else:
            raise ValueError(f"unexpected byte 0x{body[pos]:02x} at {pos}")
    return lines


def prepare_job(
    img_or_rows: ImageOrRaster,
    media: Media,
    *,
    quality: Quality = DEFAULT_QUALITY,
    density: Density = DEFAULT_DENSITY,
) -> bytes:
    """Rasterize (if needed) and build the job.

    ``img_or_rows`` is an image, or the packed rows as one byte string
    (``bytes_per_row * printed_rows`` bytes, 1 bit = black dot).

    Raises:
        DirectPrintError: (``bad_image``) if the input cannot be printed.
    """
    started = time.monotonic()

    def build() -> bytes:
        if isinstance(img_or_rows, Image.Image):
            label_rows = rows(img_or_rows, media)
        else:
            raw = bytes(img_or_rows)
            width = bytes_per_row(media)
            expected = width * printed_rows(media)
            if len(raw) != expected:
                raise ValueError(f"raster must be {expected} bytes, got {len(raw)}")
            label_rows = [raw[i : i + width] for i in range(0, len(raw), width)]
        return build_job(label_rows, media, quality=quality, density=density)

    job: bytes = guarded_build(build)
    log_event(
        logger,
        logging.DEBUG,
        "LabelWriter job built",
        quality=quality.value,
        density=density.value,
        media=media.describe(),
        job_bytes=len(job),
        build_s=round(time.monotonic() - started, 3),
    )
    return job


# -- the session ----------------------------------------------------------------


class DymoSession(PrintSession):
    """Runs one print (or probe) over ``transport``; not reusable.

    Shares the job write (``send``) and the history bookkeeping with
    :class:`~ibp_printing.direct.session.PrintSession`; the pre-check, the
    watch and the probe are its own, from the ``ESC A`` status byte.

    Uses these :class:`SessionTimeouts` fields: ``drain_s``,
    ``query_write_s``, ``query_reply_s``, ``cover_query_attempts`` (status
    attempts), ``realign_wait_s`` (waiting for the printer to be ready),
    ``job_write_s``, ``doing_s`` (for any reply after the job),
    ``idle_settle_s``, ``done_s`` and ``poll_s``.
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
        # ESC V's reply (probe only).
        self.version: Optional[str] = None

    # ------------------------------------------------------------- helpers

    def _record(self, status: int) -> None:
        if status != self.status:
            self._note(f"status {describe_status(status)}")
        self.status = status
        self.result.paper = "NO" if status & PAPER_OUT else "YES"
        self._status_event(status, describe_status(status))

    def _statuses(self, timeout_s: float) -> list[int]:
        """Read for up to ``timeout_s``; every byte is a status reply."""
        data = self.transport.read_bytes(max(0.0, timeout_s))
        for status in data:
            self._record(status)
        return list(data)

    def _write_query(self, query: bytes) -> bool:
        """Send a query; False if the printer did not take all of it."""
        sent = 0
        for _ in range(2):
            try:
                sent += self.transport.write_all(
                    query[sent:], self.timeouts.query_write_s
                )
            except OSError as exc:
                log_event(
                    logger,
                    logging.DEBUG,
                    "query not taken",
                    query=query.hex(" "),
                    error=describe_exception(exc),
                )
            if sent >= len(query):
                return True
        if sent:
            self._note(
                f"query {query.hex(' ')} cut short ({sent}/{len(query)} bytes taken)",
                logging.WARNING,
            )
        return False

    def ask_status(self) -> list[int]:
        """Ask ``ESC A`` once; every status byte received (``[]``: no reply).

        Late replies to earlier queries count too: the last byte is the
        newest status.
        """
        stale = self._statuses(0)
        if not self._write_query(QUERY_STATUS):
            return stale
        return stale + self._statuses(self.timeouts.query_reply_s)

    def _ask_with_retries(self) -> Optional[int]:
        for _ in range(max(1, self.timeouts.cover_query_attempts)):
            replies = self.ask_status()
            if replies:
                return replies[-1]
        return None

    def _wait(self, seconds: float) -> list[int]:
        return self._statuses(seconds)

    # ------------------------------------------------------------- phases

    def _precheck_steps(self) -> None:
        """Pass only when the printer is ready (see the module docstring).

        Raises:
            DirectPrintError: if printing must not start (nothing was sent).
        """
        drained = self._wait(self.timeouts.drain_s)
        if drained:
            self._note(f"drained {len(drained)} stale status byte(s)")
        status = self._ask_with_retries()
        if status is None:
            status = self._resync()
        if status is None:
            raise self._fail("no_status_reply", "printer did not answer ESC A")
        status = self._wait_until(
            status, lambda s: bool(s & READY), "ready", self.timeouts.realign_wait_s
        )
        if not status & TOP_OF_FORM:
            self._note(
                "printer is part way through a label (a cut-off job?): "
                "feeding it out first",
                logging.WARNING,
            )
            if not self._write_query(FORM_FEED):
                raise self._fail("printer_busy", "the printer did not take ESC E")
            status = self._wait_until(
                status, is_idle, "at the top of a label", self.timeouts.realign_wait_s
            )
        self._note("printer ready")

    def _resync(self) -> Optional[int]:
        """Send the sync run and ask again; the status, or None.

        A job cut off mid raster line leaves the printer reading every byte
        as dots, ESC A included, so it never answers. The sync run fills that
        line up without printing anything (seen on the station, 2026-11-26).
        """
        self._note(
            "no reply: sending the sync run in case a cut-off job left a "
            "raster line half full",
            logging.WARNING,
        )
        if not self._write_query(SYNC):
            return None
        status = self._ask_with_retries()
        if status is not None:
            self._note("printer answers again after the sync run")
        return status

    def _wait_until(
        self, status: int, done: Callable[[int], bool], what: str, wait_s: float
    ) -> int:
        """Poll until ``done(status)``; raise on a fault or after ``wait_s``.

        Raises:
            DirectPrintError: ``paper_out`` / ``paper_jam`` /
                ``printer_error`` for a fault bit, ``printer_busy`` when not
                ``what`` in time.
        """
        deadline = self.clock() + wait_s
        while True:
            reason = blocking_reason(status)
            if reason is not None:
                raise self._fail(reason, f"printer status {describe_status(status)}")
            if done(status):
                return status
            if self.clock() >= deadline:
                raise self._fail(
                    "printer_busy",
                    f"not {what} after {wait_s:g} s ({describe_status(status)})",
                )
            self._wait(self.timeouts.poll_s)
            latest = self._ask_with_retries()
            if latest is not None:
                status = latest

    def watch(self) -> SessionOutcome:
        """Poll the status after a complete write until the label is done."""
        started = self.clock()
        busy_since: Optional[float] = None
        idle_since: Optional[float] = None
        answered = False
        while True:
            for status in self.ask_status():
                now = self.clock()
                answered = True
                if status & FAULT_BITS:
                    hint = (
                        "out of labels: load a new roll and print the label again"
                        if status & PAPER_OUT
                        else "a part label may have come out"
                    )
                    self._note(
                        f"printer reports {describe_status(status)} after the job "
                        f"({hint})",
                        logging.ERROR,
                    )
                    return SessionOutcome.ERROR
                if not is_idle(status):
                    idle_since = None
                    if busy_since is None:
                        busy_since = now
                        self.result.saw_doing = True
                        self.result.timings["doing_after_s"] = round(now - started, 3)
                elif busy_since is not None:
                    self.result.saw_done = True
                    self.result.timings["print_s"] = round(now - busy_since, 3)
                    self._note(f"label printed in {self.result.timings['print_s']} s")
                    return SessionOutcome.COMPLETED
                elif idle_since is None:
                    idle_since = now
            outcome = self._deadline_outcome(
                self.clock(),
                silent_since=None if answered else started,
                idle_since=idle_since,
                busy_since=busy_since,
            )
            if outcome is not None:
                return outcome
            self._wait(self.timeouts.poll_s)

    def _deadline_outcome(
        self,
        now: float,
        *,
        silent_since: Optional[float],
        idle_since: Optional[float],
        busy_since: Optional[float],
    ) -> Optional[SessionOutcome]:
        """The outcome once a wait in :meth:`watch` has run out, else None."""
        timeouts = self.timeouts
        if silent_since is not None and now - silent_since >= timeouts.doing_s:
            self._note(
                f"printer did not answer within {timeouts.doing_s:g} s of the "
                "job (the label may or may not have printed)",
                logging.ERROR,
            )
            return SessionOutcome.UNCERTAIN
        if idle_since is not None and now - idle_since >= timeouts.idle_settle_s:
            self.result.timings["settle_s"] = round(now - idle_since, 3)
            self._note(
                "printer took the label and reports no fault (it never "
                "reported printing, so this is the only confirmation)"
            )
            return SessionOutcome.COMPLETED
        if busy_since is not None and now - busy_since >= timeouts.done_s:
            self._note(
                f"label not out after {timeouts.done_s:g} s: the printer is "
                "still feeding (a jam, or labels ran out mid-label?)",
                logging.ERROR,
            )
            return SessionOutcome.TIMEOUT
        return None

    def _probe_queries(self, probe: StatusProbe) -> None:
        """Drain, then ask ``ESC A`` and ``ESC V`` only (neither feeds)."""
        self._wait(self.timeouts.drain_s)
        status = self._probe_status_byte(probe, self._ask_with_retries(), status_flags)
        if status is None:
            return
        probe.status_ok = is_idle(status)
        probe.printing = None if is_idle(status) else "DOING"
        self.version = self._ask_version()
        if self.version:
            self._note(f"model and firmware: {self.version}")

    def _ask_version(self) -> Optional[str]:
        """``ESC V``'s 10 ASCII bytes, or None."""
        if not self._write_query(QUERY_VERSION):
            return None
        deadline = self.clock() + self.timeouts.query_reply_s
        data = b""
        while len(data) < VERSION_REPLY_BYTES and self.clock() < deadline:
            data += self.transport.read_bytes(
                max(0.0, min(self.timeouts.poll_s, deadline - self.clock()))
            )
        if not data:
            return None
        return data.decode("ascii", errors="replace").strip("\x00 ")

    def probe(self) -> StatusProbe:
        """Drain, then ask ``ESC A`` and ``ESC V`` only; never raises."""
        probe = super().probe()
        if self.version:
            probe.lines.append(f"version {self.version}")
        return probe


def probe_status(
    transport: "Transport",
    *,
    timeouts: Optional[SessionTimeouts] = None,
    clock: Callable[[], float] = time.monotonic,
) -> StatusProbe:
    """Ask a LabelWriter its status (and firmware) without printing; never raises."""
    session = DymoSession(
        transport, job_name="status probe", timeouts=timeouts, clock=clock
    )
    return session.probe()


def print_label(  # pylint: disable=too-many-arguments
    transport: "Transport",
    img_or_rows: ImageOrRaster,
    *,
    media: Media,
    job_name: str,
    quality: Quality = DEFAULT_QUALITY,
    density: Density = DEFAULT_DENSITY,
    timeouts: Optional[SessionTimeouts] = None,
    clock: Callable[[], float] = time.monotonic,
) -> SessionResult:
    """Print one label on ``media`` over an open transport.

    Returns:
        The session result (COMPLETED, UNCERTAIN, TIMEOUT or ERROR).

    Raises:
        DirectPrintError: (a ``PrintError``) when nothing was sent.
    """
    job = prepare_job(img_or_rows, media, quality=quality, density=density)
    session = DymoSession(transport, job_name=job_name, timeouts=timeouts, clock=clock)
    return session.run(job)


__all__ = [
    "DEFAULT_DENSITY",
    "DEFAULT_QUALITY",
    "QUERY_STATUS",
    "QUERY_VERSION",
    "Density",
    "DymoSession",
    "Quality",
    "blocking_reason",
    "build_job",
    "describe_status",
    "is_idle",
    "job_header",
    "parse_job",
    "place_on_label",
    "prepare_job",
    "printed_rows",
    "print_label",
    "probe_status",
    "rows",
    "status_flags",
]
