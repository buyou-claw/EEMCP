# MT8852B — Lessons Learned

Empirical findings from bench-testing the lab's Anritsu MT8852B
(GPIB address 27, NI-VISA 23.5, firmware to verify with `*IDN?`). Each
item is a **gotcha + the fix**, in priority order. Read this before
writing any new tool or script — most of the time spent on this MCP
was rediscovering these.

The commands documented below come from the Anritsu **Remote
Programming Manual** (part M-W3969AE-8.0, 8th Ed.) which is in
`reference/`. A plain-text extract of every page is in
`reference/extracted/_all_pages.txt` — grep it.

---

## 1. Not standard SCPI

**Gotcha**: every prior attempt to talk to this instrument with
colon-separated SCPI (`:BLU:TXP?`, `:MEAS:POW?`, ...) silently timed
out. The MT8852B does NOT use SCPI.

**Why**: Anritsu designed a flat named-command set before SCPI became
common. The grammar is:

    <CMD> <ws> <param1>[,<param2>...]

with whitespace as separator and commas inside parameter lists.
Examples:

    *IDN?
    SETBLECAPTYP BLE
    CFGBLECAP 0,RF
    MEASBLECAP LEOP,NA,71764129
    RUN
    *INS?
    ORESULT TEST,0,LEOP

**Fix**: every command goes through `scpi.py`. The names there are
canonical — if you find yourself writing a colon string in
`instrument.py` or `server.py`, you're doing it wrong.

---

## 2. The INS register is the only way to know when a test is done

**Gotcha**: the MT8852B does not reply to status queries while a test
is in progress. Polling `:STAT:OPER?` or anything similar hangs the
bus until the test finishes.

**Why**: the 488.2 status register model assumes a continuous poll
loop. The MT8852B firmware blocks the parser until the test completes.

**Fix**: poll `*INS?` instead. Bit 2 (CMP, mask 0x04) sets when the
test/script completes (or is terminated). Our
`instrument.wait_test_complete()` does this with a 100 ms poll.

**Bit map** (Ch. 2, Table 2-7):

| Bit | Mask | Name | Meaning |
|---|---|---|---|
| 0 | 0x01 | CON  | Connected |
| 1 | 0x02 | DIS  | Disconnected |
| 2 | 0x04 | CMP  | **Script/test complete** ← the one we wait on |
| 3 | 0x08 | INQ  | Inquiry complete |
| 4 | 0x10 | OVT  | Over-temperature warning |
| 5 | 0x20 | LEFG | BLE frame generator stopped |

The Anritsu manual numbers bits with bit 0 = LSB. The example
"`*INS?` returns 5 → bit 0 (Connected) + bit 2 (Test or Script
Complete) are set" confirms: 5 = 0x05 = CON (0x01) + CMP (0x04).

The manual says "CMP is cleared when a test starts and set on
completion or termination" — exactly the signal we need.

---

## 2a. The 8852B blocks the parser while a test runs

**Gotcha**: when you send `RUN`, the instrument's GPIB parser locks
for the duration of the test. Any query sent during that window —
including `*INS?` — just sits in the queue and only returns when the
test finishes.

**Why**: the firmware re-uses the GPIB task for both command parsing
and measurement. While a test is running, parsing is paused.

**Implication for polling**: don't tighten the pyvisa per-query
timeout. If you set it to 1 second and the test takes 5 seconds, you
get a `VI_ERROR_TMO` even though the test is healthy. The fix in
`instrument.wait_test_complete()` is to set the per-query timeout to
`timeout_s + 5s` so the pyvisa layer only errors out after the wall-
clock test timeout has already elapsed.

The single `*INS?` call will block until either (a) the test finishes
(CMP bit set) or (b) the wall-clock deadline passes. We use the wall-
clock deadline, not the pyvisa timeout, to decide "test didn't
complete".

**Gotcha**: skipping or re-ordering any step of the BLE measurement
recipe produces "execution error" via ERRLST, or worse, hangs.

**Fix** — the full recipe from Ch. 16 §16-9:

```
SETBLECAPTYP <BLE|2LE|BLR>     # 1. capture type
SYSCFG CONFIG,RANGE,<...>      # 2. RF range (AUTO is safest)
CFGBLECAP <channel>,<RF|EXT>   # 3. channel + trigger
OPMD SCRIPT                    # 4. operation mode
SCPTSEL <1..10>                # 5. WHICH SCRIPT'S LIMITS? (see §2b)
MEASBLECAP[X] <meas>,<mod>,<sync>[,<len>]   # 6. what to measure
RUN                            # 7. START
*INS?                          # 8. poll until CMP bit set
ORESULT TEST,0,<meas>          # 9. read back
```

