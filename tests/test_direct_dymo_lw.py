"""Tests for DYMO LabelWriter 450 Turbo direct printing (direct.dymo_lw)."""

import unittest
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, ImageDraw
from printing_helpers import quiet_logging

from ibp_printing.direct import dymo_lw
from ibp_printing.direct.dymo_lw import (
    ERROR,
    IDLE,
    PAPER_JAM,
    PAPER_OUT,
    QUERY_STATUS,
    QUERY_VERSION,
    READY,
    Density,
    Quality,
    blocking_reason,
    describe_status,
)
from ibp_printing.direct.session import (
    NO_SAME_PRINTER_FALLBACK,
    PAPER_OUT_HINT,
    VOLUNTEER_MESSAGES,
    DirectPrintError,
    SessionOutcome,
    SessionTimeouts,
)
from ibp_printing.printers import LABELWRITER_450_TURBO, Media

MEDIA = LABELWRITER_450_TURBO.media
assert MEDIA is not None
WIDTH = dymo_lw.bytes_per_row(MEDIA)
ROWS = dymo_lw.printed_rows(MEDIA)
WHITE = bytes(WIDTH * ROWS)
GOLDEN = Path(__file__).parent / "data" / "dymo-lw"
TOP = dymo_lw.TOP_OF_FORM


def golden_label() -> Image.Image:
    """A fixed landscape package label: a frame, bars and a corner mark."""
    img = Image.new("L", (1004, 378), 255)
    draw = ImageDraw.Draw(img)
    draw.rectangle((10, 10, 993, 367), outline=0, width=6)
    for x in range(60, 940, 40):
        draw.rectangle((x, 120, x + 14, 300), fill=0)
    draw.rectangle((20, 20, 80, 80), fill=0)
    return img


