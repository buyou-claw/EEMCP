# mcp_8852b — BLE test fix timeline (v6.x)

This file indexes the v6.x BLE-test bring-up fixes that are embedded
in `mcp_8852b/state_machine.py` as inline `v6.x fix — ...` comments.
Each row gives the version, the line range of the fix in
`state_machine.py`, the debug scripts that empirically verified the
fix, and the current status (all v6.x are landed and validated).

| v6.x | state_machine.py 行号 | 验证脚本 | 状态 |
|---|---|---|---|
| v6.2 | state_machine.py:343-456 (`configure_ble_tx`) | `debug_eut_init2.py`, `debug_v6_match.py` | ✓ 已落地 |
| v6.6 | state_machine.py:462-509 (`start()` / `wait_complete()` 1→0→1 边沿检测) | `debug_sess_v6_6.py`, `debug_lib_vs_official.py` | ✓ 已落地 |
| v6.8 | state_machine.py:544-574 (`read_result()` OPMD SCRIPT + *CLS + *OPC? 重发) | `debug_official_lib_test.py`, `debug_official_lib_test_v2.py`, `debug_bench_state.py` | ✓ 已落地 |

## v6.2 — the TEST_BLOCK must match replay_capture.py 1:1
After several iterations, the only sequence that produces valid
`ORESULT` data on the lab's 8852B (SN 001446004, FW 4.20.000) is the
exact `TEST_BLOCK` from `replay_capture.py` (which is itself a 1:1
replay of the official Anritsu GUI's NI Spy capture). "Streamlined"
versions returned `valid=FALSE` on `ORESULT` even though bench state
was identical.

Key invariants:
- `EUTHANDSHAKE NONE` (not RTS/CTS) in the v6 recipe.
- `OPMD SCRIPT` sent **TWICE** (once for activation, once after `*CLS`).
- `ULPDUTTSTEND` between blocks to drain any in-flight test.
- `SCPTTSTGP n, BLETSTS, ON` LAST in the block.
- 50 ms pacing between writes — splitting across method calls breaks
  bus alignment.

## v6.6 — CMP can only be cleared by the RUN edge
Per Remote Programming Manual p.2-7 Table 2-7 + p.3-10: `*CLS` and
`*RST` **CANNOT** clear the CMP bit. Only the `RUN` command clears it.
The `wait_complete()` path must detect the **1→0→1 transition**, not
just `CMP==1`. Snapshot `*INS?` BEFORE `RUN` so `wait_complete()` can
detect the RUN-driven 1→0 clear edge, then poll for the 0→1 set edge.

## v6.8 — re-issue OPMD SCRIPT + *CLS + *OPC? before ORESULT
`ORESULT` returns stale `*INS?` value (`46`) from `wait_complete`'s
last poll if the result-mode switch is not re-issued. The fix: re-issue
`OPMD SCRIPT` + `*CLS` + `*OPC?` immediately before `m.read_oresult`.
The `*OPC?` consumes any lingering `*INS?` response in the queue and
re-arms `ORESULT` to read the just-completed test. Per `Capture.txt`
idx 173 (official GUI does this every time).
