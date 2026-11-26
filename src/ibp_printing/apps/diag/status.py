"""``ibp-print-diag --direct-status``: probe the direct USB printers."""

import json
import logging
from typing import Optional, Sequence

from ibp_printing.api import get_backend
from ibp_printing.apps.diag.report import (
    describe_direct_devices,
    direct_device_data,
)
from ibp_printing.direct.backend import DirectFirstBackend
from ibp_printing.direct.session import StatusProbe
from ibp_printing.direct.transport import DirectDevice
from ibp_printing.discovery import direct_candidates
from ibp_printing.log import get_logger, log_event
from ibp_printing.models import PrinterCandidate, is_supported_direct_model

logger = get_logger(__name__)


def direct_layer() -> Optional[DirectFirstBackend]:
    """The active backend's direct USB layer, or None if it has none."""
    backend = get_backend()
    return backend if isinstance(backend, DirectFirstBackend) else None


def probe_direct_devices(
    devices: Sequence[DirectDevice],
    layer: Optional[DirectFirstBackend] = None,
) -> dict[str, StatusProbe]:
    """Probe every supported device (label-safe status queries only), by path."""
    layer = layer or direct_layer()
    if layer is None:
        return {}
    return {
        device.path: layer.probe(device)
        for device in devices
        if is_supported_direct_model(device.model)
    }


def candidates_by_path(
    devices: Sequence[DirectDevice],
) -> dict[str, PrinterCandidate]:
    """The direct candidate for each device, by device path."""
    return {
        candidate.direct_device.path: candidate
        for candidate in direct_candidates(devices)
        if candidate.direct_device is not None
    }


def direct_off(
    layer: Optional[DirectFirstBackend], as_json: bool, what: str
) -> Optional[int]:
    """Print why the direct layer cannot be used; None when it can."""
    if layer is None:
        message = "the active backend has no direct USB layer"
        print(json.dumps({"error": message}) if as_json else message)
        return 1
    enabled, why = layer.direct_mode()
    if enabled:
        return None
    log_event(logger, logging.INFO, f"{what}: direct USB printing is off", why=why)
    if as_json:
        data = {"direct_enabled": False, "decided_by": why, "devices": []}
        print(json.dumps(data, indent=2))
    else:
        print(
            f"-- Direct USB printers --\n  direct USB printing: OFF ({why})\n"
            f"  {what}: no USB device was opened (direct printing is turned off)"
        )
    return 1


def run_direct_status(as_json: bool) -> int:
    """``--direct-status``: list direct devices and probe them; exit code.

    With direct printing off (``IBP_PRINTING_DIRECT=0`` / ``--no-direct``)
    it says so and opens nothing.
    """
    layer = direct_layer()
    off = direct_off(layer, as_json, "--direct-status")
    if off is not None or layer is None:
        return off or 1
    enabled, why = layer.direct_mode()
    devices, errors = layer.list_devices()
    probes = probe_direct_devices(devices, layer)
    candidates = candidates_by_path(devices)
    ready = [path for path, probe in probes.items() if probe.ready]
    device_data = []
    for device in devices:
        data = direct_device_data(device, probes.get(device.path))
        candidate = candidates.get(device.path)
        data["printer_name"] = candidate.name if candidate else None
        data["usable"] = candidate.usable if candidate else False
        device_data.append(data)
    log_event(
        logger,
        logging.INFO,
        "direct status",
        enabled=enabled,
        decided_by=why,
        devices=device_data,
        errors=errors,
    )
    if as_json:
        data = {
            "direct_enabled": enabled,
            "decided_by": why,
            "devices": device_data,
            "errors": errors,
            "ready": ready,
        }
        print(json.dumps(data, indent=2, default=repr))
    else:
        lines = [
            "-- Direct USB printers (status probe: SSSGETCAP / SSSGETPAPER, or "
            "ESC ! ? for plain TSPL, only) --",
            f"  direct USB printing: {'ON' if enabled else 'OFF'} ({why})",
        ]
        lines += describe_direct_devices(devices, probes, candidates)
        lines += [f"  error: {error}" for error in errors]
        lines.append(f"  ready: {len(ready)} of {len(probes)} supported printer(s)")
        print("\n".join(lines))
    return 0 if probes and len(ready) == len(probes) else 1
