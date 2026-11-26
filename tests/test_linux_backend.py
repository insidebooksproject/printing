"""Tests for the Linux platform backend (no print queues since CUPS was dropped)."""

import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image
from printing_helpers import quiet_logging

from ibp_printing.backends import PrintError
from ibp_printing.backends import linux
from ibp_printing.direct.backend import DirectFirstBackend


def fake_sysfs(root: Path, devices: dict[str, tuple[str, str, str]]) -> None:
    """Write ``<root>/bus/usb/devices/<name>/{idVendor,idProduct,product}``."""
    base = root / "bus" / "usb" / "devices"
    base.mkdir(parents=True)
    for name, (vid, pid, product) in devices.items():
        entry = base / name
        entry.mkdir()
        (entry / "idVendor").write_text(vid + "\n")
        (entry / "idProduct").write_text(pid + "\n")
        (entry / "product").write_text(product + "\n")
    # An interface entry: no idVendor, never a device.
    (base / "1-1:1.0").mkdir()


@unittest.skipIf(sys.platform == "win32", "sysfs entry names contain ':'")
class LinuxBackendTests(unittest.TestCase):
    """USB devices are listed; nothing can be printed without the direct path."""

    def setUp(self):
        quiet_logging(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sys_root = Path(tmp.name)
        self.backend = linux.LinuxPrinterBackend(sys_root=self.sys_root)

    def test_lists_usb_devices_from_sysfs(self):
        fake_sysfs(self.sys_root, {"1-1": ("0922", "0021", "LabelWriter 450 Turbo")})
        devices = linux.list_usb_devices(self.sys_root)
        self.assertEqual(
            [(d.vid_pid, d.name, d.connected) for d in devices],
            [("0922:0021", "LabelWriter 450 Turbo", True)],
        )

    def test_no_sysfs_means_no_devices(self):
        self.assertEqual(linux.list_usb_devices(self.sys_root / "missing"), [])

    def test_discovery_has_devices_but_no_queues(self):
        fake_sysfs(self.sys_root, {"1-1": ("0922", "0021", "LabelWriter 450 Turbo")})
        discovery = self.backend.discover()
        self.assertEqual(discovery.queues, [])
        self.assertEqual(discovery.candidates, [])
        self.assertEqual(len(discovery.usb_devices), 1)
        self.assertIsNone(self.backend.get_default_printer())

    def test_print_image_refuses(self):
        with self.assertRaises(PrintError) as caught:
            self.backend.print_image(
                Image.new("L", (10, 10), 255), "Some_Queue", job_name="x"
            )
        self.assertIn(linux.NO_QUEUES, str(caught.exception))

    def test_direct_off_means_no_printers(self):
        fake_sysfs(self.sys_root, {})
        backend = DirectFirstBackend(self.backend, enabled=False)
        self.assertEqual(backend.discover().usable, [])


if __name__ == "__main__":
    unittest.main()
