# mcp_js220 — Lessons learned (extracted from README "Gotchas")

Indexable summary of the JS220 bench-validated gotchas. The README's
"Gotchas and lessons learned" section is the canonical home; this file
mirrors the load-bearing ones.

## 1. Vendor SDK, not SCPI
The JS220 uses the `joulescope` Python package over USB — there is no
VISA resource string, no `:MEAS:VOLT:DC?` style commands, no GPIB. Do
not try to talk to it with `pyvisa`. The package mirrors the SCPI
pattern (one function per command in `scpi.py`, a `Device` class for
I/O in `instrument.py`) so the maintenance pattern is the same, but
the wire-level calls are `parameter_set("source", "raw")`, `start()`,
etc.

## 2. `set_*` order matters: range → source → frequency
The recommended apply order in `js220_recommend_config` + manual
bench testing is:
1. `set_current_range(i_range)`  — locks the ADC range first
2. `set_io_voltage(io_voltage)`  — sets the GPI/O level
3. `set_sampling_frequency(sampling_frequency)`
4. `set_reduction_frequency(reduction_frequency)`
Reordering breaks the warmup-stats and produces "first sample is
gibberish" bugs that look like timing problems.

## 3. Five built-in presets
`config.PREFLIGHT_PRESETS` has 5 test types: `long_idle_log`,
`ble_active_bursts`, `fine_peak_capture`, `ultra_low_current`,
`high_current_load`. Each bundles sampling_frequency /
reduction_frequency / i_range / io_voltage with a use-when. The
preflight tool re-checks `i_range` against the operator's
`expected_current_a` / `peak_current_a` using the static
`PREFLIGHT_I_RANGE_TABLE` to give 5-10× headroom — that computation
runs **offline** (no USB).

## 4. `PYTHONIOENCODING=utf-8` is mandatory on Windows
The JS220 stats dict has `µA`, `Ω`, `°C` units and the package emits
`rationale` strings with `µ` / `°C` / `Ω` in them. On Windows the
default console code page is cp936 / cp1252, which mangles these into
`锛达oops`. Set `PYTHONIOENCODING=utf-8` (and `PYTHONUTF8=1`, or use
`python -X utf8`) for any invocation that might print a stats dict or
a preflight `rationale`. CSV/JSON writers in the scripts force
`encoding="utf-8"` already; the issue is the REPL/console display only.

## 5. First ~0.5 s warmup is sacred
After `start()`, the JS220 needs ~5-10 ms to settle the current range.
The first few `reduction_frequency` ticks carry non-zero bias from the
settling transient. `instrument.JS220.start()` discards the first
~0.5 s of stats as warmup. **Don't remove that warmup** unless you
can prove your DUT starts producing a clean signal faster than the
warmup window.
