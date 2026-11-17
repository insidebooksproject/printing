"""``ibp-print-diag --listen``: show every line the direct printer pushes."""

import contextlib
import json
import logging
import signal
import time
from collections import Counter
from typing import Any, Callable, Iterator, Optional

from ibp_printing.diagnostics.report import busy_text, describe_direct_devices
from ibp_printing.diagnostics.status import (
    candidates_by_path,
    direct_layer,
    direct_off,
)
from ibp_printing.direct import tspl
from ibp_printing.direct.backend import DirectFirstBackend
from ibp_printing.direct.transport import Transport, TransportError
from ibp_printing.log import get_logger, log_event
from ibp_printing.models import PrinterCandidate

logger = get_logger(__name__)

# Longest single read, so Ctrl+C and the deadline are prompt.
LISTEN_POLL_S = 0.5
# How long to wait for the reply to the closing SSSGETCAP.
LISTEN_FINAL_S = 2.0

# The clock --listen uses (tests replace it).
_now: Callable[[], float] = time.monotonic


@contextlib.contextmanager
def _ctrl_c_flag() -> Iterator[list[bool]]:
    """Turn Ctrl+C into a flag (``flag[0]``) while listening.

    A KeyboardInterrupt raised in the middle of a Windows overlapped read
    could leave a request pending; with the flag the current read finishes
    (at most ``LISTEN_POLL_S``) and the device is closed normally. Outside
    the main thread the handler cannot be installed and Ctrl+C raises as
    usual (still caught by :func:`run_listen`).
    """
    flag = [False]

    def handler(_signum: int, _frame: Any) -> None:
        flag[0] = True

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread
        yield flag
        return
    try:
        yield flag
    finally:
        signal.signal(signal.SIGINT, previous)


def describe_line(line: str) -> str:
    """What one line from the printer means, for ``--listen``."""
    event = tspl.parse_line(line)
    if event.kind is tspl.EventKind.COVER:
        return f"cover {event.value}"
    if event.kind is tspl.EventKind.PRINTING:
        meaning = {"DOING": "paper moving (realign or label)", "DONE": "finished"}
        return f"printing {event.value}: {meaning.get(event.value, '?')}"
    if event.kind is tspl.EventKind.PAPER:
        return f"paper sensor {event.value} (unreliable)"
    if event.kind is tspl.EventKind.COMMAND_ERROR:
        return "printer rejected a line"
    return "(not understood)"


class _Listener:
    """Reads (and logs) everything one open printer sends; see run_listen."""

    def __init__(self, transport: Transport, *, write_s: float, echo: bool) -> None:
        self.transport = transport
        self.write_s = write_s
        self.echo = echo
        self.started = _now()
        self.heard: list[dict[str, Any]] = []
        self.notes: list[str] = []
        self.failed = ""
        self._line_dirty = False

    def note(self, text: str, level: int = logging.INFO) -> None:
        """Record (and print) one event of the listen."""
        stamp = f"+{_now() - self.started:5.1f} s"
        self.notes.append(f"{stamp}  {text}")
        log_event(logger, level, "listen: " + text)
        if self.echo:
            print(f"  {stamp}  {text}", flush=True)

    def ask_cover(self) -> None:
        """Send SSSGETCAP once (the only thing --listen ever writes)."""
        query = tspl.QUERY_COVER
        if self._line_dirty:
            query = tspl.CRLF + query  # end a fragment left by a short write
        try:
            accepted = self.transport.write_all(query, self.write_s)
        except (TransportError, OSError) as exc:
            self._line_dirty = True
            self.note(f"SSSGETCAP not sent: {exc}", logging.WARNING)
            return
        self._line_dirty = 0 < accepted < len(query)
        if accepted < len(query):
            self.note(
                f"SSSGETCAP: printer took {accepted}/{len(query)} bytes (busy?)",
                logging.WARNING,
            )
        else:
            self.note("sent SSSGETCAP")

    def listen(self, seconds: float, stop: list[bool]) -> None:
        """Read for ``seconds``, or until ``stop[0]`` or a read error."""
        deadline = _now() + seconds
        while not stop[0] and not self.failed:
            remaining = deadline - _now()
            if remaining <= 0:
                return
            try:
                lines = self.transport.read_lines(min(LISTEN_POLL_S, remaining))
            except (TransportError, OSError) as exc:
                self.failed = f"reading from the printer failed: {exc}"
                self.note(self.failed, logging.ERROR)
                return
            for line in lines:
                at = round(_now() - self.started, 3)
                self.heard.append(
                    {"t_s": at, "line": line, "meaning": describe_line(line)}
                )
                self.note(f"{line!r:28} {describe_line(line)}")

    def summary(self) -> str:
        """Counts of the cover / printing lines heard."""
        counts = Counter(
            (event.kind.value, event.value)
            for event in (tspl.parse_line(item["line"]) for item in self.heard)
        )
        parts = [f"{kind} {value} x{n}" for (kind, value), n in sorted(counts.items())]
        return ", ".join(parts) or "nothing"


