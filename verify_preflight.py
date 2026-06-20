"""Preflight verification — 8 tests, no real instruments.

This script validates the 4 new preflight tools added in Phase 2:
  - dmm_preflight_measure       (mcp_34465a)
  - dmm_run_preflight_measure   (mcp_34465a)
  - js220_preflight_capture     (mcp_js220)
  - js220_run_preflight_capture (mcp_js220)

It uses unittest.mock to stub the instrument instances and the
property `connected`. Nothing talks to VISA / USB.

Run:  python verify_preflight.py
"""
from __future__ import annotations
import sys
import traceback
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

# Make sure we import the local packages, not anything else.
# Self-locate the project root via __file__ (no hard-coded bench path).
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_34465a.server import (  # noqa: E402
    dmm, dmm_preflight_measure, dmm_run_preflight_measure,
    dmm_measure_volt_dc, dmm_set_nplc, dmm_scan_test_points,
)
from mcp_js220.server import (  # noqa: E402
    js, js220_preflight_capture, js220_run_preflight_capture,
    js220_measure_current_avg, js220_set_current_range, js220_run_power_profile,
)

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
RESULTS: list[tuple[str, bool, str]] = []  # (name, ok, note)


def record(name: str, ok: bool, note: str = "") -> None:
    RESULTS.append((name, ok, note))
    tag = "PASS" if ok else "FAIL"
    print(f"[{tag}] {name}  {note}".rstrip())


@contextmanager
def patched_dmm_connected(value: bool = True):
    """Patch dmm.connected as a property — return value `value`."""
    pm = PropertyMock(return_value=value)
    with patch.object(type(dmm), "connected", pm):
        yield pm


@contextmanager
def patched_js_connected(value: bool = True):
    """Patch js.connected as a property — return value `value`."""
    pm = PropertyMock(return_value=value)
    with patch.object(type(js), "connected", pm):
        yield pm


# ---------------------------------------------------------------------------
# Test 1: 34465a preflight proposal mode (auto_confirm=False)
# ---------------------------------------------------------------------------
def test_1_dmm_propose() -> None:
    name = "1. 34465a preflight 提议模式 (auto_confirm=False)"
    try:
        with patched_dmm_connected(True), \
             patch.object(dmm, "set_nplc") as mock_set_nplc, \
             patch.object(dmm, "set_resolution") as mock_set_res, \
             patch.object(dmm, "measure_volt_dc", return_value=3.31) as mock_meas:
            result = dmm_preflight_measure(
                test_name="VCC_MAIN",
                function="VOLT:DC",
                lower=3.0, upper=3.6,
                nplc=1.0,
            )
        # Assertions
        assert isinstance(result, dict), f"expected dict, got {type(result)}"
        assert result.get("ok") is True, f"ok != True: {result.get('ok')}"
        assert result.get("needs_user_input") is True, \
            f"needs_user_input != True: {result.get('needs_user_input')}"
        pc = result.get("proposed_config")
        assert isinstance(pc, dict), f"proposed_config missing or not dict: {pc}"
        assert pc.get("lower") == 3.0, f"proposed_config.lower != 3.0: {pc.get('lower')}"
        assert pc.get("upper") == 3.6, f"proposed_config.upper != 3.6: {pc.get('upper')}"
        assert pc.get("nplc") == 1.0, f"proposed_config.nplc != 1.0: {pc.get('nplc')}"
        assert result.get("test_name") == "VCC_MAIN"
        assert result.get("function") == "VOLT:DC"
        assert isinstance(result.get("questions"), list), \
            f"questions not a list: {result.get('questions')}"
        # Critical: nothing should have been called on the instrument
        assert mock_set_nplc.call_count == 0, \
            f"set_nplc was called {mock_set_nplc.call_count} times"
        assert mock_set_res.call_count == 0, \
            f"set_resolution was called {mock_set_res.call_count} times"
        assert mock_meas.call_count == 0, \
            f"measure_volt_dc was called {mock_meas.call_count} times"
        record(name, True, "ok=True needs_user_input=True, no dmm.* methods called")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 2: 34465a preflight hard gate (user_confirmed=False)
