GARDEN SPRINKLER CONTROLLER
==========================

Release 1.1.0

Upgrading from 1.0.0: close the older app before enabling the service.
The desktop now requires the service. Configure relay/configuration
arguments on the service instead. Saved settings remain compatible.
Closing the desktop now leaves watering running.

Developed by Wally Horsman, 2026

Seven garden zones and an automatic channel 8 master pump, using the
Waveshare Modbus POE ETH Relay (B).

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

The desktop connects to a private same-user local socket. There is no
network control listener and no direct-hardware fallback.
Opening/reopening the desktop does not reset or interrupt the relays.

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

At service startup all eight outputs are commanded OFF, pump first.
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

Include BOTH files in backups. Missing schedules start disabled.
A missing pause file means not paused. Invalid files produce errors;
they are not silently overwritten. Writes use atomic replacement.
Older version 1 and 2 schedules are preserved and saved as version 3.

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

The compact opening window is 1180 x 760. Manual Watering and Test Zone
are side by side. STOP ALL and Save Settings stay fixed at the bottom.
At smaller heights (minimum 620), scroll the configuration panel.
Watering controls and Rain Pause remain outside the scrolling panel.

Select a garden zone (1-7), edit its Relay / Garden Name, and save.
Each name can have up to 40 characters.

Each weekday has four independent start/stop pairs, shown in muted
blue, green, amber and purple. Enter four-digit 24-hour times such as
0600 or 1620; 06:00 and 16:20 are also accepted.
Leave both fields blank to skip a window. Every used pair needs both
times. Stop must be later than start. Windows must be ordered without
overlaps and cannot cross midnight.

"Copy selected day's times to all days" copies all four pairs once.
Every day remains independently editable afterward.

Save Settings applies all garden zones and preserves the unused
channel 8 settings. Unsaved edits survive zone changes but do not alter
watering. Closing prompts to save/discard edits, not to stop the service.
Other desktop windows receive saved changes when they have no unsaved
edits. Stale saves are rejected; close/reopen that window before editing
again to avoid overwriting another window's changes.

Status dots:

  Red          Relay commanded/confirmed ON.
  Bright green Current time is in an enabled saved schedule, but relay
               is OFF (for example, its current window was stopped).
  Grey         No active schedule, disabled routine, or Rain Pause.
  Fault colour Status unknown; check the service and device.

Turn On Now runs for a maximum of 10 minutes. Its button becomes
STOP THIS ZONE while watering. Test Zone toggles ON/OFF and also has
a 10-minute maximum. Stop a manual/routine run before starting a test.

STOP THIS ZONE and STOP ALL suppress currently active schedule windows
until they end. Later windows still run. Suppression survives closing
the desktop, but not restarting the service/computer. Manual/test runs
can still be started unless Rain Pause is enabled.

RAIN PAUSE
----------

RAIN PAUSE immediately sends all eight outputs OFF and prevents
scheduled, manual, and test watering. It saves automatically without
saving or discarding schedule edits, and survives service/PC restarts.
Schedules remain unchanged. Turning pause OFF asks for confirmation;
an active saved schedule may start immediately for its remaining time.
Previous stop suppression still applies. Manual/test runs do not resume.

If the app loses its service connection, status becomes UNKNOWN and
watering controls are disabled. It retries the local connection every
second. The service may still be watering; do not assume outputs are OFF.
A timed-out command is not automatically replayed because it may already
have succeeded. Status polling reconciles the result.

CHANNEL 8 MASTER PUMP
---------------------

Channel 8 is labelled Master Pump, automatically ON whenever any garden
zone (1-7) is watering, including manual/test operation. It has no
independent schedule or manual/test controls.

The first valve opens before pump ON. Pump stays ON while any zone runs,
and stops before the last valve closes. Startup, STOP ALL, Rain Pause,
fault shutdown and normal service shutdown attempt pump OFF first,
then all seven valves. There are no valve-travel or pump delays.
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
