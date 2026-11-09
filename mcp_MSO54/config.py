"""MSO54 connection configuration.

Edit VISA_RESOURCE below for your setup, or override via env var MSO54_RESOURCE.

The default points to the bench MSO54 connected by USB:
  USB0::0x0699::0x0522::C041623::0::INSTR
  vendor=0x0699 (Tektronix), product=0x0522 (5 Series MSO), serial=C041623

Other common address forms:
  LAN (VXI-11/HiSLIP): TCPIP0::192.168.1.50::inst0::INSTR
  LAN (raw socket):    TCPIP0::192.168.1.50::4000::SOCKET
  USB:                 USB0::0x0699::0x0522::<serial>::0::INSTR

The bench unit (queried 2026-07-18) reports:
  TEKTRONIX,MSO54,C041623,CF:91.1CT FV:1.34.8.1078
  Options: SUP5-DVM (digital voltmeter + trigger freq counter),
           BW5-2000 (2 GHz bandwidth), LIC5-CMUSB2 (USB2.0 compliance).
  NOTE: no AFG option on this unit — the AFG tools are omitted on purpose.
"""
import os
from dataclasses import dataclass


DEFAULT_USB_RESOURCE = "USB0::0x0699::0x0522::C041623::0::INSTR"


@dataclass
class MSO54Config:
    visa_resource: str = os.environ.get("MSO54_RESOURCE", DEFAULT_USB_RESOURCE)
    # Per-query timeout. 10s covers config queries and a bounded CURVE?
    # window comfortably over USBTMC.
    timeout_ms: int = int(os.environ.get("MSO54_TIMEOUT_MS", "10000"))
    # A full-record CURVE? (up to the record length) can be large/slow;
    # waveform reads bump the transport timeout to this value.
    curve_timeout_ms: int = int(os.environ.get("MSO54_CURVE_TIMEOUT_MS", "30000"))
    # How long to wait after *RST / FACTORY / AUTOSET to settle.
    reset_settle_s: float = float(os.environ.get("MSO54_RESET_SETTLE_S", "2.0"))
    # Analog channel count for this model (MSO54 = 4).
    num_channels: int = int(os.environ.get("MSO54_NUM_CHANNELS", "4"))


CONFIG = MSO54Config()


# Automatic measurement types this MCP exposes. These map 1:1 to the
# Tektronix 5 Series `MEASUrement:MEAS<n>:TYPE` values. Kept as a tuple so
# server.py can advertise a constrained Literal and validate input.
#
# CORRECTED 2026-08-18 by bench probe on FW 1.34.8.1078: the width types are
# spelled HIGHTIME / LOWTIME on this firmware. The previously-listed
# POSITIVEWIDTH / NEGATIVEWIDTH are rejected (the slot silently keeps its old
# type), as are POSWIDTH / NEGWIDTH / WIDTH / PULSEWIDTH / DUTYCYCLE.
# Verified working: FREQUENCY, PERIOD, HIGH, LOW, HIGHTIME, LOWTIME,
# MAXIMUM, MINIMUM, PK2PK, MEAN, RMS, RISETIME, FALLTIME, AMPLITUDE, AREA,
# BURSTWIDTH. Unverified on this unit: ACRMS, DUTYCYCLE, PHASE, DELAY.
MEAS_TYPES = (
    "FREQUENCY", "PERIOD",
    "AMPLITUDE", "PK2PK", "MAXIMUM", "MINIMUM", "MEAN", "RMS", "ACRMS",
    "HIGH", "LOW",
    "RISETIME", "FALLTIME",
    "HIGHTIME", "LOWTIME",
    "PHASE", "DELAY",
    "AREA", "BURSTWIDTH",
)