# ---------------------------------------------------------------------------
def test_2_dmm_hard_gate() -> None:
    name = "2. 34465a preflight 强制位 (user_confirmed=False)"
    try:
        with patched_dmm_connected(True), \
             patch.object(dmm, "set_nplc") as mock_set_nplc, \
             patch.object(dmm, "set_resolution") as mock_set_res, \
             patch.object(dmm, "measure_volt_dc") as mock_meas:
            result = dmm_run_preflight_measure(
                test_name="VCC_MAIN",
                function="VOLT:DC",
                lower=3.0, upper=3.6,
                user_confirmed=False,  # THE critical part
            )
        # Assertions
        assert result.get("ok") is False, f"ok != False: {result.get('ok')}"
        err = (result.get("error") or "").lower()
        assert "user_confirmed" in err, \
            f"error message must mention 'user_confirmed': {result.get('error')!r}"
        # No instrument methods
        assert mock_set_nplc.call_count == 0
        assert mock_set_res.call_count == 0
        assert mock_meas.call_count == 0
        record(name, True, "ok=False, 'user_confirmed' in error, no dmm.* called")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 3: 34465a preflight bypass (auto_confirm=True)
# ---------------------------------------------------------------------------
def test_3_dmm_bypass() -> None:
    name = "3. 34465a preflight 旁路模式 (auto_confirm=True)"
    try:
        with patched_dmm_connected(True), \
             patch.object(dmm, "set_nplc") as mock_set_nplc, \
             patch.object(dmm, "set_resolution") as mock_set_res, \
             patch.object(dmm, "measure_volt_dc", return_value=3.31) as mock_meas:
            result = dmm_preflight_measure(
                test_name="VCC_MAIN",
                function="VOLT:DC",
                lower=3.0, upper=3.6,
                nplc=1.0,
                auto_confirm=True,  # bypass
            )
        # Assertions
        assert result.get("ok") is True, f"ok != True: {result.get('ok')}"
        # In bypass mode, needs_user_input is NOT set (or is None)
        assert result.get("needs_user_input") is None, \
            f"needs_user_input should be None in bypass: {result.get('needs_user_input')}"
        # Envelope fields
        assert result.get("test") == "VOLT:DC" or result.get("value") == 3.31, \
            f"missing envelope fields: {result}"
        # Confirm nplc was applied + measurement was taken
        assert mock_set_nplc.call_count == 1, \
            f"set_nplc call count != 1: {mock_set_nplc.call_count}"
        assert mock_meas.call_count == 1, \
            f"measure_volt_dc call count != 1: {mock_meas.call_count}"
        # Pass window check (3.31 is in [3.0, 3.6])
        assert result.get("pass") is True, f"expected pass=True, got {result.get('pass')}"
        record(name, True, "ok=True envelope returned, dmm.set_nplc+measure called once")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 4: js220 preflight proposal mode (auto_confirm=False)
