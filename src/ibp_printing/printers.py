"""The label printers ibp-printing knows: what each one speaks and is for.

Each :class:`PrinterModel` says how a printer is recognised (its IEEE 1284
``MDL`` and USB VID:PID), how the direct USB path talks to it
(:class:`Protocol`; ``None`` for a printer only ever driven by its print
queue), and which labels it prints (:class:`LabelRole`). The direct path
only sends to a device whose 1284 model is listed here with a protocol:
matching is never by VID:PID alone, because a VID is often shared by
unrelated devices (the PM2411BT's, 2E3C, is Artery's).

Adding a printer that speaks an existing protocol is one entry here plus,
on Linux, a line in ``packaging/linux/60-ibp-label-printer.rules`` for its
USB vendor. A new protocol also needs a job builder and session under
:mod:`ibp_printing.direct` and a branch in ``DirectFirstBackend``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union


class LabelRole(str, Enum):
    """Which labels a printer is used for."""

    # 4x6 in postage labels (shippy, shippy-gui on the shipping laptops).
    SHIPPING = "shipping"
    # Small package labels (ibp-print-server at the intake stations).
    PACKAGE = "package"


class Protocol(str, Enum):
    """How the direct USB path talks to a printer."""

    # The PM2411BT's TSPL dialect: LZO-compressed BITMAP, pushed
    # SSSGETPRINTING lines and SSSGETCAP / SSSGETPAPER queries
    # (direct.tspl, direct.session; docs/printers/pm2411bt.md).
    PM_TSPL = "pm-tspl"


@dataclass(frozen=True)
class Media:
    """A die-cut label, as the printer is told about it.

    ``width_mm`` runs across the print head, ``height_mm`` along the feed.
    ``margin_mm`` is white space kept on every side of the printed image,
    for printers whose print does not land exactly on the label.
    """

    width_mm: float
    height_mm: float
    gap_mm: float
    margin_mm: float = 0.0
    dots_per_mm: int = 8  # 203 dpi

    @property
    def width_dots(self) -> int:
        """Label width in dots."""
        return round(self.width_mm * self.dots_per_mm)

    @property
    def height_dots(self) -> int:
        """Label length in dots."""
        return round(self.height_mm * self.dots_per_mm)

    @property
    def margin_dots(self) -> int:
        """The white margin on each side, in dots."""
        return round(self.margin_mm * self.dots_per_mm)

    def describe(self) -> str:
        """``"36 x 89 mm (gap 3 mm, margin 1 mm)"``."""
        text = f"{self.width_mm:g} x {self.height_mm:g} mm (gap {self.gap_mm:g} mm"
        if self.margin_mm:
            text += f", margin {self.margin_mm:g} mm"
        return text + ")"


@dataclass(frozen=True)
class PrinterModel:
    """One kind of label printer."""

    name: str
    # IEEE 1284 MDL values, upper case.
    mdl: frozenset[str]
    # USB VID:PID, upper case ("2E3C:5760").
    vid_pid: str
    role: LabelRole
    # None: only ever printed to through its print queue.
    protocol: Optional[Protocol] = None
    # The label stock the direct path prints on (None: the protocol's own).
    media: Optional[Media] = None

    @property
    def direct(self) -> bool:
        """True when the direct USB path can drive this printer."""
        return self.protocol is not None

    @property
    def usb_vendor(self) -> str:
        """The USB vendor ID, lower case (as udev writes it)."""
        return self.vid_pid.split(":", 1)[0].lower()


PM2411BT = PrinterModel(
    name="PM2411BT",
    mdl=frozenset({"PM2411BT"}),
    vid_pid="2E3C:5760",
    role=LabelRole.SHIPPING,
    protocol=Protocol.PM_TSPL,
)

# Printed to through its CUPS queue only (for now). Its widest label is
# 2.3 in, so it never prints shipping labels.
LABELWRITER_450_TURBO = PrinterModel(
    name="LabelWriter 450 Turbo",
    mdl=frozenset({"LABELWRITER 450 TURBO"}),
    vid_pid="0922:0021",
    role=LabelRole.PACKAGE,
)

MODELS: tuple[PrinterModel, ...] = (PM2411BT, LABELWRITER_450_TURBO)


def model_for_mdl(mdl: str) -> Optional[PrinterModel]:
    """The model whose 1284 MDL is ``mdl`` (case-insensitive), or None."""
    key = mdl.strip().upper()
    if not key:
        return None
    return next((model for model in MODELS if key in model.mdl), None)


def direct_model_for_mdl(mdl: str) -> Optional[PrinterModel]:
    """Like :func:`model_for_mdl`, but only models the direct path drives."""
    model = model_for_mdl(mdl)
    return model if model is not None and model.direct else None


def model_for_vid_pid(vid_pid: Optional[str]) -> Optional[PrinterModel]:
    """The model with USB ``vid_pid``, or None. Never enough to print to it."""
    if not vid_pid:
        return None
    key = vid_pid.strip().upper()
    return next((model for model in MODELS if model.vid_pid == key), None)


def direct_vid_pids() -> frozenset[str]:
    """VID:PIDs of the models the direct path drives."""
    return frozenset(model.vid_pid for model in MODELS if model.direct)


def parse_role(value: Union[LabelRole, str, None]) -> Optional[LabelRole]:
    """``LabelRole`` from a role or its value ("shipping", "package"); None stays.

    Raises:
        ValueError: for an unknown role.
    """
    if value is None or isinstance(value, LabelRole):
        return value
    try:
        return LabelRole(value.strip().lower())
    except ValueError:
        known = ", ".join(role.value for role in LabelRole)
        raise ValueError(f"unknown label role {value!r} (known: {known})") from None


__all__ = [
    "LABELWRITER_450_TURBO",
    "MODELS",
    "PM2411BT",
    "LabelRole",
    "Media",
    "PrinterModel",
    "Protocol",
    "direct_model_for_mdl",
    "direct_vid_pids",
    "model_for_mdl",
    "model_for_vid_pid",
    "parse_role",
]
