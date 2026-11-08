GARDEN SPRINKLER CONTROLLER
==========================

Developed by Wally Horsman, 2026

Seven garden zones and an automatic channel 8 master pump, using the
Waveshare Modbus POE ETH Relay (B). An optional second board provides
independent channels 9-16, with its own IP address.

OPTIONAL SECOND BOARD
---------------------

Use the Channels 1-8 / Channels 9-16 selector to change banks.
Channels 9-16 stay disabled until a second compatible board is configured.
Without it, the first board operates as before; no second-board requests
are sent.

Set the second board to a different IP address. Check it read-only:

  .venv/bin/python waveshare_relay.py --host 192.168.0.100

Replace the example IP with the real address. Configure using:

  systemctl --user edit garden-sprinkler.service

Add:

  [Service]
  ExecStart=
  ExecStart=%h/Documents/garden-sprinkler-controller/.venv/bin/python %h/Documents/garden-sprinkler-controller/service.py --second-host 192.168.0.100
  TimeoutStopSec=60

Keep any existing primary-board or configuration arguments. Optional
--second-port, --second-slave-id and --second-timeout default to 4196,
1 and 1 second independently of the first board.

  systemctl --user daemon-reload
  systemctl --user restart garden-sprinkler.service
  systemctl --user status garden-sprinkler.service --no-pager

Reopen the desktop. Channels 9-16 map to second-board physical relays 1-8.
They have schedules, manual controls, autosave and Rain Pause exemptions.
They never activate channel 8; the master pump serves channels 1-7 only.
New channels start with disabled schedules. All settings now save as
version 7, including master pump exclusions; older versions still load.

Before removing the second board's configuration, stop the service while
both boards are reachable to switch outputs OFF. Remove --second-*
arguments and restart. Second-board schedules are preserved but ignored
while unconfigured; re-enabling can resume current saved schedules.
Removing configuration cannot switch OFF a disconnected board.
A configured board failure stops scheduling and attempts OFF on both
boards; it is not silently treated as an unconfigured second board.

BACKGROUND OPERATION
--------------------

The controller now runs as a Linux background service. It starts at boot,
continues while logged out, and does not need the desktop window open.
Open the desktop app whenever you want to see status or change settings.
Closing the window does NOT stop watering. Use RAIN PAUSE or STOP ALL.
Only the service communicates with the relay board.

Requirements: Linux with systemd and Python 3.11 or newer. The desktop
also needs Tk and CustomTkinter. The service needs no graphical display.

INSTALLATION
------------

Close any older direct-hardware version of the sprinkler app first.
The supplied unit expects the project at:

  ~/Documents/garden-sprinkler-controller

From the project folder:

  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  mkdir -p ~/.config/systemd/user
  cp garden-sprinkler.service ~/.config/systemd/user/
  loginctl enable-linger "$USER"
  systemctl --user daemon-reload
  systemctl --user enable --now garden-sprinkler.service

Enabling linger may need administrator approval on another machine.
It allows your service manager to start before login and survive logout.
Do not move/delete the project or virtual environment while installed.

Open the desktop:

  .venv/bin/python app.py

The desktop connects to a private same-user local socket and has no network
listener. An optional phone web interface is described below.
Opening/reopening the desktop does not reset or interrupt the relays.

PHONE CONTROL OVER TAILSCALE (OPTIONAL)
--------------------------------------

An optional phone-friendly web page can show channel status, run a channel
manually for 10 minutes, stop a channel, or stop all watering. The web server
uses the controller's private local socket and only binds to 127.0.0.1.
It cannot be opened directly to the LAN or internet.

Install and sign in to Tailscale on the sprinkler computer and Android phone
using your tailnet account. Tailscale Serve requires HTTPS certificates for
the tailnet; its first-use flow can prompt you to enable them. Restrict tailnet
access to people and devices you trust.

From the project folder:

  mkdir -p ~/.config/garden-sprinkler-controller ~/.config/systemd/user
  chmod 700 ~/.config/garden-sprinkler-controller
  python3 -c 'import secrets; print(secrets.token_urlsafe(36))' > ~/.config/garden-sprinkler-controller/phone-password
  chmod 600 ~/.config/garden-sprinkler-controller/phone-password
  cat ~/.config/garden-sprinkler-controller/phone-password
  cp garden-sprinkler-phone.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now garden-sprinkler-phone.service

