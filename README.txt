GARDEN SPRINKLER CONTROLLER
==========================

Version 1.3.0

Developed by Wally Horsman, 2026

Seven garden zones and an automatic channel 8 master pump, using the
Waveshare Modbus POE ETH Relay (B). An optional second board provides
independent channels 9-16, with its own IP address.
Alternatively, channels 9-16 can use individual Tapo P110M Wi-Fi plugs.

OPTIONAL TAPO P110M WI-FI PLUGS
------------------------------

The existing Waveshare board keeps channels 1-8. Up to eight P110M plugs
can control independent channels 9-16 directly over local Wi-Fi, without
Home Assistant, Zigbee or a Matter controller. This uses the community
python-kasa library, not a TP-Link-supported API. Its tested-device list
includes P110M AU hardware 1.0 / firmware 1.2.3; arriving devices still
need testing. Energy monitoring and Matter pairing are not integrated.

Prepare before delivery, from this project folder:

  .venv/bin/python -m pip install -r requirements.txt
  .venv/bin/python tapo_plugs.py prepare
  systemctl --user restart garden-sprinkler.service garden-sprinkler-phone.service

Reopen the desktop or refresh the phone. New channels 9-13 are named
Tapo Plug 1 through Tapo Plug 5, with schedules disabled; 14-16 are spare.
Existing names/settings are preserved. All slots remain Unassigned and
cannot switch or run schedules until assigned. You can edit names,
schedules, exemptions, limits and reminders beforehand. Unassigned does
not mean successfully switched OFF. This option and the second Waveshare
board are mutually exclusive.

When the plugs arrive:

1. Pair each in the Tapo app on Wi-Fi reachable from this computer.
   Avoid guest/client isolation. Disable competing native schedules and
   automations, including Matter automations.
2. Enter TP-Link credentials locally, never in chat:

     .venv/bin/python tapo_plugs.py credentials
     .venv/bin/python tapo_plugs.py discover

   Password entry is hidden. Discovery reads only; it never switches.
   Use discover --target <LAN-broadcast-address> for another interface.
   If authentication fails, check any Tapo third-party/local-access setting.
3. Reserve each plug's IPv4 address in your router. Discovery reports IP,
   MAC and name. Assign unique real IP/MAC values, with the service stopped:

     systemctl --user stop garden-sprinkler.service
     .venv/bin/python tapo_plugs.py assign 9 192.168.0.50 12:34:56:78:9a:bc
     .venv/bin/python tapo_plugs.py show
     systemctl --user start garden-sprinkler.service

   Replace the example identifiers. Repeat assignment for channels 10-13
   while stopped. Startup attempts every assigned output OFF; active saved
   schedules may then start. Test first with no equipment attached.
4. Test each actual plug before attaching a load. Observe Australian load
   and motor/inrush ratings. Independent channels never start the sprinkler
   pump. Until stopped does not need a pump exclusion on channels 9-16,
   but manual maximums and Rain Pause rules still apply.

Each switch checks MAC and P110M model first, then verifies state readback.
Switch writes are not retried. Runtime does not rediscover changed IPs;
reserve addresses. Normal status shows controller runs/last requested state,
not continuous monitoring of physical-button or other-app changes.
Desktop Read Relay Status makes a fresh query. Plug state is not proof
that a connected appliance is operating or electrically isolated.

Mappings live in settings.json.tapo.json; private account credentials in
settings.json.tapo-credentials.json beside the main settings. Credentials
require current-user ownership and no group/world access (normally 0600).
Neither file is in Git or ordinary phone/service status replies. Full-system
backups include both only inside the password-encrypted archive.
Protect and back them up separately. prepare preserves assignments.
Credential changes need a controller restart. Use --config before the
subcommand if the service uses an alternate settings path.

Stop the service with the old plug reachable before reassignment or:

  .venv/bin/python tapo_plugs.py unassign 9