The state machine (`state_machine.py`) enforces this ordering. Don't
add a "shortcut" that lets the caller skip steps.

---

## 2b. SCPTSEL must be set before MEASBLECAP[X]

**Gotcha**: every BLE TX measurement returns `ORESULT ... FALSE, ...`
(valid=FALSE) if `SCPTSEL` was not issued first. The MT8852B silently
runs the measurement against whatever script was previously selected —
which is usually a stale classic-BT script that has no BLE limits, so
the firmware reports "no valid limits" as `valid=FALSE`.

**Why**: per Ch. 16 §16-4 — "the BLE Tx test measurement is performed
using the limit parameters set in the **selected script**". The MT8852B
Ch. 4 `*OPC` example pairs `OPMD SCRIPT; SCPTSEL 3` every time:
that's not a coincidence, it's the documented prerequisite.

**Fix**: `state_machine.configure_ble_tx()` issues
`OPMD SCRIPT → SCPTSEL <script> → MEASBLECAPX`. The active script
defaults to `3` (first user-configurable); the MCP tool
`bt_run_tx_test(..., script=N)` can override. The MX885203A BLE
Measurement Software uses script 2 for its "FULLTEST" — if your DUT
requires that script's particular limits, pass `script=2`.

---

---

## 4. The DUT's payload matters — and it must match the test

| Test | Required DUT payload |
|---|---|
| LEOP (output power) | PRBS9 |
| LEICD (carrier drift) | `10101010` for BLE/2LE, `11111111` for BLR8 |
| LEMI (modulation) | `10101010` or `11110000` for BLE/2LE, `11111111` for BLR8 |

If the DUT is transmitting the wrong pattern, the 8852B either fails
to capture (timeout at MEASBLECAP) or returns garbage values that
"look real". The MCP layer cannot detect this — your DUT skill must
set the pattern correctly.

---

## 5. ORESULT "Results Valid" flag — false is not the same as FAIL

**Gotcha**: the first field of every ORESULT response is `TRUE` or
`FALSE`. `FALSE` does NOT mean the measurement was 0 — it means
"results invalid, do not use". A typical cause is the test was
aborted or never captured a packet.

**Fix**: `instrument.parse_oresult_block()` exposes it as `r.valid`.
The server layer sets `pass = None` (not False) when `valid` is
False. Don't quietly pass a `valid=False` value through to the
report.

---

## 6. The PASS/FAIL flag the instrument returns may not match your limits

The trailing `PASS` / `FAIL` in an ORESULT response is the
**instrument's** verdict — it's based on the 8852B's default pass
windows for the selected script. If you tightened limits via
`LEOPCFG`, `LEICDCFG`, etc., the instrument already knows — but if
you apply product-specific limits on the host side, you need to
re-compute `pass` yourself. The envelope's `pass` field combines both:
the instrument's PASS/FAIL AND the host's `lower`/`upper` check.

---

## 7. CFO / carrier drift units are Hz, not kHz

**Gotcha**: a previous PoC reported CFO in kHz. The actual ORESULT
field for LEICD is in **Hz** (e.g. `-800` = -800 Hz offset). Don't
re-scale silently — the `unit` field in the envelope is `Hz` so the
AI can read it directly. The spec window is ±150 kHz, which in the
envelope is `lower=-150000, upper=150000`.

---

## 8. Modulation test (LEMI) headline metric is df1_avg, not df1_max

**Gotcha**: LEMI has four numeric fields (df1_max, df1_avg, df2_max,
df2_avg). The Bluetooth spec mandates df1_avg ≥ 185 kHz, so the
headline metric the envelope returns is **df1_avg** (field index 2).
A previous PoC used df1_max by mistake.

---

## 9. Single VISA session at a time

**Gotcha**: the MT8852B over GPIB refuses a second connection while
the first holds the bus. Opening a parallel session hangs until the
first releases. Implication: don't try to "check status" from a
second process while a test is running.

**Recovery**: `TaskStop` the offending Python process, wait 1–2 s,
then retry. The MCP's `bt_connect` will then succeed.

---

## 10. SCPI / Anritsu quick reference

