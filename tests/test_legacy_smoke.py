"""pytest port of verify_preflight.py test 8 (legacy tool importability)."""
from __future__ import annotations
import pytest

from mcp_34465a.server import (
    dmm_measure_volt_dc, dmm_set_nplc, dmm_scan_test_points,
)
from mcp_js220.server import (
    js220_measure_current_avg, js220_set_current_range, js220_run_power_profile,
)


def test_8_legacy_smoke() -> None:
    """All 6 legacy tools are still importable and callable (zero breakage)."""
    for fn, label in [
        (dmm_measure_volt_dc,       "dmm_measure_volt_dc"),
        (dmm_set_nplc,              "dmm_set_nplc"),
        (dmm_scan_test_points,      "dmm_scan_test_points"),
        (js220_measure_current_avg, "js220_measure_current_avg"),
        (js220_set_current_range,   "js220_set_current_range"),
        (js220_run_power_profile,   "js220_run_power_profile"),
    ]:
        assert callable(fn), f"{label} is not callable"
