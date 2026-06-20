# `mcp_34465a` bench scripts

Ready-to-run scripts for bench data collection. They use the MCP
package for connection, SCPI, and config — they are **not** MCP tools
themselves; they're conventional Python scripts the engineer invokes
from a terminal or schedules.

Both scripts are designed to:

- **Live-write** samples to disk one at a time with `fsync` — a
  hard kill (Ctrl+C, TaskStop, power loss) never loses more than the
  in-flight sample.
- **Write outputs** to `e:\Centrak\BLE Sticker\test\HWTEST\reports\<run_label>/`
  with short filenames (`data.csv`, `summary.json`, `report.md`, `plot.png`).
- **Self-locate the project root** via `__file__` so they work from
  any working directory.

## `run_dmm_voltage_log.py`

DC voltage log over a fixed duration. Demonstrates the high-speed
sampling pattern (NPLC=0.001 + `:READ?`).

```bash
# default: 5-minute log at 3.0V ± 0.1V
python mcp_34465a/scripts/run_dmm_voltage_log.py

# customize by editing the constants at the top of the file:
#   DURATION_S, EXPECTED_NOMINAL_V, EXPECTED_TOLERANCE_V
```

**What it does**:
1. Connects, configures `VOLT:DC` with NPLC=0.001 + AZ OFF.
2. Loops with `:READ?` for `DURATION_S` seconds.
3. On finish (clean or interrupted), writes `data.csv` / `summary.json`
   / `report.md` / `plot.png` into a fresh `reports/<run_label>/`.

## `run_dmm_current_log.py`

Live-stoppable DC current log for IoT / battery-powered DUTs. Supports
three stop mechanisms (see `STOP_FILE`` in the source).

Note: `mcp_8852b/scripts/` 当前未实现 `.stop` 文件机制（见 `mcp_8852b/scripts/README.md`）.

| Stop | How |
|---|---|
| `Ctrl+C` | `KeyboardInterrupt` → clean `finally` |
| `TaskStop` (Claude) | Hard kill — survives, but `report.md` / `plot.png` need regeneration from CSV |
| `touch .stop` file | Main loop checks between samples; clean `finally` runs |

```bash
python mcp_34465a/scripts/run_dmm_current_log.py
# in another terminal:
touch e:/Centrak/BLE Sticker/test/HWTEST/reports/dmm_current_log_<TIMESTAMP>/.stop
```

**What it does**:
1. Connects, configures `CURR:DC` with NPLC=0.001 + auto-range.
2. Loops with `:READ?`, fsync'ing each sample to `data.csv`.
3. Rewrites `summary.json` every 100 samples (so the user can peek).
4. On stop, classifies samples into 5 current-level bands
   (`deep_sleep` / `light_sleep` / `idle` / `active` / `tx_burst`) and
   writes the full report.

## Reading existing reports

Reports are independent of the script — once written, you can re-read
them without re-running the test. To regenerate the markdown + plot
from a CSV (e.g. after a hard kill that left them missing), see the
[recovery snippet in the project-level guide](../../BUILDING_AN_INSTRUMENT_MCP.md#8-report-conventions).
