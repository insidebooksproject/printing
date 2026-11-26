# Direct USB printing for the DYMO LabelWriter 450 Turbo

The LabelWriter 450 Turbo is the 300 dpi thermal printer at intake station 1
(`ibp-label-printer`), loaded with 30321 Large Address labels (1.4 x 3.5 in)
for the package labels. ibp-printing drives it over USB without CUPS or
`printer-driver-dymo`, through its own job builder and session
(`ibp_printing/direct/dymo_lw.py`). Why: a printer that is out of labels or
jammed is refused with a message instead of a CUPS job sitting in the
queue, and every intake printer (DYMO and SP310) goes through one code path.

Protocol sources: DYMO's *LabelWriter 450 Series Technical Reference*,
DYMO's `raster2dymolw` (dymo-cups-drivers) and CUPS's `rastertolabel.c`,
then the station itself.

## Verified on the station (2026-11-26, Linux Mint, usblp)

- USB `0922:0021`, serial `18073014353381`, usblp `/dev/usb/lp1`,
  bidirectional. 1284 ID: `MFG:DYMO;CMD: ;MDL:LabelWriter 450 Turbo;
  CLASS:PRINTER;DESCRIPTION:DYMO LabelWriter 450 Turbo;SERN:...;`.
  Identified by `MDL:LabelWriter 450 Turbo`.
- The node is `root:lp 0660`. The print server's unit has
  `SupplementaryGroups=lp`; the udev rule's `0922` line gives a desktop
  user an ACL.
- **Status:** `ESC A` (`1B 41`) is answered at once with one byte, also
  while printing: `0x03` idle (ready, top of form), `0x01` while a label
  feeds. `ESC V` (`1B 56`) answers 10 ASCII bytes, `1750283f0J` here. Both
  answered after hours idle.
- **A label:** the printer takes the job only as fast as it prints (an
  85 kB graphics-mode job is written in ~4 s), then reports `0x01` for
  ~1 s and `0x03` when the label is out. The session counts that
  `0x01` -> `0x03` as the label printed. If it never sees `0x01`, it waits
  `idle_settle_s` (3 s) of `0x03` and reports `COMPLETED` anyway, noting
  that this is the only confirmation.
- **Speed:** graphics mode (300 x 600, what the CUPS queue used) ~5.7 s per
  label from request to done; text mode (300 x 300) ~3.2 s. Graphics stays
  the default until a text-mode label's barcode has been scanned at the
  Book Portal station.
- **Out of labels:** the printer cannot tell at idle. With the roll removed
  it still answers `0x03`, takes the job, then reports `0xa1` (ready, out
  of paper, error) and blinks its LED. The session returns `ERROR` ("out
  of labels: load a new roll and print the label again"); the next print's
  pre-check refuses with `paper_out` until the roll is reloaded, which
  clears the status back to `0x03`.
- **A cut-off job** (fewer bytes than a whole label, e.g. the connection
  lost mid-job): the printer prints what it got, then reads every later
  byte as dots, so `ESC A` gets no reply. 156 ESC bytes (the sync run every
  job starts with) end that raster line without printing; the printer then
  answers `0x01` (ready, part way through a label), and `ESC E` feeds the
  part label out, back to `0x03`. The pre-check does both by itself, so the
  next label prints whole and aligned (tested).
- **CUPS and the device:** every CUPS job, `lpinfo -v` (also the printer
  settings dialog) and a replug (CUPS probes the new device) unbind usblp
  briefly (`usblp1: removed` in the kernel log), so `/dev/usb/lp1`
  disappears. A direct print running at that moment is cut off (`DeviceGone`,
  `UNCERTAIN`); the next print recovers as above. A replug also re-enables
  the CUPS queue (`udev-configure-printer`).

## The job

```text
ESC x 156          sync
ESC L 08 34        label length: 2100 dot lines (graphics; 1050 in text mode)
ESC i | ESC h      graphics 300x600 | text 300x300
ESC e              density normal (c light, d medium, g dark)
ESC B 0            dot tab 0
ESC D 49           49 bytes per line (389 dots)
SYN <49 bytes>     a raster line, MSB first, 1 = black dot (sent twice in graphics mode)
ESC f 1 n          or: skip n (<= 255) blank lines
ESC E              form feed to the next label
```

`ESC @` (reset) is never sent: it would throw away a label still printing.

## Placement

The model copies the CUPS queue so that labels print where they did: the
page is the PPD's PageSize `w102h252` (425 x 1050 dots), and what is
printed is its ImageableArea, 389 x 961 dots starting at the head's first
dot and the label's first line, which is the raster CUPS sent (checked with
`cupsfilter` on the station). The label image is placed on the page as the
CUPS path did (`fill_page`: rotated a quarter turn counter-clockwise if
landscape, scaled to fill, centered), then the page is scaled into the
printable area. The backend's 1004 x 378 request label lands on the same
dots as through CUPS (ink box x 42-348, y 49-912); only anti-aliased edge
dots differ (thresholded here, halftoned by CUPS). The first direct test,
which filled the whole 425 x 1050 page, came out larger and ran to the
label's edge.

## Outcomes

| Printer says | Result |
|---|---|
| no reply, even after the sync run | `DirectPrintError` `no_status_reply`, nothing sent |
| out of labels / jam / error bit | `DirectPrintError` `paper_out` / `paper_jam` / `printer_error`, nothing sent, volunteer message |
| not ready, or a part label that will not feed out, after 10 s | `DirectPrintError` `printer_busy` |
| `0x01` then `0x03` after the job | `COMPLETED` |
| `0x03` throughout for 3 s | `COMPLETED` (weak) |
| a fault bit after the job | `ERROR` |
| still `0x01` after `done_s` | `TIMEOUT` |
| no reply after the job, a stalled write, the device gone | `UNCERTAIN` |

## Rollout on a station

1. Give the service the `lp` group (the unit in `packaging/linux/` has it):

   ```sh
   sudo mkdir -p /etc/systemd/system/ibp-print-server.service.d
   printf '[Service]\nSupplementaryGroups=lp\n' |
     sudo tee /etc/systemd/system/ibp-print-server.service.d/10-lp.conf
   sudo systemctl daemon-reload && sudo systemctl restart ibp-print-server
   ```

2. Leave the CUPS queue enabled: it is the fallback when the printer cannot
   be opened, and what the frontend's label-PDF fallback prints through.
3. Check: `ibp-print-diag` lists `LabelWriter 450 Turbo [USB direct]`, ready
   `0x03`; print one label from the frontend; the log shows
   `direct USB print result ... completed`.

Rollback: `Environment=IBP_PRINTING_DIRECT=0` in a drop-in (direct USB
printing off, everything goes to CUPS as before), `daemon-reload`, restart.

## Still to check

- Scan a text-mode label's barcode at the Book Portal station, then
  consider text mode (about 2.5 s faster per label).
- Whether the DYMO's own feed button, or opening the cover mid-label, shows
  up in the status byte.
