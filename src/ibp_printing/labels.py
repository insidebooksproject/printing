"""Moved to :mod:`ibp_printing.shipping.labels`; kept so old imports still work.

Everything here is also importable from ``ibp_printing`` itself.
"""

from ibp_printing.shipping.labels import (
    LabelRecord,
    LabelStatus,
    find_duplicates,
    journal_path,
    pending_labels,
    recipient_key,
    record_purchase,
    set_journal_path,
    update_status,
)

__all__ = [
    "LabelRecord",
    "LabelStatus",
    "find_duplicates",
    "journal_path",
    "pending_labels",
    "recipient_key",
    "record_purchase",
    "set_journal_path",
    "update_status",
]