Unassigning cannot switch a disconnected plug OFF. Assigned plug failures
stop ALL scheduling and attempt all outputs OFF, including the first
board. An unreachable assigned plug can prevent startup. OFF cannot be
guaranteed if Wi-Fi/controller power fails: this is not a safety interlock.
Network operations are bounded and sequential, not hard real-time.
After expansion, export a new 16-channel backup; eight-channel backups
are rejected for mismatched count. Existing eight-channel timetable
profiles remain usable in the channels 1-8 group.

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
version 9, including master pump exclusions, manual limits and continuous-run
reminders; older versions still load.

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
for a selected duration or continuously until stopped, stop a channel, or stop all watering. The web server
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

Select 10 minutes, 30 minutes, 1 hour, or 1-1440 custom minutes, then press RUN.
The default is 10 minutes. A saved per-channel maximum rejects longer runs.
RUN UNTIL STOPPED asks for confirmation and has no automatic time limit.
While active, its button turns blue and says "Now running until stopped".
Use the separate STOP button to end the run.
It requires no per-channel manual maximum. Channels 1-7 also need master
pump exclusion saved on the computer; independent channels 9-16 do not.
The controller enforces these requirements. Removing a primary channel's
exclusion stops its continuous run instead of starting the pump. Rain Pause exemption is a
separate setting; it is required only when Rain Pause is enabled.
Closing the phone page, signing out, or disconnecting Tailscale does not
stop it. Use STOP or STOP ALL. Both run modes retain saved pump exclusions
and Rain Pause exemptions. Non-exempt channels stop when Rain Pause is
enabled. Continuous runs are not saved or resumed on controller restart;
saved schedules may still start independently. Hardware faults still stop
scheduling and attempt OFF. If remote access is lost, use desktop controls.

After updating, restart the controller and phone services and reopen the
desktop app. Restarting attempts to switch all outputs OFF; active schedules
may start again, but manual runs do not resume. Choose a safe time. Manual
timers require the updated controller. Version 8 settings cannot be read by
older controllers. Protocol 2 uses null remaining time for continuous runs; old
desktop processes will report an incompatible service until reopened.
The continuous phone button is disabled until the controller is updated.

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

Activity history is kept separately as settings.json.activity.sqlite3.
Include these files in backups or when moving computers.
Missing schedules start disabled; a missing profile file means no profiles.
A missing pause file means not paused. Invalid files produce errors;
they are not silently overwritten. Writes use atomic replacement.
Older version 1, 2, 3 and 4 settings are migrated; existing zones remain
included in Rain Pause. Versions 5 and 6 also load with their existing
settings preserved. Pump participation remains unchanged on migration;
version 7 also loads with no manual limit. Version 8 loads with reminders off.
Version 9 settings are written on the next save.

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

Daily Schedule, above the editor, opens a weekday table of the selected
bank's configured garden periods sorted by start time. It includes unsaved
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

Weekly Overview, beside Daily Schedule, shows the selected bank's garden relays and all
seven weekdays at once. Each cell lists four numbered start-stop pairs;
unused periods show --. Bright red text and ! flag same-start or overlapping
periods on both affected relays; red cells flag invalid/incomplete pairs.
Click a garden or cell to open its settings.
Both tables can stay open together and update as you edit. Both can hide
disabled routines and label unsaved edits. Conflicts compare the same
weekday across enabled garden relays, not different days.
Both overviews follow Channels 1-8 / Channels 9-16, even while already
open, and identify the displayed bank in their summary. Channel numbers
and navigation retain their original identities. Conflict warnings still
compare all configured enabled garden relays, including the other bank.

The compact opening window is 940 x 760, with a minimum size of 940 x 620.
One RUN ZONE / STOP THIS ZONE button handles timed watering and stopping.
STOP ALL and Save Settings stay fixed at the bottom. At smaller heights,
scroll the configuration panel using the mouse wheel over the panel
(including on Linux) or the side scrollbar.
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
Each Start heading has its own Not Paused / Paused button directly below it.
Paused has a red background and pauses that numbered Start/Stop group for
the selected zone across all weekdays, preserving its times. Other groups,
other zones and manual runs are unaffected. Once applied, pausing stops a
scheduled run in that group. Unpausing skips the current period and waits
until its next scheduled start. Valid changes save automatically about one
second after you stop editing; Save Settings applies them immediately.
Group pauses are saved with schedules, backups and timetables and survive
restarts. There is no whole-zone pause button below RUN ZONE or on phone
channel cards. The former settings.json.zone-pauses.json sidecar is no
longer used. Restart the updated controller service and reopen the desktop.
Incomplete or invalid settings are not saved; fix the validation issue to
resume autosaving. Red borders identify overlap fields. Clicking a
conflicting Stop field opens an acknowledgment popup naming both groups.

