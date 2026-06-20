"""pytest port of verify_preflight.py tests 1-3 (mcp_34465a preflight).

The assertions are copied verbatim from verify_preflight.py — the
contract (preflight propose / hard-gate / bypass semantics) is the
load-bearing thing, not the test framework.
"""
from __future__ import annotations
import pytest
from unittest.mock import patch, PropertyMock

from mcp_34465a.server import (
    dmm, dmm_preflight_measure, dmm_run_preflight_measure,
)


@pytest.fixture
def patched_dmm_connected():
    """Patch dmm.connected as a property — return True."""
    pm = PropertyMock(return_value=True)
    with patch.object(type(dmm), "connected", pm):
        yield pm


# ---------------------------------------------------------------------------
# Test 1: 34465a preflight proposal mode (auto_confirm=False)
# ---------------------------------------------------------------------------
def test_1_dmm_propose(patched_dmm_connected) -> None:
    with patch.object(dmm, "set_nplc") as mock_set_nplc, \
         patch.object(dmm, "set_resolution") as mock_set_res, \
         patch.object(dmm, "measure_volt_dc", return_value=3.31) as mock_meas:
        result = dmm_preflight_measure(
            test_name="VCC_MAIN",
            function="VOLT:DC",
            lower=3.0, upper=3.6,
            nplc=1.0,
        )
    # Assertions (copied from verify_preflight.py test 1)
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


# ---------------------------------------------------------------------------
# Test 2: 34465a preflight hard gate (user_confirmed=False)
# ---------------------------------------------------------------------------
def test_2_dmm_hard_gate(patched_dmm_connected) -> None:
    with patch.object(dmm, "set_nplc") as mock_set_nplc, \
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


# ---------------------------------------------------------------------------
# Test 3: 34465a preflight bypass (auto_confirm=True)
# ---------------------------------------------------------------------------
def test_3_dmm_bypass(patched_dmm_connected) -> None:
    with patch.object(dmm, "set_nplc") as mock_set_nplc, \
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
