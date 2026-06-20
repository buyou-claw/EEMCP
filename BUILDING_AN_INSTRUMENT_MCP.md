# Building an Instrument MCP — Project Guide

A "instrument MCP" in this project is a Python package that wraps a
piece of bench equipment (oscilloscope, DMM, signal generator, RF
tester, power supply) as a set of atomic tools an AI can call via
the Model Context Protocol. The user is the engineer; the AI is the
operator; the MCP is the bridge.

This guide is the project-level playbook, distilled from building
`mcp_34465a` and `mcp_8852b`. Follow it for the next instrument and
you'll skip 80% of the rediscoveries.

> **Background reading** (per-instrument gotchas, already captured):
> - [mcp_34465a/NOTES.md](mcp_34465a/NOTES.md) — Keysight 34465A lessons
> - [mcp_8852b/README.md](mcp_8852b/README.md) — MT8852B state-machine design
> - [mcp_js220/NOTES.md](mcp_js220/NOTES.md) — JS220 vendor-SDK + streaming-statistics lessons

---

## 0. The five-minute decision tree

Before writing any code, answer:

| Question | Why it matters |
|---|---|
| 1. What is the **transport**? (VISA resource / USB private / LAN TCP) | Determines whether you wrap pyvisa, the vendor SDK, or raw socket. The first two examples both used pyvisa; `mcp_js220` did not. |
| 2. Is the instrument **command/response** (set-then-read), **streaming** (continuous waveform), or **captured-window** (start → wait N s → fetch stats)? | Determines whether you need a state machine and which one. |
| 3. What are the **2–3 most useful measurements** the AI will need? | These become the headline tools |
| 4. What's the **rate at which a single measurement can be repeated**? (Limits sample-log speed) | Sets the report's "rate_hz" field |
| 5. What **safety limits** (voltage, current, power) must the AI never exceed? | Becomes input validation in the tool layer |
| 6. (For new builds) Does the instrument have **multiple user-settable parameters** that change the behavior (sampling rate, range, output rate)? | If yes, you need a `get_capabilities()` tool and a `recommend_config()` / `configure_for_test()` flow. **Do not** bake assumptions into a default. |

If you can't answer 1 or 2, stop and read the instrument's programmer
guide first. The other three are emergent — you'll refine them as you
write tools.

---

## 1. Package layout

```
HWTEST/
├── mcp_34465a/                # example: 34465A DMM
│   ├── __init__.py
│   ├── README.md
│   ├── NOTES.md               # per-instrument gotchas
│   ├── config.py              # VISA resource + timeouts + limits
│   ├── scpi.py                # command builders (pure strings)
│   ├── instrument.py          # pyvisa wrapper (write/query helpers)
│   ├── state_machine.py       # OPTIONAL — see §6
│   ├── server.py              # fastmcp entry, defines the tools
│   └── reference/             # datasheets / programmer guides (PDFs)
├── mcp_8852b/                 # example: MT8852B (has state_machine.py)
├── mcp_<next_device>/         # the one you're about to build
└── BUILDING_AN_INSTRUMENT_MCP.md   # ← this file
```

**Always include `__init__.py`, `README.md`, and `config.py`.**
**Include `state_machine.py` only if the instrument needs a multi-step
lifecycle** (e.g. configure → arm → trigger → fetch). For one-shot
measurements like a DMM or power-supply set-and-read, you don't need it.

---

## 2. `config.py` — the single source of truth for instrument identity

```python
import os
from dataclasses import dataclass

DEFAULT_RESOURCE = "USB0::0x2A8D::0x0101::MY57515137::0::INSTR"  # ← change per device

@dataclass
class DeviceConfig:
    visa_resource: str = os.environ.get("DEVICE_RESOURCE", DEFAULT_RESOURCE)
    timeout_ms: int = int(os.environ.get("DEVICE_TIMEOUT_MS", "10000"))
    # ... any extra timeouts (e.g. temp_timeout_ms for slow functions)

CONFIG = DeviceConfig()

# Domain-specific constants
VALID_CHANNELS = list(range(0, 40))    # example: BLE channels
DEFAULT_LIMITS = {                     # example: per-test pass windows
    "VOLT:DC": {"lower": 0.0, "upper": 5.5},
}
```

**Rules of thumb**:
- Always allow env-var override (`os.environ.get("FOO", default)`).
- Group all magic numbers (timeouts, retries) in this file — never
  bury them in `instrument.py` or `server.py`.
- Put domain enums (channels, ranges, function names) here so the
  tool layer can validate against them.

---

## 3. `scpi.py` — one function per SCPI command