Save the generated password in a password manager and keep it private. It
must be at least 4 characters and the password file must have mode 0600.
A long password is recommended. A short PIN is easier to guess and should
only be used with trusted Tailscale devices. Five incorrect sign-ins within
five minutes block further sign-ins until those failures expire. This limit
is shared across all phones; signed-in sessions continue to work.
To change the password, edit the phone-password file, retain mode 0600, and
restart garden-sprinkler-phone.service. Existing phone sessions are signed
out, but the sprinkler controller is not restarted.
Then publish the local web server:

  tailscale serve --bg 8765
  tailscale serve status

Open the HTTPS tailnet URL shown by the status command in Chrome on the phone
while Tailscale is connected, and enter the phone-control password. Do not use
Tailscale Funnel or router port forwarding. Channel 8's master pump remains
automatic. To disable remote access:

  tailscale serve reset
  systemctl --user disable --now garden-sprinkler-phone.service

Check status:

  systemctl --user status garden-sprinkler.service

Read recent service errors:

  journalctl --user -u garden-sprinkler.service -n 50 --no-pager

Restart after checking and correcting a hardware fault:

  systemctl --user restart garden-sprinkler.service

Disable automatic startup and stop the service:

  systemctl --user disable --now garden-sprinkler.service

POWER FAILURE AND RECOVERY
--------------------------

The enabled service starts when the machine boots, even before login.
Failed startup, such as waiting for the network or relay box, retries
every 15 seconds. A runtime hardware fault stops scheduling, attempts
all outputs OFF, logs the fault, and requires a service restart.
It does not repeatedly retry ON commands after a runtime fault.

At service startup all configured outputs are commanded OFF, pump first.
Active saved schedules then resume for their remaining time unless
Rain Pause is enabled. Missed watering windows are not replayed.
Interrupted manual/test runs do not restart.

The computer must have power, network access, and remain awake.
This installation does not change system sleep or firmware settings.
If necessary, enable "Restore on AC Power Loss" or "AC Back" in the
computer's BIOS/UEFI so it powers up when mains power returns.
Software cannot switch on a powered-off computer.

SETTINGS
--------

Default schedule file:

  ~/.config/garden-sprinkler-controller/settings.json

Rain Pause is saved separately beside it:

  settings.json.rain-pause.json

Named timetable profiles are also saved separately:

  settings.json.timetables.json

Include all three files in backups or when moving computers.
Missing schedules start disabled; a missing profile file means no profiles.
A missing pause file means not paused. Invalid files produce errors;
they are not silently overwritten. Writes use atomic replacement.
Older version 1, 2, 3 and 4 settings are migrated; existing zones remain
included in Rain Pause. Versions 5 and 6 also load with their existing
settings preserved. Pump participation remains unchanged on migration;
version 7 settings are written on the next save.

The service owns settings writes. To change configuration paths or
relay parameters, use:

  systemctl --user edit garden-sprinkler.service

Override ExecStart, clearing its previous value first with ExecStart=.
The --config, --host, --port, --slave-id and --timeout arguments belong
to service.py, not app.py. Restart the service after changes.
For foreground diagnosis, stop the installed service before running:

  .venv/bin/python service.py

The desktop accepts --socket for an alternate local service endpoint.

USING THE DESKTOP
-----------------

Daily Schedule, above the editor, opens a weekday table of all
configured garden periods sorted by start time. It includes unsaved
edits, clearly labelled, and excludes the master pump. Disabled routines
are marked and can be hidden. Select a row to open that relay's settings.
Amber warnings flag identical starts and overlapping periods across
enabled relays. Adjacent end/start times do not overlap. Warnings are
advisory and do not block saving: when two enabled, unpaused schedules
overlap, both relays can run at the same time. Watering is not queued
sequentially; the shared master pump runs for their combined demand.
Invalid/incomplete pairs remain visible with a warning; they cannot be
fully checked for conflicts. Rain Pause and stale service status are
labelled. The table shows configured times, not current relay outputs.

