"""The ``ibp-print-diag`` report: which printers are usable, and why.

:func:`build_report` (text, also used by shippy and shippy-gui) and
:func:`report_data` (JSON) describe one discovery pass: the direct USB
printers with their status probes, every print queue with each detection
gate, the USB devices, the verdict, recent PrintService events and the logs.
"""

import socket
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from ibp_printing.direct.session import StatusProbe
from ibp_printing.direct.transport import DirectDevice
from ibp_printing.log import default_log_dir, log_file_names
from ibp_printing.models import (
    Discovery,
    PrinterCandidate,
    UsbDevice,
    is_supported_direct_model,
)
from ibp_printing.printers import PM2411BT, PrinterModel, model_for_vid_pid
from ibp_printing.winconst import (
    PRINTER_ATTRIBUTE_BITS,
    PRINTER_STATUS_BITS,
    describe_bits,
)

RULE = "=" * 70
NO_PRINTER_MESSAGE = "No label printer found plugged in."
# The app name in ibp-print-diag's own log file names.
LOG_APP = "diag"
PROBABLY_PM2411BT = f"{PM2411BT.name} (probably)"


def build_report(
    discovery: Discovery,
    events: Sequence[Mapping[str, Any]] = (),
    *,
    log_dir: Optional[Path] = None,
    probes: Optional[Mapping[str, StatusProbe]] = None,
) -> str:
    """Render a human-readable diagnostics report for one discovery pass.

    Args:
        discovery: The result of :func:`ibp_printing.discover`.
        events: Recent OS print-subsystem events (newest first).
        log_dir: Log directory to point the reader at; defaults to
            :func:`ibp_printing.default_log_dir`.
        probes: Status probe results by direct device path.
    """
    lines = [
        RULE,
        "ibp-printing printer diagnostics",
        f"host={socket.gethostname()} platform={sys.platform} "
        f"time={datetime.now().isoformat(timespec='seconds')}",
        RULE,
        "",
    ]
    if discovery.direct_enabled is not None:
        lines += direct_section(discovery, probes or {})
        lines.append("")
    lines += _queue_section(discovery)
    lines.append("")
    lines += _usb_section(discovery.usb_devices)
    lines.append("")
    if discovery.errors:
        lines.append("-- Discovery errors --")
        lines += [f"  {error}" for error in discovery.errors]
        lines.append("")
    lines += _verdict_section(discovery)
    lines.append("")
    lines += _events_section(events)
    lines.append("")
    lines.append("-- Logs --")
    lines.append(f"  {log_dir or default_log_dir()}")
    lines.append(
        "  (one pair per app: printer-<app>.log is human-readable, "
        "printer-<app>.jsonl has one JSON per line;"
    )
    lines.append(
        f"   this tool writes {' / '.join(log_file_names(LOG_APP))}, the label "
        "watcher printer-watcher.*, shippy printer-shippy.*, shippy-gui "
        "printer-shippy-gui.*)"
    )
    return "\n".join(lines)


def report_data(
    discovery: Discovery,
    events: Sequence[Mapping[str, Any]] = (),
    *,
    log_dir: Optional[Path] = None,
    probes: Optional[Mapping[str, StatusProbe]] = None,
) -> dict[str, Any]:
    """The same information as :func:`build_report`, as JSON-friendly data."""
    return {
        "host": socket.gethostname(),
        "platform": sys.platform,
        "time": datetime.now().isoformat(timespec="seconds"),
        "direct_enabled": discovery.direct_enabled,
        "direct_devices": [
            direct_device_data(device, (probes or {}).get(device.path))
            for device in discovery.direct_devices
        ],
        "candidates": [candidate.to_log() for candidate in discovery.candidates],
        "usb_devices": [asdict(device) for device in discovery.usb_devices],
        "errors": list(discovery.errors),
        "usable": [candidate.name for candidate in discovery.usable],
        "events": [dict(event) for event in events],
        "log_dir": str(log_dir or default_log_dir()),
    }


def direct_device_data(
    device: DirectDevice, probe: Optional[StatusProbe]
) -> dict[str, Any]:
    """One direct device, its verdict and its probe, as JSON-friendly data."""
    data = device.to_log()
    data["supported"] = is_supported_direct_model(device.model)
    data["identified_as"] = identify_device(device)
    data["open_problem"] = open_problem(device)
    data["probe"] = probe.to_log() if probe is not None else None
    return data