# Per-measurement-type unit. The MSO54 firmware on the bench does NOT
# answer `MEASUrement:MEAS<n>:UNITS?` (it times out and wedges the USBTMC
# bus — see NOTES.md §1), so we derive the unit from the measurement type
# here instead of querying it.
MEAS_UNIT = {
    "FREQUENCY": "Hz", "PERIOD": "s",
    "AMPLITUDE": "V", "PK2PK": "V", "MAXIMUM": "V", "MINIMUM": "V",
    "MEAN": "V", "RMS": "V", "ACRMS": "V", "HIGH": "V", "LOW": "V",
    "RISETIME": "s", "FALLTIME": "s",
    "HIGHTIME": "s", "LOWTIME": "s",
    "PHASE": "°", "DELAY": "s",
    "AREA": "Vs", "BURSTWIDTH": "s",
}


# Default safety limits for the CMT BLE Sticker PCBA HWDVT line. These are
# intentionally generous product-line placeholders — tighten per DUT via the
# preflight flow or per-call lower/upper overrides. The reader compares each
# measurement to the matching window; pass=False if outside.
DEFAULT_LIMITS = {
    "FREQUENCY":         {"lower": 0.0,     "upper": 1.0e10},  # Hz
    "PERIOD":            {"lower": 0.0,     "upper": 1.0e3},   # s
    "AMPLITUDE":         {"lower": 0.0,     "upper": 50.0},    # V
    "PK2PK":             {"lower": 0.0,     "upper": 50.0},
    "MAXIMUM":           {"lower": -50.0,   "upper": 50.0},
    "MINIMUM":           {"lower": -50.0,   "upper": 50.0},
    "MEAN":              {"lower": -50.0,   "upper": 50.0},
    "RMS":               {"lower": 0.0,     "upper": 50.0},
    "ACRMS":             {"lower": 0.0,     "upper": 50.0},
    "HIGH":              {"lower": -50.0,   "upper": 50.0},
    "LOW":               {"lower": -50.0,   "upper": 50.0},
    "RISETIME":          {"lower": 0.0,     "upper": 1.0},     # s
    "FALLTIME":          {"lower": 0.0,     "upper": 1.0},
    "POSITIVEWIDTH":     {"lower": 0.0,     "upper": 1.0},
    "NEGATIVEWIDTH":     {"lower": 0.0,     "upper": 1.0},
    "POSITIVEDUTYCYCLE": {"lower": 0.0,     "upper": 100.0},   # %
    "NEGATIVEDUTYCYCLE": {"lower": 0.0,     "upper": 100.0},
    "PHASE":             {"lower": -180.0,  "upper": 180.0},   # deg
    "DELAY":             {"lower": -1.0,    "upper": 1.0},     # s
    "AREA":              {"lower": -1.0e3,  "upper": 1.0e3},
    "BURSTWIDTH":        {"lower": 0.0,     "upper": 1.0},
}


# Interactive preflight — per-measurement question templates surfaced to
# the operator via AskUserQuestion before any acquisition/measurement runs.
# Types not listed fall back to the generic template built in server.py.
DEFAULT_QUESTIONS_PER_MEAS: dict[str, list[dict]] = {
    "FREQUENCY": [
        {
            "name": "pass_window",
            "question": "该信号的合格频率区间?",
            "header": "Pass window",
            "options": [
                {"label": "32.768 kHz ±1% (RTC 晶振)", "value": {"lower": 32440.0, "upper": 33096.0}},
                {"label": "我手动指定 (输入 Hz)", "value": "manual"},
                {"label": "走默认宽松区间", "value": "default"},
            ],
            "multiSelect": False,
        },
    ],
    "PK2PK": [
        {
            "name": "pass_window",
            "question": "峰峰值 (Vpp) 合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "3.0-3.6 V (3.3V 逻辑摆幅)", "value": {"lower": 3.0, "upper": 3.6}},
                {"label": "我手动指定 (输入 V)", "value": "manual"},
                {"label": "走默认宽松区间", "value": "default"},
            ],
            "multiSelect": False,
        },
    ],
}
