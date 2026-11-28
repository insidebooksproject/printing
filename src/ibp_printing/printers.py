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
    # Plain TSPL: uncompressed BITMAP, status from the one-byte ESC ! ? reply
    # (direct.plain_tspl; docs/printers/sp310.md).
    TSPL = "tspl"
    # DYMO LabelWriter raster: SYN-prefixed dot lines, status from the
    # one-byte ESC A reply (direct.dymo_lw; docs/printers/dymo-lw450.md).
    DYMO_LW = "dymo-lw"


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
    # 8 is 203 dpi (the TSPL printers); 300 dpi is 300 / 25.4.
    dots_per_mm: float = 8.0
    # The part of the label that is printed, (across, along) the feed, from
    # the head's first dot and the label's first line; None: all of it. For
    # the DYMO, the CUPS PPD's ImageableArea, so labels print where they did
    # through the queue.
    printable_mm: Optional[tuple[float, float]] = None

    @property
    def width_dots(self) -> int:
        """Label width in dots."""
        return round(self.width_mm * self.dots_per_mm)

    @property
    def height_dots(self) -> int:
        """Label length in dots."""
        return round(self.height_mm * self.dots_per_mm)

    @property
    def printable_dots(self) -> tuple[int, int]:
        """The printed area in dots, (across, along) the feed."""
        if self.printable_mm is None:
            return self.width_dots, self.height_dots
        return (
            round(self.printable_mm[0] * self.dots_per_mm),
            round(self.printable_mm[1] * self.dots_per_mm),
        )

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

# iDPRT SP310: 3-inch TSPL label printer (203 dpi). Its 1284 ID says
# "COMMAND SET:ESC/POS;MODEL:SP310" but it takes TSPL. Loaded with 36 x 89 mm
# (1.4 x 3.5 in) labels, the size of the DYMO 30321 at the intake stations.
# Its print lands up to ~0.5 mm past the label's edge, hence the margin
# (docs/printers/sp310.md).
SP310 = PrinterModel(
    name="SP310",
    mdl=frozenset({"SP310"}),
    vid_pid="20D1:7008",
    role=LabelRole.PACKAGE,
    protocol=Protocol.TSPL,
    media=Media(width_mm=36, height_mm=89, gap_mm=3, margin_mm=1),
)

# DYMO LabelWriter 450 Turbo (300 dpi) at the intake stations, loaded with
# 30321 Large Address labels (1.4 x 3.5 in). The media copies the CUPS queue
# (lw450t.ppd), so a label prints as it did through the queue: the page is
# PageSize w102h252 at 300 dpi (425 x 1050 dots), and what is printed is its
# ImageableArea, 389 x 961 dots from the first dot and line (the raster CUPS
# sent, checked against cupsfilter on the station 2026-11-26). The gap is not
# sent: the printer finds each label by its sense hole. Its widest label is
# 2.3 in, so it never prints shipping labels (docs/printers/dymo-lw450.md).
LABELWRITER_450_TURBO = PrinterModel(
    name="LabelWriter 450 Turbo",
    mdl=frozenset({"LABELWRITER 450 TURBO"}),
    vid_pid="0922:0021",
    role=LabelRole.PACKAGE,
    protocol=Protocol.DYMO_LW,
    media=Media(
        width_mm=36,
        height_mm=88.9,
        gap_mm=0,
        dots_per_mm=300 / 25.4,
        printable_mm=(32.94, 81.36),
    ),
)

MODELS: tuple[PrinterModel, ...] = (PM2411BT, SP310, LABELWRITER_450_TURBO)


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
    "SP310",
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