"Copy selected day's times to all days" copies all four pairs once.
Every day remains independently editable afterward.

The zone editor opens in one day view, showing today's single row with all
four Start/Stop pairs. The weekday dropdown selects the displayed day and
the source day for copying. See week view displays all seven rows; See one
day view returns to the selected day. Your day and view selections stay
the same when switching zones during the current app session. Switching
days or views does not alter times, copy settings or trigger saving.
Hidden days are still validated and saved.
Switching channel banks also selects the first channel in the newly selected
configured bank, keeping the schedule editor and run controls on that bank.

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

Selected-channel status shows the run source, approximate end time, and next
saved, enabled, unpaused schedule start. The phone shows this per channel.
Next starts are predictions, not guarantees: Rain Pause, stop suppression,
faults, edits or another run can prevent them. Schedule times use the
controller's local time. Unavailable status is shown as unknown, not OFF.

Activity on the desktop and Activity history on the phone show recent
controller-reported starts, stops, reasons, Rain Pause, startup and faults.
Browse older pages or refresh. Newest events appear first. Dates and times use
DD/MM HH:MM in the controller's local time at the event. Channel events show
the channel number and name; system-wide events are labelled System.
The latest 1,000 events persist across restarts.
History begins after this version starts and cannot reconstruct older events.
It reports relay commands/readback, not physical lamp, fan or valve operation.
An error saving history is logged and shown as a warning; events can be missing
after a crash, disconnect or storage failure. Relay control continues. History
requires the background service and is unavailable in standalone desktop mode.

RUN ZONE runs the selected zone for 10 minutes, 30 minutes, one hour, or
1-1440 custom minutes, even if its routine is disabled. The duration selector
is to the left of RUN ZONE; selecting a duration does not start a run. For
Custom, enter the minutes in the box that appears, then press RUN ZONE. A
summary shows the selected duration before starting. The desktop Running
button opens a live list; the phone shows that list above its channels. These
are controller-reported states, not proof of physical device movement.
Backup creates a password-encrypted full sprinkler-system recovery file on
desktop and phone. It includes saved settings for both banks, group pauses,
all timetables and selections, Rain Pause, relay connections, Tapo assignments
and credentials, the standard phone-password beside settings, activity history,
installed user service units/drop-ins, requirements and source fingerprints.
Save pending edits first: unsaved edits and active runs are not backed up.
This is not an Ubuntu image. Reinstall the app and its requirements on a
replacement computer. OS packages, app binaries, network/router settings,
TLS/Tailscale setup and unrelated secrets need separate recovery.

Choose a separate backup password (12-1024 characters); several unrelated
words work well. Enter it twice and use Show password to check it. This is
not your Tapo or phone sign-in password. Keep the password in a password
manager or a safe place separate from the backup. Lost backup passwords
cannot be recovered. Keep a copy of the .sprinkler-backup file on another
device. Encryption uses AES-256-GCM and scrypt (N=32768, r=8, p=1); backup
files and restored secrets are user-only (0600).

Restore asks for that password, validates all sections and previews contents
before confirmation. Wrong passwords, corruption and stale revisions change
nothing. Old settings-only unencrypted JSON exports are not full backups;
keep them for manual settings recovery.
An encrypted before-restore recovery file is saved beside settings using
the SAME password as the backup being restored. Outputs are stopped before
applying changes. A persistent restore safety hold blocks ALL scheduled and
manual operation, including Rain Pause exemptions. Write failures leave
the hold active and report a fault.

After restore, restart both services and reopen the desktop:
  systemctl --user restart garden-sprinkler.service garden-sprinkler-phone.service
Restarting does not clear the hold. Review devices, schedules and Rain Pause;
then select Backup > Resume operation after restore, or Resume operation
on the phone. This may start current schedules immediately. Old manual
runs/countdowns are never restored.

