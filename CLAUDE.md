# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Platform

**Windows-only.** Do not attempt this on Linux/macOS:
- `pyvisa` + NI-VISA runtime is Windows-only (the `MT8852B_VISA_LIB` path in `mcp_8852b/config.py` is `C:\Windows\System32\nivisa64.dll`).
- `joulescope` USB driver (`pyjoulescope_driver`, Cython backend) is Windows-only.
- The historical `cwd` `e:\Centrak\BLE Sticker\test\HWTEST` is a Windows bench path; the new default is the project root.

## What this project is

Three Python MCP (Model Context Protocol) servers that expose bench test instruments to Claude, plus a BLE test suite script that drives them. Built for the CMT BLE Sticker HWDVT (Hardware Design Validation Test) line. Each `mcp_<device>/` is a self-contained FastMCP package; the test script `run_ble_full_suite.py` is a thin CLI that drives `mcp_8852b/`'s prompt+executor+reporter.

| Package | Instrument | Transport | Result envelope | Has state machine? |
|---|---|---|---|---|
| `mcp_34465a` | Keysight 34465A 6½-digit DMM | pyvisa over USB (`USB0::0x2A8D::0x0101::MY57515137::0::INSTR`) | `{ok, test, value, unit, lower, upper, pass, timestamp}` | No (one-shot reads) |
| `mcp_8852b`  | Anritsu MT8852B BT Test Set | pyvisa over GPIB (`GPIB0::27::INSTR`, NI-VISA DLL) | same + `header, channel, capture_type, raw_fields, raw` | Yes: `DISCONNECTED → IDLE → CONFIGURED → TESTING → COMPLETE` |
| `mcp_js220`  | Joulescope JS220 energy analyzer | `joulescope` Python SDK over USB (no VISA) | same as 34465A | Yes: `DISCONNECTED → IDLE → STREAMING` |

The standard result envelope is the unifying contract — every measurement tool across all three packages returns it (see `BUILDING_AN_INSTRUMENT_MCP.md` §5.2). When adding a new measurement tool, **match the envelope exactly**; downstream tooling depends on it.

## The MCP package layout (apply to any new instrument)

```
mcp_<device>/
├── __init__.py        # package marker (__version__)
├── config.py          # VISA resource / device path / timeouts / DEFAULT_LIMITS
│                      #   / PREFLIGHT_PRESETS — env-var-overridable dataclass
├── scpi.py            # one function per command; pure strings, NO I/O
│                      #   (for vendor-SDK backends: validators, not strings)
├── instrument.py      # the ONLY file that imports pyvisa / joulescope / etc.
├── state_machine.py   # OPTIONAL — include when the instrument has a multi-step lifecycle
├── server.py          # FastMCP @mcp.tool() definitions; the only user-facing surface
├── README.md          # architecture + tool reference + gotchas (always required)
├── NOTES.md           # per-instrument lessons learned (recommended)
├── scripts/           # ready-to-run bench scripts; NOT MCP tools themselves
└── reference/         # datasheets, programmer guides, app notes (PDFs)
```

**Layering rule (hard)**: `server.py → instrument.py → scpi.py → config.py`. `server.py` must never import `pyvisa` or a vendor SDK — it talks to the device only through `instrument.py`. This is what keeps the policy ("gate measurements behind a preflight step") in one obvious place.

`BUILDING_AN_INSTRUMENT_MCP.md` is the project-level playbook for adding a new instrument MCP (gotcha registry, state-machine heuristics, report conventions, long-capture resilience). **Read it before starting a 4th MCP** — §16 covers non-SCPI backends (like the JS220), §17 covers the preflight config pattern, §18 covers fsync/`TaskStop` resilience.

## The interactive preflight flow (load-bearing convention)

Every test that writes to the bus or captures samples goes through a **two-step gate** in `mcp_34465a` and `mcp_js220`. This is the project-wide rule for "the operator actually reviewed the test before it ran."

```
Step 1: <thing>_preflight_<verb>(..., auto_confirm=False)   # propose, NO I/O
   → returns {ok, needs_user_input, proposed_config, questions, rationale, ...}
   → AI surfaces questions to the operator via AskUserQuestion
Step 2: <thing>_run_preflight_<verb>(..., user_confirmed=True)
   → checks user_confirmed FIRST (returns {ok: false, error: ...} before any I/O if False)
   → applies config + runs the test → returns the result envelope
```