Weekly Overview, beside Daily Schedule, shows all garden relays and all
seven weekdays at once. Each cell lists four numbered start-stop pairs;
unused periods show --. Bright red text and ! flag same-start or overlapping
periods on both affected relays; red cells flag invalid/incomplete pairs.
Click a garden or cell to open its settings.
Both tables can stay open together and update as you edit. Both can hide
disabled routines and label unsaved edits. Conflicts compare the same
weekday across enabled garden relays, not different days.

The compact opening window is 940 x 760, with a minimum size of 940 x 620.
One RUN ZONE / STOP THIS ZONE button handles timed watering and stopping.
STOP ALL and Save Settings stay fixed at the bottom. At smaller heights,
scroll the configuration panel.
Watering controls and Rain Pause remain outside the scrolling panel.

Select a garden zone (1-7) and edit its Relay / Garden Name. Valid edits
save automatically about one second after you stop editing; Save Settings
saves immediately.
Each name can have up to 40 characters.

Each weekday has four independent start/stop pairs, shown in muted
blue, green, amber and purple. Enter 24-hour times as HMM (815), HHMM
(0815), or HH:MM (08:15). Times display as HH:MM.
Leave both fields blank to skip a window. Every used pair needs both
times. Stop must be later than start. Enter windows in any numbered order,
but do not overlap windows within one zone; windows cannot cross midnight.
Click a Start heading to pause/resume that window number for the selected zone
across all weekdays. The smaller Paused marker appears below paused headings.
Valid changes save automatically about one second after you stop editing.
Incomplete or invalid settings are not saved; fix the validation issue to
resume autosaving. Red borders identify overlap fields. Clicking a
conflicting Stop field opens an acknowledgment popup naming both groups.

"Copy selected day's times to all days" copies all four pairs once.
Every day remains independently editable afterward.

Autosaving applies all garden zones and preserves the unused channel 8
settings. Save Settings remains available to save immediately. Closing
prompts to save/discard any remaining unsaved changes, not to stop the
service. Other desktop windows receive saved changes when they have no
unsaved edits. Stale saves are rejected; close/reopen that window before
editing again to avoid overwriting another window's changes.

Status dots:

  Red          Relay commanded/confirmed ON.
  Bright green Current time is in an enabled saved schedule, but relay
               is OFF (for example, its current window was stopped).
  Grey         No active schedule, disabled routine, or Rain Pause.
  Fault colour Status unknown; check the service and device.

RUN ZONE waters the selected zone for up to 10 minutes, even if its
routine is disabled. Its button becomes STOP THIS ZONE while watering.

STOP THIS ZONE and STOP ALL suppress currently active schedule windows
until they end. Later windows still run. Suppression survives closing
the desktop, but not restarting the service/computer. Manual runs can
still be started for zones exempt from Rain Pause.

RAIN PAUSE
----------

Rain Pause stops scheduled and manual watering for included zones.
Click + beside a zone number to exempt it; an X marks an exempt zone
and turns red while Rain Pause is on. Exempt zones keep their schedules
and allow manual watering during Rain Pause. Click X again to include
the zone. Zone exemptions save automatically with other valid settings.
Rain Pause itself saves immediately; pending valid edits are saved first,
and invalid edits must be fixed before enabling Rain Pause. Pause state
survives service/PC restarts. Turning pause OFF asks for confirmation;
an active saved schedule may start immediately for its remaining time.
Previous stop suppression still applies. Manual/test runs do not resume.

If the app loses its service connection, status becomes UNKNOWN and
watering controls are disabled. It retries the local connection every
second. The service may still be watering; do not assume outputs are OFF.
A timed-out command is not automatically replayed because it may already
have succeeded. Status polling reconciles the result.

SEASONAL TIMETABLE PROFILES
---------------------------

Open Timetables and choose Save current as... to save Summer, Winter,
or another name (1-40 characters). Replacing an existing profile asks
for confirmation. Valid pending edits are saved first; invalid edits
must be fixed before saving or loading.

Profiles store every channel's weekday times, routine enabled state,
and paused-window flags. Zone names, board configuration, Rain Pause
and pump exclusions, and global Rain Pause remain unchanged.