def direct_section(
    discovery: Discovery, probes: Mapping[str, StatusProbe]
) -> list[str]:
    """The "Direct USB printers" part of the report."""
    state = "ON" if discovery.direct_enabled else "OFF (queue-only)"
    lines = [
        "-- Direct USB printers (no driver, no print queue) --",
        f"  direct USB printing: {state}  (IBP_PRINTING_DIRECT=0 turns it off)",
    ]
    if not discovery.direct_enabled:
        return lines
    by_path = {
        candidate.direct_device.path: candidate
        for candidate in discovery.candidates
        if candidate.direct_device is not None
    }
    lines += describe_direct_devices(discovery.direct_devices, probes, by_path)
    return lines


def identify_device(device: DirectDevice) -> str:
    """The device's model, or e.g. ``"PM2411BT (probably)"`` by VID:PID.

    A device that could not be opened (busy in the Windows spooler, no
    permission) has no 1284 ID, so no model; a supported model's VID:PID
    still identifies it.
    """
    if device.model:
        return device.model
    model = _direct_model_by_vid_pid(device)
    if model is not None:
        return f"{model.name} (probably)"
    return "(unknown model)"


def _direct_model_by_vid_pid(device: DirectDevice) -> Optional[PrinterModel]:
    model = model_for_vid_pid(device.vid_pid)
    return model if model is not None and model.direct else None


def busy_text(device: DirectDevice) -> str:
    """Why a busy device could not be opened, for the report."""
    if device.kind == "usbprint":
        return "BUSY: another program or the Windows print queue has it open"
    return "BUSY: another program (e.g. CUPS) has it open"


def open_problem(device: DirectDevice) -> str:
    """Why discovery could not open the device ("" when it could, or unknown)."""
    if device.busy:
        return busy_text(device)
    if device.accessible is False:
        if device.kind == "usblp":
            return "NO PERMISSION: this user cannot open it"
        return "CANNOT OPEN: see the error below"
    if not device.present:
        return "MISSING: the device node does not exist"
    return ""


def _supported_text(device: DirectDevice) -> str:
    if is_supported_direct_model(device.model):
        return "YES"
    model = None if device.model else _direct_model_by_vid_pid(device)
    if model is not None:
        return (
            f"probably (VID:PID {model.vid_pid} is the {model.name}'s; its "
            "1284 ID could not be read)"
        )
    return "NO"


def describe_direct_devices(
    devices: Sequence[DirectDevice],
    probes: Mapping[str, StatusProbe],
    candidates: Optional[Mapping[str, PrinterCandidate]] = None,
) -> list[str]:
    """One block per USB printer-class device, with its probe and verdict.

    ``candidates`` (by device path) adds each direct candidate's name, exactly
    as ``--test-print`` takes it, and its verdict.
    """
    if not devices:
        return ["  (no USB printer-class devices found)"]
    lines: list[str] = []
    for index, device in enumerate(devices, start=1):
        title = identify_device(device)
        problem = open_problem(device)
        if problem:
            lines += [f"  [{index}] {title} - {problem}", f"        at {device.path}"]
        else:
            lines.append(f"  [{index}] {title} at {device.path}")
        lines += [
            f"        kind={device.kind or '-'} vid_pid={device.vid_pid or '-'} "
            f"serial={device.serial or '-'} product={device.product or '-'}",
            f"        1284 ID: {device.ieee1284_raw or '(not read)'}",
            f"        supported model: {_supported_text(device)}  "
            f"present={device.present} accessible="
            f"{'-' if device.accessible is None else device.accessible} "
            f"busy={device.busy}",
        ]
        lines += [f"        error: {error}" for error in device.errors]
        probe = probes.get(device.path)
        if probe is not None:
            lines.append(f"        status probe: {probe.summary()}")
            if probe.lines:
                lines.append(f"        printer said: {probe.lines}")
            if probe.cover == "OPEN":
                lines.append("        -> close the printer cover")
        candidate = (candidates or {}).get(device.path)
        if candidate is not None:
            for reason in candidate.reasons():
                lines.append(f"        reason: {reason}")
            lines.append(f"        printer name: {candidate.name}")
            lines.append(
                f"        => usable as {candidate.name!r}: "
                f"{'YES' if candidate.usable else 'NO'}"
            )
    return lines


