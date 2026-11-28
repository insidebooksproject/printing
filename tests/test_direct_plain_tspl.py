"""Tests for plain-TSPL direct printing (the iDPRT SP310)."""

import unittest
from typing import Callable, Optional

from PIL import Image, ImageDraw
from printing_helpers import quiet_logging

from ibp_printing.direct import plain_tspl
from ibp_printing.direct.plain_tspl import (
    COVER_OPEN,
    PAPER_OUT,
    PRINTING,
    QUERY_STATUS,
    blocking_reason,
    describe_status,
)
from ibp_printing.direct.session import (
    PAPER_OUT_HINT,
    VOLUNTEER_MESSAGES,
    DirectPrintError,
    SessionOutcome,
    SessionTimeouts,
)
from ibp_printing.printers import SP310, Media

MEDIA = SP310.media
assert MEDIA is not None
WHITE = b"\xff" * plain_tspl.raster_size(MEDIA)


class VirtualClock:
    """A monotonic clock the scripted printer advances."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ScriptedTspl:  # pylint: disable=too-many-instance-attributes
    """An SP310 on a virtual clock, honouring the Transport contract.

    ``status_at(t)`` gives the status byte ``t`` seconds after the job was
    fully written (``idle`` before that). A status query is answered after
    ``reply_s`` unless ``silent``; ``refuse_queries`` makes that many query
    writes accept nothing (busy). ``accept(data)`` decides how much of a job
    write is taken (or raises).
    """

    def __init__(self, clock: VirtualClock) -> None:
        self.clock = clock
        self.idle = 0x00
        self.status_at: Callable[[float], int] = self.printed_in(2.0)
        self.reply_s = 0.01
        self.silent = False
        self.refuse_queries = 0
        self.accept: Optional[Callable[[bytes], int]] = None
        self.job_written_at: Optional[float] = None
        self.jobs: list[bytes] = []
        self.queries = 0
        self._replies: list[tuple[float, bytes]] = []
        self.closed = False

    @staticmethod
    def printed_in(seconds: float) -> Callable[[float], int]:
        """Printing for ``seconds`` after the job, then ready."""
        return lambda t: PRINTING if t < seconds else 0x00

    def status(self) -> int:
        if self.job_written_at is None:
            return self.idle
        return self.status_at(self.clock.now - self.job_written_at)

    def write_all(self, data: bytes, timeout_s: float) -> int:
        if data and QUERY_STATUS.startswith(data):
            if self.refuse_queries:
                self.refuse_queries -= 1
                self.clock.now += timeout_s
                return 0
            if data == QUERY_STATUS:
                self.queries += 1
                if not self.silent:
                    self._replies.append(
                        (self.clock.now + self.reply_s, bytes([self.status()]))
                    )
            return len(data)
        accepted = self.accept(data) if self.accept else len(data)
        self.jobs.append(bytes(data[:accepted]))
        if accepted == len(data):
            self.job_written_at = self.clock.now
        return accepted

    def read_bytes(self, timeout_s: float) -> bytes:
        due = [r for r in self._replies if r[0] <= self.clock.now + timeout_s]
        if not due:
            self.clock.now += timeout_s
            return b""
        self.clock.now = max(self.clock.now, max(at for at, _ in due))
        self._replies = [r for r in self._replies if r not in due]
        return b"".join(data for _, data in due)

    def close(self) -> None:
        self.closed = True


class SessionTestCase(unittest.TestCase):
    def setUp(self):
        quiet_logging(self)
        self.clock = VirtualClock()
        self.printer = ScriptedTspl(self.clock)

    def run_print(self, payload=WHITE, **overrides):
        timeouts = SessionTimeouts(**overrides)
        return plain_tspl.print_label(
            self.printer,
            payload,
            media=MEDIA,
            job_name="test",
            timeouts=timeouts,
            clock=self.clock,
        )

    def assert_not_sent(self, reason: str, **overrides) -> DirectPrintError:
        with self.assertRaises(DirectPrintError) as caught:
            self.run_print(**overrides)
        self.assertEqual(caught.exception.reason, reason)
        self.assertEqual(self.printer.jobs, [])
        return caught.exception


class SuccessTests(SessionTestCase):
    def test_full_success(self):
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        self.assertTrue(result.ok)
        self.assertTrue(result.saw_doing and result.saw_done)
        self.assertEqual(self.printer.jobs, [plain_tspl.build_job(WHITE, MEDIA)])
        self.assertAlmostEqual(result.timings["print_s"], 2.0, delta=0.5)
        self.assertTrue(any("0x20 (printing)" in line for line in result.history))

    def test_image_input(self):
        img = Image.new("RGB", (1004, 378), "white")
        ImageDraw.Draw(img).rectangle((100, 100, 900, 200), fill="black")
        result = self.run_print(img)
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        raster = plain_tspl.parse_job(self.printer.jobs[0], MEDIA)
        self.assertLess(int.from_bytes(raster).bit_count(), len(raster) * 8)

    def test_waits_out_a_previous_label(self):
        self.printer.idle = PRINTING
        self.clock_at_start = self.clock.now

        def later_idle(_t: float) -> int:
            return 0x00

        # Becomes idle 1 s into the session, before the job is written.
        original = self.printer.status

        def status() -> int:
            if self.printer.job_written_at is None:
                return PRINTING if self.clock.now < 1001.0 else 0x00
            return original()

        self.printer.status = status
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)
        del later_idle

    def test_busy_query_writes_are_retried(self):
        self.printer.refuse_queries = 1
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.COMPLETED)


class PrecheckTests(SessionTestCase):
    def test_cover_open(self):
        self.printer.idle = COVER_OPEN
        error = self.assert_not_sent("cover_open")
        self.assertEqual(str(error), VOLUNTEER_MESSAGES["cover_open"])
        self.assertFalse(error.same_printer_fallback)

    def test_paper_out(self):
        self.printer.idle = PAPER_OUT
        self.assert_not_sent("paper_out")

    def test_no_reply(self):
        self.printer.silent = True
        error = self.assert_not_sent("no_status_reply")
        self.assertEqual(self.printer.queries, 2)
        self.assertFalse(error.same_printer_fallback)

    def test_still_printing_a_previous_label(self):
        self.printer.idle = PRINTING
        self.assert_not_sent("printer_busy", realign_wait_s=2.0)

    def test_bad_input(self):
        self.assert_not_sent("bad_image", payload=b"\x00" * 10)


class OutcomeTests(SessionTestCase):
    def test_never_prints(self):
        self.printer.status_at = lambda t: 0x00
        result = self.run_print(doing_s=3.0)
        self.assertIs(result.outcome, SessionOutcome.UNCERTAIN)

    def test_still_printing(self):
        self.printer.status_at = lambda t: PRINTING
        result = self.run_print(done_s=5.0)
        self.assertIs(result.outcome, SessionOutcome.TIMEOUT)
        self.assertTrue(result.saw_doing)

    def test_paper_runs_out_mid_label(self):
        self.printer.status_at = lambda t: PRINTING if t < 0.5 else PAPER_OUT
        result = self.run_print()
        self.assertIs(result.outcome, SessionOutcome.ERROR)

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
        return plain_tspl.probe_status(
            self.printer, timeouts=SessionTimeouts(), clock=self.clock
        )

    def test_ready(self):
        probe = self.probe()
        self.assertTrue(probe.ready)
        self.assertEqual(probe.status_byte, 0)
        self.assertEqual(probe.summary(), "status 0x00 (ready)")
        self.assertEqual(probe.cover, "CLOSE")
        self.assertEqual(self.printer.jobs, [])

    def test_cover_open(self):
        self.printer.idle = COVER_OPEN
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertEqual(probe.cover, "OPEN")
        self.assertEqual(probe.flags, ["cover open"])

    def test_no_reply(self):
        self.printer.silent = True
        probe = self.probe()
        self.assertFalse(probe.ready)
        self.assertIn("no reply", probe.summary())


class JobTests(unittest.TestCase):
    def test_header_matches_the_hardware_test(self):
        self.assertEqual(
            plain_tspl.job_header(MEDIA),
            b"\r\nSIZE 36 mm,89 mm\r\nGAP 3 mm,0 mm\r\nDIRECTION 0,0\r\n"
            b"REFERENCE 0,0\r\nCLS\r\nBITMAP 0,0,36,712,0,",
        )
        job = plain_tspl.build_job(WHITE, MEDIA)
        self.assertTrue(job.endswith(b"\r\nPRINT 1,1\r\n"))
        self.assertEqual(plain_tspl.parse_job(job, MEDIA), WHITE)

    def test_wrong_raster_size(self):
        with self.assertRaises(ValueError):
            plain_tspl.build_job(b"\xff", MEDIA)

    def test_landscape_label_is_rotated_and_kept_inside_the_margin(self):
        img = Image.new("L", (1004, 378), 0)  # all black
        label = plain_tspl.place_on_label(img, MEDIA)
        self.assertEqual(label.size, (288, 712))
        left, top, right, bottom = Image.eval(label, lambda v: 255 - v).getbbox()
        margin = MEDIA.margin_dots
        self.assertGreaterEqual(min(left, top), margin)
        self.assertLessEqual(right, 288 - margin)
        self.assertLessEqual(bottom, 712 - margin)
        # Fits the height (the long side), centred across.
        self.assertEqual((top, bottom), (margin, 712 - margin))

    def test_exact_size_is_used_dot_for_dot(self):
        img = Image.new("L", (288, 712), 255)
        img.putpixel((0, 0), 0)
        raster = plain_tspl.rasterize(img, MEDIA)
        self.assertEqual(raster[0], 0x7F)  # first dot black (0 bit)
        self.assertEqual(len(raster), plain_tspl.raster_size(MEDIA))

    def test_other_media(self):
        media = Media(width_mm=50, height_mm=25, gap_mm=2)
        self.assertIn(b"SIZE 50 mm,25 mm", plain_tspl.job_header(media))
        self.assertEqual(plain_tspl.raster_size(media), 50 * 200)


class StatusTests(unittest.TestCase):
    def test_blocking_reasons(self):
        self.assertIsNone(blocking_reason(0x00))
        self.assertIsNone(blocking_reason(PRINTING))
        self.assertEqual(blocking_reason(0x01), "cover_open")
        self.assertEqual(blocking_reason(COVER_OPEN | PAPER_OUT), "cover_open")
        self.assertEqual(blocking_reason(PAPER_OUT | PRINTING), "paper_out")
        self.assertEqual(blocking_reason(0x02), "paper_jam")
        self.assertEqual(blocking_reason(0x10), "paused")
        self.assertEqual(blocking_reason(0x80), "printer_error")

    def test_describe(self):
        self.assertEqual(describe_status(0x00), "0x00 (ready)")
        self.assertEqual(describe_status(0x44), "0x44 (out of labels, cover open)")


if __name__ == "__main__":
    unittest.main()