Choose a profile, click Load selected timetable, and confirm.
Settings are saved and applied immediately: scheduled relays may start
or stop. Existing manual/test runs retain their timers.
Before last load automatically stores the previous timetable.
Loading this recovery profile swaps it with the current timetable,
allowing you to undo the load.

The Timetable button, footer and dialog show the selected profile and mark it modified
when its timetable changes. Long names are shortened on the button;
the dialog shows the full name. Autosave never overwrites named profiles;
save explicitly under the same name to update one.
Profiles must match the stored channel count (8 or 16), not just the
configured hardware count. Preserved channel 8 schedules and disabled
second-board schedules are included but remain inactive.
Seasonal switching is manual; there is no automatic date-based change.
A corrupt profile file prevents startup instead of discarding profiles.

CHANNEL 8 MASTER PUMP
---------------------

The P control beside each zone's Rain Pause control toggles master pump
participation for channels 1-7. P means uses pump; P- means excluded,
with saved exclusions highlighted amber. Changes autosave.
An excluded zone still runs on schedule or manually, but does not start
or keep the pump running. Other non-excluded running zones still use it.
Changing only this setting during a run does not restart or stop the zone.
It is independent of Rain Pause. Channel 8 and channels 9-16 have no
editable pump control; channels 9-16 already operate independently.

Channel 8 is labelled Master Pump, automatically ON whenever any garden
zone (1-7) that is not pump-excluded is watering, including manual/test operation. It has no
independent schedule or watering controls.

The first valve opens before pump ON. Pump stays ON while any zone runs,
and stops before the last valve on channels 1-7 closes. Startup, STOP ALL,
fault shutdown and normal service shutdown attempt pump OFF first,
then all configured relays. Rain Pause keeps the pump on only for running
Rain Pause-exempt, pump-using zones on channels 1-7.
There are no valve-travel or pump delays.
Command order does not prove a physical valve has finished moving.

The old channel 8 name and schedule are preserved but ignored.
To restore eight independent garden zones:

  1. Stop the service.
  2. In controller.py, set MASTER_PUMP_CHANNEL = None.
  3. Restart the service and reopen the desktop.

The nearby code comment explains this setting. The original scheduling
and editing logic remains available. Review the old channel 8 routine
before restoring it; an enabled active routine can start immediately.

HARDWARE AND SAFETY
-------------------

Confirmed device parameters:

  IP address       192.168.0.99
  TCP port         4196
  Work mode        TCP Server
  Transfer mode    None (Modbus RTU-over-TCP, NOT Modbus TCP framing)
  Slave ID         1
  Internal serial  115200 baud, 8 data bits, no parity, 1 stop bit

Zones 1-7 use coils 0-6. Master pump uses coil 7.
The adapter checks CRC, response address/function, write echoes and
coil readback. ON commands are never automatically retried.
Readback confirms logical relay state, NOT valve movement or water flow.
Do not use competing controllers or incompatible board linkage modes.

Optional read-only checks (no writes):

  python3 waveshare_relay.py
  python3 waveshare_relay.py --discover

Discovery tries slave IDs 1-247 and stops at the first valid response.
It may take several minutes; cancel with Ctrl+C.

Scheduling uses the computer's local time/day, checked every 250 ms
when idle. Network/IPC operations can delay checks; this is not real
time. Manual/test durations use a monotonic clock.
Clock adjustments and daylight-saving changes can shorten/skip runs.
Multiple zones may water together; there is no sequential queue or
flow/pressure monitoring.

Normal service shutdown sends all outputs OFF. A killed service,
network failure, sleeping PC or frozen process cannot guarantee OFF.
Timers are SOFTWARE ONLY, not a hardware watchdog.
Use normally-open contacts and an independent hardware cutoff.
Check channel mapping and shutdown with valve power disconnected first.
Never rely on this app alone to prevent flooding or protect a pump.
Use suitably rated switching/contactors for pump starting current.
Have mains wiring and protection checked by a qualified electrician.
Independent dry-run, pressure and overload protection may be needed.

TESTS
-----

Core, transport and service tests (no display needed):

  python3 -m unittest -v test_controller test_waveshare_relay test_service

All tests, including the desktop (Tk/CustomTkinter and display needed):

  .venv/bin/python -m unittest -v

Tests use fake hardware and local loopback/Unix sockets. They do not
operate the physical relay board.
