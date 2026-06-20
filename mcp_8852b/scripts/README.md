# `mcp_8852b` bench scripts

Ready-to-run Python scripts for bench data collection. They use the
`mcp_8852b` package for connection, SCPI, and config — they are
**not** MCP tools themselves; they're conventional Python scripts
the engineer invokes from a terminal or schedules.

All scripts follow the same conventions as `mcp_34465a/scripts/` and
`mcp_js220/scripts/`:

- **Self-locate** the project root via `__file__` so they work from
  any working directory.
- **Write outputs** to
  `e:\Centrak\BLE Sticker\test\HWTEST\reports\<run_label>/` with
  short filenames (`result.json`, `result.md`, `result.csv`).
- **Live-write** artefacts at the end (one short test per run, no
  long log streams to fsync).

Currently implemented conventions: stdout progress, summary.json output, fail-fast on SCPI error.
**Not yet implemented**: `.stop` file mechanism (see `mcp_34465a/scripts/README.md` for the pattern).

## `preflight_check.py`

Validates the bench BEFORE a real HWDVT run. Stops at the first
failure with an actionable error message.

```bash
# default: RS232 HCI, 115200 baud, dry-run capture on ch 19
python -m mcp_8852b.scripts.preflight_check

# BLE2WIRE (typical for BLE Sticker that doesn't expose HCI UART)
python -m mcp_8852b.scripts.preflight_check --interface BLE2WIRE

# Different channel for the dry-run
python -m mcp_8852b.scripts.preflight_check --channel 0
```

Checks: GPIB link, *IDN, BOOTSTATUS, EUT interface config, EUT
address acquisition, HCI reset, MEASBLECAPX dry-run. Exit 0 = ready,
exit 1 = something is wrong, exit 2 = connect failure.

## `run_ble_tx_test.py`

Runs a single BLE TX measurement (LEOP / LEICD / LEMI) end-to-end
and writes the envelope to `reports/ble_<meas>_<timestamp>/`.

```bash
# LEOP (output power) on ch 19 with default PRBS9
python -m mcp_8852b.scripts.run_ble_tx_test

# LEICD (carrier drift) on ch 17 with 10101010 payload
python -m mcp_8852b.scripts.run_ble_tx_test \
    --meas LEICD --mod-type MOD10101010 --channel 17

# LEMI (modulation) on multiple channels
python -m mcp_8852b.scripts.run_ble_tx_test \
    --meas LEMI --mod-type MOD11110000 --channels 0 19 39

# Custom pass window (overrides Bluetooth spec default)
python -m mcp_8852b.scripts.run_ble_tx_test \
    --meas LEOP --lower -10 --upper 8

# BLE2WIRE instead of RS232
python -m mcp_8852b.scripts.run_ble_tx_test \
    --meas LEOP --interface BLE2WIRE

# Custom run label
python -m mcp_8852b.scripts.run_ble_tx_test \
    --label ble_sticker_pa_cal_v3
```

Outputs:
- `result.json` — full structured result (plan + EUT config + per-channel results)
- `result.md` — human-readable summary table
- `result.csv` — one row per channel

Exits 0 on PASS (all channels), 1 on FAIL, 2 on test error / connect failure.

## Adding more scripts

The pattern for a new script (per the project's `BUILDING_AN_INSTRUMENT_MCP.md`):

1. Import `MT8852BSession` from `mcp_8852b.state_machine`
2. Self-locate the project root via the `__file__` block at top
3. Use `_run_one()` (or a copy of it) to run a single test
4. Write outputs to `reports/<run_label>/`
5. Exit 0 on success, 1 on failure, 2 on connect error
