# MT8852B MCP Server

An MCP (Model Context Protocol) server that exposes an **Anritsu MT8852B
Bluetooth Test Set** as atomic tools for Claude. Built for the CMT BLE
Sticker HWDVT project.

The MT8852B does NOT speak standard colon-separated SCPI — it uses
flat named commands with whitespace-separated keywords and
comma-separated parameters (e.g. `MEASBLECAP LEOP,NA,71764129`).
The full grammar lives in the Anritsu Remote Programming Manual in
`reference/`; the command builders in `scpi.py` are a faithful summary
of the BLE measurement recipe in Ch. 16.

**Two non-obvious gotchas** that the official Anritsu BLE Measurement
Software handles but tripped us up — see [NOTES.md](NOTES.md) for
the full story:

- `SCPTSEL <n>` is **required** before `MEASBLECAPX`, or the test
  silently returns `valid=FALSE`. See NOTES §2b.
- `TESTMODE` is for classic Bluetooth ACL, **not** BLE DTM. Do not
  use it on a BLE Sticker — recovery via `*RST` is documented in §15.

## Files

| File | Purpose |
|---|---|
| `config.py` | VISA resource string + timeouts; env var overrides |
| `scpi.py` | Named command builders (one function per command); no I/O |
| `instrument.py` | pyvisa wrapper (connect / write / query / wait / ORESULT parsing) |
| `state_machine.py` | Session lifecycle (`DISCONNECTED → IDLE → CONFIGURED → TESTING → COMPLETE`) |
| `server.py` | fastmcp entry; defines the MCP tools |
| `scripts/preflight_check.py` | Bench script — validates the 8852B + EUT link is ready (no real test) |
| `scripts/run_ble_tx_test.py` | Bench script — runs one BLE TX test, writes report to `reports/` |
| `reference/` | Anritsu Remote Programming Manual (PDF) + extracted text |

## Setup

```bash
pip install fastmcp pyvisa
# NI-VISA 23.5+ must be installed separately (download from NI).
```

## Connection

Edit the VISA resource in `config.py` or set the env var:

```bash
# GPIB (this lab's unit)
set MT8852B_RESOURCE=GPIB0::27::INSTR
# LAN
set MT8852B_RESOURCE=TCPIP0::192.168.1.100::5025::SOCKET
# Override VISA backend (default niVisa64)
set MT8852B_VISA_LIB=niVisa64
```

## Claude Desktop config

`%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mt8852b": {
      "command": "python",
      "args": ["-m", "mcp_8852b.server"],
      "cwd": "e:\\Centrak\\BLE Sticker\\test\\HWTEST"
    }
  }
}
```

## Tools

