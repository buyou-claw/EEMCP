# mcp_34465a — Lessons learned (extracted from README "Gotchas")

These are the bench-validated gotchas for the Keysight 34465A. They are
duplicated here (the README's "Gotchas and lessons learned" section is
the canonical home; this file is the indexable summary). If you change
the README's gotchas, update this file too — or just rewrite this file
and link the README at it.

## 1. High-rate sampling — the universal ~100× speedup
Naive `:MEAS:VOLT:DC?` per iteration: **~2 Hz** on USB. Configure-once
then loop `:READ?`: **~228 Hz** at NPLC=0.001. Throw the first read
after any config change away (warmup). Pattern in
`mcp_34465a/scripts/run_dmm_voltage_log.py`.

## 2. Display control — leave it ON
`:DISP:STAT OFF` blanks the front panel (confuses bench users) for ~2%
throughput gain on USB. Not worth it. Always wrap `:DISP:TEXT` in
`try/finally` with `:DISP:TEXT:CLE` + `:DISP:STAT ON` — otherwise the
text persists after the script exits.

## 3. Single VISA session over USB
A second `open_resource()` from another process hangs until the first
releases. Don't open Keysight Connection Expert while a long test is
running. Recovery: 1-2s backoff loop, up to 8 retries.

## 4. Temperature probes need extended timeout
`:MEAS:TEMP:RTD?` (and `:THER`, `:TC`) **block the bus** until a real
transducer is read, not until the aperture elapses. With no probe
connected, they hang for the full timeout. The package uses
`temperature_timeout_ms` (default 30 000 ms); tune via
`DMM34465A_TEMP_TIMEOUT_MS=60000` if your probe is slow. To release a
stuck read, send `:ABOR`.

## 5. Overrange sentinel — `9.9e37` means "no signal"
Open inputs return `9.9e37`, not `inf`. Treat `>= 9e37` as "out of
range / not connected" in continuity / resistance checks. The
`_envelope` helper does not do this; add it in your caller.

## 6. Preflight two-step gate — design motivation
The preflight flow (propose → user_confirmed → run) exists to force
the operator to look at the proposed pass window before any
measurement. `DEFAULT_LIMITS` are **product-line placeholders** —
generous on purpose so the MCP runs out of the box, but they will pass
garbage (e.g. 3 V supply at 0.5 V "passes" `{"lower":0,"upper":5.5}`).
The hard gate (`user_confirmed` is the first thing the tool checks) is
the only thing standing between a sloppy config and a wrong "PASS" in
the report.
