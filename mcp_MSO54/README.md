# mcp_MSO54 — Tektronix 5 Series MSO Oscilloscope MCP

FastMCP server that exposes the bench MSO54 (Tektronix, FW 1.34.8.1078,
USBTMC `USB0::0x0699::0x0522::C041623::0::INSTR`) as a tool surface.

> **Looking for how to measure ripple / noise voltage correctly?**
> See [`MEASUREMENT_GUIDE.md`](./MEASUREMENT_GUIDE.md) — required scope
> settings, MCP code patterns, and the `9.91e37` / `CURVE?` traps.
>
> **Looking for bus-protocol lessons learned?** See [`NOTES.md`](./NOTES.md)
> — the four USBTMC wedge triggers, auto-scale override, etc.

Every tool was validated on the real instrument on **2026-07-18** via
`scripts/validate.py` (phases P1–P8 + P10 + P11, all passing in ~9 s)
and `scripts/verify.py` (7 structural + 7 smoke tests, all green).
Tools not exercised in validation are deliberately not exposed.

---

## Quick start (60 seconds)

```bash
# 1. Install Python deps (Windows; NI-VISA runtime must already be on PATH)
pip install -r mcp_MSO54/requirements.txt

# 2. Verify the package structure + connect to your bench scope
python -m mcp_MSO54.scripts.verify
# Expects: "ALL CHECKS PASSED" (14/14)

# 3. Run the full validation suite against the bench
python -m mcp_MSO54.scripts.validate
# Expects: P1-P8 + P10 + P11 all PASS in ~9 s

# 4. Try a real measurement: CH1 voltage snapshot (probe already on CH1)
python -m mcp_MSO54.scripts.ch1_voltage_report

# 5. Launch the MCP server
python -m mcp_MSO54.server
# or equivalently:
python -m mcp_MSO54
```

---

## Layering (per `BUILDING_AN_INSTRUMENT_MCP.md` §1)

```
server.py     # FastMCP @mcp.tool() definitions — the user-facing surface
   ↓
instrument.py # the ONLY file that imports pyvisa
   ↓
scpi.py       # pure-string command builders (no I/O); strict write/query split
   ↓
config.py     # env-var-overridable dataclass + MEAS_TYPES / MEAS_UNIT / DEFAULT_LIMITS
```

`server.py` never imports pyvisa. All communication with the scope goes
through `MSO54`, whose `write()` and `query()` enforce the write/query
separation at runtime (see **Safety, §1** below).

**Single VISA session at a time**: opening a second `MSO54` while one
is connected will likely wedge the USBTMC bulk-IN endpoint — keep one
process at a time, same as the 34465A rule (see `CLAUDE.md`).

---

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `MSO54_RESOURCE` | `USB0::0x0699::0x0522::C041623::0::INSTR` | VISA resource string |
| `MSO54_TIMEOUT_MS` | `10000` | Per-query timeout |
| `MSO54_CURVE_TIMEOUT_MS` | `30000` | Timeout for CURVE? / large reads |
| `MSO54_RESET_SETTLE_S` | `2.0` | Settle time after *RST / FACTORY / AUTOSET |
| `MSO54_NUM_CHANNELS` | `4` | Analog channel count (MSO54 = 4) |

LAN alternative (unverified on this bench): `TCPIP0::192.168.1.50::4000::SOCKET`.

---

## Tool reference (23 tools)

### Connection & status
| Tool | Notes |
|---|---|
| `scope_connect(resource=None)` | Open session + send `HEADER OFF` |
| `scope_disconnect()` | Close session |
| `scope_status()` | Returns idn, options, STB |
| `scope_reset()` | `*RST` |
| `scope_autoset()` | `AUTOSET EXECUTE` |

### Channel (vertical) — read
| Tool | Notes |
|---|---|
| `scope_get_channel_state(channel)` | All vertical settings for one channel |

### Channel — write (all hard-gated by `user_confirmed=True`)
| Tool | Notes |
|---|---|
| `scope_set_channel_scale(channel, volts_per_div)` | See Safety §2: auto-scale override |
| `scope_set_channel_position(channel, divs)` | |
| `scope_set_channel_offset(channel, volts)` | |
| `scope_set_channel_coupling(channel, "DC"/"AC"/"DCREJECT")` | |
| `scope_set_channel_display(channel, on)` | No gate (low-risk) |
| `scope_set_channel_label(channel, text)` | |

### Horizontal
| Tool | Notes |
|---|---|
| `scope_get_horizontal_state()` | Read |
| `scope_set_horizontal_scale(sec_per_div)` | Write, gated |
| `scope_set_horizontal_position(percent)` | Write, gated |

