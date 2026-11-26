# Print server

`ibp-print-server` runs on each intake station (Linux) and prints the
request labels the IBP frontend shows, silently, on the station's USB label
printer. The frontend ([`frontend`](https://github.com/insidebooksproject/frontend),
`src/lib/printServer.ts`) posts the label PNG to
`http://localhost:40121/print-image`; if the print server is down or answers
with an error, the frontend offers the label PDF instead.

It replaces the archived standalone `jonkensta/ibp-print-server` repo
(`print-server` command), with the same HTTP contract, and prints through
this library: the same printer discovery, direct USB printing and logs as
everything else here.

## HTTP contract

| Request | Response |
|---|---|
| `GET /health` | `200 {"status": "ok", "service": "print-server", "printers": {"count": 1, "names": ["LabelWriter 450 Turbo (USB direct, serial 18073014353381)"]}}`; `"status": "degraded"` with `"error"` if the printers could not be listed |
| `POST /print-image`, body: the raw image (`Content-Type: image/png`), at most 1 MB | `200 {"status": "queued"}` once queued |
| | `400` not an image / bad `Content-Length`, `413` too large, `503` no label printer, `500` unexpected: `{"status": "error", "error": "..."}` |
| `OPTIONS` (CORS preflight) | `200` with `Access-Control-Allow-Methods: POST, GET, OPTIONS`, `Access-Control-Allow-Headers: Content-Type` (and `Access-Control-Allow-Private-Network: true` when asked) |
| anything else | `404 {"status": "error", ...}` |

Every response has `Access-Control-Allow-Origin: *`. The query string is
ignored. "Queued" means accepted, not printed: one worker thread prints the
labels one at a time, in the order they came in, and follows each one on
the printer for up to 60 s (see "Logs" below). A label that fails or may not have printed
is logged at ERROR and never retried or sent to a second printer. On
`SIGINT` / `SIGTERM` the server stops taking requests, lets the label being
printed finish (up to 90 s), and logs how many queued labels it dropped.

The old server's form-encoded `POST /` (`data=<label JSON>`, rendered into a
Code128 label by the server) is gone: the backend renders the label
(`GET /api/requests/{code}/label`) and nothing posted to it any more.

## Which printer, and how the label is sized

- **Printer:** the best usable package-label printer ibp-printing drives
  directly over USB (the DYMO LabelWriter 450 Turbo, the iDPRT SP310; see
  `ibp_printing/printers.py`), from `ibp_printing.discover()` with
  `label="package"`, falling back to the next one only when a printer
  definitely never got the label. Known printers for 4x6 shipping labels
  (the PM2411BT) are never used, nor listed by `/health`. There are no
  print queues on Linux: CUPS support was dropped on 2026-11-26, so a
  printer not in `printers.py` cannot be used, and turning direct printing
  off (`IBP_PRINTING_DIRECT=0`) leaves no printer at all. The service user
  needs the `lp` group to open the printer (`SupplementaryGroups=lp` in the
  unit; `docs/printers/dymo-lw450.md`, `docs/printers/sp310.md`).
  `--printer NAME` pins one printer by its direct name
  (`LabelWriter 450 Turbo (USB direct, serial ...)`, from `list`).
- **Size:** each printer's label stock is part of its model (`Media` in
  `printers.py`). The label PNG from the backend (1004x378, 85x32 mm at
  300 DPI) is turned to portrait and scaled, keeping its aspect ratio, to
  fill the label. On the DYMO it lands on the same dots as it did through
  the old CUPS queue (`docs/printers/dymo-lw450.md`).

## Install on an intake station

This is the setup of a station installed by hand, such as
`ibp-label-printer`. The [kiosk](https://github.com/insidebooksproject/kiosk)
image installs and runs the print server itself (see its `docs/apps.md`).

As root, once:

```sh
sudo apt install git
sudo useradd --create-home ibp        # if the station has no ibp user yet
```

As the `ibp` user:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install "ibp-printing @ git+https://github.com/insidebooksproject/printing@deploy"
```

Plug in the printer. The service gets the `lp` group from its unit; to try
the commands below as `ibp` first, run them with `sg lp -c '...'` or add
`ibp` to `lp`. Then check it:

```sh
ibp-print-server list                 # same as ibp-print-diag: "=> usable: YES"
ibp-print-server test                 # a test label on the best usable printer
ibp-print-server print label.png      # print a PNG the way the server would
```

A CUPS queue for the printer is not needed by the print server. Keep one
only if volunteers should be able to print the frontend's label-PDF
fallback from the browser; while CUPS prints, or runs `lpinfo -v`, it
takes the device away from the print server for a moment (see
`docs/printers/dymo-lw450.md`).

Install and start the service from a clone of this repo
(`git clone https://github.com/insidebooksproject/printing && cd printing`;
the unit files are in [`packaging/linux/`](../packaging/linux/)):

```sh
sudo install -m 0644 packaging/linux/ibp-print-server.service \
  packaging/linux/ibp-print-server-resume.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ibp-print-server
sudo systemctl enable ibp-print-server-resume
curl -s localhost:40121/health
```

Every start or restart, including a reboot, runs `uv tool upgrade
ibp-printing` first, so the station picks up the latest `deploy` branch (see
the README's [Releasing](../README.md#releasing)). If the upgrade fails (no
network), the installed version starts anyway. Closing a laptop's lid only
suspends it, so `ibp-print-server-resume` restarts the server after each
wake as well (once the network is up, or after a minute).

**Moving from the old `print-server`:** stop it first, since both use port
40121.

```sh
sudo systemctl disable --now print-server
sudo rm /etc/systemd/system/print-server.service
sudo -u ibp /home/ibp/.local/bin/uv tool uninstall print-server
```


## Commands

```sh
ibp-print-server server [--port 40121] [--printer NAME]   # run the server
ibp-print-server print IMAGE [--printer NAME]             # print one image file
ibp-print-server list                                     # ibp-print-diag --events 0
ibp-print-server test [--printer NAME]                    # ibp-print-diag --events 0 --test-print [NAME]
ibp-print-server --log-dir DIR ...                        # log somewhere else
```

The server listens on every interface. The frontend's default URL is
`http://localhost:40121`; browsers block plain-HTTP requests from an HTTPS
page to any other host.

## Logs

`server` and `print` write `printer-print-server.log` / `.jsonl` in
`~/.local/state/ibp-printing/logs/` of the user running it (`/home/ibp/...`
for the service), and INFO and above to stderr, which ends up in the journal:

```sh
journalctl -u ibp-print-server -f
```

Each label is one attempt (`BEGIN print-server job` ... `END`) with the
printer chosen, every status byte the printer sent (`direct USB print`,
`status 0x..`), and the outcome: `label printed (completed)`, or an ERROR
`label may not have printed (<outcome>); check the printer` with the
session's history. `/health` polls are not logged unless they fail. The
outcomes per printer are in `docs/printers/dymo-lw450.md` and
`docs/printers/sp310.md`.
