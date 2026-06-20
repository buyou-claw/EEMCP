"""MT8852B connection configuration.

For this lab:
  - VISA backend: NI-VISA (`nivisa64.dll`), since Keysight IO Libraries
    is also installed but its `visa32.dll` segfaults on this hardware.
    The bare name "niVisa64" only works if the `pyvisa_ni` wrapper is
    installed (it isn't on this machine) — we therefore use the full
    absolute path so `pyvisa.ResourceManager(...)` loads the DLL
    directly and skips the wrapper.
  - Resource: GPIB0::27::INSTR (the MT8852B's GPIB address, set on its
    front panel; default is 16, our unit is set to 27).

To override at runtime, set env vars:
  MT8852B_VISA_LIB   path to visa DLL (default: C:/Windows/System32/nivisa64.dll)
  MT8852B_RESOURCE   VISA resource string (default: GPIB0::27::INSTR)
  MT8852B_TIMEOUT_MS query/write timeout in ms (default: 15000)
"""
import os
from dataclasses import dataclass


# Default to the absolute NI-VISA DLL path. The pyvisa 'niVisa64'
# shorthand requires the `pyvisa_ni` wrapper, which only ships with
# the NI-VISA Windows installer (not on PyPI), so we can't rely on it.
_DEFAULT_NI_VISA = r"C:\Windows\System32\nivisa64.dll"


@dataclass
class MT8852BConfig:
    visa_library: str = os.environ.get("MT8852B_VISA_LIB", _DEFAULT_NI_VISA)
    visa_resource: str = os.environ.get("MT8852B_RESOURCE", "GPIB0::27::INSTR")
    timeout_ms: int = int(os.environ.get("MT8852B_TIMEOUT_MS", "15000"))
    dut_ready_timeout_s: float = float(os.environ.get("MT8852B_DUT_TIMEOUT_S", "10"))
    dut_ready_poll_s: float = float(os.environ.get("MT8852B_DUT_POLL_S", "0.5"))
    test_completion_timeout_s: float = float(
        os.environ.get("MT8852B_TEST_TIMEOUT_S", "30")
    )
    # Seconds to sleep after a `*RST` for the instrument to settle
    # before accepting further commands.
    reset_settle_s: float = float(
        os.environ.get("MT8852B_RESET_SETTLE_S", "2.0")
    )


CONFIG = MT8852BConfig()


# BLE channel range (advertising channels 37/38/39 + data 0..36)
BLE_CHANNELS = list(range(0, 40))

# Per-Phy valid channel set (per Bluetooth Core Spec)
BLE_PHY_CHANNELS = {
    "1M": list(range(0, 40)),          # 1M uses all 40 channels
    "2M": list(range(0, 40)),          # 2M uses all 40 channels
    "Coded": list(range(0, 40)),       # Coded S2/S8 uses all 40 channels
    "Coded-S2": list(range(0, 40)),
    "Coded-S8": list(range(0, 40)),
}
