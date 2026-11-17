"""Old import paths that shippy and shippy-gui use keep working after moves."""

import unittest

import ibp_printing
from ibp_printing import labels as old_labels
from ibp_printing.shipping import labels


class CompatTests(unittest.TestCase):
    """Each old path re-exports the same objects as the new one."""

    def test_labels(self):
        for name in old_labels.__all__:
            self.assertIs(getattr(old_labels, name), getattr(labels, name), name)
        self.assertIs(ibp_printing.record_purchase, labels.record_purchase)


if __name__ == "__main__":
    unittest.main()