```python
def idn() -> str:                return "*IDN?"
def reset() -> str:              return "*RST"
def conf_volt_dc() -> str:       return ":CONF:VOLT:DC"
def set_nplc(function: str, n: float) -> str:
    if not 0.001 <= n <= 1000:
        raise ValueError(f"nplc must be 0.001..1000, got {n}")
    return f":{function}:NPLC {n:g}"
def meas_one_shot(function: str) -> str:
    return f":MEAS:{function}?"
```

> **Non-SCPI instruments** (see §16): this file becomes
> "validated parameter setters" — same one-function-per-command
> shape, but the body validates and returns the canonical value
> (e.g. `"auto"`, `"100 kHz"`) instead of building a SCPI string.

**Why a separate file**:
- Centralizes command syntax — easy to grep, easy to diff when the
  instrument gets a firmware update that changes a command.
- Lets you swap dialect variants (e.g. Anritsu 4.x vs 3.x) by editing
  one function instead of all callers.
- `raise NotImplementedError` for commands you haven't wrapped yet —
  callers know not to invoke them.

**No I/O happens here.** `scpi.py` returns strings. Side effects go
in `instrument.py`. This is the line that keeps the package maintainable.

---

## 4. `instrument.py` — pyvisa wrapper

```python
class DeviceError(RuntimeError): ...

class Device:
    def __init__(self, resource: Optional[str] = None):
        self.resource = resource or CONFIG.visa_resource
        self.rm = None
        self.instr = None
        self._connected = False

    def connect(self) -> str:           # returns *IDN?
        ...
    def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...

    def write(self, cmd: str) -> None: ...
    def query(self, cmd: str) -> str: ...
    def query_float(self, cmd: str) -> float: ...

    # Domain helpers (one per common operation)
    def measure(self, function: str) -> float: ...
    def set_voltage(self, volts: float) -> None: ...
    def read_current(self) -> float: ...
```

**The rule**: `server.py` must never call `pyvisa` directly. It only
talks to `device.instr` through these methods. Single point for
timeout handling, retries, error mapping.

**Connection lifecycle**:
- `connect()` is idempotent — calling twice returns the cached IDN.
- `disconnect()` must handle partial state (instrument opened but
  resource manager never returned).

---

## 5. `server.py` — the fastmcp tool surface

### 5.1 The shape of every tool

Three families:

1. **Connection** (4 tools): `device_connect`, `device_disconnect`,
   `device_status`, `device_reset`.
2. **Configuration / per-function** (N tools): one per major SCPI
   command, named `<verb>_<noun>` (e.g. `dmm_measure_volt_dc`).
3. **High-level** (1–2 tools): batch / scan / one-shot convenience
   (e.g. `dmm_scan_test_points`).

### 5.2 The result envelope — keep it uniform across instruments

Every measurement tool returns the same shape so the AI can consume
results uniformly:

```json
{
  "ok": true,
  "test": "VOLT:DC",
  "value": 3.0625,
  "unit": "V",
  "lower": 3.0,
  "upper": 3.6,
  "pass": true,
  "timestamp": "2026-06-15T10:23:45"
}
```

- `ok: false, error: "..."` on any failure (don't raise from a tool —
  the AI parses dicts, not exceptions).
- `pass: null` (not false) when no limits are set — distinguishable
  from "fails limits".
- `unit` in plain ASCII or with `Ω`, `°C` (the AI handles either).
  Windows console display is the only thing that mangles it.

### 5.3 Always validate, never trust the AI

Tool functions should re-validate arguments even if the type hint
says `int`. The AI may pass a string or out-of-range number. Bad input
→ return `{"ok": False, "error": "..."}`, never crash.

---

## 6. State machines — when do you need one?

`mcp_34465a` does **not** have a state machine. `mcp_8852b` does.
`mcp_js220` does too, but with a smaller state set. Heuristic:

| Instrument style | Need state machine? |
|---|---|
| One-shot read (DMM, simple power supply meter) | **No** |
| Configure → start → wait → read (BLE tester, data logger) | **Yes** |
| Triggered capture (scope, SA, transient recorder) | **Yes** |
| Streaming (waveform, log) | **Yes** (a small "IDLE/RUNNING/COMPLETE" one) |
| Configure → start streaming → stop (energy analyzer, power meter) | **Yes** (3-state: DISCONNECTED / IDLE / STREAMING) |

If you do need one, model it as:

```python
class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE         = "IDLE"
    CONFIGURED   = "CONFIGURED"
    TESTING      = "TESTING"
    COMPLETE     = "COMPLETE"
    ERROR        = "ERROR"
```