| Want | Command |
|---|---|
| Identity | `*IDN?` |
| Reset | `*RST` |
| Clear status | `*CLS` |
| Read status | `*INS?` |
| Last error | `ERRLST` |
| Self-test status | `BOOTSTATUS?` |
| Set RF range | `SYSCFG CONFIG,RANGE,<AUTO\|n>` |
| Set capture type | `SETBLECAPTYP BLE\|2LE\|BLR` |
| Configure capture | `CFGBLECAP <ch>,<RF\|EXT>` |
| Op mode | `OPMD SCRIPT\|STEST,<test>` |
| Trigger TX test | `MEASBLECAP <LEOP\|...>,<NA\|MOD...>,<sync>` |
| Trigger TX test (packet length) | `MEASBLECAPX <meas>,<mod>,<sync>,<len>` |
| Trigger TX test (CTE) | `MEASBLECAPX2 <meas>,<mod>,<sync>,<len>,<AOA\|AOD1\|AOD2>,<2..20>` |
| Start | `RUN` |
| Abort | `ABORT` |
| Abort BLE capture | `ABORTCAP` |
| Read one test | `ORESULT TEST,<ext>,<test>` |
| Read whole script | `ORESULT SCRIPT,<ext>` |
| Read extended result | `XRESULT <test>,<HOPOFFL\|HOPOFFM\|HOPOFFH>` |
| Set LEOP limits | `LEOPCFG <script>,<field>,<value>` |
| Set LEICD limits | `LEICDCFG <script>,<field>,<value>` |
| Set LEMI limits | `LEMICFG <script>,<field>,<value>` |
| Set LESS limits | `LESSCFG <script>,<field>,<value>` |
| Set LEPRI limits | `LEPRICFG <script>,<field>,<value>` |

### ORESULT response shapes (Ch. 15, §15-6..15-56)

**LEOP0** — BLE output power summary (`ORESULT TEST,0,LEOP`):

```
LEOP0,<valid>,<avg_dBm>,<max_avg_dBm>,<min_avg_dBm>,
        <peak_to_avg_dB>,<failed>,<tested>,<PASS|FAIL>
```

Example:
`LEOP0,TRUE,1.32,1.32,1.32,0.19,0,2,PASS`

Headline = avg_dBm (index 1).

**LEICD0** — BLE carrier frequency offset + drift:

```
LEICD0,<valid>,<avg_fn_Hz>,<max_pos_Hz>,<max_neg_Hz>,
        <drift_rate_Hz_per_50us>,<avg_drift_Hz>,<max_drift_Hz>,
        <failed>,<tested>,<PASS|FAIL>
```

Example:
`LEICD0,TRUE,-800,300,-2300,-1865,-2000,-2000,0,10,PASS`

Headline = avg_fn_Hz (index 1). Spec: ±150 kHz.

**LEMI0** — BLE modulation characteristics:

```
LEMI0,<valid>,<df1_max_Hz>,<df1_avg_Hz>,<df2_max_Hz>,<df2_avg_Hz>,
        <ratio>,<failed_count>,<count>,<failed>,<tested>,<PASS|FAIL>,
        <pass_rate_%>
```

Example:
`LEMI0,TRUE,2.717e+005,2.644e+005,2.086e+005,2.191e+005,0.820,0,8640,0,30,PASS,100.00%`

Headline = df1_avg_Hz (index 2). Spec: ≥ 185 kHz.

**LESS0** — BLE receiver sensitivity (PER-based):

```
LESS0,<valid>,<fer_pct>,<frames_DUT>,<frames_tester>,<PASS|FAIL>
```

Example: `LESS0,TRUE,0.016,1476,1500,PASS`

**LEMP0** — BLE maximum input power:

```
LEMP0,<valid>,<fer_pct>,<frames_DUT>,<frames_tester>,<PASS|FAIL>
```

(Format is the same as LESS0.)

**LEPRI0** — BLE PER report integrity (variable-length; see Ch. 15 §15-55):

```
LEPRI0,<valid>,<mode>,<cycles>,<per_1>,<rcv_1>,<xmt_1>,<PASS|FAIL_1>,...
```

---

## 11. Pre-test checklist

- [ ] `*IDN?` returns a sensible serial and firmware
- [ ] `bt_boot_status()` reports `PASS` (or the last self-test result)
- [ ] `*INS?` shows the unit is in IDLE / CONFIGURED (not TESTING) before the test
- [ ] DUT is in DTM mode and transmitting on the correct channel
- [ ] DUT payload matches the test (PRBS9 for LEOP, 10101010 for LEICD, etc.)
- [ ] `bt_status()` shows the right plan after `bt_configure_ble_tx`
- [ ] If you tightened pass/fail limits, send the right `LE*CFG` command before `RUN`
- [ ] VISA resource matches `MT8852B_RESOURCE` env var or the `config.py` default

