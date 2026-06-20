# Keysight 34465A MCP Server (PoC)

An MCP server that exposes a Keysight 34465A 6½-digit digital multimeter
as 24 atomic tools for Claude. Pairs with `mcp_8852b` (BT test set) and
`mcp_js220` (energy analyzer) for the CMT BLE Sticker HWDVT line.

> **All test runs go through the two-step interactive preflight flow**
> (see [Interactive preflight](#interactive-preflight)). Never let the AI
> call a measure tool with no operator review of the pass window and
> NPLC — silent fallback to `DEFAULT_LIMITS` is what made the original
> pattern 不规范.

## Table of contents

- [Architecture](#architecture)
- [Default instrument](#default-instrument)
- [Setup](#setup)
- [Connection](#connection)
- [Claude Desktop config](#claude-desktop-config)
- [Tools](#tools)
- [The MCP user-interaction gate](#the-mcp-user-interaction-gate)
- [Result envelope](#result-envelope)
- [Gotchas and lessons learned](#gotchas-and-lessons-learned)
- [Known PoC limitations](#known-poc-limitations)
- [Test the PoC](#test-the-poc)
- [Reference documents](#reference-documents)
- [See also](#see-also)

## Architecture

```
mcp_34465a/
├── __init__.py        # package marker (__version__)
├── config.py          # VISA resource, timeouts, MEAS_FUNCTIONS, DEFAULT_LIMITS,
│                      #   DEFAULT_QUESTIONS_PER_FUNCTION
├── scpi.py            # one function per SCPI command (no I/O)
├── instrument.py      # DMM34465A — pyvisa wrapper, owns the session
├── server.py          # FastMCP entry; defines 24 @mcp.tool functions
├── scripts/           # ready-to-run bench scripts (see scripts/README.md)
└── reference/         # datasheets, programmer's guides, app notes
```

**Layering** (each layer only talks to the one below):

```
server.py       @mcp.tool()  ──┐  user-facing; never touches pyvisa
                              │
instrument.py   DMM34465A  ──┘  pyvisa wrapper; one open session
                              │
scpi.py         builders    ───  pure functions: SCPI string in, string out
                              │
config.py       constants   ───  env-var-overridable dataclass + limit tables
```

**Per-function lifecycle** (typical one-shot measurement):

```
dmm_connect()          ──►  open pyvisa session, *IDN? cached
  │
  └─► dmm_preflight_measure(...)     ◄── step 1: propose, NO SCPI
        │  (returns needs_user_input=true + proposed_config + questions)
        │
        ▼  user reviews + answers
        │
      dmm_run_preflight_measure(..., user_confirmed=True)
        │  ──► dmm.set_nplc(function, nplc)        (if nplc given)
        │  ──► dmm.set_resolution(function, res)   (if resolution given)
        │  ──► dmm.measure_<function>()            :MEAS:<FUNC>?
        │  ──► _envelope(test, value, unit, lower, upper)  → JSON
        │
  └─► dmm_disconnect()       ──►  close session
```

The two-step gate (preflight → `user_confirmed`) is enforced in
`server.py`, not `instrument.py`, so the instrument layer stays a
thin pyvisa wrapper and the policy lives in one obvious place.

## Default instrument

```
USB0::0x2A8D::0x0101::MY57515137::0::INSTR
```

- `0x2A8D` — Keysight Technologies (vendor ID)
- `0x0101` — 34465A (product ID)
- `MY57515137` — serial number
- `0` — USB interface index

Override with `DMM34465A_RESOURCE` env var or by passing `resource=` to
`dmm_connect`.

## Setup

```bash
pip install fastmcp pyvisa
# NI-VISA must be installed separately (download from NI website).
# Keysight IO Libraries Suite also works as a VISA backend.
```

## Connection

```bash
# USB (default — bench 34465A)
set DMM34465A_RESOURCE=USB0::0x2A8D::0x0101::MY57515137::0::INSTR
# LAN
set DMM34465A_RESOURCE=TCPIP0::192.168.1.50::5025::SOCKET
# GPIB
set DMM34465A_RESOURCE=GPIB0::17::INSTR

# Optional timeouts (ms)
set DMM34465A_TIMEOUT_MS=10000         # default per-query
set DMM34465A_TEMP_TIMEOUT_MS=30000    # temperature probes (slower)
```

## Claude Desktop config

```json
{
  "mcpServers": {
    "dmm34465a": {
      "command": "python",
      "args": ["-m", "mcp_34465a.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    }
  }
}
```

## Tools

The MCP channel exposes **6** tools — 4 connection / status tools plus
the 2 preflight tools that make up the user-interaction gate. All other
helpers (the 18 atomic measure / config / scan functions) are
**library-only**: they remain as plain Python functions on
`mcp_34465a.server` for scripts and tests to import, but they are NOT
decorated with `@mcp.tool()`, so FastMCP does not register them. See
[The MCP user-interaction gate](#the-mcp-user-interaction-gate) below
for why.

### Connection / status (MCP-exposed)

| Tool | What it does |
|---|---|
| `dmm_connect(resource?)` | Open VISA session; returns `*IDN?` |
| `dmm_disconnect()` | Close session |
| `dmm_status()` | Connection state + cached IDN |
| `dmm_reset()` | `*RST` to power-on defaults |

### Test entry points (MCP-exposed, gated) **[required for any test]**

| Tool | Role |
|---|---|
| `dmm_preflight_measure(...)` | Step 1: propose config + ask user via `needs_user_input=true` |
| `dmm_run_preflight_measure(..., user_confirmed=True)` | Step 2: apply config + execute. **Hard-gated** by `user_confirmed`. |

See [Interactive preflight](#interactive-preflight) for the full flow.

### Library-only helpers (NOT exposed via MCP)

These remain on `mcp_34465a.server` for scripts and `verify_preflight.py`
to call directly:

**One-shot measurements** (return the [Result envelope](#result-envelope)):

| Function | SCPI | Unit |
|---|---|---|
| `dmm_measure_volt_dc(lower?, upper?)` | `:MEAS:VOLT:DC?` | V |
| `dmm_measure_volt_ac(lower?, upper?)` | `:MEAS:VOLT:AC?` | V (RMS) |
| `dmm_measure_curr_dc(lower?, upper?)` | `:MEAS:CURR:DC?` | A |
| `dmm_measure_curr_ac(lower?, upper?)` | `:MEAS:CURR:AC?` | A (RMS) |
| `dmm_measure_resistance(lower?, upper?)` | `:MEAS:RES?` | Ω (2-wire) |
| `dmm_measure_resistance_4w(lower?, upper?)` | `:MEAS:FRES?` | Ω (4-wire) |
| `dmm_measure_frequency(lower?, upper?)` | `:MEAS:FREQ?` | Hz |
| `dmm_measure_period(lower?, upper?)` | `:MEAS:PER?` | s |
| `dmm_measure_continuity(lower?, upper?)` | `:MEAS:CONT?` | Ω |
| `dmm_measure_diode(lower?, upper?)` | `:MEAS:DIOD?` | V |
| `dmm_measure_capacitance(lower?, upper?)` | `:MEAS:CAP?` | F |
| `dmm_measure_temperature(probe?, lower?, upper?)` | `:MEAS:TEMP:RTD/THER/TC?` | °C |
| `dmm_measure(function, lower?, upper?)` | any of the above by name | (auto) |

**Configuration writes**:

| Function | What it does |
|---|---|
| `dmm_set_nplc(function, nplc)` | Integration time, 0.001..1000 |
| `dmm_set_resolution(function, digits)` | Display resolution, 4..10 digits |
| `dmm_set_continuity_threshold(ohms)` | 1..1000 Ω (default 10 Ω) |
| `dmm_set_beep("ON"/"OFF")` | Front-panel beep for cont/diode limits |

**High-level batch**:

| Function | What it does |
|---|---|
| `dmm_scan_test_points(points)` | Run a list of measurements, return per-point + overall PASS/FAIL |

`points` example:

```python
[
  {"name": "VCC_MAIN",  "function": "VOLT:DC", "lower": 3.0, "upper": 3.6},
  {"name": "VBAT",      "function": "VOLT:DC", "lower": 2.7, "upper": 3.3},
  {"name": "ANT_RF",    "function": "RES",     "lower": 0,   "upper": 5},
  {"name": "TEST_LED",  "function": "DIOD",    "lower": 1.5, "upper": 2.5},
  {"name": "GND_SHORT", "function": "CONT",    "lower": 0,   "upper": 1.0}
]
```

### Interactive preflight **[recommended]**

The preflight flow is the **only** way to run a measurement the user has
actually reviewed. Two steps, two tools, hard-gated by `user_confirmed`.

**Step 1 — propose (no SCPI sent)**:

```python
dmm_preflight_measure(
    test_name="VCC_MAIN",
    function="VOLT:DC",
    lower=3.0, upper=3.6,   # optional; falls back to DEFAULT_LIMITS
    nplc=1.0,                # optional; default 1.0
    resolution=None,          # optional; default = instrument default
    auto_range=True,          # optional; default True
    auto_confirm=False,       # default: pause for user review
)
```

Returns:

```json
{
  "ok": true,
  "needs_user_input": true,
  "interaction_type": "confirm_test_setup",
  "test_name": "VCC_MAIN",
  "function": "VOLT:DC",
  "proposed_config": {"lower": 3.0, "upper": 3.6, "nplc": 1.0, "resolution": null, "auto_range": true},
  "rationale": "DEFAULT_LIMITS['VOLT:DC']=[0.0, 5.5] 是产品线宽松值, 已提议收紧到 3.0-3.6 V 贴合本测试点 3.3V 电源 10% 容差",
  "questions": [
    {"name": "pass_window", "question": "VCC_MAIN 合格区间?", "header": "Pass window", "options": [...], "multiSelect": false},
    {"name": "nplc", "question": "积分时间 NPLC?", "header": "NPLC", "options": [...], "multiSelect": false}
  ],
  "next_action": "把用户回答合并到 proposed_config, 再调 dmm_run_preflight_measure"
}
```

The AI client should call `AskUserQuestion` with `questions` verbatim, then
merge the user's answer back into `proposed_config` and call step 2.

**Step 2 — apply + measure (gated)**:

```python
dmm_run_preflight_measure(
    test_name="VCC_MAIN",
    function="VOLT:DC",
    lower=3.0, upper=3.6,    # required (no fallback to DEFAULT_LIMITS in step 2)
    nplc=1.0,
    resolution=None,
    auto_range=True,
    user_confirmed=True,     # MUST be True; False returns error WITHOUT touching the bus
)
```

Returns the [Result envelope](#result-envelope) (`test`, `value`, `unit`,
`lower`, `upper`, `pass`, `timestamp`).

**Bypass for batch / scripted runs**: pass `auto_confirm=True` to step 1.
The tool applies the proposed config and runs the measurement in a single
call. Default is `auto_confirm=False` (i.e. always pause for user review).

**Hard constraints** (verified by `verify_preflight.py` smoke test):

- `dmm_preflight_measure(..., auto_confirm=False)` makes **zero** calls
  to `dmm.write` / `dmm.query` / `dmm.set_nplc` / `dmm.measure*`. The
  DMM is genuinely untouched.
- `dmm_run_preflight_measure(..., user_confirmed=False)` returns
  `{"ok": false, "error": "user_confirmed must be True..."}` **before**
  any instrument check. The gate is the first thing the tool does.

**Per-function question templates**: `config.DEFAULT_QUESTIONS_PER_FUNCTION`
is keyed by SCPI function string. At the time of writing, `VOLT:DC`,
`RES`, `DIOD`, `CURR:DC`, `TEMP:RTD`, and `TEMP:TC` have custom templates;
everything else falls back to a generic pass_window + nplc template. To
add a custom template, add an entry to that dict — the code picks it up
automatically.

**Don't confuse with `dmm_scan_test_points`**: that tool takes a static
list of test points and runs them all in one shot, no interactive review.
It's the right choice for fully-automated regression runs. The preflight
flow is for the human-in-the-loop case.

## The MCP user-interaction gate

The MCP channel enforces **one rule**: an AI client cannot drive a
measurement without first asking the operator. This is not enforced by
a prompt inside an individual tool — it is enforced by **removing the
measure / config / scan tools from the MCP surface entirely**.

**What the AI sees (the MCP tool registry) — 6 tools:**

- Connection / status: `dmm_connect`, `dmm_disconnect`, `dmm_status`,
  `dmm_reset`
- The two-step preflight gate:
  - `dmm_preflight_measure(...)` — propose config, return
    `needs_user_input=true`
  - `dmm_run_preflight_measure(..., user_confirmed=True)` — apply +
    execute

**What the AI does NOT see (library-only) — 18 tools:**

- One-shot measurements: `dmm_measure_volt_dc`, `dmm_measure_volt_ac`,
  `dmm_measure_curr_dc`, `dmm_measure_curr_ac`,
  `dmm_measure_resistance`, `dmm_measure_resistance_4w`,
  `dmm_measure_frequency`, `dmm_measure_period`,
  `dmm_measure_continuity`, `dmm_measure_diode`,
  `dmm_measure_capacitance`, `dmm_measure_temperature`, `dmm_measure`
- Configuration writes: `dmm_set_nplc`, `dmm_set_resolution`,
  `dmm_set_continuity_threshold`, `dmm_set_beep`
- High-level: `dmm_scan_test_points`

These remain as plain Python functions on `mcp_34465a.server` for
scripts (`mcp_34465a/scripts/run_dmm_voltage_log.py`,
`run_dmm_current_log.py`) and for `verify_preflight.py` to import
(legacy atomic-tool smoke test, test 8). They are importable but NOT
decorated with `@mcp.tool()`, so FastMCP does not register them. A
chat agent that tries to call `dmm_measure_volt_dc` via MCP gets a
tool-not-found error.

**Why this design and not just the two-step gate?**

The two-step gate alone is not enough. Even with `user_confirmed=True`
gating `dmm_run_preflight_measure`, an AI client could in principle
skip `dmm_preflight_measure` entirely if the atomic measure tools
were also MCP-visible — there's nothing structural forcing the AI to
go through the proposal step first. By removing the atomic tools
from the MCP surface, the **only** way to send SCPI to the DMM via
MCP is through `dmm_run_preflight_measure`, which in turn is only
reachable after a proposal. The user-interaction gate becomes a
topological property of the tool registry, not a runtime convention
the AI can choose to honor or not.

**Verified by `verify_mcp34465a_gate.py`** (4 tests, no real DMM):

- 18 atomic / config / scan tools are absent from the MCP registry
- `dmm_run_preflight_measure.user_confirmed` defaults to `False` and
  is a required parameter (no way to skip the gate by omission)
- All 24 functions are still importable + callable from Python
  (the script / test path is preserved)
- The 6 expected MCP tools (4 connection/status + 2 preflight) are
  still registered

Run it with:

```bash
PYTHONIOENCODING=utf-8 python verify_mcp34465a_gate.py
```

The companion `verify_preflight.py` (8 tests, owned by another
agent) covers the preflight semantics itself — propose mode makes
no SCPI calls, the `user_confirmed=False` gate short-circuits before
any I/O, the bypass mode applies the proposed config end-to-end, and
the legacy atomic tools are still callable from Python.

## Result envelope

Every measurement tool returns the same shape so the AI can consume
results uniformly across instruments (matches `mcp_8852b` and `mcp_js220`):

```json
{
  "ok": true,
  "test": "VOLT:DC",
  "value": 3.287,
  "unit": "V",
  "lower": 3.0,
  "upper": 3.6,
  "pass": true,
  "timestamp": "2026-06-15T10:23:45"
}
```

`pass` is `None` if no lower/upper is supplied AND the function has no
default window. Default windows are in `config.DEFAULT_LIMITS` and are
**product-line defaults** — tighten to your specific DUT spec, or pass
per-call `lower`/`upper` overrides.

## Gotchas and lessons learned

Empirical findings from bench-testing the bench unit (S/N `MY57515137`,
FW `A.03.03-02.40`). Read this before writing any new script.

### 1. High-rate sampling — biggest win, easiest to get wrong

Naive `dmm.measure_volt_dc()` (which sends `:MEAS:VOLT:DC?`) gives **~2 Hz**
on USB, even though the DMM is capable of thousands. `:MEAS:VOLT:DC?`
re-sends the function config on every call; the SCPI parser + USB
roundtrip is the bottleneck.

**Fix — two-step pattern** (configure once, loop `:READ?`):

```python
dmm.write(":CONF:VOLT:DC")
dmm.write(":VOLT:DC:NPLC 0.001")
dmm.write(":VOLT:DC:AZ OFF")
dmm.query(":READ?")              # warmup; throw away
for _ in range(N):
    v = float(dmm.query(":READ?"))
```

| Mode | Throughput |
|---|---|
| `:MEAS:VOLT:DC?` per iter (NPLC=1) | ~2 Hz |
| `:READ?` per iter (NPLC=1) | ~20 Hz |
| `:READ?` per iter (NPLC=0.001) | **~228 Hz** |

~100× speedup over the naive approach. First read is slower than steady
state — always throw one away after a config change. See
`scripts/run_dmm_voltage_log.py` for the full pattern.

### 2. Display control — don't be clever

`:DISP:STAT OFF` blanks the front panel (confuses bench users) and only
saves ~2% throughput over USB — not worth it. Leave the display ON.

`:DISP:TEXT "..."` **persists on the display after the script exits** if
the `:DISP:TEXT:CLE` clear fails. Always wrap in `try/finally`:

```python
try:
    dmm.write(':DISP:TEXT "DMM LOGGING"')
    samples = list(acquire_loop(...))
finally:
    dmm.write(":DISP:TEXT:CLE")
    dmm.write(":DISP:STAT ON")
```

### 3. Only one VISA session at a time

The 34465A over USB refuses a second connection while a first client
holds the session. A second `open_resource()` from another process hangs
until the first releases the bus, then times out with `VI_ERROR_TMO`.

**Implication**: while a long-running test (5-minute log, batch scan) is
in progress, do not open a parallel session or Keysight Connection Expert.

**Recovery**: `TaskStop` the hung Python process, wait 1-2s, retry
connect with a 2-3s backoff:

```python
for attempt in range(8):
    try:
        dmm.connect()
        break
    except pyvisa.VisaIOError:
        time.sleep(2.0)
```

### 4. Temperature probes — extend the timeout

`:MEAS:TEMP:RTD?` (and `:THER`, `:TC`) **block the bus** until a real
transducer is read, not until a normal aperture window elapses. With no
probe connected, they hang for the full default timeout.

**Fix**: `mcp_34465a` uses `temperature_timeout_ms` (default **30 000 ms**)
for all three TEMP functions; tune via `DMM34465A_TEMP_TIMEOUT_MS=60000`
if your probe is slow. To release a stuck read, send `:ABOR`.

### 5. Overrange sentinel — `9.9e37` means "no signal"

Open inputs (disconnected probes, open switches) return `9.9e+37`, not
`inf`. Comparisons silently give wrong pass/fail. In code that decides
pass/fail, treat `>= 9e37` as "out of range / not connected":

```python
def is_open_circuit(v): return v >= 9e37
```

The `_envelope` helper in `server.py` does not currently do this — add
it if you're logging continuity / resistance.

### 6. Per-product limits — defaults are not your spec

`config.DEFAULT_LIMITS` are **product-line defaults** that make the MCP
run out of the box, but they will pass almost any random reading. A
3.0 V supply at 0.5 V is "pass" with `{"lower":0,"upper":5.5}`. Override
per call with `lower`/`upper`, or edit `DEFAULT_LIMITS` for the product.
**This is exactly why the [preflight flow](#interactive-preflight)
exists** — it forces the operator to look at the proposed pass window
before any measurement runs.

### 7. NPLC=0.001 produces a bimodal-looking histogram

At NPLC=0.001, the histogram of a stable DC signal is **not Gaussian** —
it has a small dip at the mean with two peaks on either side. At 228 Hz,
USB polling jitter and the 34465A's internal aperture timing interact.
Don't trust the histogram shape at NPLC=0.001 for noise analysis; use
NPLC=1 or NPLC=10 for clean Gaussian (you'll lose ~100× throughput).
The peak-to-peak is still a meaningful measure of worst-case deviation.

### 8. Continuity / diode thresholds

| Function | Default threshold | Range | Note |
|---|---|---|---|
| `:CONT` | 10 Ω | 1–1000 Ω | Beep when R < threshold |
| `:DIOD` | (none — fixed 5 V range) | n/a | Returns forward V |

Override continuity threshold with `dmm.set_continuity_threshold(50)`.

### 9. Report layout — one subfolder per run

Run outputs go to `reports/<run_label>/` with stable filenames:
```
reports/<run_label>/{data.csv, summary.json, report.md, plot.png}
```
The folder name is the timestamp; the files inside are stable so
downstream tooling can find them without parsing the run id.

### 10. Common pyvisa + 34465A SCPI recipes

| Want | SCPI |
|---|---|
| Trigger one reading | `:READ?` (after `:CONF:`) |
| One-shot everything | `:MEASure:VOLT:DC?` (slower, see §1) |
| Identity | `*IDN?` |
| Reset to defaults | `*RST` |
| Abort hanging read | `:ABOR` |
| Clear status regs | `*CLS` |
| Set integration time | `:VOLT:DC:NPLC 0.001` |
| Auto-zero | `:VOLT:DC:AZ OFF|ON|ONCE` |
| Display text | `:DISP:TEXT "X"` / `:DISP:TEXT:CLE` |
| Display on/off | `:DISP:STAT ON|OFF` |

### 11. Pre-test checklist

- [ ] `dmm.resource` matches the intended bench unit (check `*IDN?` serial)
- [ ] `timeout_ms` is long enough for the function (TEMP needs 30 s)
- [ ] If high-rate, configure once and loop `:READ?` — NOT `:MEAS:?`
- [ ] If writing display text, wrap in `try/finally` with `:DISP:TEXT:CLE`
- [ ] Pass / fail limits are per-product, not the default
- [ ] After a hang: `TaskStop` the python process, wait 1-2 s, retry connect
- [ ] Output goes into its own `reports/<label>/` subfolder (see §9)
- [ ] If running a real test, use the [preflight flow](#interactive-preflight)

### 12. Current measurement — `CURR:DC` specifics

**Auto-range is a moving target.** `CURR:DC` has 8 ranges (1 µA .. 3 A)
the DMM auto-cycles between; each range change takes ~1 ms of relay +
ADC settling. With NPLC=0.001 on `CURR:DC` and an IoT device that
swings µA → 100 mA, the *practical* rate is closer to **~78 Hz**, not
the 228 Hz seen on `VOLT:DC`.

**`9.9e37` is the overrange sentinel — including the moment of a range
change.** A range change from 100 mA to 1 A range will emit one or two
`9.9e+37` values while the relay is moving. These are *not* a sign the
current actually exceeds 3 A; they're an auto-range artifact.

**Burden voltage scales with range.** Each current range has a different
shunt resistor (50 kΩ at 1 µA, 5 Ω at 100 mA, 0.1 Ω at 3 A). For a
low-voltage IoT rail (e.g. 1.8 V), the 500 mV burden on the mA range is
**28%** of the supply — non-trivial. If your rail is sensitive, use a
fixed range or an external current sense amp (INA180/INA219).

**3A terminal vs 10A terminal — physical, not software.** The 34465A
has TWO front-panel current inputs: **I** (3 A fused) and **10A** (10 A
fused). SCPI cannot switch between them. If the current is expected to
exceed 3 A, **physically move the lead to the 10A terminal before the
test**. Above 3 A on the I terminal risks blowing the 3 A fuse.

### 13. `TaskStop` (Claude background-task tool) does NOT run cleanup

`TaskStop` kills the Python process directly. On Windows, Python's
`signal.SIGTERM` handler is **not reliably delivered** to a long-running
script, so `try/finally` blocks in `run_dmm_current_log.py` do NOT run.

**Implication**: After a `TaskStop`, only `data.csv` (line-by-line
`fsync`) and the last `summary.json` update are safe. `report.md` and
`plot.png` are missing and must be regenerated from the CSV. The
`run_dmm_*.py` scripts support a stop file (`touch <RUN_DIR>/.stop`)
that lets the `finally` block run; prefer that over `TaskStop`.

## Known PoC limitations

- **No scan / list mode.** A long `dmm_scan_test_points` makes N
  one-shot `:MEASure?` queries; for very large point lists switch to
  `INIT:CONT` + `:FETCh?` to stream. Not yet wrapped.
- **Auto-ranging** is on by default; for fastest tests at a known level
  set `:CONF:FUNC <range>` first (not yet exposed as a tool).
- **Multi-unit / parallel** is not supported (single global `dmm`). Add
  a session map keyed by VISA resource if you need parallelism.

## Test the PoC

```python
from mcp_34465a.server import *
print(dmm_connect()["idn"])            # Keysight 34465A ...
# Interactive: preflight first, then run
rec = dmm_preflight_measure("VCC_MAIN", "VOLT:DC")
# ... user reviews rec["questions"], picks answers ...
print(dmm_run_preflight_measure("VCC_MAIN", "VOLT:DC",
        lower=3.0, upper=3.6, user_confirmed=True))
# Or quick: bypass review
print(dmm_preflight_measure("VBAT", "VOLT:DC",
        lower=2.7, upper=3.3, auto_confirm=True))
dmm_disconnect()
```

## Reference documents

Datasheets, programmer's guides, and app notes in `reference/`. Use them
as ground truth when an SCPI command behaves unexpectedly — AI
pre-training is at best ~70% accurate per command.

## See also

- [`../BUILDING_AN_INSTRUMENT_MCP.md`](../BUILDING_AN_INSTRUMENT_MCP.md) —
  project-level guide: package layout, state machines, pre-flight pattern,
  common gotchas. Read this when starting a new instrument MCP.
- [`../mcp_8852b/README.md`](../mcp_8852b/README.md) — BT tester MCP
  (the original envelope shape and pre-flight convention).
- [`../mcp_js220/README.md`](../mcp_js220/README.md) — JS220 energy
  analyzer MCP; preflight is identical in shape, differs in test_type
  presets.
- [`../verify_preflight.py`](../verify_preflight.py) — 8-test verifier
  for the interactive preflight flow on both `mcp_34465a` and `mcp_js220`.
