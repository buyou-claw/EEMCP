# SPEC — MT8852B BLE Test MCP, Interactive Operator Flow

> Status: DRAFT v1 (LEAD agent)
> Target: refactor of `mcp_8852b/` so that running a BLE TX/RX test campaign
>
> **已实现部分**（2026-06-18）:
> - §3 connection-mode prompt: 实现于 `mcp_8852b/server.py:660 bt_preflight`
> - §5.3 prompt envelope shape: 实现于 `mcp_8852b/interactive.py`（Preflight/run_preflight/run_campaign）
> - §7 v6.8 三 fix: 已在 `mcp_8852b/state_machine.py:343-456, 462-509, 544-574` 落地
> is a guided, operator-confirmed flow rather than "the agent silently
> re-uses captured defaults".

---

## 1. Goal

The current MCP has fine-grained atomic tools (`bt_set_eut_interface`,
`bt_run_tx_test`, etc.) that work, but the operator-facing workflow is
incomplete: there is no prompt asking which EUT interface is wired up,
no prompt asking which subset of BLE tests to run, no per-test parameter
form (e.g. TX power level, sensitivity search level, FER/PER limit), and
the CLI runner `run_ble_full_suite.py` is a one-shot script — not an
interactive surface that the MCP can drive from a chat session.

The goal of this refactor is to provide a single, properly interactive
entry point that:

1. Asks the operator for the EUT wiring mode (HCI RS232 / HCI USB /
   2-Wire / Vendor-Controlled / 2-Wire-USB) — only the choices that
   actually change the bench code path.