…with **illegal-transition errors** (e.g. read result before
`COMPLETE` raises `DeviceError`). The AI can recover by asking for
`device_status()`. See [mcp_8852b/state_machine.py](mcp_8852b/state_machine.py)
for the full pattern.

For a **streaming-only** lifecycle (no TESTING/COMPLETE states), the
minimal working set is:

```python
class State(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    IDLE         = "IDLE"          # open, configured, not streaming
    STREAMING    = "STREAMING"     # data flowing into internal buffer
    ERROR        = "ERROR"
```

`start()` requires IDLE; `stop()` requires STREAMING; both go back
to IDLE. `disconnect()` is allowed from any state. See
[mcp_js220/state_machine.py](mcp_js220/state_machine.py) for the
full pattern.

**Why illegal transitions matter**: when an instrument has internal
state (a buffered acquisition in progress, a streaming session open),
letting `server.py` call the driver in the wrong order produces
**silently wrong** results — not an error, just garbage. The state
machine turns those into explicit `ok: False, error: ...` envelopes
the AI can react to.

---

## 7. High-rate sampling — the universal trick

For any instrument that supports it, the same 100× speedup applies:
**configure once, then loop with the read-only command**.

```python
# 1) Configure ONCE
device.write(":CONF:VOLT:DC")
device.write(":VOLT:DC:NPLC 0.001")  # fastest integration
device.write(":VOLT:DC:AZ OFF")     # skip auto-zero
device.query(":READ?")              # warm-up (throw away)

# 2) Tight loop with the cheapest read command
while not done:
    v = float(device.query(":READ?"))   # NOT :MEAS:VOLT:DC?
```

Empirically (Keysight 34465A, USB, NPLC=0.001):
- `:MEAS:VOLT:DC?` per loop: ~2 Hz (re-sends config each time)
- `:READ?` per loop:        ~228 Hz

The lesson generalizes: **if the instrument has a "just read" command
distinct from "configure+read", use it for any loop > 10 samples.**
Look for `:READ?`, `:FETCh?`, `:TRAC:DATA?` etc.

Also note: at high rate, the **first read after config is slower** —
always throw one away as a warm-up.

---

## 8. Report conventions

For any test that produces samples (voltage log, current log, sweep),
follow these conventions so reports don't accumulate as a mess in
`reports/`:

```
reports/
└── <run_label>/            # one folder per test (e.g. dmm_voltage_log_20260615_105200)
    ├── data.csv            # raw samples
    ├── summary.json        # machine-readable summary
    ├── report.md           # markdown report
    └── plot.png            # time series + distribution
```

**Live-safety rule for long-running tests**:
- Write each sample to `data.csv` with `flush()` + `os.fsync()` so a
  hard kill never loses more than the in-flight sample.
- Rewrite `summary.json` every ~100 samples — cheap, lets the user
  peek at progress.
- Only write `report.md` and `plot.png` in the cleanup path — they
  need full data to render.

If you get hard-killed and `report.md` / `plot.png` are missing,
**regenerate them from the CSV**:

```python
import sys
from pathlib import Path
sys.path.insert(0, r"e:\Centrak\BLE Sticker\test\HWTEST")
import run_dmm_current_log as m       # ← your script's module
m.CSV_PATH  = Path("...") / "data.csv"
m.JSON_PATH = Path("...") / "summary.json"
m.MD_PATH   = Path("...") / "report.md"
m.PNG_PATH  = Path("...") / "plot.png"
m.RUN_LABEL = "..."

import csv
samples = []
with m.CSV_PATH.open() as f:
    r = csv.reader(f); next(r)
    for row in r: samples.append((float(row[0]), float(row[1])))

summary = m._summarize(samples)
m._write_markdown(meta, summary)
m._write_plot(samples, summary)
```

This is why you should put `_summarize`, `_write_markdown`, and
`_write_plot` at **module level** (not inside `main()`) — they're the
public regeneration API.

### Multi-scale plots when the dynamic range is huge

For a power analyzer / current probe / scope capture, the signal
often spans **5-6 orders of magnitude** (e.g. 2 µA standby + 1 A
peaks on the same DUT). A linear plot hides the small-signal
behavior completely.

Pattern: emit **two plots** — one linear, one log-y for the wide-range
signal:

```
plot.png       # linear: voltage + current on the same time axis
plot_log.png   # log-y current only — exposes µA standby and A peaks together
```

