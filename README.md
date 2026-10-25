# Garden Sprinkler Controller

A dark-mode CustomTkinter desktop app for eight independently named sprinkler
zones, connected directly to the **Waveshare Modbus POE ETH Relay (B)**.
The relay box must be powered and reachable whenever the app is opened.

## Setup and launch

Requires Python **3.11 or newer**, Tk, and a graphical desktop session.
On Debian/Ubuntu, install the Python desktop/virtual-environment prerequisites:

```sh
sudo apt install python3-tk python3-venv
```

From this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python app.py
```

On Windows, use `.venv\Scripts\python` instead of `.venv/bin/python`. The standard
python.org installer includes Tk when the Tcl/Tk option is selected.

To choose a different settings location:

```sh
.venv/bin/python app.py --config ./settings.json
```

The default is `~/.config/garden-sprinkler-controller/settings.json`. Settings
are validated and written using atomic replacement. Missing settings start with
eight disabled routines. Invalid existing settings produce an error and are
never silently overwritten.

## Your Waveshare relay

The supplied device-settings screenshot confirms:

| Setting | Value |
| --- | --- |
| IP address | `192.168.0.99` |
| TCP port | `4196` |
| Work mode | TCP Server |
| Transfer Protocol | None (Modbus RTU-over-TCP, **not** Modbus TCP framing) |
| Internal serial configuration | 115200, 8 data bits, no parity, 1 stop bit |

Leave the board settings unchanged. Zone 1 maps to relay 1 (coil address 0),
through zone 8 / relay 8 (coil address 7).

The [manufacturer's protocol documentation](https://www.waveshare.com/wiki/Modbus_POE_ETH_Relay_(B))
shows slave address **1** in its examples. A read-only status request to your
relay at `192.168.0.99:4196` confirmed that slave ID **1** responds and all eight
relays reported **OFF**. No relay write commands were sent during this check.

First run a **read-only** status check from a computer on the same LAN. This
command needs only Python, not Tk or any third-party packages:

```sh
python3 waveshare_relay.py
```

If address 1 does not respond, optionally discover a responding slave:

```sh
python3 waveshare_relay.py --discover
```

Discovery only sends function 01 (read eight coil states), tries addresses 1–247,
and stops at the first valid response. It does not send writes or change settings.
It can take several minutes if the device is not responding. Cancel with Ctrl+C.
If no address responds, check network access, relay power, and port/protocol
settings; a failed scan does not establish that the slave ID is wrong.

Start the hardware-connected GUI:

```sh
.venv/bin/python app.py
```

Every app launch connects to `192.168.0.99`, port `4196`, slave ID `1`. Startup
sends OFF commands to all eight relays and verifies each state, then starts any
saved routines that are scheduled now. A routine can therefore energize a valve
immediately on launch. The **Read Relay Status** button queries and displays the
current physical states without changing them. Optional `--host`, `--port`,
`--slave-id`, and `--timeout` arguments override connection parameters.

There is no simulation mode. If the relay is unreachable, startup fails visibly;
the app does not substitute a fake backend.

The network adapter in [waveshare_relay.py](waveshare_relay.py) uses RTU CRC16,
checks response addresses, functions, CRCs, write echoes, and coil readback.
Requests use a fresh TCP connection and a bounded transaction timeout (default
1 second, configurable with `--timeout`, maximum 10). ON commands are never
retried or treated as successful after a timeout; fault handling attempts OFF
on all channels instead. Coil readback confirms the
device's reported logical state, **not** valve movement, water flow, or contact
integrity. Other programs and the board's digital inputs can change outputs;
use command-compatible, non-linkage relay modes and avoid competing controllers.

On a communication error, the app stops scheduling, attempts OFF on every
channel, displays the failure, and requires restart. Output state is labelled
unknown when a fault occurs.
STOP ALL remains available to retry OFF commands. Requests are synchronous:
the GUI may pause during network operations; a failed all-channel OFF sequence
can take up to eight transaction timeouts, plus connection overhead. A successful
write also requires a separate readback transaction.

## Using the controller

- Select each relay/zone on the left, edit its **Relay / Garden Name** at the
  top of the settings panel (up to 40 characters), then press **Save Settings**.
  The custom name appears beside that relay in the left-hand list and is saved
  separately for each relay.
- Enable its routine, then set up to **two independent watering windows for
  each weekday** in the daily schedule. Enter each start and stop in **24-hour
  HH:MM** format. Leave both fields blank to skip a window; if you enter one
  time, its matching start or stop is required. Window 2 must start at or after
  window 1 ends. Windows cannot cross midnight. Enabled routines need at least
  one complete time window; a different weekday can have different times.
- To reuse a day's schedule, choose that weekday in the menu above the schedule
  and check **Copy selected day's times to all days**. This copies both windows
  once; each day remains independently editable afterward.
- Older version 1 settings are migrated in memory: their existing selected
  weekdays and time window become window 1 for those days, with window 2 blank.
  The upgraded version 2 settings are written on the next save.
- Validation updates while typing. **Save Settings** saves and applies **all
  eight zones**. Unsaved edits survive switching between zones but do not alter
  the running schedule. Closing prompts to save or discard them.
- Red indicates a commanded/confirmed energized relay, blue an enabled
  schedule that is active at the current time, and grey no current watering
  window. Sidebar status uses the
  **saved/applied** routine, not unsaved edits.
- **Turn On Now** waters for **10 minutes**, regardless of whether a routine is
  enabled. Its button becomes **STOP THIS ZONE** whenever that zone is watering.
- **Test Zone** waters for exactly **60 seconds**. It is disabled while the
  selected zone is already running.
- **STOP THIS ZONE** and **STOP ALL** act immediately and suppress the currently
  active automatic watering window(s) until they end. Later scheduled windows
  that day are unaffected. Another manual/test run can still be started.
  Suppression is not persisted: restarting during a saved window starts that
  routine again.
- Manual/test watering replaces that zone's current scheduled run; when the
  timed override ends, the routine does not resume in the same window.
  Disabling a routine and saving stops its scheduled run, but does not cancel
  an independent manual/test run; use a stop button for that.

## Timing and operational limits

The app must remain open and the computer awake for schedules to run. It checks
outputs every 250 ms when idle; network operations can delay these checks.
Start/stop actions do not have real-time precision. It uses the operating system's **local time and
weekday**. Launching during a scheduled window runs only its remaining time;
missed windows are not replayed.

Routine runs end at their stop time or their originally calculated elapsed-time
deadline, whichever occurs first. Manual/test durations use a monotonic clock.
This prevents a backwards wall-clock adjustment from extending an active run.
Clock changes and daylight-saving transitions can shorten or skip watering; this
is not a timezone-aware scheduling service. Multiple zones may run simultaneously;
there is no pump interlock, sequential queuing, or flow/pressure monitoring.

Normal window closure sends OFF to all eight outputs. A killed process, network
failure, sleeping computer, or frozen event loop cannot guarantee shutdown.
**Timers are software-only; they do not program a hardware auto-off or watchdog.**
Use normally-open contacts and an independently designed hardware cutoff. Test
with valve power disconnected first; verify correct channel mapping, command
mode, readback, and shutdown before connecting actual valves. Never rely on the
app alone to prevent flooding or protect a pump.

## Code layout

The UI and scheduling code use the `RelayBackend` protocol in
[controller.py](controller.py).

- [app.py](app.py): dark-mode desktop interface and hardware launch.
- [controller.py](controller.py): schedule engine, timed overrides, JSON persistence.
- [waveshare_relay.py](waveshare_relay.py): RTU-over-TCP adapter and read-only
  status/discovery command.
- [test_controller.py](test_controller.py) and
  [test_waveshare_relay.py](test_waveshare_relay.py): automated tests.

## Tests

The core and network adapter have no third-party dependencies and can be tested without Tk or a
display:

```sh
python3 -m unittest -v
```

Tests cover validation, configuration round-trips and failure preservation,
schedule boundaries, weekdays, exact manual/test deadlines, override
suppression, clock rollback, independent zones, and shutdown behavior. Protocol
tests cover vendor CRC examples, fragmented responses, malformed frames, device
exceptions, timeout errors, write/readback validation, and read-only discovery.
A loopback TCP server tests controller startup, manual operation, and shutdown
over real local sockets. Tests do not contact the physical relay board.