Restored .connections.json relay settings override command-line relay flags.
Service units are reference copies, NOT automatically installed/executed.
Review installation paths, usernames, sockets, environment overrides and
ports on a replacement computer. The .restored-service-config/RECOVERY.txt
guide, unit copies and version information are saved alongside settings.
The standard phone-password is restored; sign in with the password stored
in that backup after restarting phone service. Custom --password-file
locations outside the settings directory are not collected: copy the
restored standard password file there or adjust the flag. Custom external
secret files and network configuration need separate recovery.
Phone session cookies are not backed up.

If the controller cannot start, install the app dependencies, stop the
controller service, and use offline recovery:
  systemctl --user stop garden-sprinkler.service
  .venv/bin/python system_backup.py restore /path/to/file.sprinkler-backup --config /path/to/settings.json
The password is entered without echoing. Type RESTORE to confirm.
The tool refuses a configuration owned by a running controller and keeps
the safety hold active. Offline recovery does not create another recovery
archive: keep the original and the automatic before-restore backup.
Use inspect instead of restore to check a backup without changing settings.
Activity history can be filtered by channel, system or faults; older years
appear with the date. Lost connection means output states are unknown.
An optional per-channel continuous-run reminder of 1-1440 minutes appears
on screen while the interface is open. It does not stop equipment or alert
you when the app or phone page is closed. Other run types do not trigger it.
Until stopped requires confirmation, and channels 1-7 also need a saved
pump exclusion. Independent channels 9-16 do not. Its button becomes STOP THIS ZONE
while running. Below the schedule, an optional 1-1440 minute maximum limits
manual/test runs, not schedules, and disables Until stopped. Save the limit
before starting a run. Adding or lowering a limit stops an active manual,
test or continuous run. Timetable profiles do not change the limit.

STOP THIS ZONE and STOP ALL suppress currently active schedule windows
until they end. Later windows still run. Suppression survives closing
the desktop, but not restarting the service/computer. Manual runs can
still be started for zones exempt from Rain Pause.

RAIN PAUSE
----------

Rain Pause stops scheduled and manual watering for included zones.
Click R beside a zone number to exempt it; R- marks an exempt zone
and turns red while Rain Pause is on. Exempt zones keep their schedules
and allow manual watering during Rain Pause. Click R- again to include
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
for confirmation. The window follows Channels 1-8 / Channels 9-16 and
labels its group. Saving/loading affects ONLY that group's eight channels.
The other group's saved schedules, selected profile and recovery are unchanged.
Both groups can use the same names independently. Valid pending edits in
the selected group are saved first; invalid edits there must be fixed.
Other-bank pending edits remain untouched (ordinary autosave still applies).

Profiles store the selected group's weekday times, routine enabled state,
and paused-window flags. Zone names, board configuration, Rain Pause
and pump exclusions, and global Rain Pause remain unchanged.

Choose a profile, click Load selected timetable, and confirm.
Settings are saved and applied immediately: scheduled relays may start
or stop. Existing manual/test runs retain their timers.
Before last load automatically stores this group's previous timetable.
Loading this recovery profile swaps it with the group's current timetable,
allowing you to undo the load.

The Timetable button, footer and dialog show the selected profile and mark it modified
when its timetable changes. Long names are shortened on the button;
the dialog shows the full name. Autosave never overwrites named profiles;
save explicitly under the same name to update one.
Preserved channel 8 schedules and disabled
second-board schedules are included but remain inactive.
Seasonal switching is manual; there is no automatic date-based change.
A corrupt profile file prevents startup instead of discarding profiles.
Legacy eight-channel profiles belong to channels 1-8. Legacy sixteen-channel
profiles split into both groups with names preserved. The next profile
save/load writes version 2; older apps/services cannot read that format.
Update app and service together. Full-system backups cover both banks
and include the separate named-profile file.

CHANNEL 8 MASTER PUMP
---------------------

The P control beside each zone's Rain Pause control toggles master pump
participation for channels 1-7. P means uses pump; P- means excluded,
with saved exclusions highlighted red. Changes autosave.
The selected channel panel also shows labelled pump and Rain Pause switches
below the schedule, their effects, and a summary; pending exclusion edits
are marked unsaved.
Channels 9-16 show pump independence rather than a pump switch.
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
