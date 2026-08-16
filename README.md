# NutController

A self-hosted web dashboard and Telegram bot for monitoring a UPS through [NUT (Network UPS Tools)](https://networkupstools.org/), with automatic emergency shutdown of other Proxmox VE containers during long power outages.

Runs on a dedicated Proxmox LXC container with a USB-attached UPS. Ships a real-time web dashboard, sends Telegram notifications on power events, and — when battery runtime gets critical — shuts down the other containers on the Proxmox host in order to avoid unclean shutdowns, then powers them back on once the power is restored.

![License](https://img.shields.io/github/license/drakmanga/NutController)
![Last commit](https://img.shields.io/github/last-commit/drakmanga/NutController)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Flask](https://img.shields.io/badge/flask-3.x-black)
![NUT](https://img.shields.io/badge/NUT-Network%20UPS%20Tools-orange)
![Platform](https://img.shields.io/badge/platform-Proxmox%20VE%20(LXC)-informational)

## Table of contents

- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [USB passthrough (Proxmox LXC)](#usb-passthrough-proxmox-lxc)
- [Configuration](#configuration)
- [Web dashboard](#web-dashboard)
- [Guided settings](#guided-settings)
  - [Connect Telegram](#connect-telegram)
  - [Connect NUT](#connect-nut)
  - [UPS emergency](#ups-emergency)
- [Telegram bot](#telegram-bot)
- [Emergency shutdown logic](#emergency-shutdown-logic)
- [State and log files](#state-and-log-files)
- [Repository layout](#repository-layout)
- [Security](#security)
- [Known limitations](#known-limitations)
- [License](#license)

## Architecture

```
UPS (USB) ──► NUT (usbhid-ups driver) ──► upsd ──► upsmon
                                             │           │
                                             │           └─► NOTIFYCMD ──► ups-notify.sh ──► Telegram
                                             │
                                             └─► ups-web.py (Flask, port 80)
                                                    ├─ web dashboard
                                                    ├─ Telegram bot (/stats, /history)
                                                    └─ guided-setup API (Telegram + NUT + emergency)

cron (every minute) ──► ups-emergency.sh ──► SSH to the Proxmox host ──► shuts down / restarts the other CTs
systemd (on boot)   ──► ups-boot-check.sh ──► notifies if the reboot happened during a power outage
```

All components run inside the same NUT-dedicated LXC container (original setup: a Proxmox container running NUT in `standalone` mode, UPS attached over USB passthrough).

## Requirements

- A Linux container/host with NUT installed (`nut`, `nut-client` on Debian/Ubuntu) and a NUT-supported UPS attached (USB in the reference setup, but any NUT driver works).
- Python 3.9+ with `flask`, `requests`, `pyTelegramBotAPI` (`telebot`).
- Passwordless SSH (key-based) from the container to the Proxmox host, if you want to use `ups-emergency.sh` to shut down/restart other containers.
- A Telegram bot (created via [@BotFather](https://t.me/BotFather)) — the token is set from the dashboard, see [Connect Telegram](#connect-telegram).

## Installation

1. **Set the container's timezone.** All blackout timestamps (Telegram messages, the web history table) are generated from the system clock/timezone — there's no separate app-level setting for it. Freshly created containers/VMs are very often set to UTC by default, which silently offsets every logged time from your actual local time. Set it once, before anything else:
   ```bash
   timedatectl list-timezones | grep -i <your-city-or-region>   # find your zone name
   timedatectl set-timezone <Region/City>                        # e.g. Europe/Rome, America/New_York
   timedatectl status                                            # confirm it took
   ```
   This persists across reboots on its own (it just repoints `/etc/localtime`) — no further action needed. It only needs to be redone if the container is ever rebuilt from a fresh template. Using a named zone (not a fixed UTC offset) also means daylight saving changes are handled automatically from then on.
2. Install NUT and set up your UPS (see [USB passthrough](#usb-passthrough-proxmox-lxc) below if running inside a Proxmox LXC, then use the [Connect NUT](#connect-nut) dashboard button, or configure `ups.conf` manually with `nut-scanner -U`).
3. Copy the scripts:
   ```bash
   cp bin/ups-web.py bin/ups-notify.sh bin/ups-boot-check.sh bin/ups-emergency.sh /usr/local/bin/
   chmod +x /usr/local/bin/ups-*.sh
   ```
4. Copy the example config (the Telegram token can be left empty — it's set from the dashboard):
   ```bash
   cp config/nutcontroller.conf.example /etc/nut/nutcontroller.conf
   ```
5. Install the systemd units:
   ```bash
   cp systemd/ups-web.service systemd/ups-boot-check.service /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now ups-web.service
   systemctl enable ups-boot-check.service   # oneshot, runs automatically on boot
   ```
6. Wire `ups-notify.sh` into `upsmon` in `/etc/nut/upsmon.conf`:
   ```
   NOTIFYCMD /usr/local/bin/ups-notify.sh
   NOTIFYFLAG ONLINE  SYSLOG+WALL+EXEC
   NOTIFYFLAG ONBATT  SYSLOG+WALL+EXEC
   NOTIFYFLAG LOWBATT SYSLOG+WALL+EXEC
   ```
7. (Optional) Add `ups-emergency.sh` to cron for protecting the other containers:
   ```
   * * * * * /usr/local/bin/ups-emergency.sh
   ```
8. Open `http://<container-ip>/` and connect Telegram and NUT from the two buttons in the top-right corner.

## USB passthrough (Proxmox LXC)

A UPS talks USB HID directly to the `usbhid-ups` driver via `libusb` — it isn't a standard kernel HID input device, so Proxmox's usual "USB device" GUI passthrough (meant for VMs) doesn't apply the same way to containers. For an **LXC container**, the UPS's USB device node just needs to be bind-mounted into the container with the right cgroup permissions. This is what makes `upsc`/`usbhid-ups` inside the container able to see the UPS at all — if you're setting this up from scratch and can't find the UPS from inside the container, this is almost certainly the missing piece.

1. **On the Proxmox host** (not inside the container), plug in the UPS and find its USB bus/device numbers:
   ```bash
   lsusb
   # Bus 001 Device 002: ID 0764:0601 Cyber Power System, Inc. PR1500LCDRT2U UPS
   ```
   Note the `Bus 001 Device 002` values — they map to `/dev/bus/usb/001/002` on the host.
2. **Edit the container's config file on the host**, `/etc/pve/lxc/<CTID>.conf` (this file lives on the Proxmox host filesystem, *not* inside the container), and add:
   ```
   lxc.cgroup2.devices.allow: c 189:* rwm
   lxc.mount.entry: /dev/bus/usb/001/002 dev/bus/usb/001/002 none bind,optional,create=file
   ```
   - `lxc.cgroup2.devices.allow: c 189:* rwm` grants the container read/write/mknod access to USB device nodes (major number `189` is the kernel's `usbfs` device class).
   - `lxc.mount.entry: ...` bind-mounts the specific device node from the host into the same path inside the container.

   This is the exact configuration used in the reference installation (CT 111, `/etc/pve/lxc/111.conf`).
3. **Restart the container**: `pct reboot <CTID>`.
4. **Verify from inside the container**:
   ```bash
   ls -l /dev/bus/usb/001/002       # the device node should exist
   nut-scanner -U                    # should detect the UPS over USB
   ```
5. Configure `/etc/nut/ups.conf` (`driver = usbhid-ups`, `port = auto` works with libusb regardless of the exact bus/device path) — or just use the **Connect NUT** button in the dashboard, which runs `nut-scanner -U` for you and writes the config.

**Caveats:**
- The container in the reference setup is a **privileged** LXC (no `unprivileged: 1` in its config). Unprivileged containers typically need extra `lxc.idmap` mapping for device passthrough to work the same way.
- Bus/device numbers (`001/002`) can change if the UPS is unplugged and replugged into a different physical port, or if other USB devices are added/removed on the host. If the dashboard suddenly can't see the UPS after a host reboot or a cable change, re-run `lsusb` on the host and update the `lxc.mount.entry` line accordingly.
- `port = auto` in `ups.conf` lets `usbhid-ups` find the device by USB vendor/product ID rather than by a fixed path, which is more robust across reconnects than pinning an exact device path there — the passthrough line in `lxc.conf` is what needs the bus/device numbers, not `ups.conf` itself.

## Configuration

Behavior is centralized in `/etc/nut/nutcontroller.conf` (`KEY=value` pairs, see `config/nutcontroller.conf.example`):

| Key | Meaning |
|---|---|
| `BOT_TOKEN`, `CHAT_ID` | Telegram bot credentials. Set from the UI, no need to edit the file by hand. |
| `PROXMOX_HOST` | IP of the Proxmox host used to shut down/restart the other CTs in an emergency. |
| `NUT_CT_ID` | ID of the CT running NutController itself (excluded from emergency shutdowns). |
| `WEB_URL` | Dashboard URL, included in Telegram messages. |
| `BATTERY_VOLTAGE`, `BATTERY_AH`, `BATTERY_EFFICIENCY`, `LOAD_WATTS` | Battery parameters, used as a fallback when the UPS doesn't report direct data. |
| `BATTERY_RUNTIME_FULL` | Seconds of runtime at full charge, as reported by the UPS: used to scale the runtime bar in the dashboard (0–100%). |
| `THRESHOLD_RUNTIME_LOW` | Below this estimated runtime (seconds), emergency shutdown of the other CTs kicks in. Must stay above the UPS's own `battery.runtime.low`, otherwise the UPS forces its own shutdown (FSD) before the other CTs are shut down in order. |
| `THRESHOLD_RUNTIME_RESTORE` | Above this estimated runtime, the other CTs are automatically powered back on once power is restored. |

`BOT_TOKEN`/`CHAT_ID` and the emergency parameters are rewritten automatically in this file when you set them from the dashboard's guided settings — the rest of the file (comments, other keys) is left untouched.

## Web dashboard

`http://<container-ip>/` (Flask, port 80, served by `ups-web.service`):

- Real-time UPS status (online / on battery / low battery / charging) with estimated runtime from `battery.runtime`.
- Load, estimated power draw (`ups.realpower.nominal × load%`), system uptime.
- Historical charts (battery charge, power draw) over 1h to 1 year ranges, with CSV export.
- Outage history with stats (total count, average duration, worst event, last event) and a sortable table.
- Three settings buttons in the top-right corner, below the last-update timestamp: **Connect Telegram**, **Connect NUT**, and **UPS emergency**.

## Guided settings

All three buttons in the header are red when not connected/configured, green when everything works (amber for **UPS emergency** while an emergency shutdown is actually in progress), and open a modal with guided configuration — so you never have to edit config files by hand.

### Connect Telegram

1. Create a bot with [@BotFather](https://t.me/BotFather) on Telegram and copy its token.
2. In the dashboard, click **Connect Telegram** → paste the token → **Connect**.
3. The server validates the token (`getMe`) and listens (`getUpdates`) for up to 2 minutes.
4. Open Telegram, find the bot you just created and send it any message (e.g. `/start`): the `chat_id` is detected automatically — no need to look it up by hand.
5. A **Save** button appears: from that point on, the token and chat id are written to `nutcontroller.conf` and the command bot (`/stats`, `/history`) restarts with the new credentials.

**Disconnect** removes the credentials and stops the bot. If you connect a different bot while one is already active, the previous one keeps running until you press "Save" on the new one — cancelling an in-progress link resumes the original bot automatically.

### Connect NUT

Built for the "I don't remember how I configured this" scenario: no need to hand-edit `ups.conf` or remember driver/port names.

1. Click **Connect NUT**: the modal shows the current state (driver, port, whether the UPS responds).
2. **Scan for connected UPS (USB)** runs a **read-only** USB scan (`nut-scanner -U`) — it changes nothing, it just lists what it finds with suggested driver/port.
3. Pick the detected device (values are pre-filled but editable) and press **Save and restart driver**.
4. The server writes `driver`/`port`/`desc` into the `[myups]` stanza of `/etc/nut/ups.conf` and restarts **only** `nut-driver@myups` (not `upsd`/`upsmon`): a few seconds of monitoring downtime, then the dashboard turns green again once the UPS responds.

If the UPS isn't detected at all, see [USB passthrough](#usb-passthrough-proxmox-lxc) — the container most likely can't see the USB device yet.

Reference NUT setup for this installation (for anyone rebuilding it by hand): USB-attached UPS, `driver = usbhid-ups`, `port = auto`, device name `myups`, `nut.conf` set to `MODE=standalone`, `upsd.users` with an `admin` user in `master` mode used by `upsmon.conf` (`MONITOR myups@localhost 1 admin <password> master`).

### UPS emergency

Configures, from the UI, the parameters `ups-emergency.sh` uses to decide when to shut down (and later restart) all the other Proxmox containers (see [Emergency shutdown logic](#emergency-shutdown-logic)), without hand-editing `nutcontroller.conf`:

- **Proxmox host** and **this CT's ID** (excluded from shutdown).
- **Shutdown threshold** and **restore threshold**, in seconds of estimated runtime.
- **Test SSH connection**: a read-only check (`pct list` over SSH) that confirms access works and previews which CTs would be shut down — without shutting anything down.
- The header button turns amber whenever an emergency is actually in progress (other CTs already shut down, waiting for power to come back).

Unlike Telegram/NUT, saving here **doesn't restart any service**: `ups-emergency.sh` re-reads `nutcontroller.conf` on every run (cron, every minute), so new values take effect from the next run. If the chosen shutdown threshold is too low (at or below the UPS's own `battery.runtime.low`), the save still succeeds but the dashboard shows a warning, because the UPS could force its own shutdown before the other CTs are shut down in order.

## Telegram bot

Available commands (only responds to the linked `chat_id`):

- `/start`, `/help` — welcome message and link to the dashboard.
- `/stats` — current status, charge, runtime, power draw.
- `/history` — last 20 recorded outages.

Automatic notifications (outage start/end, low battery, etc.) come from `ups-notify.sh` (the `upsmon` `NOTIFYCMD` hook) and `ups-boot-check.sh` (if the container itself rebooted during an outage that was still ongoing).

## Emergency shutdown logic

`ups-emergency.sh`, run from cron every minute:

- If estimated runtime (`battery.runtime`) drops below `THRESHOLD_RUNTIME_LOW`, or the UPS reports `LB` (low battery), it shuts down every Proxmox CT except `NUT_CT_ID` over SSH (`pct shutdown`).
- Once runtime climbs back above `THRESHOLD_RUNTIME_RESTORE`, it powers the shut-down CTs back on.
- Emergency state is tracked in `/var/lib/nut/emergency.state` so actions aren't repeated.

`battery.charge` (the percentage) on some UPS units updates its estimate in jumps and isn't reliable in real time — that's why the emergency thresholds use `battery.runtime` (seconds), reported directly by the UPS, instead of a theoretical Ah/W calculation.

## State and log files

All under `/var/lib/nut/` (excluded from the repository — see `.gitignore` — because they're runtime state, not code):

| File | Contents |
|---|---|
| `blackout.flag` | Unix timestamp of outage start, present only while an outage is ongoing. |
| `blackout.log` | Text history of all events (`INIZIO`/`FINE blackout`), used by the dashboard and bot. |
| `metrics.jsonl` | One JSON line per minute (battery charge, load%, power draw), feeds the historical charts. |
| `emergency.state` | Present while an emergency shutdown is active (CTs shut down, waiting to be restarted). |

## Repository layout

```
nutcontroller/
├── bin/
│   ├── ups-web.py           # Flask dashboard + Telegram bot + guided-settings API
│   ├── ups-notify.sh        # upsmon NOTIFYCMD hook: Telegram notifications on UPS events
│   ├── ups-boot-check.sh    # Runs on boot: notifies if the reboot happened during an outage
│   └── ups-emergency.sh     # Cron job: shuts down/restarts the other Proxmox CTs based on runtime
├── systemd/
│   ├── ups-web.service
│   └── ups-boot-check.service
├── config/
│   └── nutcontroller.conf.example   # Secret-free template: copy to /etc/nut/nutcontroller.conf
├── .gitignore
├── LICENSE
└── README.md
```

## Security

- The dashboard and its API (including the Telegram/NUT/emergency guided-settings endpoints) have **no authentication**: it's built for a local/trusted network. If you expose it beyond your LAN, put it behind a reverse proxy with authentication, or a VPN.
- `nutcontroller.conf` stores the Telegram bot token in plaintext: don't commit it (already excluded via `.gitignore`), and restrict its permissions (`chmod 600`) on multi-user systems.
- Emergency shutdown (`ups-emergency.sh`) uses SSH to the Proxmox host: make sure the key it uses has only the permissions it needs (`pct shutdown`/`pct start`), not full root access if you can avoid it.

## Known limitations

- `battery.charge` on some UPS units (especially "budget" firmware) stays pinned at 100% in float mode even when the battery isn't fully charged: the dashboard shows a more realistic post-outage estimate when available, but the instant value should be taken with a grain of salt.
- Restarting the driver from **Connect NUT** interrupts UPS readings for a few seconds: if a real outage happens in that exact window, the event might not be recorded correctly.
- Only a single UPS device (`myups`) is supported by the dashboard; multi-UPS setups require manual edits to `ups.conf`.

## License

[MIT](LICENSE) — see the `LICENSE` file.
