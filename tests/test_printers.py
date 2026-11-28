"""Tests for the printer model registry."""

import unittest

from ibp_printing import printers
from ibp_printing.models import SUPPORTED_DIRECT_MODELS, is_supported_direct_model
from ibp_printing.printers import LabelRole, Protocol


class RegistryTests(unittest.TestCase):
    """Lookups by 1284 model and VID:PID."""

    def test_lookup_by_mdl_is_case_insensitive(self):
        self.assertIs(printers.model_for_mdl(" pm2411bt "), printers.PM2411BT)
        self.assertIsNone(printers.model_for_mdl(""))
        self.assertIsNone(printers.model_for_mdl("PM241"))

    def test_labelwriter_450_is_direct(self):
        dymo = printers.model_for_mdl("LabelWriter 450 Turbo")
        self.assertIs(dymo, printers.LABELWRITER_450_TURBO)
        self.assertIs(dymo.protocol, Protocol.DYMO_LW)
        self.assertIs(printers.direct_model_for_mdl("LabelWriter 450 Turbo"), dymo)
        self.assertTrue(is_supported_direct_model("LabelWriter 450 Turbo"))
        self.assertIn("0922:0021", printers.direct_vid_pids())
        self.assertEqual(dymo.usb_vendor, "0922")
        # The CUPS queue's PageSize w102h252 (30321) at 300 dpi.
        self.assertEqual((dymo.media.width_dots, dymo.media.height_dots), (425, 1050))
        self.assertEqual(dymo.media.margin_dots, 0)

    def test_a_model_without_a_protocol_is_not_direct(self):
        queue_only = printers.PrinterModel(
            name="X", mdl=frozenset({"X"}), vid_pid="1234:5678", role=LabelRole.PACKAGE
        )
        self.assertFalse(queue_only.direct)
        self.assertIsNone(printers.direct_model_for_mdl("LabelWriter 4XL"))

    def test_supported_direct_models_follow_the_registry(self):
        direct = {mdl for m in printers.MODELS if m.direct for mdl in m.mdl}
        self.assertEqual(SUPPORTED_DIRECT_MODELS, direct)
        self.assertIn("PM2411BT", SUPPORTED_DIRECT_MODELS)

    def test_lookup_by_vid_pid(self):
        self.assertIs(printers.model_for_vid_pid("2e3c:5760"), printers.PM2411BT)
        self.assertIsNone(printers.model_for_vid_pid(None))
        self.assertIsNone(printers.model_for_vid_pid("1234:5678"))
        self.assertEqual(printers.PM2411BT.usb_vendor, "2e3c")

    def test_roles(self):
        self.assertIs(printers.PM2411BT.role, LabelRole.SHIPPING)
        self.assertIs(printers.LABELWRITER_450_TURBO.role, LabelRole.PACKAGE)
        self.assertIs(printers.parse_role("Package"), LabelRole.PACKAGE)
        self.assertIs(printers.parse_role(LabelRole.SHIPPING), LabelRole.SHIPPING)
        self.assertIsNone(printers.parse_role(None))
        with self.assertRaises(ValueError):
            printers.parse_role("4x6")


class MediaTests(unittest.TestCase):
    """Label sizes in dots."""

    def test_dots(self):
        media = printers.Media(width_mm=36, height_mm=89, gap_mm=3, margin_mm=1)
        self.assertEqual((media.width_dots, media.height_dots), (288, 712))
        self.assertEqual(media.margin_dots, 8)
        self.assertEqual(media.describe(), "36 x 89 mm (gap 3 mm, margin 1 mm)")


if __name__ == "__main__":
    unittest.main()