```python
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Linear
fig, (ax_v, ax_i) = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
ax_v.plot(t, v, color="tab:blue")
ax_i.plot(t, i, color="tab:red")
fig.savefig("plot.png", dpi=120, bbox_inches="tight")

# Log (only the wide-range signal)
fig, ax = plt.subplots(figsize=(14, 5))
mask = i > 0
ax.plot(t[mask], i[mask])
ax.set_yscale("log")
fig.savefig("plot_log.png", dpi=120, bbox_inches="tight")
```

### Mode segmentation (auto-detect operating states)

For captures where the user manually cycles the DUT through modes
(standby → active → standby → ...), the report should call out
**what the DUT was doing at each time window**. Heuristic:

1. Bucket each sample by its current magnitude (e.g. < 100 µA =
   "standby", 100 µA – 10 mA = "low_active", > 10 mA = "active").
2. Find runs of consecutive same-bucket samples.
3. Filter runs shorter than 0.3 s (likely transient noise).
4. Report per-bucket: count, total duration, weighted average current
   / voltage, peak current / voltage.

This makes the report useful at a glance: the user sees "you spent
193 s in standby at 2.3 µA, 37 s in active at 51 mA" without having
to eyeball the plot.

### Live mode with `.stop` file

For long captures where the user wants to control when to stop (e.g.
"run until I press the button"):

```python
# In the capture loop:
stop_file = run_dir / ".stop"
while not stop_file.exists():
    ... capture one tick ...
    if elapsed >= max_duration:
        break
```

Document in the report: "create `<run_dir>/.stop` to stop cleanly".
This avoids the race where the AI tries to gracefully stop a script
that's also trying to gracefully stop itself.

Alternative: the script also accepts `Ctrl+C` (KeyboardInterrupt
handler in the loop). Either path produces the same final report.

---

## 9. Testing the MCP — two phases

### Phase 1: Offline (no instrument connected)

If you structure the code right, you can test the tool layer with a
mock instrument:

```python
class MockDevice:
    connected = True
    def measure(self, function): return 3.14
    def idn(self): return "Mock Inc, Mock 1.0, 0"
```

Swap `dmm` for `MockDevice()` in `server.py` and run the tools. This
catches: type bugs, envelope shape, default-limit logic, error
handling. Won't catch: real SCPI syntax, real timing, real quirks.

### Phase 2: Bench (real instrument connected)

Always run this checklist before declaring the MCP done:

- [ ] `device_connect()` — returns IDN
- [ ] `device_disconnect()` — no resource leak (try 5× in a loop)
- [ ] One measurement per function
- [ ] One configuration command (e.g. NPLC change)
- [ ] One batch tool (e.g. scan)
- [ ] `fastmcp.list_tools()` — all tools registered, correct count
- [ ] Stress test: rapid connect/measure/disconnect, no VISA errors

Speed test (if instrument supports it):
```python
import time
device.connect()
device.write(":CONF:VOLT:DC")
device.write(":VOLT:DC:NPLC 0.001")
device.query(":READ?")  # warm-up
t0 = time.monotonic()
n = 0
while time.monotonic() - t0 < 5:
    device.query(":READ?")
    n += 1
print(f"{n/(time.monotonic()-t0):.1f} Hz")
```

---

## 10. Per-instrument `NOTES.md`

Create one alongside the package, with sections:

1. **High-rate sampling recipe** (if applicable) — NPLC + command
2. **Display behavior** — what to write, what not to
3. **Connection quirks** — single VISA session, retry timing
4. **Per-function timeout** — anything that hangs without a probe
5. **Overrange / sentinel values** — 9.9e37, "1.0E+99", etc.
6. **Default limits caveat** — they're placeholders, not specs
7. **Bimodal / weird histogram findings** — non-obvious behavior
8. **Threshold / range notes** — continuity, diode, current ranges
9. **Report layout** — where output goes
10. **SCPI quick-reference** — most-used commands in one place
11. **Pre-test checklist** — 5–10 items, copy-pasteable
12. **Per-physical-channel gotchas** — current input, voltage input
13. **Hard-kill behavior** — what doesn't survive `TaskStop` / SIGKILL

Example: [mcp_34465a/NOTES.md](mcp_34465a/NOTES.md). Update it every
time you discover something — future-you will thank present-you.

---

## 11. Gotcha registry — common across instruments