class VirtualClock:
    """A monotonic clock the scripted printer advances."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedDymo:  # pylint: disable=too-many-instance-attributes
    """A LabelWriter on a virtual clock, honouring the Transport contract.

    ``status_at(t)`` gives the ESC A reply ``t`` seconds after the job was
    fully written (``idle`` before that). A query is answered after
    ``reply_s`` unless ``silent``. ``accept(data)`` decides how much of a job
    write is taken.
    """

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.idle = IDLE
        self.status_at: Callable[[float], int] = self.printed_in(2.0)
        self.reply_s = 0.01
        self.silent = False
        self.version = b"93089v0K\x00\x00"
        # A job cut off mid raster line: ESC A is read as dots until the
        # sync run.
        self.cut_off = False
        self.form_feeds = 0
        self.accept: Optional[Callable[[bytes], int]] = None
        self.job_written_at: Optional[float] = None
        self.jobs: list[bytes] = []
        self.queries: list[bytes] = []
        self.pending: list[tuple[float, bytes]] = []

    @staticmethod
    def printed_in(seconds: float) -> Callable[[float], int]:
        """Busy (off the top of form) for ``seconds``, then idle."""
        return lambda t: READY if t < seconds else IDLE

    def status(self) -> int:
        if self.job_written_at is None:
            return self.idle
        return self.status_at(self.clock.now - self.job_written_at)

    def write_all(self, data: bytes, timeout_s: float) -> int:
        del timeout_s
        if data == dymo_lw.SYNC:
            self.cut_off = False
            self.queries.append(bytes(data))
            return len(data)
        if data == dymo_lw.FORM_FEED:
            self.form_feeds += 1
            self.queries.append(bytes(data))
            self.idle = IDLE
            return len(data)
        if self.cut_off and data in (QUERY_STATUS, QUERY_VERSION):
            self.queries.append(bytes(data))
            return len(data)
        if data in (QUERY_STATUS, QUERY_VERSION):
            self.queries.append(bytes(data))
            if not self.silent:
                reply = bytes([self.status()]) if data == QUERY_STATUS else self.version
                self.pending.append((self.clock.now + self.reply_s, reply))
            return len(data)
        accepted = self.accept(data) if self.accept else len(data)
        self.jobs.append(bytes(data[:accepted]))
        if accepted == len(data):
            self.job_written_at = self.clock.now
        return accepted

    def read_bytes(self, timeout_s: float) -> bytes:
        due = [r for r in self.pending if r[0] <= self.clock.now + timeout_s]
        if not due:
            self.clock.now += timeout_s
            return b""
        self.clock.now = max(self.clock.now, max(at for at, _ in due))
        self.pending = [r for r in self.pending if r not in due]
        return b"".join(data for _, data in due)


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)
        self.clock = VirtualClock()
        self.printer = ScriptedDymo(self.clock)

    def run_print(self, payload=WHITE, **overrides):
        return dymo_lw.print_label(
            self.printer,
            payload,
            media=MEDIA,
            job_name="test",
            timeouts=SessionTimeouts(**overrides),
            clock=self.clock,
        )

    def assert_not_sent(self, reason: str, **overrides) -> DirectPrintError:
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print(**overrides)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.printer.jobs, [])
        self.assertLessEqual(set(self.printer.queries), {QUERY_STATUS, dymo_lw.SYNC})
        return caught.exception


class SuccessTests(SessionTestCase):
    def test_busy_then_idle_is_completed(self):
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertTrue(result.saw_doing and result.saw_done)
        self.assertEqual(self.printer.jobs, [dymo_lw.prepare_job(WHITE, MEDIA)])
        self.assertAlmostEqual(result.timings["print_s"], 2.0, delta=0.5)

    def test_idle_throughout_is_a_weak_completion(self):
        self.printer.status_at = lambda t: IDLE
        result = self.run_print(idle_settle_s=3.0)
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertFalse(result.saw_doing)
        self.assertGreaterEqual(result.timings["settle_s"], 3.0)
        self.assertIn("only confirmation", " ".join(result.history))

    def test_recovers_from_a_cut_off_job(self):
        self.printer.cut_off = True
        self.printer.idle = READY  # part way through a label
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertEqual(self.printer.form_feeds, 1)
        self.assertIn(dymo_lw.SYNC, self.printer.queries)
        self.assertIn("feeding it out", " ".join(result.history))

    def test_feeds_out_a_part_label(self):
        self.printer.idle = READY
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertEqual(self.printer.form_feeds, 1)

    def test_waits_for_ready(self):
        original = self.printer.status

        def status() -> int:
            if self.printer.job_written_at is None and self.clock.now < 1001.0:
                return 0x00
            return original()

        self.printer.status = status
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)

    def test_image_input(self):
        result = self.run_print(golden_label())
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)


class PrecheckTests(SessionTestCase):
    def test_reasons(self):
        cases = {
            PAPER_OUT | ERROR | READY: "paper_out",
            PAPER_OUT | TOP: "paper_out",
            PAPER_JAM | READY: "paper_jam",
            ERROR: "printer_error",
        }
        for status, reason in cases.items():
            with self.subTest(status=hex(status)):
                self.printer = ScriptedDymo(self.clock)
                self.printer.idle = status
                error = self.assert_not_sent(reason)
                self.assertEqual(str(error), VOLUNTEER_MESSAGES[reason])
                self.assertFalse(error.same_printer_fallback)

    def test_no_reply(self):
        self.printer.silent = True
        error = self.assert_not_sent("no_status_reply")
        # Twice, the sync run, twice again.
        self.assertEqual(
            self.printer.queries,
            [QUERY_STATUS] * 2 + [dymo_lw.SYNC] + [QUERY_STATUS] * 2,
        )
        self.assertFalse(error.same_printer_fallback)

    def test_never_ready(self):
        self.printer.idle = 0x00
        self.assert_not_sent("printer_busy", realign_wait_s=2.0)

    def test_stale_bytes_are_drained(self):
        self.printer.pending.append((self.clock.now, b"\x21\x21"))
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)

    def test_bad_input(self):
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print(payload=b"\x00" * 10)
        self.assertEqual(caught.exception.reason, "bad_image")
        self.assertEqual(self.printer.queries, [])

    def test_reasons_need_the_volunteer(self):
        for reason in ("paper_out", "paper_jam", "printer_error", "no_status_reply"):
            self.assertIn(reason, VOLUNTEER_MESSAGES)
            self.assertIn(reason, NO_SAME_PRINTER_FALLBACK)


class OutcomeTests(SessionTestCase):
    def test_paper_runs_out_mid_label(self):
        self.printer.status_at = lambda t: READY if t < 0.5 else PAPER_OUT | ERROR
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.ERROR)

    def test_jam_after_the_job(self):
        self.printer.status_at = lambda t: PAPER_JAM
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.ERROR)

    def test_still_busy(self):
        self.printer.status_at = lambda t: READY
        result = self.run_print(done_s=5.0)
        self.assertIs(result.outcome, SessionOutcome.TIMEOUT)
        self.assertTrue(result.saw_doing)

    def test_no_replies_after_the_job(self):
        original = self.printer.write_all

        def write_all(data: bytes, timeout_s: float) -> int:
            if self.printer.job_written_at is not None:
                self.printer.silent = True
            return original(data, timeout_s)

        self.printer.write_all = write_all
        result = self.run_print(doing_s=2.0)
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_stalled_write(self):
        self.printer.accept = lambda data: len(data) // 2
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)
        self.assertIn(PAPER_OUT_HINT, " ".join(result.history))

    def test_nothing_accepted(self):
        self.printer.accept = lambda data: 0
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print()
        self.assertEqual(caught.exception.reason, "job_not_accepted")


class ProbeTests(SessionTestCase):
    def probe(self):
        return dymo_lw.probe_status(
            self.printer, timeouts=SessionTimeouts(), clock=self.clock
        )

    def test_ready(self):
        probe = self.probe()
        self.assertTrue(probe.ready)
        self.assertEqual(probe.status_byte, IDLE)
        self.assertEqual(probe.summary(), "status 0x03 (ready, top of form)")
        self.assertIn("version 93089v0K", probe.lines)
        self.assertEqual(self.printer.queries, [QUERY_STATUS, QUERY_VERSION])
        self.assertEqual(self.printer.jobs, [])

    def test_out_of_labels(self):
        self.printer.idle = PAPER_OUT | ERROR
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertEqual(probe.paper, "NO")
        self.assertEqual(probe.flags, ["out of labels", "error"])

    def test_busy(self):
        self.printer.idle = 0x00
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertEqual(probe.summary(), "status 0x00 (busy)")

    def test_no_reply(self):
        self.printer.silent = True
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertIn("no reply", probe.summary())
        self.assertNotIn(QUERY_VERSION, self.printer.queries)


def blank_rows(count: int = ROWS) -> list[bytes]:
    return [bytes(WIDTH)] * count


class JobTests(unittest.TestCase):
    def test_header(self):
        header = dymo_lw.job_header(MEDIA, quality=Quality.TEXT)
        self.assertEqual(
            header,
            b"\x1b" * 156 + b"\x1bL\x04\x1a" + b"\x1bh\x1be\x1bB\x00\x1bD\x31",
        )
        graphics = dymo_lw.job_header(MEDIA, density=Density.DARK)
        self.assertIn(b"\x1bL\x08\x34\x1bi\x1bg", graphics)  # 2100 lines

    def test_rows_and_skips(self):
        label = blank_rows()
        label[0] = b"\x80" + bytes(WIDTH - 1)
        job = dymo_lw.build_job(label, MEDIA, quality=Quality.TEXT)
        body = job[len(dymo_lw.job_header(MEDIA, quality=Quality.TEXT)) :]
        self.assertTrue(body.startswith(b"\x16\x80" + bytes(WIDTH - 1)))
        # 960 blank rows: 255 + 255 + 255 + 195.
        skips = body[1 + WIDTH : -2]
        self.assertEqual(skips, b"\x1bf\x01\xff" * 3 + b"\x1bf\x01\xc3")
        self.assertTrue(job.endswith(b"\x1bE"))
        self.assertNotIn(b"\x1b@", job[len(dymo_lw.SYNC) :])
        self.assertEqual(dymo_lw.parse_job(job, MEDIA, quality=Quality.TEXT), label)

    def test_graphics_sends_each_row_twice(self):
        label = blank_rows()
        label[5] = b"\xff" * WIDTH
        job = dymo_lw.build_job(label, MEDIA)
        lines = dymo_lw.parse_job(job, MEDIA)
        self.assertEqual(len(lines), 2 * ROWS)
        self.assertEqual(lines[10], lines[11])
        self.assertEqual(lines[10], b"\xff" * WIDTH)
        self.assertEqual(job.count(b"\x16" + b"\xff" * WIDTH), 2)

    def test_wrong_sizes(self):
        with self.assertRaises(ValueError):
            dymo_lw.build_job(blank_rows(10), MEDIA)
        with self.assertRaises(ValueError):
            dymo_lw.build_job([b"\x00"] * ROWS, MEDIA)

    def test_too_wide_for_the_head(self):
        wide = Media(width_mm=60, height_mm=20, gap_mm=0, dots_per_mm=300 / 25.4)
        with self.assertRaises(ValueError):
            dymo_lw.rows(Image.new("L", (10, 10), 255), wide)
        with self.assertRaises(ValueError):
            dymo_lw.job_header(wide)

    def test_package_label_is_rotated_into_the_printable_area(self):
        # The PPD's ImageableArea, as the CUPS raster had it.
        self.assertEqual(MEDIA.printable_dots, (389, 961))
        label = dymo_lw.place_on_label(golden_label(), MEDIA)
        self.assertEqual(label.size, (389, 961))
        rows = dymo_lw.rows(golden_label(), MEDIA)
        self.assertEqual(len(rows), ROWS)
        self.assertTrue(all(len(row) == WIDTH for row in rows))
        # Turned counter-clockwise, like the CUPS path: the corner mark at
        # the image's top left ends up at the label's bottom left.
        bottom_left = label.getpixel((60, 915))
        top_left = label.getpixel((60, 46))
        self.assertEqual((bottom_left, top_left), (0, 255))

    def test_cups_placement_of_a_request_label(self):
        # A request label from the backend's renderer (made-up inmate): its
        # ink lands on the box the CUPS raster had on the station (2026-11-26).
        img = Image.open(GOLDEN / "request_label.png")
        self.assertEqual(img.size, (1004, 378))
        ink = Image.eval(dymo_lw.place_on_label(img, MEDIA), lambda v: 255 - v)
        left, top, right, bottom = ink.point(lambda v: 255 if v > 0x50 else 0).getbbox()
        self.assertEqual((left, top, right - 1, bottom - 1), (42, 49, 348, 912))

    def test_page_sized_image_is_scaled_into_the_printable_area(self):
        img = Image.new("L", (425, 1050), 255)
        img.putpixel((0, 0), 0)
        self.assertEqual(dymo_lw.place_on_label(img, MEDIA).size, (389, 961))
        rows = dymo_lw.rows(img, MEDIA)
        self.assertEqual(rows[-1][-1] & 0x07, 0)  # padding past dot 388 is white

    def test_golden_job(self):
        job = dymo_lw.prepare_job(golden_label(), MEDIA)
        self.assertEqual(job, (GOLDEN / "golden_job.bin").read_bytes())


class StatusTests(unittest.TestCase):
    def test_blocking_reasons(self):
        self.assertIsNone(blocking_reason(IDLE))
        self.assertIsNone(blocking_reason(READY))
        self.assertEqual(blocking_reason(0x21), "paper_out")
        self.assertEqual(blocking_reason(0xA1), "paper_out")
        self.assertEqual(blocking_reason(0x41), "paper_jam")
        self.assertEqual(blocking_reason(0x80), "printer_error")

    def test_describe(self):
        self.assertEqual(describe_status(0x03), "0x03 (ready, top of form)")
        self.assertEqual(describe_status(0xA0), "0xa0 (out of labels, error)")
        self.assertEqual(describe_status(0x00), "0x00 (busy)")


if __name__ == "__main__":
    unittest.main()