def _open_listen_printer(
    layer: DirectFirstBackend, as_json: bool
) -> Optional[tuple[PrinterCandidate, Transport]]:
    """Open the best usable direct printer for --listen; None (reported) if not."""
    devices, errors = layer.list_devices()
    candidates = candidates_by_path(devices)
    usable = sorted(
        (c for c in candidates.values() if c.usable), key=PrinterCandidate.rank_key
    )
    if not usable:
        lines = ["--listen: no usable direct USB printer to listen to"]
        lines += describe_direct_devices(devices, {}, candidates)
        lines += [f"  error: {error}" for error in errors]
        log_event(logger, logging.ERROR, "\n".join(lines))
        print(
            json.dumps({"error": lines[0], "devices": len(devices)})
            if as_json
            else "\n".join(lines)
        )
        return None
    candidate = usable[0]
    device = candidate.direct_device
    assert device is not None
    try:
        return candidate, layer.open_device(device)
    except (TransportError, OSError) as exc:
        reason = getattr(exc, "reason", "")
        problem = busy_text(device) if reason == "busy" else "CANNOT OPEN"
        message = f"--listen: cannot open {candidate.name}: {problem} ({exc})"
        log_event(logger, logging.ERROR, message)
        print(json.dumps({"error": message}) if as_json else message)
        return None


def run_listen(seconds: float, as_json: bool) -> int:
    """``--listen SECONDS``: show what the direct printer pushes; exit code.

    Opens the best usable direct printer, sends ``SSSGETCAP`` (so the first
    reply shows the link works), reads and prints every line for
    ``seconds`` (cover pushes ``SSSGETCAP:OPEN`` / ``CLOSE``, the realign's
    ``SSSGETPRINTING:DOING`` / ``DONE``), sends ``SSSGETCAP`` again and
    releases the device. Nothing else is written, so no label moves except
    the printer's own realign. Ctrl+C stops early and still releases the
    device. Exit status 0 when at least one line was heard, 1 otherwise,
    130 after Ctrl+C.
    """
    layer = direct_layer()
    off = direct_off(layer, as_json, "--listen")
    if off is not None or layer is None:
        return off or 1
    opened = _open_listen_printer(layer, as_json)
    if opened is None:
        return 1
    candidate, transport = opened
    if not as_json:
        print(
            f"-- Listening to {candidate.name} for {seconds:g} s --\n"
            "  sends only SSSGETCAP (now and at the end); open and close the "
            "cover now. Ctrl+C stops early.",
            flush=True,
        )
    listener = _Listener(
        transport, write_s=layer.timeouts.query_write_s, echo=not as_json
    )
    with transport, _ctrl_c_flag() as stop:
        try:
            listener.ask_cover()
            listener.listen(seconds, stop)
            if not stop[0] and not listener.failed:
                listener.ask_cover()
                listener.listen(max(LISTEN_FINAL_S, layer.timeouts.query_reply_s), stop)
        except KeyboardInterrupt:
            stop[0] = True
        if stop[0]:
            listener.note("stopped early (Ctrl+C)")
    interrupted = stop[0]
    log_event(
        logger,
        logging.INFO,
        "listen finished",
        printer=candidate.name,
        seconds=seconds,
        heard=listener.heard,
        interrupted=interrupted,
        failed=listener.failed,
        device_released=transport.closed,
    )
    if as_json:
        data = {
            "printer": candidate.name,
            "seconds": seconds,
            "heard": listener.heard,
            "notes": listener.notes,
            "interrupted": interrupted,
            "error": listener.failed or None,
            "device_released": transport.closed,
        }
        print(json.dumps(data, indent=2))
    else:
        print(
            f"-- Heard {len(listener.heard)} line(s): {listener.summary()}; "
            "printer released --"
        )
    if interrupted:
        return 130
    return 0 if listener.heard and not listener.failed else 1