| Gotcha | Where to defend |
|---|---|
| **Single VISA session** — only one client over USB at a time | Document in `NOTES.md`; never try to "check status" from a second process while a test runs |
| **Hard kill loses in-memory state** | Always fsync samples; rewrite summary periodically |
| **NPLC integration is per-function** | Each function has its own NPLC — set per-function, not globally |
| **Auto-range settles ~1 ms** | Document the flatline; don't claim "no sample loss" without caveats |
| **Burden voltage on current measurements** | Document per-range burden; warn if rail is sensitive |
| **Fused inputs (e.g. 3A on DMM)** | SCPI cannot bypass; must move lead manually |
| **Reading of `9.9e37` = overrange/open** | Filter before pass/fail comparison |
| **First read after config is slow** | Always throw one away as warm-up |
| **Display-off saves only ~2% over USB** | Not worth the surprise; leave display on |
| **Custom display text persists on exception** | `try/finally` with `:DISP:TEXT:CLE` |
| **Triggered readings need explicit arm** | Most scopes/SA: `:INIT` then `:FETCh?`, not `:MEAS?` |
| **Per-channel instrument has different SCPI paths** | E.g. `:CHAN1:SCAL` not `:CHAN:SCAL` — wrap in `scpi.py` |
| **Calibration data in NV RAM is fragile** | Never write to `:CAL:...` from a tool unless explicitly needed |
| **Private-protocol instruments** (no SCPI) | Wrap the vendor SDK the same way you wrap pyvisa — `instrument.py` is the only file that imports the vendor module. `scpi.py` becomes a "command builders" file that validates values. |
| **Vendor SDK delivers data on a background thread** | Lock the stats buffer; never call back into the device from a callback. Document in NOTES.md. |
| **Stats callback fires at N Hz regardless of how often the user reads** | Buffer caps; if the user reads slower than the rate, they lose samples. Surface `n_samples` vs `expected_n` in the report. |
| **Unicode keys in the data dict** (e.g. `µ`, `∫`, `σ2`) | The vendor's canonical names may be non-ASCII. Always extract via the **english alias** (`avg`, `std`, `integral`) and document the mapping in `scpi.py`. |
| **`parameter_get` may return wrong dtype or fail** | If the driver has a bug (e.g. `_parameter_get_info` AttributeError on a subclass), always go through the validated setter/getter wrappers in `instrument.py` instead of raw `parameter_get` from `server.py`. |
| **Windows console can't print `µ` / `°` / `Ω`** | Force `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` for every Python invocation, or use `python -X utf8`. In CSV / JSON dumps, set `encoding="utf-8"` explicitly. |
| **Forgetting to ask the user about test conditions** | Don't bake default config into the long-running capture. Add a `get_capabilities()` + `recommend_config()` + `configure_for_test()` flow so the user reviews before you write to the instrument. See §17. |
| **Plotting wide-dynamic-range data on a linear axis** | Always emit a second plot with log-y for any signal that may span > 100× (current, power). See §8. |

---

## 12. Templates — drop-in for a new instrument

These are the minimum viable files. Replace `XXX` with the
instrument's identifier and `device` with its short name.

### `mcp_xxx/__init__.py`
```python
"""XXX MCP Server package."""
__version__ = "0.1.0"
```

### `mcp_xxx/config.py`
See §2.

### `mcp_xxx/scpi.py`
```python
def idn() -> str:           return "*IDN?"
def reset() -> str:         return "*RST"
# ... one function per SCPI command
```

### `mcp_xxx/instrument.py`
```python
class XXXError(RuntimeError): ...

class XXX:
    def __init__(self, resource=None): ...
    def connect(self) -> str: ...
    def disconnect(self) -> None: ...
    def write(self, cmd): ...
    def query(self, cmd) -> str: ...
    def query_float(self, cmd) -> float: ...
    # ... domain helpers
```

### `mcp_xxx/server.py` (skeleton)
```python
from fastmcp import FastMCP
from .instrument import XXX, XXXError
from .config import CONFIG

mcp = FastMCP("xxx")
device = XXX()

def _envelope(test, value, unit, lower=None, upper=None): ...
def _ok(d): return {"ok": True, **d}
def _err(e): return {"ok": False, "error": str(e)}

@mcp.tool()
def device_connect(resource=None): ...
# ... one @mcp.tool() per command

if __name__ == "__main__":
    mcp.run()
```

---

## 13. Adding to Claude Desktop

Edit `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mcp_34465a": {
      "command": "python",
      "args": ["-m", "mcp_34465a.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    },
    "mcp_<next>": {
      "command": "python",
      "args": ["-m", "mcp_<next>.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    }
  }
}
```

Each MCP runs as its own subprocess; no port conflicts. The `cwd` is
the project root so the package can be imported with `from mcp_xxx.server import ...`.

---

## 14. Cross-instrument coordination