> **User-interaction gate.** The MCP channel exposes **only**
> `bt_run_full_suite` for running tests. Every other write/test
> action is reachable only via Python import from `mcp_8852b.server`
> (intended for batch scripts and the `run_ble_full_suite.py` CLI).
> This is enforced — see [The MCP user-interaction gate](#the-mcp-user-interaction-gate)
> and `verify_mcp8852b_gate.py`.

### Connection / lifecycle (MCP-exposed)

| Tool | What it does |
|---|---|
| `bt_connect(resource?)` | Open VISA session |
| `bt_disconnect()` | Close session |
| `bt_status()` | Current state + active plan |

### Diagnostics / read-only (MCP-exposed)

| Tool | What it does |
|---|---|
| `bt_eut_interface()` | Active EUT control interface + DUT BD_ADDR |
| `bt_diag_eut_link()` | EUTSRCE, EUTRS232, ERRLST, `*INS?` decoded — full link-up diagnostic |
| `bt_boot_status()` | `BOOTSTATUS?` — last self-test |
| `bt_ins()` | `*INS?` + bit decode (CMP, CON, INQ, …) |
| `bt_read_oresult(test, ext_code)` | `ORESULT TEST,ext,test` (low-level) |
| `bt_read_script_result(ext_code)` | `ORESULT SCRIPT,ext` (full script, parsed) |
| `bt_preflight()` | Bench-state snapshot (OPTSTATUS, BOOTSTATUS, OPMD, SCPTSEL, LEPKTLEN, EUTSRCE) — called automatically by `bt_run_full_suite` |

### Test entry point — the ONLY way to run tests via MCP

| Tool | What it does |
|---|---|
| `bt_run_full_suite(eut_interface?, tests?, channels?, capture_type?, packet_length?, report_dir?, run_label?, test_params?, eut_baud?, eut_handshake?)` | Interactive campaign. Asks the operator for EUT interface, which tests to run (LEOP / LEICD / LEMI / LESS / LEMP / LEPRI / LEPS), per-test parameters (payload, TX power, FERLIM, …), channels, capture type, packet length, report directory, run label. Returns `state="awaiting_input"` envelopes; the chat agent re-invokes with the answered kwarg. Always asks — no `non_interactive` bypass on the MCP channel. |

### Python library (NOT MCP-exposed — for scripts only)

| Function | What it does |
|---|---|
| `bt_run_tx_test(channel, meas, ...)` | Full Configure → Start → Wait → Read recipe for one TX measurement |
| `bt_configure_ble_tx(...)` | Configure only (no run) |
| `bt_start()` | Send `RUN` |
| `bt_wait_test_complete(timeout_s)` | Poll `*INS?` until CMP bit set |
| `bt_abort()` | `ABORT` and return to IDLE |
| `bt_reset()` | `*RST` + return to IDLE |
| `bt_set_eut_interface(interface, baud, handshake)` | Configure EUT control port |

These exist as plain Python functions on `mcp_8852b.server` for
batch / scripted use. `run_ble_full_suite.py` and any CI regression
script should import them directly:

```python
from mcp_8852b.server import session, bt_run_tx_test, bt_set_eut_interface
session.connect()
bt_set_eut_interface("RS232", baud=115200, handshake="NONE")
result = bt_run_tx_test(channel=19, meas="LEOP")
```

**Do not** add `@mcp.tool(...)` back to these without also adding a
propose/confirm gate — `verify_mcp8852b_gate.py` will fail.

### Result envelope

Every measurement tool returns the same shape so the AI can use it
uniformly:

```json
{
  "ok": true,
  "test": "TX_POWER_DBM",
  "header": "LEOP",
  "channel": 0,
  "capture_type": "BLE",
  "value": -2.34,
  "unit": "dBm",
  "lower": -20.0,
  "upper": 20.0,
  "pass": true,
  "raw_fields": ["-2.34", "-1.94", "-2.78", "0.12", "0", "30"],
  "raw": "LEOP0,TRUE,-2.34,-1.94,-2.78,0.12,0,30,PASS",
  "timestamp": "2026-06-15T10:23:45"
}
```

`pass` is computed against the Bluetooth-spec default pass window
defined in `server.DEFAULT_LIMITS`. Override per-call with
`lower=` / `upper=` (e.g. for a tighter DUT-specific range).

`pass` is `null` when the ORESULT "Results Valid" flag is FALSE
(instrument did not produce a valid measurement). It is `false` when
the value is out of bounds OR when the instrument's own PASS/FAIL flag
is FAIL.

## State machine

```
DISCONNECTED ──bt_connect──▶ IDLE
IDLE ──bt_configure_ble_tx──▶ CONFIGURED
CONFIGURED ──bt_start──▶ TESTING
TESTING ──bt_wait_test_complete──▶ COMPLETE
COMPLETE ──bt_read_oresult──▶ IDLE
COMPLETE ──bt_abort──▶ IDLE
any ──error──▶ ERROR
```

Illegal transitions (e.g. reading results before COMPLETE) raise
`MT8852BError` and the tool returns `{ok: False, error: ...}`.

## The MCP user-interaction gate

The MCP channel enforces **one rule**: an AI client cannot drive a
test without first asking the operator. This is not enforced by a
prompt inside an individual tool — it is enforced by **removing the
test tools from the MCP surface entirely**.

**What the AI sees (the MCP tool registry):**

- Connection: `bt_connect`, `bt_disconnect`, `bt_status`
- Diagnostics / read-only: `bt_eut_interface`, `bt_diag_eut_link`,
  `bt_boot_status`, `bt_ins`, `bt_read_oresult`,
  `bt_read_script_result`, `bt_preflight`
- The one test entry point: `bt_run_full_suite`

**What the AI does NOT see (library-only):**

- `bt_run_tx_test`, `bt_configure_ble_tx`, `bt_start`,
  `bt_wait_test_complete`, `bt_abort`, `bt_reset`,
  `bt_set_eut_interface`

These remain as plain Python functions on `mcp_8852b.server` for
scripts (`run_ble_full_suite.py`, CI regression, custom batch jobs).
They are importable but **not** decorated with `@mcp.tool()`, so
FastMCP does not register them. A chat agent that tries to call
`bt_run_tx_test` via MCP gets a tool-not-found error.

`bt_run_full_suite` is the only path the MCP offers. It:

1. Runs the bench preflight (`bt_preflight`-equivalent) so the
   operator sees the current EUT interface / boot status.
2. Walks the operator through `CAMPAIGN_PROMPT_ORDER`
   (`interactive.py`):
   `eut_interface → tests → capture_type → channels → packet_length
   → run_label → per-test param prompts`. Each step returns a
   `state="awaiting_input"` envelope with `prompt: {id, question,
   choices, default, help}`. The chat agent shows one question at a
   time and re-invokes the tool with the answered kwarg.
3. There is **no `non_interactive` parameter** on the MCP tool —
   that bypass has been removed. The library function
   `mcp_8852b.interactive.run_campaign(session, non_interactive=True,
   ...)` is still available for scripts that explicitly opt out.
4. **Non-skippable final confirmation.** Once every campaign
   parameter has been collected (or pre-filled by the AI), the tool
   emits one more `state="awaiting_input"` envelope with
   `prompt.id="final_confirm"` showing the full plan (EUT
   interface, tests, channels, capture type, packet length, run
   label) and choices `["Yes, run all", "Cancel"]`. Only after the
   operator picks "Yes, run all" does the tool issue
   `session.start()` / `MEASBLECAPX` on the bench. This closes the
   "AI pre-fills every kwarg, human never sees anything" loophole.
   Enforced by `run_campaign(..., force_final_confirm=True)` on the
   MCP path; library callers (CI / batch scripts) leave it False to
   skip the gate. Verified by `verify_mcp8852b_gate.py` test 5.

**Why this design and not a two-step preflight gate** (the
mcp_34465a / mcp_js220 pattern)? Because 8852 has **seven
campaign-level questions plus per-test param forms**, not a single
test. A two-step "propose / user_confirmed" gate per atomic tool
would require either (a) seven separate gates the AI has to
remember to invoke, or (b) bundling everything into one
unsurvivable super-form. The single entry point with a
`NeedInput`-driven state machine is the only design where every
invocation **guarantees** user interaction without trusting the
AI's choice of tool.

Run `python verify_mcp8852b_gate.py` to confirm:

- 7 atomic tools are absent from the MCP registry
- `bt_run_full_suite` has no `non_interactive` parameter
- The 11 expected tools (connection + diagnostics + preflight +
  `bt_run_full_suite`) are still present
- The 18 Python functions are still importable from `mcp_8852b.server`
- `bt_run_full_suite`'s body always passes
  `force_final_confirm=True` to `run_campaign(...)` (test 5)

## The BLE measurement recipe (Ch. 16, §16-9)

For every BLE TX test, the MT8852B requires this exact sequence:

```
SETBLECAPTYP BLE              ; or 2LE / BLR
SYSCFG CONFIG,RANGE,AUTO
CFGBLECAP 0,RF                ; channel, trigger
OPMD SCRIPT
SCPTSEL 3                     ; which script's limits?  DEFAULT 3.
MEASBLECAP LEOP,NA,71764129   ; or MEASBLECAPX for packet-length / MEASBLECAPX2 for CTE
RUN
*INS?                         ; poll until bit 4 (CMP) set
ORESULT TEST,0,LEOP           ; read the result
```

**The 8852B also commands the DUT** to start transmitting — over the
"EUT Control" port on the front panel. Pick the right interface with
`bt_set_eut_interface()` *before* the first test. The 5 choices are
`RS232 / USB / BLE2WIRE / USBBLE2WIRE / USBADAPTOR` (see `NOTES.md`
§14 for when to use each). Most BLE Stickers use `BLE2WIRE`.

The DUT must be in DTM mode before `RUN` — the 8852B's HCI/2-wire
path sends the `HCI_LE_Transmitter_Test` automatically, but the BLE
controller on the DUT must be ready to accept it.

### Payload requirements (Ch. 16 notes)

| Test | DUT payload |
|---|---|
| LEOP (output power) | PRBS9 |
| LEICD (carrier drift) | `10101010` for BLE/2LE, `11111111` for BLR8 |
| LEMI (modulation)   | `10101010` or `11110000` for BLE/2LE, `11111111` for BLR8 |

## DUT skill contract

This MCP only controls the **instrument**. The DUT (PCBA) is expected
to already be in DTM (Direct Test Mode) and to be transmitting BLE
packets on the configured channel by the time `bt_start()` is called.
A separate skill (per platform: Silicon Labs DVK, Nordic DK, …) is
responsible for the AT/HCI commands that put the board into DTM, set
the payload, and select the channel.

Typical sequence:

1. (skill) Put PCBA in DTM, configure PHY & channel & payload
2. (mcp)  `bt_connect`
3. (mcp)  `bt_run_tx_test(channel=20, meas="LEOP")`
4. (skill) Stop DTM on PCBA
5. (mcp)  `bt_disconnect`

## CLI demo

```bash
python -m mcp_8852b.demo                       # use default GPIB resource
python -m mcp_8852b.demo --dry-run             # walk without an instrument
python -m mcp_8852b.demo --csv results.csv     # save results to CSV
python -m mcp_8852b.demo TCPIP0::...::5025::SOCKET
```

## Known limitations

- **CTE / AoA / AoD** (used by BLE Sticker) — `MEASBLECAPX2` is
  supported in `scpi.py` but the headline-metric extractor in
  `server.py` only covers LEOP / LEICD / LEMI. Use
  `bt_configure_ble_tx` + `bt_start` + `bt_wait_test_complete` +
  `bt_read_oresult` for CTE tests and parse the result manually.
- **RX tests** (LESS, LEMP, LEPRI) are not yet wrapped in
  `bt_run_tx_test`. The MT8852B-side commands are simple
  (`ORESULT TEST,0,LESS`) — see NOTES.md §10.
- **Default pass windows** are Bluetooth-spec values, not product
  limits. Override per call with `lower=` / `upper=` or edit
  `DEFAULT_LIMITS` in `server.py`.
- **Single global session per MCP process**. For multi-unit /
  parallel testing, open a second MCP server with a different
  VISA resource.
- **Script-level config** (LEOPCFG, LEICDCFG, etc.) is exposed as
  `scpi.leopcfg(...)` etc. but not yet wrapped as MCP tools. Add
  thin wrappers in `server.py` if you need to change pass/fail limits
  at runtime.

## Test the MCP

```bash
# Walk the wizard-less demo against a real 8852B
python -m mcp_8852b.demo

# Verify the MCP user-interaction gate (no real instrument needed)
python verify_mcp8852b_gate.py

# Run the full BLE test suite via the interactive CLI (legacy path)
python run_ble_full_suite.py

# From a Python REPL — use the LIBRARY surface for one-shot scripts:
from mcp_8852b.server import session, bt_run_tx_test, bt_set_eut_interface
session.connect()
bt_set_eut_interface("RS232", baud=115200, handshake="NONE")
print(bt_status())                                          # IDLE
bt_run_tx_test(channel=20, meas="LEOP")                    # one-shot LEOP
bt_run_tx_test(channel=20, meas="LEICD")                   # one-shot LEICD
bt_run_tx_test(channel=20, meas="LEMI", mod_type="MOD10101010")
bt_disconnect()

# From Claude Desktop — use the MCP surface (always interactive):
#   bt_connect()
#   bt_run_full_suite()        # ← the AI walks the operator through every question
```