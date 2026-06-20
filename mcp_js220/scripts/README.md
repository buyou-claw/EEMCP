# mcp_js220/scripts — ready-to-run bench scripts

These scripts use the `mcp_js220` package for connection, parameter
management, and statistics collection. They run from a regular
`python` interpreter — no MCP server needed.

## Recommended flow

**Before** every capture, run `preflight_check.py` to pick the right
JS220 configuration. Then pass the chosen values to
`run_js220_power_profile.py`.

## `preflight_check.py`

Asks 6 questions (or accepts flags) and prints a recommended JS220
configuration + the exact command-line to run the capture.

Interactive mode:

```bash
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/preflight_check.py
```

Non-interactive (for CI / scripted use):

```bash
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/preflight_check.py \
    --test-type ble_active_bursts \
    --expected-current-a 0.05 \
    --io-voltage 3.3V
```

The 5 test types are documented in `mcp_js220/README.md` under
"Configuring the JS220 for a test".

## `run_js220_power_profile.py`

Capture a long power profile and write it to `reports/`.

```bash
# Defaults: 60 s @ 10 Hz, ble_active_bursts preset, auto current range
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py

# 5-minute profile at 20 Hz
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py --duration 300 --rate 20

# Live mode: stop with Ctrl+C or by creating <run_dir>/.stop
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py --live

# Use the BLE-active-bursts preset with a 50 mA expected current
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py \
    --test-type ble_active_bursts --expected-current-a 0.05 --live

# Custom configuration (overrides preset)
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py \
    --sampling 2MHz --current-range 10A --io-voltage 3.3V --live
```

The script writes the following files to `reports/<label>/`:

- `data.csv` — per-tick stats (current/voltage/power avg/std/min/max/p2p + charge/energy accumulators)
- `summary.json` — machine-readable summary
- `report.md` — markdown report
- `plot.png` — linear-scale time series of voltage & current (regenerate with `plot_power_profile.py`)
- `plot_log.png` — log-scale current to show both µA standby and A peaks

The CSV is flushed to disk every ~0.5 s, so a hard kill (Ctrl+C,
TaskStop) loses at most half a second of samples.

## `plot_power_profile.py`

Regenerate the time-series plots from a `data.csv` (e.g. if the
original `run_js220_power_profile.py` crashed before writing them,
or you want to re-plot with a different style):

```bash
PYTHONIOENCODING=utf-8 python mcp_js220/scripts/plot_power_profile.py \
    e:/Centrak/BLE\ Sticker/test/HWTEST/reports/<run_label>
```

## Why a script when the MCP tool does the same thing?

The MCP tool (`js220_run_power_profile`) requires the MCP server
process to be running and a Claude session to call it. This script
is for cases when you want to schedule a capture from cron, a shell,
or CI without a Claude session in the loop.