def _queue_section(discovery: Discovery) -> list[str]:
    lines = ["-- Print queues --"]
    queues = [
        candidate for candidate in discovery.candidates if not candidate.is_direct
    ]
    if not queues:
        lines.append("  (no print queues found)")
        return lines
    for index, candidate in enumerate(queues, start=1):
        lines += _describe_candidate(index, candidate)
    return lines


def _describe_candidate(index: int, candidate: PrinterCandidate) -> list[str]:
    queue = candidate.queue
    lines = [
        f"  [{index}] name={queue.name!r}{'  (default)' if queue.is_default else ''}",
        f"        port={queue.port!r} driver={queue.driver!r} queued_jobs={queue.jobs}",
        f"        status={describe_bits(queue.status, PRINTER_STATUS_BITS)}",
        f"        attributes={describe_bits(queue.attributes, PRINTER_ATTRIBUTE_BITS)}",
    ]
    if not candidate.usb_matching:
        lines.append("        gates: not used on this platform (every queue is usable)")
    elif candidate.vid_pid is None:
        lines.append(
            "        gate 1 (name ends in VID:PID): NO MATCH "
            "(rename the queue to end in e.g. ' 0922:0028')"
        )
    else:
        lines.append(f"        gate 1 (name ends in VID:PID): {candidate.vid_pid}")
        connected = candidate.connected_devices
        present = "YES" if connected else "NO"
        ghosts = len(candidate.usb_devices) - len(connected)
        lines.append(
            f"        gate 2 (USB {candidate.vid_pid} present): {present}"
            f" ({len(connected)} matching devices"
            + (f", {ghosts} not connected)" if ghosts else ")")
        )
        for device in candidate.usb_devices:
            lines.append(f"          - {_describe_device(device)}")
        lines.append(
            "        health (ranking only): "
            f"device {'OK' if candidate.device_healthy else 'NOT OK'}, "
            f"queue {'has a problem' if queue.has_problem else 'OK'}"
        )
    for reason in candidate.reasons():
        lines.append(f"        reason: {reason}")
    lines.append(f"        => usable: {'YES' if candidate.usable else 'NO'}")
    return lines


def _describe_device(device: UsbDevice) -> str:
    error_code = "-" if device.error_code is None else device.error_code
    present = "-" if device.present is None else device.present
    text = (
        f"{device.name!r} status={device.status!r} error_code={error_code} "
        f"present={present} class={device.pnp_class!r} id={device.device_id}"
    )
    reason = device.absence_reason
    return f"{text}  NOT CONNECTED: {reason}" if reason else text


def _usb_section(devices: Sequence[UsbDevice]) -> list[str]:
    lines = ["-- USB devices with a VID:PID --"]
    with_ids = sorted(
        (device for device in devices if device.vid_pid),
        key=lambda device: (device.vid_pid or "", device.device_id),
    )
    if not with_ids:
        lines.append("  (none reported)")
        return lines
    for device in with_ids:
        lines.append(f"  {device.vid_pid}  {_describe_device(device)}")
    without = len(devices) - len(with_ids)
    if without:
        lines.append(f"  ({without} more USB entities without a VID:PID not shown)")
    return lines


def _verdict_section(discovery: Discovery) -> list[str]:
    lines = ["-- Verdict --"]
    usable = discovery.usable
    lines.append(f"usable label printers: {len(usable)}")
    if not usable:
        lines.append(f"  (printing would fail with: {NO_PRINTER_MESSAGE!r})")
    for rank, candidate in enumerate(usable, start=1):
        how = "  [direct USB]" if candidate.is_direct else ""
        lines.append(f"  {rank}. {candidate.name}{how}")
    return lines


def _events_section(events: Sequence[Mapping[str, Any]]) -> list[str]:
    lines = ["-- Recent PrintService events (newest first) --"]
    if not events:
        lines.append("  (none, or not available on this platform)")
        return lines
    for event in events:
        if "error" in event:
            lines.append(f"  {event.get('channel')}: unavailable: {event['error']}")
            continue
        head = (
            f"  {event.get('time')} {event.get('channel')} "
            f"id={event.get('event_id')} level={event.get('level')}"
        )
        detail = event.get("message") or event.get("data") or ""
        lines.append(f"{head}: {' '.join(str(detail).split())}")
    return lines