# ---------------------------------------------------------------------------
def test_4_js_propose() -> None:
    name = "4. js220 preflight 提议模式 (auto_confirm=False)"
    try:
        with patched_js_connected(True), \
             patch.object(js, "set_io_voltage") as mock_iov, \
             patch.object(js, "set_sampling_frequency") as mock_sf, \
             patch.object(js, "set_reduction_frequency") as mock_rf, \
             patch.object(js, "set_current_range") as mock_cr, \
             patch.object(js, "collect_for") as mock_cf:
            result = js220_preflight_capture(
                test_name="BLE_ADV",
                test_type="ble_active_bursts",
                expected_current_a=0.05,
                duration_s=10.0,
                auto_confirm=False,
            )
        # Assertions
        assert result.get("ok") is True
        assert result.get("needs_user_input") is True, \
            f"needs_user_input != True: {result.get('needs_user_input')}"
        pc = result.get("proposed_config")
        assert isinstance(pc, dict)
        # None of the instrument methods may be called
        assert mock_iov.call_count == 0, f"set_io_voltage called {mock_iov.call_count}x"
        assert mock_sf.call_count == 0, f"set_sampling_frequency called {mock_sf.call_count}x"
        assert mock_rf.call_count == 0, f"set_reduction_frequency called {mock_rf.call_count}x"
        assert mock_cr.call_count == 0, f"set_current_range called {mock_cr.call_count}x"
        assert mock_cf.call_count == 0, f"collect_for called {mock_cf.call_count}x"
        record(name, True, "needs_user_input=True, no js.* methods called")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 5: js220 preflight hard gate
# ---------------------------------------------------------------------------
def test_5_js_hard_gate() -> None:
    name = "5. js220 preflight 强制位 (user_confirmed=False)"
    try:
        with patched_js_connected(True), \
             patch.object(js, "set_io_voltage") as mock_iov, \
             patch.object(js, "set_sampling_frequency") as mock_sf, \
             patch.object(js, "set_reduction_frequency") as mock_rf, \
             patch.object(js, "set_current_range") as mock_cr, \
             patch.object(js, "collect_for") as mock_cf:
            result = js220_run_preflight_capture(
                test_name="BLE_ADV",
                test_type="ble_active_bursts",
                expected_current_a=0.05,
                duration_s=10.0,
                user_confirmed=False,
            )
        # Assertions
        assert result.get("ok") is False
        err = (result.get("error") or "").lower()
        assert "user_confirmed" in err, \
            f"error must mention 'user_confirmed': {result.get('error')!r}"
        assert mock_iov.call_count == 0
        assert mock_sf.call_count == 0
        assert mock_rf.call_count == 0
        assert mock_cr.call_count == 0
        assert mock_cf.call_count == 0
        record(name, True, "ok=False 'user_confirmed' in error, no js.* called")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 6: js220 preflight bypass
# ---------------------------------------------------------------------------
def test_6_js_bypass() -> None:
    name = "6. js220 preflight 旁路模式 (auto_confirm=True)"
    # Build a single stats-sample dict that _one_shot_measure can parse.
    sample = {
        "signals": {
            "current": {"avg": {"value": 0.05, "units": "A"}},
            "voltage": {"avg": {"value": 3.3,  "units": "V"}},
            "power":   {"avg": {"value": 0.165, "units": "W"}},
        },
        "charge": {"value": 0.5,  "units": "C"},
        "energy": {"value": 1.65, "units": "J"},
    }
    try:
        with patched_js_connected(True), \
             patch.object(js, "set_io_voltage") as mock_iov, \
             patch.object(js, "set_sampling_frequency") as mock_sf, \
             patch.object(js, "set_reduction_frequency") as mock_rf, \
             patch.object(js, "set_current_range") as mock_cr, \
             patch.object(js, "collect_for", return_value=[sample]) as mock_cf:
            result = js220_preflight_capture(
                test_name="BLE_ADV",
                test_type="ble_active_bursts",
                expected_current_a=0.05,
                duration_s=10.0,
                auto_confirm=True,
            )
        # Assertions
        assert result.get("ok") is True, f"ok != True: {result.get('ok')}"
        # Envelope fields
        assert result.get("test") == "CURR:AVG" or result.get("value") == 0.05, \
            f"missing envelope fields: {result}"
        # All four setters + collect_for should have been called once each
        assert mock_iov.call_count == 1, f"set_io_voltage call_count={mock_iov.call_count}"
        assert mock_sf.call_count == 1, f"set_sampling_frequency call_count={mock_sf.call_count}"
        assert mock_rf.call_count == 1, f"set_reduction_frequency call_count={mock_rf.call_count}"
        assert mock_cr.call_count == 1, f"set_current_range call_count={mock_cr.call_count}"
        assert mock_cf.call_count == 1, f"collect_for call_count={mock_cf.call_count}"
        record(name, True, "ok=True envelope returned, all setters+collect_for called once")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 7: End-to-end propose + confirm flow (dmm)
