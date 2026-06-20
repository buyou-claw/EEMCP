# Joulescope JS220 MCP Server (PoC)

An MCP server that exposes a **Joulescope JS220** precision energy
analyzer as 26 atomic tools for Claude. Pairs with `mcp_34465a` (DMM)
and `mcp_8852b` (BT tester) for the CMT BLE Sticker HWDVT line:

| MCP | Instrument | Role |
|---|---|---|
| `mcp_34465a` | Keysight 34465A | DC voltage / current / resistance |
| `mcp_8852b`  | Anritsu MT8852B | BT RF power / freq / sensitivity |
| `mcp_js220`  | Joulescope JS220 | Current, voltage, power, energy over time |

> **All capture runs go through the two-step interactive preflight flow**
> (see [Interactive preflight](#interactive-preflight)). The pre-existing
> `js220_configure_for_test(..., apply=True)` flow has no "are you sure?"
> gate — preflight adds it.

## Table of contents

- [Architecture](#architecture)
- [Default instrument](#default-instrument)
- [Setup](#setup)
- [Connection](#connection)
- [Claude Desktop config](#claude-desktop-config)
- [Tools](#tools)
- [Result envelope](#result-envelope)
- [Gotchas and lessons learned](#gotchas-and-lessons-learned)
- [Known PoC limitations](#known-poc-limitations)
- [Test the PoC](#test-the-poc)
- [Reference documents](#reference-documents)
- [See also](#see-also)

## Architecture

```
mcp_js220/
├── __init__.py        # package marker (__version__)
├── config.py          # env vars, JS220Config, VALID_* (sampling/range/io_voltage),
│                      #   DEFAULT_LIMITS, PREFLIGHT_PRESETS, PREFLIGHT_I_RANGE_TABLE
├── scpi.py            # parameter validators (no I/O; SCPI-equivalent indirection)
├── instrument.py      # JS220 — joulescope API wrapper, owns the Device
├── state_machine.py   # DISCONNECTED → IDLE → STREAMING guard (State, StateMachine)
├── server.py          # FastMCP entry; defines 26 @mcp.tool functions
├── scripts/           # power profile, pre-flight check (see scripts/README.md)
└── reference/         # JS220 user guides (PDFs)
```

**Layering** (each layer only talks to the one below):

```
server.py         @mcp.tool()  ──┐  user-facing; never touches joulescope directly
                                │
state_machine.py  guard       ──┤  lifecycle legality (can't start() from STREAMING)
                                │
instrument.py     JS220       ──┤  joulescope Device wrapper, stats callback mgmt
                                │
scpi.py           validators  ──┤  pure functions: param name in, canonical value out
                                │
config.py         constants   ───  env-var-overridable dataclass + preset tables
```

**Per-capture lifecycle** (typical one-shot current measurement):

```
js220_connect(...)        ──►  open joulescope Device, cache IDN
  │
  └─► js220_preflight_capture(...)            ◄── step 1: propose, NO USB
        │  (returns needs_user_input=true + proposed_config + questions)
        │  Pure offline: uses PREFLIGHT_PRESETS + PREFLIGHT_I_RANGE_TABLE
        ▼  user reviews + answers
        │
      js220_run_preflight_capture(..., user_confirmed=True)
        │  ──► js.set_io_voltage(io_voltage)
        │  ──► js.set_sampling_frequency(sampling_frequency)
        │  ──► js.set_reduction_frequency(reduction_frequency)
        │  ──► js.set_current_range(i_range)
        │  ──► _one_shot_measure("current","avg", duration_s, "CURR:AVG","A", lower, upper)
        │         └──► js.start() → sleep duration_s → js.stop()
        │         └──► statistics_callback snapshots into a thread-locked list
        │  ──► _envelope("CURR:AVG", avg, "A", lower, upper)  → JSON
        │
  └─► js220_disconnect()     ──►  force-stop first (safe from any state)
```

The two-step gate (preflight → `user_confirmed`) is enforced in
`server.py`, not `instrument.py`, so the instrument layer stays a
thin joulescope wrapper and the policy lives in one obvious place.

**Why `state_machine.py` exists (unlike `mcp_34465a`)**: the JS220 has
a real multi-step lifecycle (connect → configure → start streaming →
collect stats → stop streaming) with rules about which steps are legal
from which state. The state machine makes those rules explicit so the
MCP tools can call illegal-transition errors instead of silently
producing garbage. States: `DISCONNECTED` → `IDLE` → `STREAMING`,
plus `ERROR` for recovery. `ERROR` can only exit via `js220_reset()`.

## Default instrument

The default behaviour is to take `scan()[0]` — the first JS220 the
driver enumerates over USB. To pin a specific unit:

```bash
# By full path
set JS220_DEVICE_PATH=u/js220/005742
# Or by serial suffix
set JS220_DEVICE_PATH=005742
```

The bench unit shipped in the project so far is `JS220-005742`.
Override per-call with `js220_connect(device_path="u/js220/005742")`.

## Setup

```bash
pip install joulescope fastmcp
# pyvisa is NOT required — the JS220 uses the joulescope package's
# native USB driver. NI-VISA is unnecessary.
```

`joulescope` pulls in `pyjoulescope_driver` (Cython USB backend) and
`pyjls` (Joulescope Logfile Stream format) transitively.

## Connection

```bash
# Pin by serial
set JS220_DEVICE_PATH=005742
# Or accept scan()[0]
unset JS220_DEVICE_PATH
```

Per-call override: `js220_connect(device_path="u/js220/005742")`.

## Claude Desktop config

```json
{
  "mcpServers": {
    "js220": {
      "command": "python",
      "args": ["-m", "mcp_js220.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    }
  }
}
```

## Tools

**9 MCP-exposed tools + 17 library-only atomic tools** (26 functions
total, all preserved as plain Python for script import).

> **User-interaction gate.** The MCP channel exposes **only**
> `js220_preflight_capture` / `js220_run_preflight_capture` for running
> any capture. Every atomic write / measure tool
> (`js220_set_*`, `js220_measure_*`, `js220_run_power_profile`, ...)
> is reachable only via Python import from `mcp_js220.server`
> (intended for batch scripts). This is enforced — see
> [The MCP user-interaction gate](#the-mcp-user-interaction-gate)
> and `verify_mcpjs220_gate.py`.

### MCP-exposed tools (9)

These are the only tools Claude can call via MCP. Anything else in
the package is library-only.

#### Connection / lifecycle (MCP-exposed)

| Tool | What it does |
|---|---|
| `js220_list_devices()` | List the JS220s currently visible to `scan()` |
| `js220_connect(device_path?)` | Open the device; returns IDN + path |
| `js220_disconnect()` | Close the device (force-stops streaming first) |
| `js220_status()` | State, IDN, all key parameter values |
| `js220_reset()` | Disconnect + reconnect (soft reset, clears ERROR) |

#### Configuration discovery (MCP-exposed, read-only)

| Tool | What it does |
|---|---|
| `js220_get_capabilities()` | Enumerate ALL settable parameters, valid values, and built-in test presets |
| `js220_recommend_config(test_type, expected_current_a)` | Pick a starting config from test type + current estimate, with reasoning (does **not** apply) |

#### Interactive preflight (MCP-exposed, the only capture entry point)

| Tool | What it does |
|---|---|
| `js220_preflight_capture(...)` | Step 1 — propose sampling/reduction/range/duration/pass-window; returns `needs_user_input=True` with `questions` for `AskUserQuestion` (no USB). Pass `auto_confirm=True` to skip review. |
| `js220_run_preflight_capture(..., user_confirmed=True)` | Step 2 — apply user-confirmed config + run capture. Returns `{"ok": false, "error": "user_confirmed must be True..."}` **before** any connection check if `user_confirmed` is False. |

### Library-only atomic tools (17 — import directly)

These tools live in `mcp_js220.server` as plain Python functions but
have **no `@mcp.tool()` decorator**. Import them for batch scripts,
custom test runners, or recovery utilities:

```python
from mcp_js220.server import (
    # Configuration writers
    js220_configure_for_test,    # recommend + apply (legacy convenience)
    js220_set_source,            # "off" | "raw"
    js220_set_current_range,     # auto / 10 A / 2 A / 180 mA / ...
    js220_set_sampling_frequency, # 10 Hz .. 2 MHz
    js220_set_reduction_frequency,# 1 Hz .. 100 Hz
    js220_set_io_voltage,        # 1.8V .. 5.0V
    js220_clear_accumulators,    # reset charge/energy counters
    # Streaming lifecycle
    js220_start, js220_stop,
    js220_collect,               # stream + summary
    # One-shot measurements (return standard envelope)
    js220_measure_current_avg,
    js220_measure_voltage_avg,
    js220_measure_power_avg,
    js220_measure_current_peak,
    js220_measure_power_peak,
    # High-level
    js220_capture_window,        # stream + per-signal stats (no disk)
    js220_run_power_profile,     # long capture → reports/<label>/...
)
```

**Do not** add `@mcp.tool(...)` back to these without also adding a
propose/confirm gate — `verify_mcpjs220_gate.py` will fail.

### Interactive preflight — usage **[recommended MCP path]**

The preflight flow is the **only** way to run a capture via MCP. Two
steps, two tools, hard-gated by `user_confirmed`.

**Step 1 — propose (no USB sent)**:

```python
p = js220_preflight_capture(
    test_name="BLE_TX_BURST",
    test_type="ble_active_bursts",
    expected_current_a=0.05,
    peak_current_a=0.5,         # optional; defaults to expected * 10
    duration_s=60.0,
    io_voltage="3.3V",          # optional; defaults to 3.3V
    lower=0.0, upper=0.500,     # optional; falls back to DEFAULT_LIMITS['CURR:AVG']
    auto_confirm=False,         # default: pause for user review
)
```

Returns:

```json
{
  "ok": true,
  "needs_user_input": true,
  "interaction_type": "confirm_capture_setup",
  "test_name": "BLE_TX_BURST",
  "test_type": "ble_active_bursts",
  "proposed_config": {
    "sampling_frequency": "100 kHz",
    "reduction_frequency": "10 Hz",
    "i_range": "2 A",
    "io_voltage": "3.3V",
    "duration_s": 60.0,
    "lower": 0.0,
    "upper": 0.500
  },
  "rationale": "ble_active_bursts 默认 100kHz/10Hz 适合捕获 BLE 突发; peak 500 mA + 期望 50 mA → i_range=2A 给 5-10x headroom",
  "questions": [
    {"name": "test_type", "question": "测试类型?", "header": "Test type", "options": [...], "multiSelect": false},
    {"name": "i_range", "question": "电流档?", "header": "i_range", "options": [...], "multiSelect": false},
    {"name": "pass_window", "question": "CURR:AVG 合格区间?", "header": "Pass window", "options": [...], "multiSelect": false}
  ],
  "next_action": "把用户回答合并到 proposed_config, 再调 js220_run_preflight_capture"
}
```

The AI client should call `AskUserQuestion` with `questions` verbatim,
merge the answer back into `proposed_config`, then call step 2.

**Step 2 — apply + capture (gated)**:

```python
r = js220_run_preflight_capture(
    test_name="BLE_TX_BURST",
    test_type="ble_active_bursts",
    expected_current_a=0.05,
    peak_current_a=0.5,
    duration_s=60.0,
    io_voltage="3.3V",
    sampling_frequency="100 kHz",
    reduction_frequency="10 Hz",
    i_range="2 A",               # computed from expected/peak with 5-10x headroom
    lower=0.0, upper=0.500,
    user_confirmed=True,         # MUST be True; False returns error WITHOUT touching the bus
)
```

Returns the [Result envelope](#result-envelope) on the `CURR:AVG` signal.

**Bypass for batch / scripted runs**: pass `auto_confirm=True` to step 1.
The tool applies the proposed config and runs the capture in a single
call. Equivalent to calling `js220_configure_for_test(test_type,
expected_current_a, apply=True)` followed by
`js220_measure_current_avg(duration_s)`, but with a uniform envelope
and a single entry point.

**Hard constraints** (verified by `verify_preflight.py` smoke test):

- `js220_preflight_capture(..., auto_confirm=False)` makes **zero**
  calls to `set_*` / `start` / `stop` / `collect_for`. The device is
  genuinely untouched. (Propose can even be called pre-`js220_connect`.)
- `js220_run_preflight_capture(..., user_confirmed=False)` returns
  `{"ok": false, "error": "user_confirmed must be True..."}` **before**
  any connection check. The gate is the first thing the tool does.

**Preset data** (`config.PREFLIGHT_PRESETS`): five built-in test types
— `long_idle_log`, `ble_active_bursts`, `fine_peak_capture`,
`ultra_low_current`, `high_current_load`. Each preset bundles
sampling_frequency / reduction_frequency / i_range / io_voltage with
a use-when and a human-readable rationale. The preflight tool uses
the preset's sampling/reduction/io_voltage but re-checks `i_range`
against the operator's `expected_current_a` / `peak_current_a` using
the static `PREFLIGHT_I_RANGE_TABLE` to give 5-10× headroom. This
computation runs **offline** — it doesn't read any live parameter
from the device.

**i_range nudge formula** (in `server.py:_nudge_i_range`):

```
target = peak_current_a              if given
       else expected_current_a * 10
walk PREFLIGHT_I_RANGE_TABLE; first bucket where target < upper
```

The `* 10` rule mirrors the `js220_recommend_config` logic so the two
flows agree on what "good headroom" means.

## The MCP user-interaction gate

The MCP channel enforces **one rule**: an AI client cannot run a
capture without the user having reviewed the proposed config first.
This is the same hardcoded rule applied to `mcp_8852b` and
`mcp_34465a` — after MCP startup, the user is asked before any test
runs.

**How the gate is enforced for `mcp_js220`**: `@mcp.tool(...)` is
removed from every atomic write/measure function
(`js220_set_*`, `js220_measure_*`, `js220_run_power_profile`,
`js220_capture_window`, `js220_collect`, `js220_start`, `js220_stop`,
`js220_configure_for_test`). The only MCP-callable capture entry
points are `js220_preflight_capture` (propose) and
`js220_run_preflight_capture` (execute, hard-gated by
`user_confirmed=True`). Re-adding `@mcp.tool(...)` to a removed tool
without a paired gate will fail `verify_mcpjs220_gate.py`.

**The two-step preflight**:

1. `js220_preflight_capture(...)` — proposes a config + returns
   `questions` for `AskUserQuestion`. No USB sent (verified by
   `verify_preflight.py` test 4: zero `js.set_*` / `collect_for`
   calls). Set `auto_confirm=True` to skip the user review and
   execute in one call (batch / scripted run path).
2. `js220_run_preflight_capture(..., user_confirmed=True)` — applies
   the confirmed config and runs the capture. The first thing the
   function does is check `user_confirmed`; if False, it returns
   `{"ok": false, "error": "user_confirmed must be True..."}` before
   touching the bus (verified by `verify_preflight.py` test 5).

**Why this design and not a single point-of-no-return entry**: the JS220
has fine-grained per-test parameters (sampling freq, reduction freq,
i_range, io_voltage, pass window, peak vs avg). A single
"run the test" entry would have to bundle all of them into one form.
A two-step propose/confirm flow lets the user see and override each
parameter, while still making the review mandatory.

**Library-only path is preserved**: scripts and recovery utilities
that need batch mode still import the atomic Python functions
directly:

```python
from mcp_js220.server import js220_run_power_profile, js220_collect
# These bypass the MCP gate by design — they are only reachable to
# scripts that import the module, not to an AI client over MCP.
```

Run `python verify_mcpjs220_gate.py` to confirm:

- 17 atomic tools are absent from the MCP registry
- `js220_preflight_capture` still has `auto_confirm=False` default
- `js220_run_preflight_capture` still has `user_confirmed=False` default
- 9 MCP-exposed tools (5 connection + 2 read-only discovery + 2 preflight)
  are present
- All 26 functions remain importable + callable as plain Python

Run `python verify_preflight.py` to confirm the preflight gate logic
(propose / hard-gate / bypass / end-to-end / smoke) is intact
(8/8 tests).

## Result envelope

Every measurement tool returns the same shape so the AI can consume
results uniformly across instruments (matches `mcp_34465a` and
`mcp_8852b`):

```json
{
  "ok": true,
  "test": "CURR:AVG",
  "value": 0.012345,
  "unit": "A",
  "lower": 0.0,
  "upper": 0.5,
  "pass": true,
  "timestamp": "2026-06-15T11:45:25"
}
```

`pass` is `None` if no lower/upper is supplied AND the function has no
default window. Default windows are in `config.DEFAULT_LIMITS` and are
**product-line defaults** — tighten to your specific DUT spec, or pass
per-call `lower`/`upper` overrides.

## Gotchas and lessons learned

Empirical findings from building and validating the JS220 MCP. Read
before debugging anything.

### 1. The JS220 is not SCPI

The JS220 uses a private USB protocol wrapped by the official
`joulescope` Python package. There is no VISA resource string, no
`:MEAS:VOLT:DC?` style commands, no GPIB. **Do not** try to talk to
it with `pyvisa` — the driver just doesn't speak that language.

The package mirrors the SCPI pattern (one function per command in
`scpi.py`, a `Device` class for I/O in `instrument.py`) so the
maintenance pattern is the same, but the wire-level calls are
`parameter_set("source", "raw")`, `start()`, etc.

### 2. Statistics callbacks are on a USB processing thread

The `joulescope` driver delivers statistics on a background USB
thread. **Don't call into the device from a stats callback** — the
docs explicitly warn: "any calls back into self MUST BE
resynchronized." The package locks all access to the stats buffer
with a `threading.Lock` and never calls back into the device from a
callback. The user-facing tools (`js220_collect`,
`js220_run_power_profile`) all read via a snapshot.

### 3. Always throw away the first ~0.5 s

After `start()`, the JS220 needs ~5-10 ms to settle the current
range, and `reduction_frequency` ticks at e.g. 10 Hz arrive 100 ms
apart. The first few ticks have non-zero bias from the settling
transient. `instrument.JS220.start()` discards the first ~0.5 s of
stats as warmup. **Don't remove that warmup** unless you can prove
your DUT starts producing a clean signal faster than the warmup window.

### 4. `i_range = "off"` means no measurement

If `i_range` is set to `"off"`, the current sensor is disabled and
all current readings will be 0 (or sensor-noise). This is the
sensor's "safe" state when you're not actively measuring. The
package's `set_current_range` validator catches this as a string
choice — don't accidentally pass it. Default in `config.py` is
`"auto"`, which lets the JS220 down-range intelligently. Lock to a
fixed range only when you know the current is in a narrow band.

### 5. `sampling_frequency` ≠ `reduction_frequency`

- **`sampling_frequency`** — input ADC rate, up to 2 MHz. Affects
  the per-window min/max resolution (a 1 ms window at 2 MHz captures
  2000 samples, vs 10 samples at 10 kHz).
- **`reduction_frequency`** — how often the stats callback fires. The
  avg/std/min/max in each tick are computed *firmware-side* across
  the samples in the reduction window.

For a "see the BLE burst peak" capture, use HIGH `sampling_frequency`
(100 kHz..2 MHz) and LOW `reduction_frequency` (1..2 Hz) so each
stats window contains many ADC samples. For a "track average current
over time" capture, use MODERATE `sampling_frequency` (10..100 kHz)
and HIGH `reduction_frequency` (10..100 Hz) so you get a smooth time
series.

### 6. USB is fragile under hot-unplug

The JS220 enumerates as `u/js220/<serial>`. If you yank the USB
cable mid-capture, the driver's USB processing thread will raise a
device-removed event. The `disconnect()` method is safe to call from
any state — it force-stops streaming first, then closes. If you get
`RuntimeError: device not found` mid-capture, the instrument is gone.
Replug and re-`js220_connect`.

### 7. `parameter_get` may return wrong dtype

In the version we tested (`joulescope==1.5.0`,
`pyjoulescope_driver==2.2.9`), the bare `parameter_get(name)` call
returns the string option (e.g. `"100 kHz"`), not the integer Hz.
The `dtype="actual"` path calls `_parameter_get_info` which is
**not implemented on the JS220 subclass** and raises
`AttributeError`. The package therefore always uses the string form
and lets the user pass it back unchanged to `parameter_set`.

### 8. The "actual" reduction rate is limited by the driver

Even if you set `reduction_frequency = "100 Hz"`, the actual rate
of `statistics_callback` invocations tops out around 85 Hz on a
fast USB host. The driver batches samples internally. Don't be
surprised when `js220_run_power_profile(rate_hz=100)` reports
`rate_hz_actual: 100` (the setting) but `n_samples < duration * 100`.

### 9. Accumulator semantics

`charge` and `energy` are *cumulative* since the last
`statistics_accumulators_clear()`. They monotonically increase while
streaming. To get "energy in this burst", call
`js220_clear_accumulators()` immediately before the burst and read
`energy_J` immediately after.

### 10. µ / ∫ / σ2 are unicode keys

The stats payload uses unicode keys for the same fields:

| English alias | Unicode | Meaning |
|---|---|---|
| `avg` | `µ` | mean |
| `std` | `σ2` | variance (driver quirk — `std` is also present and is the actual std) |
| `integral` | `∫` | integral (charge, C) |

The package extracts via the **english aliases** (`avg`, `std`, etc.)
which are guaranteed present in every stats tick.

### 11. Default limits are placeholders, not specs

`config.DEFAULT_LIMITS` are product-line defaults for the CMT BLE
Sticker PCBA. They are intentionally generous. Tighten to your
specific DUT spec in your test plan, or pass per-call `lower` /
`upper` overrides. **This is exactly why the
[preflight flow](#interactive-preflight) exists** — it forces the
operator to look at the proposed pass window before any capture runs.

### 12. Windows-specific: µ in pprint crashes the REPL

`pprint.pprint()` on a stats dict will hit
`UnicodeEncodeError: 'gbk' codec can't encode character '\xb5'`
when stdout is a Windows console in CP936 encoding. This is a REPL
display issue, not a data issue. The package's CSV writer and JSON
dumper both force `encoding="utf-8"`. The preflight `rationale`
field is UTF-8 clean when written to file or transmitted over
JSON-RPC. For interactive work in a regular `python.exe` REPL, run:

```bash
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
# ...or use `python -X utf8`
```

### 13. Pre-test checklist

- [ ] USB cable connected (use a short, shielded cable — the JS220
      is sensitive to radiated noise on the current shunt)
- [ ] `js220_list_devices()` returns your unit
- [ ] `js220_connect()` returns IDN with `serial_number` matching
      the label on the back
- [ ] `js220_set_io_voltage("3.3V")` matches the DUT rail
- [ ] `js220_set_current_range("auto")` for unknown currents, or
      a fixed range for known-bounded signals
- [ ] `js220_clear_accumulators()` if you need absolute charge/energy
- [ ] First capture is a known-bounded signal (e.g. a fixed resistor
      load) to sanity-check the wiring and range
- [ ] If running a real test, use the [preflight flow](#interactive-preflight)

### 14. Hard-kill behavior

`js220_run_power_profile` writes to `data.csv` with a background
flush thread (`f.flush()` + `os.fsync()`) every 0.5 s. A `Ctrl+C`
or `TaskStop` loses at most ~0.5 s of samples.

The `summary.json` and `report.md` are written only at the end of
the capture — they will be missing on hard-kill. To regenerate from
a partial `data.csv`, see the script's `_summarize_stats` and
`_write_markdown` helpers.

### 15. SCPI quick-reference (param names, not commands)

| What you want to do | `parameter_set("name", "value")` |
|---|---|
| Turn sensor on | `"source", "raw"` |
| Turn sensor off | `"source", "off"` |
| Lock 10 A range | `"i_range", "10 A"` |
| Auto-range | `"i_range", "auto"` |
| 100 kHz sampling | `"sampling_frequency", "100 kHz"` |
| 10 Hz stats rate | `"reduction_frequency", "10 Hz"` |
| 3.3 V GPI/O | `"io_voltage", "3.3V"` |
| Reset accumulators | (separate call: `statistics_accumulators_clear()`) |

## Known PoC limitations

- **One device at a time.** The package owns a single `js` instance.
  Add a session map keyed by `device_path` if you need parallelism.
- **Auto range settling**: the JS220 takes ~5-10 ms to down-range; the
  first ~0.5 s of every capture is discarded as warmup.
- **Stats callback thread safety**: all access to the stats buffer
  is locked, but the buffer is bounded (10k samples) — for long
  captures use `js220_run_power_profile` which streams to disk.
- **Pattern sources** (`pattern_usb`, `pattern_control`,
  `pattern_sensor`) are exposed via `set_source` for completeness
  but no tool wraps the resulting test waveforms.

## Test the PoC

```python
from mcp_js220.server import *
print(js220_list_devices())               # {'ok': True, 'devices': [...]}
print(js220_connect())                    # {'ok': True, 'state': 'IDLE', ...}
print(js220_status())                     # parameters
# Interactive: preflight first, then run
p = js220_preflight_capture("BLE_TX", "ble_active_bursts",
                            expected_current_a=0.05, duration_s=2.0)
# ... user reviews p["questions"], picks answers ...
print(js220_run_preflight_capture("BLE_TX", "ble_active_bursts",
        expected_current_a=0.05, peak_current_a=0.5, duration_s=2.0,
        io_voltage="3.3V", sampling_frequency="100 kHz",
        reduction_frequency="10 Hz", i_range="2 A",
        lower=0.0, upper=0.500, user_confirmed=True))
# Or quick: bypass review
print(js220_preflight_capture("BLE_TX", "ble_active_bursts",
        expected_current_a=0.05, duration_s=2.0, auto_confirm=True))
js220_disconnect()
```

## Reference documents

Datasheets, programmer's guides, and user guides in `reference/`. The
Joulescope Python API documentation is at
https://joulescope.readthedocs.io/.

Tested against `joulescope==1.5.0` and `pyjoulescope_driver==2.2.9`.

## See also

- [`../BUILDING_AN_INSTRUMENT_MCP.md`](../BUILDING_AN_INSTRUMENT_MCP.md) —
  project-level guide: package layout, state machines, pre-flight pattern,
  common gotchas. Read this when starting a new instrument MCP.
- [`../mcp_8852b/README.md`](../mcp_8852b/README.md) — BT tester MCP
  (the original envelope shape and pre-flight convention).
- [`../mcp_34465a/README.md`](../mcp_34465a/README.md) — DMM MCP;
  preflight is identical in shape, differs in test function templates.
- [`../verify_preflight.py`](../verify_preflight.py) — 8-test verifier
  for the interactive preflight flow on both `mcp_34465a` and `mcp_js220`.