For batch / scripted runs, pass `auto_confirm=True` to step 1 to skip the review in a single call. The preflight tools are **additive** — all original tools are preserved unchanged (verified by `verify_preflight.py` test 8).

`mcp_8852b` predates this convention and uses a different prompt module (`mcp_8852b/interactive.py`) for the BLE suite — see `SPEC_mcp_interaction.md` for the planned alignment.

## Scripts and how to run them

There is no `requirements.txt`, no Makefile, no pytest config. The "test" surface is a hand-rolled offline verifier plus a few bench scripts.

```bash
# Verify the preflight flow for mcp_34465a and mcp_js220 (no real instruments needed)
python verify_preflight.py
# Expects "Summary: 8/8 tests passed" / "GO"

# DMM bench scripts (output to reports/<run_label>/{data.csv, summary.json, report.md, plot.png})
python mcp_34465a/scripts/run_dmm_voltage_log.py
python mcp_34465a/scripts/run_dmm_current_log.py   # also accepts touch <run_dir>/.stop for clean Ctrl+C-free stop

# MT8852B bench scripts
python -m mcp_8852b.scripts.preflight_check
python -m mcp_8852b.scripts.run_ble_tx_test --meas LEOP --channel 19
python run_ble_full_suite.py   # the full BLE TX/RX suite (LEOP/LEICD/LEMI/LESS) with prompts

# JS220 bench scripts — MUST set PYTHONIOENCODING=utf-8 on Windows
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/preflight_check.py
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/plot_power_profile.py reports/<run_label>
```

**Windows console encoding**: any Python invocation that might print a stats dict, a preflight `rationale`, or any field with `µ` / `°C` / `Ω` must run with `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1`, or use `python -X utf8`. CSV/JSON writers in the scripts force `encoding="utf-8"` already; the issue is the REPL/console display only.

**Stop mechanisms for long captures** (in priority order):
1. `Ctrl+C` → `KeyboardInterrupt`, clean `finally` runs
2. `touch <run_dir>/.stop` → main loop checks between samples, clean `finally` runs
3. `TaskStop` (Claude) / `SIGKILL` → hard kill, `try/finally` may not run on Windows; only `data.csv` (line-by-line `fsync`) survives, `report.md`/`plot.png` are missing and must be regenerated from the CSV. Each script's `_summarize` / `_write_markdown` / `_write_plot` are at **module level** precisely so a recovery script can import and call them — see `BUILDING_AN_INSTRUMENT_MCP.md` §18 for the pattern.

## High-rate sampling (the universal ~100× speedup)

For any instrument that supports it: **configure once, then loop with the read-only command**. Re-sending `:MEAS:VOLT:DC?` per iteration is ~2 Hz on the 34465A; `:READ?` per iteration is ~228 Hz at NPLC=0.001. The first read after a config change is always slower — throw it away as warmup. See `BUILDING_AN_INSTRUMENT_MCP.md` §7 and `mcp_34465a/scripts/run_dmm_voltage_log.py` for the full pattern.

## Default safety limits are not your spec

`config.DEFAULT_LIMITS` in every package are **product-line placeholders** for the CMT BLE Sticker PCBA, intentionally generous (e.g. `VOLT:DC` 0–5.5 V will pass a 0.5 V rail). The preflight flow exists precisely to force the operator to look at the proposed pass window before any measurement runs. Override per call with `lower=` / `upper=`, or edit `DEFAULT_LIMITS` for the product.

## Connection resource strings / env vars

All three packages read defaults from `config.py` and allow env-var overrides:

| Package | Env var | Default |
|---|---|---|
| `mcp_34465a` | `DMM34465A_RESOURCE` | `USB0::0x2A8D::0x0101::MY57515137::0::INSTR` |
| `mcp_34465a` | `DMM34465A_TIMEOUT_MS` | `10000` (per-query), `DMM34465A_TEMP_TIMEOUT_MS=30000` for temperature |
| `mcp_8852b`  | `MT8852B_RESOURCE` | `GPIB0::27::INSTR` (front-panel GPIB address) |
| `mcp_8852b`  | `MT8852B_VISA_LIB` | `C:\Windows\System32\nivisa64.dll` (absolute path; `niVisa64` shorthand requires the `pyvisa_ni` wrapper which isn't on this box) |
| `mcp_8852b`  | `MT8852B_TIMEOUT_MS` | `15000` |
| `mcp_js220`  | `JS220_DEVICE_PATH` | `None` → uses `scan()[0]`; bench unit is `JS220-005742` (set `JS220_DEVICE_PATH=005742` to pin) |
| `mcp_js220`  | `JS220_TIMEOUT_MS` | `5000` (per-call USB transport timeout) |

**Single VISA session at a time** over USB — a second `open_resource()` from another process hangs the 34465A. The `mcp_8852b` `set_eut_interface()` runs an unstick trio (`SCPTRST` + `SCPTTSTGP` + `EDRTSTS,OFF` + `BLETSTS,ON`) when the bench isn't already in BLE script mode (see `mcp_8852b/NOTES.md` §15 and `SPEC_mcp_interaction.md` §2.2 for the full EUT-control table).

**MT8852B does NOT speak standard SCPI.** It uses Anritsu's flat named commands (`SETBLECAPTYP BLE`, `MEASBLECAP LEOP,NA,71764129`, `RUN`, `*INS?`, `ORESULT TEST,0,LEOP`). All builders live in `mcp_8852b/scpi.py`. Two non-obvious gotchas: `SCPTSEL <n>` is required before `MEASBLECAPX` or the test silently returns `valid=FALSE`; `TESTMODE` is for classic Bluetooth ACL, not BLE DTM (recovery via `*RST`, see `mcp_8852b/NOTES.md` §2b, §15).

## Claude Desktop registration

Edit `%APPDATA%\Claude\claude_desktop_config.json`; each MCP runs as its own subprocess, no port conflicts:

```json
{
  "mcpServers": {
    "dmm34465a": {"command": "python", "args": ["-m", "mcp_34465a.server"], "cwd": "<project root>"},
    "mt8852b":   {"command": "python", "args": ["-m", "mcp_8852b.server"],   "cwd": "<project root>"},
    "js220":     {"command": "python", "args": ["-m", "mcp_js220.server"],   "cwd": "<project root>"}
  }
}
```

`cwd` must be the project root so the package can be imported as `from mcp_xxx.server import ...`. The default cwd in the READMEs is the historical bench path `e:\Centrak\BLE Sticker\test\HWTEST` — update if you've moved the repo. **Platform: Windows-only** (NI-VISA + joulescope driver are Windows-only; see top-of-file "Platform" section).

## Cross-instrument coordination

A "full PCBA test" (power up, measure VCC on the DMM, run BLE TX tests on the 8852B, log power on the JS220) is **not** an MCP tool — it's a separate script that imports both device modules. `run_ble_full_suite.py` is the example for the BLE side. Pattern from `BUILDING_AN_INSTRUMENT_MCP.md` §14:

```
mcp_34465a/       # owns the DMM
mcp_8852b/        # owns the BT tester
mcp_js220/        # owns the energy analyzer
run_full_pcba_test.py   # imports all three, runs the flow
```

The DUT (PCBA) is expected to already be in DTM (Direct Test Mode) and transmitting on the configured channel by the time `bt_start()` is called. A separate "DUT skill" (per platform: Silicon Labs DVK, Nordic DK, ...) is responsible for the AT/HCI commands that put the board into DTM and set the payload — see `mcp_8852b/README.md` §"DUT skill contract".

## Project status context

`SPEC_mcp_interaction.md` is a DRAFT v1 spec for refactoring `mcp_8852b/` to a guided, operator-confirmed flow matching the preflight pattern in the other two MCPs. `doubao_thread.md` is a long planning/decision log. The 14 `debug_*.py` and 6 `replay*.log` files (in `mcp_8852b/scripts/_debug_history/`) are investigation artifacts from a recent BLE-tester bring-up — they capture reproducible failure modes and are documented in `mcp_8852b/state_machine.py` 注释（v6.2/v6.6/v6.8 标记处）和 `SPEC_mcp_interaction.md §7` but should not be re-run or edited without re-reading the surrounding context. `verify_preflight.py` is the canonical "is the gate still working" check — run it after any change to `mcp_34465a/server.py` or `mcp_js220/server.py`.
