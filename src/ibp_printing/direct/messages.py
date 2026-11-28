"""What the volunteer is told when a direct USB print is refused.

Also re-exported by :mod:`ibp_printing.direct.session`.
"""

PAPER_OUT_HINT = (
    "printer stopped accepting data (paper out? jam?) - a partial job is in "
    "the printer: power-cycle it before reloading paper"
)


# What the volunteer is told for pre-check failures that need them to act.
VOLUNTEER_MESSAGES = {
    "cover_open": (
        "Close the printer cover: the label printer's cover is open, so "
        "nothing was printed."
    ),
    "realign_busy": (
        "Close the printer cover and wait a few seconds: the label printer was "
        "still lining up its labels (it does this every time the cover is "
        "closed), so nothing was printed."
    ),
    "no_cover_reply": (
        "Turn the label printer off and on again: it did not answer, so "
        "nothing was printed."
    ),
    "job_not_accepted": (
        "Turn the label printer off and on again: it did not take the label, "
        "so nothing was printed."
    ),
    # Plain-TSPL printers (direct.plain_tspl), from their status byte.
    "no_status_reply": (
        "Turn the label printer off and on again: it did not answer, so "
        "nothing was printed."
    ),
    "paper_out": (
        "Load a new roll of labels: the label printer is out of labels, so "
        "nothing was printed."
    ),
    "paper_jam": (
        "Open the label printer, clear the stuck label and close it again: "
        "the labels are jammed, so nothing was printed."
    ),
    "paused": (
        "Press the label printer's button once to un-pause it: it is paused, "
        "so nothing was printed."
    ),
    "printer_busy": (
        "Wait a few seconds and print again: the label printer was still "
        "busy with the last label, so nothing was printed."
    ),
    "printer_error": (
        "Turn the label printer off and on again: it reports an error, so "
        "nothing was printed."
    ),
}

NO_SAME_PRINTER_FALLBACK = frozenset(VOLUNTEER_MESSAGES)
"""Reasons after which the label must not go to the same physical printer's
print queue (its VID:PID twin): the cover is open or the printer is
realigning (the queue would print into an open printer or race the realign),
or the printer is not answering / not taking data and may be holding a
partial job (it would swallow the queue's job as bitmap data and the queue
would report success), or (plain TSPL) it is out of labels, jammed, paused,
busy or in error, which its queue could not print through either. The label
waits in ``to-print/`` instead."""