---

## 12. Per-channel gotchas (BLE-specific)

- **Channel 37 / 38 / 39 are the advertising channels.** They are valid
  for data tests too, but the DUT's hopping behavior may interfere if
  the DUT is not in pure DTM mode. Stick to mid-band (ch 17–25) for
  sanity-check runs.
- **Coded PHY (BLR, S=2 / S=8) requires option 36 or 62.** Without it,
  `SETBLECAPTYP BLR` succeeds but `MEASBLECAP LEOPLR8` returns an
  execution error.
- **2LE (2 Msym/s) requires option 35.** Without it, `SETBLECAPTYP 2LE`
  fails similarly.

---

## 13. `TaskStop` behavior

**Gotcha**: `TaskStop` in the Claude harness kills the Python process
without running `finally` blocks. The MT8852B may be left mid-test
(bits 4/2 set in `*INS?`, no way for the next call to know).

**Mitigation**: every MCP entry point that talks to the instrument
sends `ABORT` if the state is wrong (look for `MT8852BError` paths
that catch it). After a hard kill, the next `bt_connect` followed by
`bt_reset` returns the unit to a clean state.

For longer bench scripts (`mcp_8852b/scripts/run_ble_tx_test.py`,
custom test runners), wrap the main loop in `try/except KeyboardInterrupt`
and call `bt_abort()` in the finally block so the 8852B doesn't sit
in TESTING state forever.

---

## 14. EUT (DUT) interface — the 8852B has 5 ways to talk to the DUT

The MT8852B doesn't just passively listen on its RF input. It also
**commands** the DUT to start transmitting on a specific channel with
a specific payload. The physical interface used for that control link
must be configured before any BLE TX test. The MCP exposes this as
`bt_set_eut_interface()` and the bench script
`mcp_8852b/scripts/preflight_check.py --interface <...>`.

| `interface` | Physical cable | When to use |
|---|---|---|
| `RS232`        | Front-panel "EUT Control" → DUT UART (4-wire, 8N1) | DUT exposes a Bluetooth HCI UART. Common on dev boards and some modules. **Pass `baud` (default 115200).** |
| `USB`          | Front-panel USB-A → DUT USB | DUT enumerates as a Bluetooth HCI device (typical for laptops / dev kits). |
| `BLE2WIRE`     | Front-panel "EUT Control" → DUT 2-wire (CC debug-style) | **Most BLE Stickers** — Anritsu's proprietary 2-wire protocol. Talks directly to the BLE controller's test registers; doesn't need a full HCI driver. |
| `USBBLE2WIRE`  | USB-A → adapter → 2-wire | Same as BLE2WIRE but the upstream side of the adapter is USB. |
| `USBADAPTOR`   | USB-A → CDC-ACM adapter → DUT UART | DUT has a UART HCI but you want to use the USB port. |
| ~~`INQUIRY`~~  | — | **Not valid for BLE** (classic-BT only). |
| ~~`MANUAL`~~   | — | **Not valid for BLE** (requires a known BD_ADDR). |

The setting is sticky: once `bt_set_eut_interface` succeeds, it stays
configured until the next call. The 8852B also "initialises the EUT
for tests" on this command (per manual Ch. 5 § EUTSRCE) — typically
a HCI_Reset, which causes the BLE controller to reboot. Allow ~1 s
before the next TX test.

### Verifying the interface works

`preflight_check.py` (in `mcp_8852b/scripts/`) checks 7 things in
order. Step 5 (EUT address acquisition) is the killer: if the 8852B
returns `000000000000` for `SYSCFG? EUTADDR`, the EUT link is broken.
**Don't** skip to "the RF path is bad" — fix the control link first.

```bash
python -m mcp_8852b.scripts.preflight_check --interface BLE2WIRE
```

### Common failure modes

- **RS232 + baud mismatch.** The 8852B defaults to 115200. If the
  DUT's HCI UART is at 9600 (some Nordic / TI bootloaders), no
  traffic. Fix: pass `--baud 9600`.
- **DUT not in DTM mode.** Some BLE controllers need an explicit
  "enter test mode" command from the application processor before
  they'll accept HCI_LE_Transmitter_Test. The 8852B's HCI path
  assumes the controller is in a "test-ready" state.