### Acquisition
| Tool | Notes |
|---|---|
| `scope_get_acquisition_state()` | Read |
| `scope_acq_run()` / `scope_acq_stop()` | Required before user-set scale will persist |

### Trigger (A event, edge)
| Tool | Notes |
|---|---|
| `scope_get_trigger_state()` | Read |
| `scope_set_trigger_edge(source, slope, level_volts)` | Write, gated |

### Automatic measurements (two-step preflight)
| Tool | Notes |
|---|---|
| `scope_get_measurement_list()` | Read — returns populated slots |
| `scope_preflight_add_measurement(test_name, meas_type, source, lower, upper, auto_confirm)` | Step 1 — NO SCPI, returns questions |
| `scope_run_preflight_add_measurement(test_name, meas_type, source, lower, upper, user_confirmed)` | Step 2 — gated; adds + reads back live value |

### DVM (option SUP5-DVM, installed on this bench unit)
| Tool | Notes |
|---|---|
| `scope_read_dvm()` | Read mode/source/value |
| `scope_set_dvm(mode, source, user_confirmed)` | Write, gated |

### Waveform transfer (CURVE?)
| Tool | Notes |
|---|---|
| `scope_read_curve(source, start, stop, user_confirmed)` | Gated; stop-start+1 ≤ 10000; returns scaled `times`/`values` + full `preamble` |

### Screenshot
| Tool | Notes |
|---|---|
| `scope_save_image(instrument_path, user_confirmed)` | Write-only; **operator must pull file off manually** (READFILE is unsafe on USB — see Safety §1) |

---

## Safety

### §1 USBTMC bulk-IN lockup — four independent triggers

On the bench MSO54, four different command patterns wedge the USBTMC
bulk-IN endpoint. Once wedged: writes still work, but every read
returns `VI_ERROR_IO`. `viClear` reports success but doesn't recover
the IN pipe. See **NOTES.md §1** for the full incident log and the
memory entry `feedback_usbtmc_setonly_query.md`.

**Cheap recovery:** unplug + replug the USB cable (or disable+re-enable
the USB port in Device Manager). NI-VISA re-enumerates the device and
`*IDN?` works within seconds. Only fall back to a scope power-cycle if
USB-port reset fails.

### §2 MSO54 auto-scale override

While `ACQUIRE:STATE` is RUN, the scope continuously auto-adjusts
vertical scale to fit the waveform. Any user-written `CH<n>:SCALE`
value is overwritten within ~100 ms. **Workaround:** call
`scope_acq_stop()` before `scope_set_channel_scale()`, then
`scope_acq_run()` afterwards. Verified on FW 1.34.8.1078 (2026-07-18).

### §3 Unknown filesystem path conventions

Tek 5 Series MSO filesystem paths are not well documented. The naive
guess (`/tmp/...`) caused P9 to wedge the bus three times. Treat any
path string as unverified until you've confirmed it via the scope's
touchscreen file manager.

---

## Result envelope

All measurement tools return the standard 7-key shape from
`common/envelope.py`:

```json
{
  "ok": true,
  "test": "FREQUENCY",
  "value": 1.05e9,
  "unit": "Hz",
  "lower": 1.0e9,
  "upper": 1.1e9,
  "pass": true,
  "timestamp": "2026-07-18T16:14:23"
}
```

Plus extra keys on a per-tool basis (e.g. `slot`, `source`, `preamble`).

---

## Validation

Run `python -m mcp_MSO54.scripts.validate` from the project root.
Each phase is independently runnable via `--phase pN`. The script
must complete without raising for the package to be considered
working on real hardware.

| Phase | Validates |
|---|---|
| P1 | Connect, `*IDN?`, `*OPT?`, STB (control-endpoint read) |
| P2 | All read-only queries (channel / horizontal / acquisition / trigger / measurement / DVM / preamble) |
| P3 | `HEADER OFF` round-trip via `*IDN?` |
| P4 | `AUTOSET EXECUTE` + readback |
| P5 | `CH1:SCALE` write+readback (with `ACQUIRE:STATE STOP` workaround) |
| P6 | `TRIGGER:A:EDGE:*` write+readback |
| P7 | `MEASUREMENT:ADDMEAS` + `MEAS<n>:TYPE?` (never queries ADDMEAS itself) |
| P8 | DVM mode/source write + `DVM:MEASUREMENT:VALUE?` |
| ~~P9~~ | **Removed** — `FILESYSTEM:READFILE` wedges USBTMC bulk-IN |
| P10 | `CURVE?` end-to-end (1000 pts + preamble decode) |
| P11 | `SAVE:IMAGE` write-only safety check (instrument remains responsive) |