# ---------------------------------------------------------------------------
def test_7_end_to_end() -> None:
    name = "7. 端到端 提议→确认→执行 (DMM)"
    try:
        # Step 1: propose
        with patched_dmm_connected(True), \
             patch.object(dmm, "set_nplc") as mock_set_nplc, \
             patch.object(dmm, "set_resolution") as mock_set_res, \
             patch.object(dmm, "measure_volt_dc", return_value=3.31) as mock_meas:
            proposal = dmm_preflight_measure(
                test_name="VBAT",
                function="VOLT:DC",
                lower=3.0, upper=3.6,
                nplc=1.0,
            )
        assert proposal.get("ok") is True
        assert proposal.get("needs_user_input") is True
        pc = proposal.get("proposed_config")
        assert isinstance(pc, dict) and pc.get("nplc") == 1.0
        # Step 2: simulate AI client merging user answers (use defaults)
        final_lower = pc.get("lower")
        final_upper = pc.get("upper")
        final_nplc = pc.get("nplc")
        # Step 3: run with user_confirmed=True
        with patched_dmm_connected(True), \
             patch.object(dmm, "set_nplc") as mock_set_nplc2, \
             patch.object(dmm, "set_resolution") as mock_set_res2, \
             patch.object(dmm, "measure_volt_dc", return_value=3.31) as mock_meas2:
            result = dmm_run_preflight_measure(
                test_name="VBAT",
                function="VOLT:DC",
                lower=final_lower,
                upper=final_upper,
                nplc=final_nplc,
                user_confirmed=True,
            )
        # Validate the result is the standard envelope
        for key in ("test", "value", "unit", "lower", "upper", "pass", "timestamp"):
            assert key in result, f"envelope missing key '{key}': {result}"
        assert result.get("test") == "VOLT:DC"
        assert result.get("value") == 3.31
        assert result.get("unit") == "V"
        assert result.get("lower") == 3.0
        assert result.get("upper") == 3.6
        assert result.get("pass") is True
        assert mock_set_nplc2.call_count == 1
        assert mock_meas2.call_count == 1
        record(name, True, "propose -> run user_confirmed=True -> envelope ok")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Test 8: Legacy tools still importable / callable
# ---------------------------------------------------------------------------
def test_8_legacy_smoke() -> None:
    name = "8. 原 tool 仍可 import + callable (零破坏冒烟)"
    try:
        for fn, label in [
            (dmm_measure_volt_dc,       "dmm_measure_volt_dc"),
            (dmm_set_nplc,              "dmm_set_nplc"),
            (dmm_scan_test_points,      "dmm_scan_test_points"),
            (js220_measure_current_avg, "js220_measure_current_avg"),
            (js220_set_current_range,   "js220_set_current_range"),
            (js220_run_power_profile,   "js220_run_power_profile"),
        ]:
            assert callable(fn), f"{label} is not callable"
        record(name, True, "6/6 legacy tools callable")
    except Exception:
        record(name, False, traceback.format_exc())


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def main() -> int:
    print("=" * 72)
    print("Preflight verification (8 tests, no real instruments)")
    print("=" * 72)
    test_1_dmm_propose()
    test_2_dmm_hard_gate()
    test_3_dmm_bypass()
    test_4_js_propose()
    test_5_js_hard_gate()
    test_6_js_bypass()
    test_7_end_to_end()
    test_8_legacy_smoke()
    print("=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"Summary: {passed}/{total} tests passed")
    print("=" * 72)
    if passed == total:
        print("GO")
        return 0
    else:
        print("NO-GO")
        return 1


if __name__ == "__main__":
    sys.exit(main())