When you have multiple MCPs (e.g. 34465A + 8852B), they each own one
instrument. **Higher-level test flows** (e.g. "power up the DUT,
measure VCC, run BLE test, log all results") belong in a separate
script that **imports** the device modules — not in either MCP.

Pattern:
```
mcp_34465a/        # owns the DMM
mcp_8852b/         # owns the BT tester
run_full_pcba_test.py   # imports both, runs the flow
```

See [mcp_34465a/README.md](mcp_34465a/README.md) §"DUT skill contract"
for the DMM-side version of this.

---

## 15. Reference documents (datasheets)

For each instrument MCP, drop the relevant PDFs in
`mcp_<device>/reference/`. The user already did this for the 34465A:

```
mcp_34465a/reference/
├── 34465A_120_C13SPWR-903(2).pdf     # datasheet
├── 5991-1983.pdf                      # programmer's guide
├── 9018-03876.pdf                     # service guide
└── Create Test Sequences without Programming Using BenchVue Test Flow.pdf
```

These are invaluable when an SCPI command behaves unexpectedly —
the official docs are the ground truth, the AI's pre-training
knowledge is at best 70% accurate per command.

---

## 16. Non-SCPI instruments — wrapping vendor SDKs

Not every instrument speaks SCPI. Some examples from this project:
Joulescope JS220, many low-end USB oscilloscopes, NI DAQ cards, any
instrument that ships a Python SDK. The pattern is the same as the
pyvisa case, but the indirection layer is different.

### Mapping the package layout to a vendor-SDK backend

| File | pyvisa-style (SCPI) | Vendor-SDK style |
|---|---|---|
| `scpi.py` | Returns SCPI strings (`:MEAS:VOLT:DC?`) | Returns the SDK parameter name + validated value (`"source", "raw"`) |
| `instrument.py` | Owns `pyvisa.ResourceManager` + a `Session` | Owns the vendor `Device` object returned by `vendor.scan()` |
| `config.py` | Has a `visa_resource` field | Has a `device_path` / `serial` field (or `None` to pick `scan()[0]`) |
| `state_machine.py` | Same as SCPI | Same as SCPI |
| `server.py` | Same envelope, same tool shape | Same envelope, same tool shape |

The **envelope and the tool surface don't change**. Only the
"transport" half of `instrument.py` is different. This is by
design — the AI's interaction with the MCP is the same regardless
of how the bits get onto the wire.

### Defensive wrappers

`scpi.py` for a vendor SDK is no longer "build the SCPI string" —
it's "validate the value and return the canonical form." Example
for JS220:

```python
# NOT: def conf_volt_dc(): return ":CONF:VOLT:DC"
# IS:  def set_current_range(value: str) -> str:
def set_current_range(value: str) -> str:
    if value not in VALID_CURRENT_RANGES:
        raise ValueError(...)
    return value   # canonical form for vendor.parameter_set()
```

This is the same pattern as SCPI validation: one function per
callable command, raises `ValueError` for bad input, no I/O.

### The three MCPs so far, side-by-side

| Concern | 34465A (SCPI) | MT8852B (SCPI) | JS220 (vendor SDK) |
|---|---|---|---|
| Transport | pyvisa, NI-VISA backend | pyvisa, NI-VISA backend | `joulescope` USB driver |
| Resource string | `USB0::0x2A8D::0x0101::...` | `TCPIP0::192.168.1.21::5025::SOCKET` | n/a — `scan()` |
| Command style | `instr.write(":MEAS:VOLT:DC?")` | `instr.write("AT")` | `device.parameter_set("source", "raw")` |
| State machine | none | DISCONNECTED → IDLE → CONFIGURED → TESTING → COMPLETE | DISCONNECTED → IDLE → STREAMING |
| Streaming | `:INIT` + `:FETCh?` (not used) | per-step | `start()` + `statistics_callback` |
| Threads in the driver | none | none | **yes** (USB processing thread delivers callbacks) |

The AI's perspective in all three: "call `dmm_measure_volt_dc` /
`bt_test_tx_power` / `js220_measure_current_avg` and get back the
same `{ok, test, value, unit, lower, upper, pass, timestamp}`
envelope."

### When you discover the instrument is non-SCPI

If you can't answer "what is the VISA resource string" in the
five-minute decision tree, look for:

1. **A vendor Python package on PyPI** (e.g. `pip install joulescope`)
2. **A vendor SDK download** with a Python wheel (e.g. PicoSDK)
3. **Raw socket** to a known port (e.g. many LAN-attached instruments
   still expose a SCPI-like command set over TCP port 5025; some
   expose a JSON-RPC instead)
4. **USB with libusb** + a vendor protocol (last resort; expect to
   write the protocol from the datasheet)

If option 1 exists, **always use it**. It saves you from
reverse-engineering the wire format and you get threading /
hot-plug / error handling for free.

---

## 17. Pre-flight configuration — ask before you write

When the instrument has **multiple user-settable parameters** that
change its behavior (sampling rate, range, output rate, signal
source), **do not** start a long capture with baked-in defaults.
The user knows more about their DUT than you do.

Pattern: three MCP tools, in order.

### `get_capabilities()`

Returns:
- Device identity (model, firmware, hardware revision)
- For each user-settable parameter:
  - Current value
  - All valid values (from the driver's `parameter.options`)
  - Units
  - One-line description
- A few **built-in test presets** matched to common test types
  (long log, burst capture, peak capture, low current, etc.)

```python
@mcp.tool()
def js220_get_capabilities() -> dict:
    """Enumerate the JS220's configurable parameters and valid values.
    Call this BEFORE a test so you can see what's available.
    """
    if not js.connected:
        return {"ok": False, "error": "not connected"}
    params = {}
    for name, valid, units, brief in [...]:
        params[name] = {
            "current": js.parameter_get(name),
            "valid_values": list(valid),
            "units": units,
            "brief": brief,
        }
    return {"ok": True, "device": ..., "parameters": params,
            "default_test_recommendations": {...}}
```

### `recommend_config(test_type, expected_current_a)`

Picks a starting config from a high-level test type and a
back-of-envelope current estimate. Returns the recommendation +
the **reasoning** (which preset, why this range, how many samples
per window). The recommendation is **not** auto-applied.

### `configure_for_test(test_type, expected_current_a, apply=False)`

Convenience wrapper. With `apply=False` it just delegates to
`recommend_config()`. With `apply=True` it also calls the
underlying `js220_set_*` setters in dependency order. The two-step
(`apply=False` then `apply=True`) lets the AI show the user the
plan and get sign-off.

### Why this matters

Without this flow, the AI either (a) bakes `auto` everywhere —
boring and often wrong — or (b) hallucinates parameter values that
the driver rejects. With it, the AI becomes a **configurator**:
it reads capabilities, proposes a config with reasoning, applies
it after user OK, and runs the capture.

### Standalone-script mirror

For the bench scripts that don't go through the MCP server, mirror
the same flow with `preflight_check.py`:

```python
# preflight_check.py
@p.add_argument("--test-type", choices=["long_idle_log", "ble_active_bursts", ...])
@p.add_argument("--expected-current-a", type=float)
@p.add_argument("--io-voltage", type=str)
# Interactive mode: stdin is a TTY, ask 6 questions, print recommendation
# Non-interactive: use the flags, print recommendation
```

The `run_js220_power_profile.py` script then accepts the same
flags and forwards them to the resolver. The user never has to
hand-edit `config.py` between test types.

### Built-in preset table

For instruments with a stable parameter set, bake 4–5 presets into
`config.py` and reference them in `recommend_config()`. Keep the
table small — the user can override any field per-call.

| Test type | sampling_frequency | reduction_frequency | i_range | io_voltage |
|---|---|---|---|---|
| `long_idle_log` | 1 kHz | 2 Hz | auto | 3.3V |
| `ble_active_bursts` | 100 kHz | 10 Hz | auto | 3.3V |
| `fine_peak_capture` | 2 MHz | 1 Hz | 10 A | 3.3V |
| `ultra_low_current` | 100 Hz | 2 Hz | 18 µA | 3.3V |
| `high_current_load` | 10 kHz | 20 Hz | 10 A | 3.3V |

---

## 18. Long-capture resilience — survive Ctrl+C, TaskStop, SIGKILL

For any capture that runs longer than ~30 s, the user will
eventually kill it. Whether they hit Ctrl+C, the harness runs
`TaskStop`, or the OS OOMs the process, your goal is the same:
**lose at most the in-flight sample** and leave a CSV that can be
re-plot / re-summarized.

### The three layers of defense

**Layer 1: `flush()` + `os.fsync()` on every sample**

```python
with csv_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.writer(f)
    w.writerow(header)
    f.flush()
    for sample in stream:
        w.writerow(row(sample))
        f.flush()
        os.fsync(f.fileno())   # <-- commit to disk
```

Without `fsync`, a kill -9 between `flush()` and the OS page
cache flush can lose up to ~30 seconds of samples (default OS
write-back delay). With `fsync`, you lose **at most the sample
currently being written**.

For high-rate capture, `fsync` per sample is too slow. Use a
**background flush thread** that runs every ~0.5 s:

```python
stop_flag = [False]
def flush_thread_fn():
    while not stop_flag[0]:
        time.sleep(0.5)
        f.flush()
        try: os.fsync(f.fileno())
        except OSError: pass
t = threading.Thread(target=flush_thread_fn, daemon=True)
t.start()
# ... main loop writes samples, f.flush()es per row, thread fsyncs ...
stop_flag[0] = True
t.join(timeout=1.0)
```

This caps data loss at ~0.5 s.

**Layer 2: live-mode stop files**

For captures that should run until the user says stop (not for a
fixed duration), accept a `.stop` sentinel file in the run
directory. The capture loop checks for it every tick:

```python
stop_file = run_dir / ".stop"
while end_time is None or time.monotonic() < end_time:
    if stop_file.exists():
        break
    ...
```

The report.md should document this — "create
`<run_dir>/.stop` to stop cleanly". This is the cleanest way for
a long-running test to be stopped by a non-interactive process
(cron, CI, the AI itself).

**Layer 3: regenerate from the CSV**

If `report.md` or `plot.png` is missing because the process died
between CSV-write-complete and summary-write-start, you can
re-derive them from the partial CSV. The summary/plot functions
should live at **module level** (not inside `main()`) so a
recovery script can import and call them:

```python
# In your script:
def _summarize(samples): ...
def _write_markdown(meta, summary): ...
def _write_plot(samples, summary): ...

# From a recovery shell:
import sys
sys.path.insert(0, "...")
import my_script
my_script.CSV_PATH = Path(".../data.csv")
samples = my_script._read_csv()
summary = my_script._summarize(samples)
my_script._write_markdown(meta, summary)
my_script._write_plot(samples, summary)
```

This is why **don't bury these in `main()`** — they are the public
regeneration API.

### What the report.md should say about how it ended

```markdown
- **Stopped by**: stop_file   (or "duration" / "keyboard_interrupt")
```

So a year from now, when the user looks at a stale report, they
know why it ended.

---

## Appendix A: Pre-flight checklist for the next MCP

Copy this into your scratch when starting:

```markdown
## Pre-flight for mcp_<new>

### Pre-coding (5 minutes)
- [ ] §0 decision tree answered for all 6 questions (transport,
      command style, headline measurements, rate, safety limits,
      settable parameters)
- [ ] VISA resource string OR vendor SDK confirmed (or both — see §16)
- [ ] If vendor SDK: package installable, `scan()` returns the bench unit
- [ ] If SCPI: `*IDN?` returned successfully via pyvisa

### Code skeleton
- [ ] `__init__.py`, `README.md`, `NOTES.md`, `config.py` present
- [ ] `state_machine.py` if §6 heuristic says you need one
- [ ] `scpi.py` (or `commands.py` for non-SCPI — see §16) — one
      function per command, all values validated
- [ ] `instrument.py` — single owner of the transport; no pyvisa /
      vendor imports outside this file
- [ ] `server.py` — fastmcp tools, envelope shape from §5.2
- [ ] If multiple user-settable parameters exist: `get_capabilities()`,
      `recommend_config()`, `configure_for_test()` (see §17)

### Tool shape
- [ ] Result envelope shape matches §5.2
- [ ] Connection / configuration / measurement tools follow §5.1
- [ ] Always-validate, never-trust-the-AI (§5.3)
- [ ] One tool per high-frequency operation; batch tool for the rest

### Hardening
- [ ] At least one set/read cycle works end-to-end on the bench
- [ ] State machine rejects illegal transitions with `ok: False, error: ...`
- [ ] If streaming: statistics callback thread is locked; no call
      back into the device from a callback
- [ ] If long capture: `flush()` + `os.fsync()` on samples;
      background thread if rate > 1 Hz (see §18)
- [ ] Live-mode (`.stop` file) if the capture duration is user-driven
- [ ] 5× connect/measure/disconnect stress test passes

### Reporting
- [ ] Module-level `_summarize` / `_write_markdown` / `_write_plot`
      (public regeneration API per §8)
- [ ] If dynamic range > 100×: emit both linear AND log-y plots
- [ ] If user cycles the DUT through modes: include per-mode
      segmentation in the report (see §8)
- [ ] `report.md` includes "stopped_by" (duration / stop_file /
      keyboard_interrupt) and links to all artifacts

### Tool registration
- [ ] `fastmcp.list_tools()` shows the right count and names
- [ ] NOTES.md has at least sections 1, 2, 3, 10, 11 (from §10)
- [ ] Reference PDFs dropped in `mcp_<new>/reference/`
- [ ] Claude Desktop config updated (`%APPDATA%\Claude\claude_desktop_config.json`)
```
