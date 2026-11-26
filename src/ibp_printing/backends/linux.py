"""Linux platform backend: no print queues, only the USB device list.

On Linux every supported label printer is printed to directly over USB
(:mod:`ibp_printing.direct`), so this backend, which
:class:`~ibp_printing.direct.backend.DirectFirstBackend` wraps, has no
queues to offer: ``discover()`` lists the plugged-in USB devices (for
``ibp-print-diag``) and nothing else, and ``print_image()`` refuses. CUPS
support was dropped on 2026-11-26, once the DYMO LabelWriter 450 Turbo,
the last printer that needed it, printed directly. A printer that is not in
:mod:`ibp_printing.printers` cannot be printed to on Linux.

Devices are read from sysfs (``/sys/bus/usb/devices/*/idVendor``), so no
udev library is needed.
"""

import logging
from pathlib import Path
from typing import Optional, Union

from PIL import Image

from ibp_printing.backends.base import PrinterBackend, PrintError
from ibp_printing.discovery import discovery_from
from ibp_printing.log import get_logger, log_event
from ibp_printing.models import Discovery, PrintResult, UsbDevice

logger = get_logger(__name__)

NO_QUEUES = (
    "no print queues on Linux: only the label printers ibp-printing drives "
    "directly over USB can be printed to"
)


def list_usb_devices(sys_root: Union[str, Path] = "/sys") -> list[UsbDevice]:
    """Plugged-in USB devices from ``<sys_root>/bus/usb/devices``.

    Only device entries have ``idVendor`` / ``idProduct`` (interfaces such as
    ``1-1:1.0`` do not), and sysfs lists only devices that are connected now.
    """
    devices: list[UsbDevice] = []
    root = Path(sys_root) / "bus" / "usb" / "devices"
    if not root.is_dir():
        return devices

    def read(path: Path) -> str:
        try:
            return path.read_text(encoding="latin-1").strip()
        except OSError:
            return ""

    for entry in sorted(root.iterdir()):
        vid, pid = read(entry / "idVendor"), read(entry / "idProduct")
        if not (vid and pid):
            continue
        name = f"{read(entry / 'manufacturer')} {read(entry / 'product')}".strip()
        devices.append(
            UsbDevice(
                device_id=f"usb:{entry.name}",
                name=name,
                status="OK",
                pnp_class="USB",
                vid_pid=f"{vid}:{pid}".upper(),
                present=True,
            )
        )
    return devices


class LinuxPrinterBackend(PrinterBackend):
    """No queues; the plugged-in USB devices, for diagnostics."""

    platform_name = "linux"

    def __init__(self, sys_root: Union[str, Path] = "/sys") -> None:
        self.sys_root = sys_root

    def discover(self) -> Discovery:
        """The plugged-in USB devices, and no queues."""
        errors: list[str] = []
        usb_devices: list[UsbDevice] = []
        try:
            usb_devices = list_usb_devices(self.sys_root)
        except OSError as exc:
            errors.append(f"USB device enumeration failed: {exc!r}")
            logger.exception("USB device enumeration failed")
        log_event(
            logger,
            logging.INFO,
            "discovery (no print queues on Linux)",
            usb=[f"{device.vid_pid} {device.name}" for device in usb_devices],
            errors=errors,
        )
        return discovery_from([], usb_devices, errors)

    def get_default_printer(self) -> Optional[str]:
        """There is no default printer without queues."""
        return None

    def print_image(
        self,
        img: Image.Image,
        printer_name: str,
        *,
        job_name: str,
        track_timeout_s: float = 0.0,
    ) -> PrintResult:
        """Refuse: only direct USB printers can be printed to on Linux."""
        del img, job_name, track_timeout_s
        raise PrintError(f"cannot print to {printer_name!r}: {NO_QUEUES}")


__all__ = ["NO_QUEUES", "LinuxPrinterBackend", "list_usb_devices"]
