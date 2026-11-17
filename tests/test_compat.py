"""Old import paths that shippy and shippy-gui use keep working after moves."""

import unittest

import subprocess
import sys

import ibp_printing
from ibp_printing import diagnostics as old_diagnostics
from ibp_printing import labels as old_labels
from ibp_printing.apps import diag
from ibp_printing.shipping import labels


class CompatTests(unittest.TestCase):
    """Each old path re-exports the same objects as the new one."""

    def test_labels(self):
        for name in old_labels.__all__:
            self.assertIs(getattr(old_labels, name), getattr(labels, name), name)
        self.assertIs(ibp_printing.record_purchase, labels.record_purchase)
        self.assertIn("set_journal_path", old_labels.__all__)

    def test_diagnostics(self):
        for name in diag.__all__:
            self.assertIs(getattr(old_diagnostics, name), getattr(diag, name), name)
        # Public names of the old single-module diagnostics.
        for name in (
            "describe_direct_devices",
            "describe_line",
            "direct_section",
            "identify_device",
            "open_problem",
            "probe_direct_devices",
            "run_direct_status",
            "run_direct_test_print",
            "run_listen",
        ):
            self.assertTrue(callable(getattr(old_diagnostics, name)), name)
        self.assertIs(old_diagnostics.build_report, diag.build_report)
        self.assertIs(old_diagnostics.main, diag.main)

    def test_old_and_new_module_entry_points_run(self):
        for module in ("ibp_printing.diagnostics", "ibp_printing.apps.diag"):
            proc = subprocess.run(
                [sys.executable, "-m", module, "--help"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("ibp-print-diag", proc.stdout)


if __name__ == "__main__":
    unittest.main()
