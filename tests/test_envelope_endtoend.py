"""pytest port of verify_preflight.py test 7 (end-to-end propose + confirm)."""
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


def test_7_end_to_end(patched_dmm_connected) -> None:
    # Step 1: propose
    with patch.object(dmm, "set_nplc") as mock_set_nplc, \
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
    with patch.object(dmm, "set_nplc") as mock_set_nplc2, \
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