- **Wrong cable wired to wrong port.** The MT8852B's "EUT Control"
  port is on the **front** panel — not the rear RS232 (which is for
  PC comms). The rear RS232's mode is set with
  `SYSCFG CONFIG,RSMODE,EXTCOM` (default) or `EXTHCI` (when using
  the rear port for EUT HCI — less common).
- **2-wire cable polarity.** The Anritsu 2-wire cable has a specific
  pinout (CCLK, CDIO, GND, VDD). If you've hand-rolled a cable, swap
  the data/clock lines and try again.

### What the 8852B actually sends

When `MEASBLECAPX <meas>,<mod>,<sync>,<len>` + `RUN` is called and
the EUT link is up, the 8852B internally sends (over the configured
interface) the standard Bluetooth HCI command
`HCI_LE_Transmitter_Test {TX_Channel, Length, Payload_Type}` to the
DUT. The 8852B then waits for packets to arrive on its RF input and
measures them. After the capture, it sends `HCI_LE_Test_End`.

For RX tests (LESS / LEMP / LEPRI), the roles reverse: the 8852B
generates the BLE packets, the DUT's receiver counts them, and the
8852B reads back the count via `HCI_LE_Test_End` → `LE_Test_End` event
parameters.

---

## 15. TESTMODE is for classic Bluetooth ACL, NOT BLE DTM

**Gotcha**: `TESTMODE` (Ch. 17 §17-12) is documented as
"Put the EUT into Test Mode" and looks like the obvious thing to
send before `MEASBLECAPX`. **It is the wrong command for BLE DTM.**