2. Asks the operator which tests to run (LEOP, LEICD, LEMI, LESS, LEMP,
   LEPRI, LEPS), and for each selected test collects the parameters
   the Anritsu BLE Measurement Software GUI exposes (and only those
   that are not already filled in by the lab's preloaded script).
3. Asks the operator where to save the report (default:
   `reports/ble_full_<ts>/`).
4. Runs the bench, aggregates results, writes
   `report.md` + `summary.json` + `data.csv` (the format
   `run_ble_full_suite.py` already produces — keep that contract).

Operator-confirmed values are NEVER silently overridden by an agent
default. The fast-path heuristics the lab uses today
(`skip_if_ready=True`, auto RS232 init, default RS232) remain as
defaults the operator can accept with a single keystroke — but they
must be VISIBLE in the prompt so the operator can change them.

---

## 2. Connection-mode prompt

### 2.1 What the operator sees

```
Step 1 / 3 — How is the MT8852B wired to the DUT?

  1) HCI RS232 (EUT Control port → UART, 4-wire, 8N1)
  2) HCI USB   (DUT enumerates as a Bluetooth HCI device on USB)
  3) 2-Wire    (Anritsu proprietary CC-debug-style for BLE-only controllers)
  4) 2-Wire-USB (USB→2Wire adapter, USB on PC side, 2-Wire on DUT side)
  5) USB-Serial (USB-to-serial adapter, CDC ACM)
  6) No Control (DUT driven by vendor's own control software)

Which connection? [1] >
```

If the operator picks `1) HCI RS232`, follow up:

```
RS232 settings
  Baud rate   [115200]   (1200 / 2400 / 4800 / 9600 / 19200 / 38400
                          / 57600 / 115200 / 230400 / 460800)
  Handshake   [NONE]     (NONE / RTSCTS  — XON/XOFF not supported)
```

### 2.2 What determines the SCPI code path

The mapping is exact and already implemented in
`mcp_8852b/instrument.py::MT8852B.set_eut_interface()` and
`mcp_8852b/state_machine.py::MT8852BSession.set_eut_interface()`:

| Operator choice | SCPI path |
|---|---|
| `1) HCI RS232` | `SYSCFG EUTRS232,<baud>` → `SYSCFG EUTHANDSHAKE,<NONE\|RTS/CTS>` → `SYSCFG EUTSRCE, RS232` |
| `2) HCI USB`   | `SYSCFG EUTSRCE, USB` (no baud/handshake) |
| `3) 2-Wire`    | `SYSCFG EUTSRCE, BLE2WIRE` |
| `4) 2-Wire-USB`| `SYSCFG EUTSRCE, USBBLE2WIRE` |
| `5) USB-Serial`| `SYSCFG USBADAPTOR,PORT,A` then `SYSCFG EUTSRCE, USBADAPTOR` |
| `6) No Control`| **No** `SYSCFG EUTSRCE`. Per `BLE_MSM §2-2` and App Note 13000-00323, Script Testing is unavailable — only single-shot RX/TX tests via vendor control software. The MCP must warn the operator: *"Script testing is disabled when EUT Control = No Control. You can still run the per-test menu, but the 8852B cannot issue HCI_Reset / HCI_LE_Transmitter_Test — the vendor app drives the DUT."* |

The `RS232` path then triggers the full v6.8 unstick trio (SCPTRST +
SCPTTSTGP STDTSTS,OFF + EDRTSTS,OFF + BLETSTS,ON) only if
`skip_if_ready` is False or the bench isn't already in BLE script mode.

### 2.3 What does NOT change in `state_machine.py`

- `set_eut_interface()` signature stays the same:
  `set_eut_interface(interface, baud=None, handshake=None, init_timeout_s=10.0, skip_if_ready=True)`.
- The fast-path probe `_bench_ble_ready()` stays as-is.
- The unstick trio is unchanged.

### 2.4 What is new

- A prompt function in the new module (e.g. `interactive/prompts.py`)
  called `prompt_eut_interface() -> EutConfig` that returns the same
  shape `EutConfig` already uses (`interface`, `baud`, `handshake`,
  `eut_address`).
- The existing `bt_set_eut_interface` MCP tool keeps its current
  signature so it can still be called programmatically — but the
  top-level `bt_run_full_suite` (see §5) calls the prompt first.

---

## 3. Per-test parameter prompt

For each test the operator selects, ask for the parameters the BLE
Measurement Software GUI exposes (per `BLE_MSM` Chapters 4 / 5 / 7).
Where the lab's preloaded script 3 already provides a working default,
we default the prompt value to that and the operator hits Enter.

The mapping below cites the SCPI command that ultimately sets the value
(per `Remote_Programming_Manual.txt` Ch. 10) and the GUI control it
mirrors (per `BLE_Measurement_Software_Operation_Manual.txt` Ch. 4/5/7).

### 3.1 Common to all tests (per-test, not global)

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| Frequency / channel | "Frequency" drop-down (Ch.4 step 2 / Ch.5 step 4 / Ch.7 step 2) | `CFGBLECAP <ch>,RF` and per-test `*CFG L/T/H FREQSEL,ON` | low / mid / high = ch 0 / 19 / 39, all three on | 0..39 |
| Capture type (data rate) | "Data Rate" (Ch.4 step 9 / Ch.5 step 10) | `LESCPTCFG <script>,LEPKTTYPE,<BLE\|2LE\|LR8\|LR2>,TRUE` | BLE | BLE / 2LE / LR8 / LR2 |
| Trigger | "Trigger" (Ch.4 step 4) | `CFGBLECAP <ch>,RF` / `EXT` | Internal RF (RF) | RF / EXT — warn if EXT (no numeric results per Ch.4 note) |
| Sync word | "Sync word" (Ch.4 step 6) | hard-coded default; no operator input on lab bench | `71764129` (decimal) — locked | 8-hex |
| Number of packets | "Number of packets" (Ch.4 / Ch.5) | `*CFG <script>,NUMPKTS,<n>` | 10 (script 3 default) | 1..10000 |
| Path loss offset | (Script dialog Ch.6 step 4) | applies to RF level offset, not an SCPI var we own | 0 dB | float |

### 3.2 LEOP — Output Power (TX, BLE_MSM Ch.4)

Per `RPM p.10-38 §LEOPCFG` and `BLE_MSM p.4-2 §4`:

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **Payload** | "Payload" (Ch.4 step 7) | `LESCPTCFG ... LEPKTTYPE,BLE,TRUE` (pkt type) and the script's stored payload type | **PRBS9** (per RPM p.10-38 — "PRBS9 payload type is the default for output power") | PRBS9 / 10101010 / 11110000 |
| **Packet length** | "Payload length" (Ch.4 step 8) | `LEPKTLEN <script>,<n>` | 37 | 2..255 |
| **AVGMXLIM** (avg high limit, dBm) | (Script dialog — Output Power Test Conditions) | `LEOPCFG <script>,AVGMXLIM,<val>` | 20.0 | -50..+30 |
| **AVGMNLIM** (avg low limit, dBm) | same | `LEOPCFG <script>,AVGMNLIM,<val>` | -20.0 (per `_limits_for` in server.py) | -50..+30 |
| **PEAKLIM** (peak-to-avg limit, dB) | same | `LEOPCFG <script>,PEAKLIM,<val>` | (script 3 default) | float dB |
| **EUT Tx Power level** | "EUT Tx Power" drop-down (Ch.4 step 3 / Ch.6 step 2) | no SCPI — the 8852B is in AUTO ranging by default; affects bench RF path | "Automatic Ranging" (i.e. `SYSCFG CONFIG,RANGE,AUTO`) | AUTO / 1 / 2 / 3 |

### 3.3 LEICD — Carrier Frequency Offset & Drift (TX, BLE_MSM Ch.4)

Per `RPM p.10-40 §LEICDCFG`:

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **Payload** | "Payload" (Ch.4 step 7 — must be `10101010` per Ch.4 note) | stored in script 3 | **10101010** | locked to 10101010 (BLE/2LE) or 11111111 (BLR8) per RPM p.10-40 |
| **Packet length** | "Payload length" | `LEPKTLEN <script>,<n>` | 37 | 2..255 |
| **MXPOSLIM** (positive offset limit, Hz) | script dialog | `LEICDCFG <script>,MXPOSLIM,<val>` | 150000 (per `_limits_for`) | Hz |
| **MXNEGLIM** (negative offset limit, Hz) | script dialog | `LEICDCFG <script>,MXNEGLIM,<val>` | -150000 | Hz |
| **DFTBLELIM** (packet drift limit, Hz) | script dialog | `LEICDCFG <script>,DFTBLELIM,<val>` | 50000 (RPM p.10-40 default) | Hz |
| **INITDFTBLERATE** (initial drift rate limit, Hz/50 µs) | script dialog | `LEICDCFG <script>,INITDFTBLERATE,<val>` | (script 3 default) | Hz |
| **DFTBLERATE** (drift rate limit, Hz/50 µs) | script dialog | `LEICDCFG <script>,DFTBLERATE,<val>` | (script 3 default) | Hz |

### 3.4 LEMI — Modulation Characteristics (TX, BLE_MSM Ch.4)

Per `RPM p.10-43 §LEMICFG`. Note the **TOGGLE** field per Ch.12 §TOGGLE:
the BLE RF spec requires two payloads (10101010 + 11110000). TOGGLE=ONCE
runs each once; TOGGLE=CONT (default) runs each per measurement stage.

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **Payload pattern** | "Payload" (Ch.4 step 7 — see Ch.4 note: 10101010 OR 11110000; PRBS9 = "Modulation Index measurement is unavailable if the payload is set to PRBS9") | script stored | both, TOGGLE=CONT | 10101010 / 11110000 |
| **TOGGLE** | (script dialog) | `LEMICFG <script>,TOGGLE,<ONCE\|CONT>` | CONT (spec-compliant) | ONCE / CONT |
| **F1AVGMIN** (f1avg min limit, Hz) | script dialog | `LEMICFG <script>,F1AVGMIN,<val>` | 185000 (per `_limits_for`) | Hz |
| **F1AVGMAX** (f1avg max limit, Hz) | script dialog | `LEMICFG <script>,F1AVGMAX,<val>` | (script 3 default — spec has no max) | Hz |
| **F2MAXLIM** (f2max limit, Hz) | script dialog | `LEMICFG <script>,F2MAXLIM,<val>` | (script 3 default) | Hz |
| **F1F2MAX** (f1/f2 ratio max) | script dialog | `LEMICFG <script>,F1F2MAX,<val>` | (script 3 default) | float |

### 3.5 LESS — Receiver Sensitivity (RX, BLE_MSM Ch.5)

Per `RPM p.10-48 §LESSCFG` and `BLE_MSM Ch.5-2 §5-2` (DTM-controlled RX).
This is the test where the operator judgment matters most.

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **Payload** | "Payload" (Ch.5 step 6) | script stored | PRBS9 (per RPM p.10-48) | PRBS9 |
| **Packet length** | "Payload length" (Ch.5 step 7) | `LEPKTLEN <script>,<n>` | 37 | 2..255 |
| **TXPWR** (EUT Rx power level, dBm) | "Power Level" drop-down (Ch.5 step 5) | `LESSCFG <script>,TXPWR,<val>` | -70 (typical for BLE Sticker) | -90..0 dBm |
| **FERLIM** (PER limit, %) | (script dialog) | `LESSCFG <script>,FERLIM,<val>` | 30.8 (Bluetooth spec default for 37-byte packets) | 0.1..100.0 |
| **NUMPKTS** (per Ch.5 step 11) | "Packets" / Number of packets | `LESSCFG <script>,NUMPKTS,<n>` | 1500 (script 3 default) | 1..10000 |
| **Dirty TX** | (Ch.7 step 8) | `LESSCFG <script>,DIRTYTX,<ON\|OFF>` | OFF | ON / OFF |

If the operator wants a **sensitivity sweep** (BLE_MSM Ch.7), expose
the Ch.7 fields as an opt-in sub-prompt:

| Sweep param (Ch.7) | SCPI | Default | Validation |
|---|---|---|---|
| Start RF level (dBm) | pre-configures `LESSCFG TXPWR` start | -90 | -90..0 |
| Stop RF level (dBm) | stop | -60 | -90..0 |
| Step size (dB) | controls how many `TXPWR` steps | 2 | 1..10 |
| PER limit for sweep stop | `LESSCFG FERLIM` | 30.8 | 0.1..100.0 |
| Offset (path loss, dB) | applied as additive offset to TXPWR | 0 | float |

Note: Ch.7 is a per-channel sensitivity sweep. The official
`bt_run_tx_test` does NOT support a sweep loop — see §6 (MCP tool
surface) for the `bt_run_full_suite` extension.

### 3.6 LEMP — Maximum Input Signal (RX, BLE_MSM Ch.5)

Per `RPM p.10-52 §LEMPCFG`:

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **Payload** | "Payload" (Ch.5 step 6) | script stored | PRBS9 | PRBS9 |
| **Packet length** | "Payload length" (Ch.5 step 7) | `LEPKTLEN <script>,<n>` | 37 | 2..255 |
| **TXPWR** (EUT Rx power level, dBm) | "Power Level" drop-down (Ch.5 step 5) | `LEMPCFG <script>,TXPWR,<val>` | -10 (spec uses ~-10 dBm for max-input test) | -90..0 dBm |
| **FERLIM** | (script dialog) | `LEMPCFG <script>,FERLIM,<val>` | (script 3 default — typically 1 % for max-input) | 0.1..100.0 |
| **NUMPKTS** | "Packets" | `LEMPCFG <script>,NUMPKTS,<n>` | 1500 | 1..10000 |

### 3.7 LEPRI — PER Report Integrity (RX, BLE_MSM Ch.5)

Per `RPM p.10-50 §LEPRICFG`. Per App Note 13000-00323 §Rx, the 8852B
sends PRBS9 packets at -30 dBm with alternating valid/invalid CRC and
the EUT reports the count. **NUMPKTS must be EVEN** when PKTNUMMODE=FIXED
(RPM p.10-50, p.12-20).

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **PKTNUMMODE** | (script dialog) | `LEPRICFG <script>,PKTNUMMODE,<RANDOM\|FIXED>` | RANDOM (default per RPM p.12-22) | RANDOM / FIXED |
| **NUMPKTS** (FIXED mode only) | (script dialog) | `LEPRICFG <script>,NUMPKTS,<n>` | 100 (even) | even, 10..10000 |
| **NUMCYC** (cycles) | (script dialog) | `LEPRICFG <script>,NUMCYC,<n>` | 3 | 1..5 (per RPM p.12-19) |
| **TXPWR** (EUT Rx power level, dBm) | (script dialog) | `LEPRICFG <script>,TXPWR,<val>` | -30 (per RPM p.10-50) | -90..0 |
| **LOWPERLIM** / **HIGHPERLIM** | (script dialog) | `LEPRICFG <script>,LOWPERLIM,<lo>` / `HIGHPERLIM,<hi>` | (script 3 default) | float % |

### 3.8 LEPS — Tx Power Stability (TX, BLE_MSM Ch.4, Opt 37 only)

Per `RPM p.10-46 §LEPSCFG`. **Requires option 37 (BLE CTE / 2LE CTE)**
on the MT8852B. The bench is currently NOT option-37-equipped per
project memory — the prompt must check `OPTSTATUS?` and skip LEPS
with a clear message if option 37 is absent.

| Prompt | GUI control | SCPI | Default | Validation |
|---|---|---|---|---|
| **CTESLOT** (CTE slot duration, 1US / 2US) | (script dialog) | `LEPSCFG <script>,CTESLOT,<1US\|2US>,<TRUE\|FALSE>` | 2US TRUE | 1US / 2US |
| **NUMANT** / **NUMANTMODE** | (script dialog) | `LEPSCFG <script>,NUMANTMODE,<AUTO\|MANUAL>` + `LEPSCFG <script>,NUMANT,<n>` | AUTO (per RPM p.12-17) | AUTO / MANUAL + 2..75 |
| **SWPATLEN** (switching pattern length) | (script dialog) | `LEPSCFG <script>,SWPATLEN,<n>` | 2 | 2..75 |
| **REFPWRLIM** (reference power ratio limit, dB) | (script dialog) | `LEPSCFG <script>,REFPWRLIM,<val>` | (script 3 default) | float dB |
| **SLOTPWRLIM** (slot power ratio limit, dB) | (script dialog) | `LEPSCFG <script>,SLOTPWRLIM,<val>` | (script 3 default) | float dB |
| **Packet length** | (Ch.4 step 8) | `LEPKTLEN <script>,<n>` (use `0` per `scpi.meas_ble_cap_cte` doc) | 0 (per `scpi.py` line 405) | 0 or 2..255 |
| **CTE time** (2..20, units of 8 µs) | (script dialog) | `LEPSCFG <script>,LECTETIME,<n>` | 20 | 2..20 |

### 3.9 Common per-run parameters

After per-test params, the prompt asks for the campaign-wide plan:

| Prompt | Default | Validation |
|---|---|---|
| Channels to test (low/mid/high/all/single/<n,m,...>) | low+mid+high (0, 19, 39) | 0..39 |
| Output report directory | `reports/ble_full_<YYYYMMDD_HHMMSS>/` | absolute path under `reports/` |
| Run label (suffix) | empty | free text, sanitized |

---

## 4. Per-test code path differences (HCI vs 2-Wire)

The MT8852B's BLE Measurement Software treats all five EUT modes
identically **for the bench-side SCPI** — the difference is what HCI
commands the 8852B sends to the DUT:

| Step in `configure_ble_tx` → `start` | HCI (RS232 / USB / USBADAPTOR) | 2-Wire (BLE2WIRE / USBBLE2WIRE) |
|---|---|---|
| `SYSCFG EUTSRCE, <iface>` | sends HCI_Reset + Read_Local_Version + Read_BD_ADDR over the wired HCI transport | sends the equivalent vendor "enter test mode" command over the 2-Wire CC-debug-style bus |
| Per-test TX trigger (after `OPMD SCRIPT` + `SCPTSEL` + `SCPTTSTGP`, before `RUN`) | `SERVULP <meas>,<payload>,<mod>,<pktlen>,` then `RUN` | same — the 8852B's vendor-2wire code path translates the SERVULP into the 2-wire equivalent |
| Per-test RX trigger (LESS / LEMP / LEPRI) | `SERVULP` (with no payload, mod=NA), then `RUN`; the 8852B transmits and the EUT reports RX count via HCI_LE_Test_End | same wire form; transport is 2-Wire |
| `ULPDUTTSTEND` (drain) | identical | identical |
| `ULPDUTTXTEST <ch>,<len>,<pattern>` (DTM shortcut — per `scpi.ulpdut_txtest`) | available; bypasses SERVULP+RUN. **NOT** what the v6 path uses (per `project_8852b_test_trigger_block.md`) | available; not preferred for the v6 path |
| HCI handshake (RS232 only) | NONE / RTS/CTS — operator-prompted | n/a (2-Wire has no handshake) |
| RS232 baud | operator-prompted | n/a |

**There is no separate `configure_ble_tx` fork for HCI vs 2-Wire in
the current state machine.** The bench's `SYSCFG EUTSRCE, <iface>`
choice is what selects the underlying vendor translation. This
refactor does NOT add a code fork — it only changes how the operator
is prompted for `interface`, and adds a `(None, No Control)` option
that explicitly skips `SYSCFG EUTSRCE`.

The one place a 2-Wire DUT might differ operationally: the
`_wait_eut_ready()` poll loop in `state_machine.py:269` waits for
EUTADDR to become non-zero. Per Anritsu App Note 13000-00323 §"Method 1"
and `project_8852b_eutaddr_zero_normal.md`, EUTADDR=0 is **expected
normal** for BLE-over-RS232 (no BT connection is established — the
8852B does NOT use BD_ADDR for BLE; the manual says the address is
not required). EUTADDR will be all-zero for a healthy bench.

**This refactor's UX rule:** the prompt must not flag EUTADDR=0 as an
error after a successful RS232/USB/2-Wire init. The pre-existing
`set_eut_interface` already handles this correctly (it logs a
warning, doesn't raise). Don't change that behavior.

---

## 5. MCP tool surface

### 5.1 Decision: new MCP tool, not just CLI

The existing `run_ble_full_suite.py` is a one-shot CLI. It is fine
for unattended runs, but it cannot be driven from a Claude chat
session — there's no way for the agent to ask the operator one
question at a time, or to receive structured answers back.

**Decision:** add a new MCP tool `bt_run_full_suite` that drives
the prompts and writes the report. Keep `run_ble_full_suite.py` as
a thin wrapper that calls the same library functions (the
report-writer, the per-test executor) so CLI users don't lose
anything.

### 5.2 Tool signature

```python
@mcp.tool()
def bt_run_full_suite(
    # Optional override: skip prompts and use the values below.
    # If ANY of these is None, the tool falls back to interactive
    # prompts (one question per call, returned in a 'prompt' envelope
    # so a chat agent can ask the operator and call again).
    eut_interface:  Optional[Literal["RS232","USB","BLE2WIRE","USBBLE2WIRE","USBADAPTOR","NONE"]] = None,
    eut_baud:       Optional[int]   = None,
    eut_handshake:  Optional[Literal["NONE","RTSCTS"]] = None,

    tests:          Optional[list[Literal[
        "LEOP","LEICD","LEMI","LESS","LEMP","LEPRI","LEPS"
    ]]] = None,

    # Per-test param overrides (None = prompt). Each test's params
    # are a flat dict; see §3 for the keys.
    test_params:    Optional[dict[str, dict]] = None,

    # Campaign-wide plan (None = prompt).
    channels:       Optional[list[int]] = None,
    capture_type:   Optional[Literal["BLE","2LE","BLR"]] = None,
    packet_length:  Optional[int] = None,
    report_dir:     Optional[str] = None,
    run_label:      Optional[str] = None,

    # If True, do not interact at all — fail on missing param.
    # For non-interactive scripts (CI / lab unattended).
    non_interactive: bool = False,
) -> dict:
    """Run an interactive BLE TX/RX test campaign.

    Returns one of:
      {"ok": True,  "state": "running",  "report_dir": "...",
       "started": "...", "results": [...]}
      {"ok": True,  "state": "awaiting_input",
       "prompt": {"id": "eut_interface", "question": "...",
                  "choices": [...]}}
      {"ok": False, "error": "..."}
    """
```

The contract is **stateless across calls**: the tool returns
`state="awaiting_input"` with a `prompt` envelope describing the
question (and any `choices`); the caller invokes the tool again
with the corresponding override parameter set. This lets a chat
agent show one question at a time to the operator.

### 5.3 The prompt envelope shape

```python
{
  "ok": True,
  "state": "awaiting_input",
  "prompt": {
    "id":          "eut_interface",   # matches the kwarg name on the tool
    "question":    "How is the MT8852B wired to the DUT?",
    "choices":     [
      {"label": "HCI RS232",  "value": "RS232"},
      {"label": "HCI USB",    "value": "USB"},
      {"label": "2-Wire",     "value": "BLE2WIRE"},
      {"label": "2-Wire-USB", "value": "USBBLE2WIRE"},
      {"label": "USB-Serial", "value": "USBADAPTOR"},
      {"label": "No Control", "value": "NONE"},
    ],
    "default":     "RS232",
    "help":        "BLE_MSM §3 — EUT Control setting on [Connection] tab",
  },
  "session": {
    # Echoed so a stateless caller can resume.
    "completed":  ["eut_interface"],   # names of params already set
    "remaining":  ["tests", "channels", "packet_length", "test_params"],
  }
}
```

### 5.4 State machine and the existing tools

The new tool reuses:
- `MT8852BSession.set_eut_interface(...)` — unchanged
- `MT8852BSession.configure_ble_tx(plan)` — unchanged
- `MT8852BSession.start()` / `wait_complete()` / `read_result()` —
  unchanged (and protected — see §7)
- `mcp_8852b.scpi.le*cfg(...)` builders — extend with new helpers
  for AVGMXLIM / FERLIM / TXPWR / etc. (see §3 column "SCPI")

The atomic MCP tools (`bt_set_eut_interface`, `bt_run_tx_test`,
`bt_diag_eut_link`, etc.) remain. The new tool is a higher-level
wrapper that does the prompting then drives them.

### 5.5 Pre-flight checks before the EUT init prompt

Per project memory `feedback_mcp_interactive_preflight.md` and
`project_preflight_mcp_tools.md`, the new tool MUST check the
bench's state and present a preflight summary BEFORE asking the
operator for inputs:

1. Open VISA (`bt_connect` equivalent), read `*IDN?`, `OPTSTATUS?`,
   `BOOTSTATUS?`.
2. Read `OPMD?`, `SCPTSEL?`, `SCPTTSTGP? n`, `LEPKTLEN? n` to see
   if the bench is already in BLE script mode (the `_bench_ble_ready`
   probe).
3. Read `EUTSRCE?`, `EUTRS232?`, `EUTHANDSHAKE?`, `EUTADDR?`,
   `EUTLEFEAT?`, `ERRLST?` (the `bt_diag_eut_link` shape).
4. Show operator: `*IDN`, OPTSTATUS (with 27/35/36/37 availability),
   current `EUTSRCE`, last `ERRLST`, link-up flag.

The preflight informs the prompts. E.g. if OPTSTATUS shows no Opt 37,
LEPS is greyed out. If EUTSRCE is already RS232@115200, the
default in the EUT-interface prompt can show that as the current
setting.

---

## 6. Report naming convention

The lab's existing reports dir is:
`E:\Centrak\BLE Sticker\test\HWTEST\reports\`

Existing patterns (from `ls reports`):
- `ble_full_20260617_170928/`     (the `run_ble_full_suite.py` output)
- `ble_full_20260617_171457/`
- `ble_leop_20260615_162506/`
- `BLE_Sticker_ProductTest_20260615/` (project-level)
- `dmm_voltage_log_20260615_105200/`
- `full_sweep_20260617_153105.md`  (single-file)

**Proposed convention** (extends the existing `ble_full_*` pattern):

```
reports/ble_full_<run_label>_<YYYYMMDD_HHMMSS>/
    report.md
    summary.json
    data.csv
    bench.log                (added: full pyvisa log captured during run)
    bench_preflight.json     (added: pre-run *IDN / OPTSTATUS / EUTSRCE / ERRLST)
```

Where `<run_label>` is:
- empty if the operator doesn't set one
- else the operator's free-text, sanitized to `[A-Za-z0-9_-]+`

Concrete examples:
- `reports/ble_full_LEOP_only_ch0_20260617_182300/`
- `reports/ble_full_HCI_RTSCTS_sensitivity_sweep_20260617_182300/`
- `reports/ble_full_20260617_182300/`     (default — no label)

The run label is what shows in the operator's quick scan of
`reports/`; the timestamp guarantees uniqueness. The label is
limited to 32 chars to keep path length sane.

**Per-record file naming:** no change. `data.csv` and `summary.json`
remain per-run, not per-test (the per-test detail lives in
`summary.json["results"]`).

---

## 7. Constraints / must-not-break (the v6.8 invariants)

These are the three v6.8 fixes from
`project_8852b_v68_three_bug_fix.md`. The new interactive tool MUST
NOT regress them. When the implementer writes the new
`bt_run_full_suite` tool, they will be re-using the state machine's
`wait_complete` / `read_result` / `set_eut_interface` — those
implementations stay untouched.

### 7.1 `wait_complete()` MUST detect the CMP 1→0→1 transition

Per `Remote_Programming_Manual.txt` p.2-7 Table 2-7:
> "CMP — Script or test completion. This bit is cleared when a test
> or script has started and is set on its completion or termination."

And p.2-7:
> "The INS register is read with the *INS? Command. It cannot be
> cleared by reading it or by the *CLS command."

And p.3-10 (*RST):
> "GPIB Status registers and Input/Output queues are not cleared."

Therefore `*CLS` and `*RST` CANNOT clear the CMP bit. Only the RUN
edge clears it. `MT8852B.wait_test_complete()` must continue to
require `require_transition=True` — see the call site in
`MT8852BSession.wait_complete()` (`state_machine.py:505-529`).

**Don't change `wait_complete()` to use `require_transition=False`.**

### 7.2 `read_result()` MUST issue OPMD SCRIPT + *CLS + *OPC? before ORESULT

Per the official GUI capture (per `project_8852b_capture_txt_decode.md`,
idx 173 in the official NI Spy capture of the working bench), the
official Anritsu BLE Measurement Software re-issues `OPMD SCRIPT +
*CLS + *OPC?` between every test's wait_complete and its ORESULT.
Without this re-issue, the ORESULT bus is mis-aligned: the first
query returns a stale `*INS?` value (often "46") and subsequent
queries return data shifted by one test.

**Don't remove the `OPMD SCRIPT + *CLS + *OPC?` block from
`MT8852BSession.read_result()` (`state_machine.py:564-574`).**

### 7.3 `set_eut_interface()` MUST have `skip_if_ready=True` default

Per `project_8852b_v68_three_bug_fix.md` bug E (stale VISA session /
HCI CMD TIMEOUT on hot DUT): the bench is often "hot" from a prior
session in BLE script mode. The full EUT init (`*RST → SYSCFG
EUTSRCE → HCI re-init`) re-triggers HCI and hangs the DUT in
HCI CMD TIMEOUT lockout. The `skip_if_ready=True` fast path
(`state_machine.py:228-237` and `_bench_ble_ready()` at
`state_machine.py:307-340`) detects this and skips the full init.

**Keep the default as `skip_if_ready=True`.** The preflight prompt
must show the operator what the probe sees (OPMD / SCPTSEL /
SCPTTSTGP / LEPKTLEN) so the operator can override with
`skip_if_ready=False` if they want a full re-init.

### 7.4 Other invariants to keep

- `*IDN?` response is verified in a 5-attempt loop with 0.3 s sleep
  (`instrument.py:220-231`).
- The v6.2 `configure_ble_tx` block (the TEST_BLOCK in
  `state_machine.py:343-456`) — don't simplify it. Removing
  `SETBLECAPTYP` / `CFGBLECAP` was a v6.2 fix, not gold-plating.
- `EUTHANDSHAKE` must be set to `NONE` (not `RTS/CTS`) for the
  v6 path, even when the DUT uses RTS/CTS in the init path (per
  `state_machine.py:425-430` + the v6.2 comments).
- The "EUTADDR=0 is NORMAL for BLE" rule: do not raise on EUTADDR=0
  after a clean RS232 init. Per `project_8852b_eutaddr_zero_normal.md`
  and Anritsu App Note 13000-00323.

---

## 8. Out of scope (not changing in this round)

The following are explicitly NOT touched by this refactor:

- **The atomic MCP tool surface** (`bt_connect`, `bt_disconnect`,
  `bt_status`, `bt_reset`, `bt_set_eut_interface`, `bt_eut_interface`,
  `bt_diag_eut_link`, `bt_boot_status`, `bt_ins`, `bt_run_tx_test`,
  `bt_configure_ble_tx`, `bt_start`, `bt_wait_test_complete`,
  `bt_abort`, `bt_read_oresult`, `bt_read_script_result`) — all
  keep their existing signatures and behavior. They may be called
  by the new `bt_run_full_suite` internally, but they are not
  re-shaped.
- **The bench state machine** (`MT8852BSession`) — its state
  diagram (DISCONNECTED → IDLE → CONFIGURED → TESTING → COMPLETE)
  and the v6.8 fixes are load-bearing (see §7).
- **The SCPI command builders** in `mcp_8852b/scpi.py` — extend
  with new `le*cfg(field, value)` helpers only (the §3 SCPI
  column maps to new helpers, not new wire formats). Existing
  builders stay as-is.
- **The DUT lockout recovery flow** — `*RST` + power-cycle
  procedures for the HCI CMD TIMEOUT lockout (per
  `project_8852b_eut_recovery.md`) are not re-engineered. The
  v6.8 `skip_if_ready=True` is the primary mitigation; the
  lockout recovery is a separate script.
- **The bench's preloaded script 3** — we trust its default
  payload/limit values for any field the operator doesn't
  override. Re-flushing SCPTCFG / LEMICFG.TOGGLE between iterations
  was what broke v6 (per `project_8852b_v68_three_bug_fix.md`).
- **The `Run_ble_full_suite.py` CLI** — kept as-is. It is the
  non-interactive path. The new `bt_run_full_suite` MCP tool
  reuses the report writer and per-test executor, so the two
  paths produce identical output files.
- **GUI capture replay / debug scripts** under `debug_*.py` and
  `__pycache__` — unchanged.
- **Other MCPs in this project** (`mcp_34465a`, `mcp_js220`) —
  not touched. The interactive preflight pattern from
  `project_preflight_mcp_tools.md` is the same idea, but each MCP
  has its own session/transport and we don't share.
- **A separate `bt_run_sensitivity_sweep` tool** — the sweep is
  offered as an opt-in sub-prompt inside `bt_run_full_suite`
  (§3.5). If a future round needs a standalone sweep tool, that's
  a follow-up.

---

## 9. Open questions

These should be answered by the implementer (or by a follow-up
chat) before the code is written:

1. **Where does the `bt_run_full_suite` tool live?** Options:
   (a) directly in `mcp_8852b/server.py` alongside the existing
   atomic tools, (b) in a new `mcp_8852b/interactive.py` module
   imported by `server.py`. (b) is cleaner — the prompts,
   preflight, and the state-machine driver are non-trivial code
   that shouldn't bloat `server.py`.

2. **Where does the prompt engine live?** Options:
   (a) `input()`-based for terminal use, (b) MCP envelope
   (`state="awaiting_input"`) only, (c) both. The CLI runner
   should keep using `input()`; the MCP tool should return the
   envelope. A single prompt function that returns the value
   (or raises if a non-interactive caller) seems cleanest —
   `mcp_8852b/interactive/prompts.py` has `prompt_eut_interface()`
   etc. that return typed values, and a separate
   `mcp_8852b/interactive/mcp_form.py` translates those into
   the envelope shape.

3. **Should the new tool re-use the existing
   `MT8852BSession`, or instantiate its own?** The existing
   `server.py` has a single module-level `session =
   MT8852BSession()`. If `bt_run_full_suite` and `bt_run_tx_test`
   are both called in the same Claude session, they will share
   state. Sharing is fine — the prompts lock the EUT interface
   for the duration of the campaign, and `bt_run_tx_test` should
   be the same. But we need to be careful that
   `set_eut_interface(skip_if_ready=True)` doesn't sneak in a
   fast-path under the operator's feet (we want them to know
   what the bench state is).

4. **Sensitivity-sweep support — Option 37 only?** LEPS already
   requires Opt 37. Should the sensitivity sweep be tagged as
   "Opt 27 only" (it doesn't need CTE) and exposed even if Opt 37
   is missing, or should it be hidden behind the same Opt check?

5. **No-Control mode UX — is the menu grayed, hidden, or
   warning-confirmed?** Per `BLE_MSM §6-1`: "Test scripts cannot
   be run from the Bluetooth low energy measurement software if
   the EUT does not have an available Direct Test Mode
   connection." If the operator picks "No Control", what does
   the test-execution path do? The honest answer: nothing
   automated — the operator must drive the DUT from the vendor
   app, and the MCP can only poll `*INS?` and read whatever
   ORESULT comes back. Recommend: grayed if the bench is in
   SCRIPT mode, but available with a `?` confirmation prompt
   if `OPMD?` is SCRIPT and the operator wants a "watch the
   bench while I drive the DUT externally" mode.

6. **Should the report include the bench's preflight snapshot
   (`*IDN`, `OPTSTATUS`, `EUTSRCE`, `ERRLST`, `OPMD?`)?** The
   spec proposes yes (§6 — `bench_preflight.json`). It's
   useful for post-mortem when an invalid=FALSE row appears.
   No new state-machine change; just a save-on-start.

7. **Are there operator-supplied limit overrides (AVGMXLIM, etc.)
   that we should ALWAYS prompt for, or only when the operator
   asks to override the script-3 defaults?** Recommend: prompt
   only when the operator has flagged the field as "override"
   (i.e. the §3 prompts that show `[default]` and accept Enter
   for default). Keep the interactive flow short for the common
   "use script 3 as-is" case.

---

## 10. Document references (paths are absolute)

- BLE Measurement Software Operation Manual (CLI user view):
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\reference\Bluetooth Test Set MT8852B\BLE_Measurement_Software_Operation_Manual.txt`
  - Ch.4 (TX testing) lines 593-686
  - Ch.5 (RX testing) lines 687-789
  - Ch.7 (sensitivity search) lines 854-899
- MT8852B Remote Programming Manual (SCPI reference):
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\reference\Bluetooth Test Set MT8852B\Remote_Programming_Manual.txt`
  - LEOPCFG: p.10-38 (lines 6916-7015)
  - LEICDCFG: p.10-40 (lines 7020-7185)
  - LEMICFG: p.10-43 (lines 7190-7310)
  - LEPSCFG: p.10-46 (lines 7316-7428) — Opt 37
  - LESSCFG: p.10-48 (lines 7433-7533)
  - LEPRICFG: p.10-50 (lines 7538-7618)
  - LEMPCFG: p.10-52 (lines 7623-7712)
  - LEPKTTYPE (per-test packet type): lines 8420-8521
  - TOGGLE: lines 9052-9082
  - NUMPKTS / NUMANT / NUMCYC / etc.: lines 8720-8748, 8588-8640
  - CMP / *INS / *RST rules: p.2-7 / p.3-10 (per the v6.8 invariants)
- App Note 13000-00323 (HCI vs 2-Wire — "A Bluetooth connection is
  not established between the MT8852B and the EUT and the EUT
  Bluetooth address is therefore not required."):
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\reference\Bluetooth Test Set MT8852B\Testing_a_Bluetooth_Low_Energy_Device_Controlled_through_an_HCI_or_2-Wire_Interface.txt`
- Existing CLI runner (report shape contract):
  `E:\Centrak\BLE Sticker\test\HWTEST\run_ble_full_suite.py`
- Existing MCP code (atomic tools + state machine):
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\server.py`,
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\state_machine.py`,
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\scpi.py`,
  `E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\instrument.py`
- v6.8 fix notes (must not regress):
  `E:\Centrak\BLE Sticker\test\HWTEST\memory\project_8852b_v68_three_bug_fix.md`
  (also surfaced in the user-private memory in this conversation)

---

*End of SPEC.*
