"""pytest port of verify_preflight.py tests 4-6 (mcp_js220 preflight)."""
from __future__ import annotations
import pytest
from unittest.mock import patch, PropertyMock

from mcp_js220.server import (
    js, js220_preflight_capture, js220_run_preflight_capture,
)


@pytest.fixture
def patched_js_connected():
    """Patch js.connected as a property — return True."""
    pm = PropertyMock(return_value=True)
    with patch.object(type(js), "connected", pm):
        yield pm


# ---------------------------------------------------------------------------
# Test 4: js220 preflight proposal mode (auto_confirm=False)
# ---------------------------------------------------------------------------
def test_4_js_propose(patched_js_connected) -> None:
    with patch.object(js, "set_io_voltage") as mock_iov, \
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


# ---------------------------------------------------------------------------
# Test 5: js220 preflight hard gate
# ---------------------------------------------------------------------------
def test_5_js_hard_gate(patched_js_connected) -> None:
    with patch.object(js, "set_io_voltage") as mock_iov, \
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


# ---------------------------------------------------------------------------
# Test 6: js220 preflight bypass
# ---------------------------------------------------------------------------
def test_6_js_bypass(patched_js_connected) -> None:
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
    with patch.object(js, "set_io_voltage") as mock_iov, \
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