**Why**:
- TESTMODE is for **classic Bluetooth ACL test mode**. It requires an
  ACL connection to exist (see `TXTEST` in Ch. 17 §17-14: "rejected
  with an execution error if an ACL connection does not already exist
  or if the device is not already in test mode").
- A BLE Sticker is BLE-only and doesn't support classic ACL test mode.
  When the 8852B sends TESTMODE, the DUT may either silently NACK
  (no execution error visible) or enter an unexpected state (the lab's
  unit showed "in calibration mode" on the front panel after a stray
  TESTMODE — recovery: `*RST`).
- The legitimate path to BLE DTM is to send the BLE test command
  directly. `MEASBLECAPX <meas>,<mod>,<sync>,<len>` does this
  internally per Ch. 16 and the BLE Measurement Software App Note
  (PN 13000-00323): "For EUTs with a Direct Test Mode interface, the
  control commands are sent to the DUT when the capture is initiated."

**Fix**: do NOT send TESTMODE for BLE testing. Use MEASBLECAPX + RUN.
The MCP does NOT issue TESTMODE in any code path.

**Recovery if you sent TESTMODE anyway**:
```
*RST                # back to factory defaults
*CLS                # clear status
SYSCFG EUTSRCE,...  # re-init the EUT link
```

---

## 16. *RST recovery — the "instrument is wedged" escape hatch

If the 8852B is in a weird state (e.g. showed "in calibration mode",
or `*INS?` returns unexpected bits, or the front panel refuses to
respond), the recovery sequence is:

```
*RST      # full reset; clears EUT link, scripts, range, OPMD
*CLS      # clear status registers (ESR, EUT fail, BLE fail)
SYSCFG EUTRS232,115200      # if you want RS232
SYSCFG EUTHANDSHAKE,NONE
SYSCFG EUTSRCE,RS232        # re-initialise the EUT link
*OPC?                      # wait for init to settle
```

Then re-run the preflight check. The bench script
`mcp_8852b/scripts/preflight_check.py` does this sequence at step 0 if

---

## 17. The MCP user-interaction gate — atomic tools removed from the MCP surface

**Decision (2026-06-18)**: the MCP channel exposes **only**
`bt_run_full_suite` for running tests. The seven atomic test tools
(`bt_run_tx_test`, `bt_set_eut_interface`, `bt_configure_ble_tx`,
`bt_start`, `bt_wait_test_complete`, `bt_abort`, `bt_reset`) are
intentionally **not** decorated with `@mcp.tool()`. They remain as
plain Python functions on `mcp_8852b.server` for the script layer
(`run_ble_full_suite.py`, CI regressions, custom batch jobs).

**Why not the mcp_34465a / mcp_js220 two-step preflight pattern?**

The 34465a and js220 each have ONE action that writes to the bus
(DMM `measure_volt_dc`, JS220 `collect_for`), so a single
`<verb>_preflight_<verb>` + `<verb>_run_preflight_<verb>` pair with
a `user_confirmed=True` hard gate is sufficient. The 8852B has
**seven** campaign-level questions (eut_interface → tests →
capture_type → channels → packet_length → run_label → per-test
param prompts) before any test can run. Wrapping each atomic tool
with its own gate would require either seven gates the AI has to
remember to invoke, or one super-form that defeats the gate (the
AI fills in defaults and the human never sees anything). The
single-entry-point design (one tool, state-machine-driven
`NeedInput` envelopes, no `non_interactive` bypass on the MCP
channel) is the only design where every invocation **guarantees**
operator interaction without trusting the AI's choice of tool.

**Enforcement:**

- `mcp_8852b/server.py` — the seven atomic tools exist as plain
  Python functions, no `@mcp.tool()` decorator. The comment above
  each `def` says so.
- `bt_run_full_suite` — the `non_interactive` parameter has been
  removed from the MCP-exposed signature; the internal call to
  `mcp_8852b.interactive.run_campaign` passes `non_interactive=False`
  unconditionally. Scripts that need batch mode call
  `run_campaign(session, non_interactive=True, ...)` directly.
- `verify_mcp8852b_gate.py` — 4 tests, no real instrument:
  1. 7 atomic tools are absent from the MCP tool registry.
  2. `bt_run_full_suite` has no `non_interactive` parameter.
  3. Library import channel is intact (scripts can call atomic
     functions directly).
  4. The 11 expected MCP tools (connection + diagnostics +
     preflight + `bt_run_full_suite`) are still present.

**Follow-up (2026-06-18, final confirmation gate).** The above
removes the "AI chooses the wrong atomic tool" loophole but not the
"AI pre-fills every kwarg and skips every question" loophole — an
AI client could call `bt_run_full_suite(eut_interface="RS232",
tests=["LEOP"], capture_type="BLE", channels=[19],
packet_length=37, run_label="quick_check")` on the first try, and
each `prompt_*()` function would silently validate-and-return the
override. The operator never sees anything. To close that, we added
a **non-skippable final confirmation prompt** in
`mcp_8852b/interactive.py`:

- New function `prompt_final_confirm(plan, *, override=None,
  non_interactive=False, cli_input=input) -> bool` — model on
  `prompt_no_control_confirm`. In MCP mode it raises `NeedInput`
  with `state="awaiting_input"` and
  `prompt: {id: "final_confirm", question: "Run the BLE test
  campaign now?", choices: [Yes, run all / Cancel], default: "no",
  help: <summary + plan>, plan: {...}}`. In CLI mode it prints a
  banner with the summary and asks yes/no.
- New `run_campaign(..., force_final_confirm: bool = False)`
  keyword. When `True`, the prompt is emitted AFTER every campaign
  parameter has been collected AND `apply_test_params()` has run
  (so per-test params are already on the bench), but BEFORE
  `_execute_campaign()` does any `session.start()` /
  `MEASBLECAPX`. Library callers leave it False (default) so they
  can run unattended; the MCP tool sets it to True unconditionally.
- `bt_run_full_suite`'s body now passes
  `force_final_confirm=True` on the `run_campaign(...)` call. There
  is no parameter on the MCP signature to bypass it — the gate is
  source-level enforced. See the new docstring paragraph "Non-
  skippable final confirmation" on the tool.
- `verify_mcp8852b_gate.py` test 5: parses the source of
  `bt_run_full_suite` and asserts (a) `prompt_final_confirm` is
  exported from `mcp_8852b.interactive`, and (b) the body contains
  `force_final_confirm=True` and never `force_final_confirm=False`
  or any other value. Catches accidental removal or weakening.

**If you want to add a new atomic tool**: it must be a plain
Python function on `mcp_8852b.server`, NOT a `@mcp.tool()`-decorated
function. Re-adding a decorator without adding a propose/confirm
gate will fail `verify_mcp8852b_gate.py` test 1.

**If you want to run a one-shot batch** (CI, regression, scripted
characterisation): import directly:

```python
from mcp_8852b.server import session, bt_run_tx_test, bt_set_eut_interface
session.connect()
bt_set_eut_interface("RS232", baud=115200, handshake="NONE")
r = bt_run_tx_test(channel=19, meas="LEOP", capture_type="BLE",
                   packet_length=37)
session.disconnect()
```

This path bypasses the operator gate by design — the script is
the operator.
it detects a non-zero boot status or `*INS?` non-IDLE.