"""34465A connection configuration.

Edit VISA_RESOURCE below for your setup, or override via env var DMM34465A_RESOURCE.

The default points to the bench 34465A connected by USB:
  USB0::0x2A8D::0x0101::MY57515137::0::INSTR
  vendor=0x2A8D (Keysight), product=0x0101 (34465A), serial=MY57515137

Other common address forms:
  LAN:    TCPIP0::192.168.1.50::5025::SOCKET
  GPIB:   GPIB0::17::INSTR
  USB:    USB0::0x0957::0x1807::MY44001234::0::INSTR
"""
import os
from dataclasses import dataclass


DEFAULT_USB_RESOURCE = "USB0::0x2A8D::0x0101::MY57515137::0::INSTR"


@dataclass
class DMM34465AConfig:
    visa_resource: str = os.environ.get("DMM34465A_RESOURCE", DEFAULT_USB_RESOURCE)
    # Default per-query timeout. 10s covers a one-shot `:MEAS:VOLT:DC?`
    # comfortably over USB. For `dmm_measure_temperature`, the instrument
    # sits waiting for a real transducer reading — bump to 30s+ if you
    # use temperature probes on a flaky connection.
    timeout_ms: int = int(os.environ.get("DMM34465A_TIMEOUT_MS", "10000"))
    # Temperature probes wait on a valid transducer reading; without one
    # the instrument holds the bus until timeout. Set this higher than
    # the normal read timeout.
    temperature_timeout_ms: int = int(
        os.environ.get("DMM34465A_TEMP_TIMEOUT_MS", "30000")
    )
    # How long to wait after a `*RST` to settle.
    reset_settle_s: float = float(os.environ.get("DMM34465A_RESET_SETTLE_S", "1.0"))


CONFIG = DMM34465AConfig()


# 34465A measurement functions
MEAS_FUNCTIONS = (
    "VOLT:DC", "VOLT:AC",
    "CURR:DC", "CURR:AC",
    "RES", "FRES",                  # 2-wire / 4-wire resistance
    "FREQ", "PER",                  # frequency / period
    "CONT", "DIOD",                 # continuity / diode
    "CAP",                          # capacitance
    "TEMP:RTD", "TEMP:THER", "TEMP:TC",  # temperature (RTD/thermistor/TC)
)


# Default safety limits for the BLE Sticker PCBA HWDVT line. These are
# generous product-line defaults — tighten for your specific DUT or load
# from a test plan. The reader functions compare each measurement to the
# matching window; pass=False if the value is outside.
DEFAULT_LIMITS = {
    "VOLT:DC":   {"lower": 0.0,    "upper": 5.5},     # VCC rail
    "VOLT:AC":   {"lower": 0.0,    "upper": 5.0},
    "CURR:DC":   {"lower": -1.0,   "upper": 1.0},     # A (rough)
    "CURR:AC":   {"lower": 0.0,    "upper": 1.0},
    "RES":       {"lower": 0.0,    "upper": 1.0e7},   # ohms
    "FRES":      {"lower": 0.0,    "upper": 1.0e7},
    "FREQ":      {"lower": 0.0,    "upper": 1.0e9},   # Hz
    "PER":       {"lower": 0.0,    "upper": 1.0e3},   # s
    "CONT":      {"lower": 0.0,    "upper": 10.0},    # ohms (≤ ~10 Ω = short)
    "DIOD":      {"lower": 0.0,    "upper": 5.0},     # V
    "CAP":       {"lower": 0.0,    "upper": 1.0e-2},  # F
    "TEMP:RTD":  {"lower": -55.0,  "upper": 150.0},   # °C
    "TEMP:THER": {"lower": -55.0,  "upper": 150.0},
    "TEMP:TC":   {"lower": -210.0, "upper": 1820.0},
}


# ---------------------------------------------------------------------------
# Interactive preflight — function-specific question templates
# ---------------------------------------------------------------------------
# Each entry is the list of `questions` that `dmm_preflight_measure` will
# surface to the AI client (via `AskUserQuestion`) for the matching SCPI
# function. The AI is expected to gather the user's answers, merge them
# into the `proposed_config`, and call `dmm_run_preflight_measure` to
# execute the test.
#
# Functions not present in this dict fall back to the generic template
# (pass_window + nplc), which is built dynamically inside server.py.
#
# Keep this dict READ-ONLY data — no logic. All execution / SCPI
# behavior lives in server.py. The design contract is documented in
# INTERACTIVE_DESIGN.md § 3.1.
DEFAULT_QUESTIONS_PER_FUNCTION: dict[str, list[dict]] = {
    "VOLT:DC": [
        {
            "name": "pass_window",
            "question": "该测试点的合格电压区间?",
            "header": "Pass window",
            "options": [
                {"label": "3.0-3.6 V (10% 容差, 3.3V 电源推荐)", "value": {"lower": 3.0, "upper": 3.6}},
                {"label": "3.2-3.4 V (5% 容差, 紧)", "value": {"lower": 3.2, "upper": 3.4}},
                {"label": "2.7-4.5 V (宽松, 仅 smoke test)", "value": {"lower": 2.7, "upper": 4.5}},
                {"label": "我手动指定 (输入数字)", "value": "manual"},
            ],
            "multiSelect": False,
        },
        {
            "name": "nplc",
            "question": "积分时间 NPLC?",
            "header": "NPLC",
            "options": [
                {"label": "1 (默认, ~20Hz, 6½位)", "value": 1.0},
                {"label": "10 (高准确度, ~2Hz)", "value": 10.0},
                {"label": "0.001 (快速, ~228Hz)", "value": 0.001},
            ],
            "multiSelect": False,
        },
    ],
    "RES": [
        {
            "name": "wiring",
            "question": "使用 2-wire 还是 4-wire (Kelvin) 测电阻?",
            "header": "Wiring",
            "options": [
                {"label": "2-wire (默认, 普通电阻 > 10Ω)", "value": "2w"},
                {"label": "4-wire (低电阻 / 消除引线电阻)", "value": "4w"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "电阻的合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "我手动指定 (输入 lower/upper)", "value": "manual"},
                {"label": "0-10 kΩ (走通用模板)", "value": "default"},
            ],
            "multiSelect": False,
        },
        {
            "name": "nplc",
            "question": "积分时间 NPLC?",
            "header": "NPLC",
            "options": [
                {"label": "1 (默认)", "value": 1.0},
                {"label": "10 (高准确度)", "value": 10.0},
                {"label": "0.001 (快速)", "value": 0.001},
            ],
            "multiSelect": False,
        },
    ],
    "DIOD": [
        {
            "name": "diode_target",
            "question": "测的是哪颗二极管?",
            "header": "Target",
            "options": [
                {"label": "电源指示 LED (Vf ~2.0V @ 10mA)", "value": "led_indicator"},
                {"label": "TVS / 保护二极管 (Vf ~0.7V)", "value": "tvs"},
                {"label": "肖特基 (Vf ~0.3V)", "value": "schottky"},
                {"label": "通用 (走默认 0-5V 区间)", "value": "generic"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "二极管正向压降合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "0.5-0.9 V (普通 Si 二极管)", "value": {"lower": 0.5, "upper": 0.9}},
                {"label": "1.8-2.4 V (红/绿 LED @10mA)", "value": {"lower": 1.8, "upper": 2.4}},
                {"label": "我手动指定 (输入数字)", "value": "manual"},
            ],
            "multiSelect": False,
        },
    ],
    "CURR:DC": [
        {
            "name": "terminal",
            "question": "DUT 串到哪个电流输入端? (3A 端子 vs 10A 端子)",
            "header": "Terminal",
            "options": [
                {"label": "3A 端子 (I, 默认, 1µA-3A 范围)", "value": "3A"},
                {"label": "10A 端子 (>3A 才用, 10A 保险丝)", "value": "10A"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "电流合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "我手动指定 (输入 mA)", "value": "manual"},
                {"label": "走默认 -1.0..+1.0 A", "value": "default"},
            ],
            "multiSelect": False,
        },
        {
            "name": "nplc",
            "question": "积分时间 NPLC?",
            "header": "NPLC",
            "options": [
                {"label": "1 (默认)", "value": 1.0},
                {"label": "10 (高准确度)", "value": 10.0},
                {"label": "0.001 (快速)", "value": 0.001},
            ],
            "multiSelect": False,
        },
    ],
    "TEMP:RTD": [
        {
            "name": "rtd_type",
            "question": "RTD 类型 / 标称阻值?",
            "header": "RTD type",
            "options": [
                {"label": "PT100 (100Ω @ 0°C, 工业标准)", "value": "PT100"},
                {"label": "PT1000 (1000Ω @ 0°C, 高灵敏度)", "value": "PT1000"},
                {"label": "走仪器默认 10kΩ (不指定)", "value": "default"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "温度合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "走默认 -55..+150°C (产品线宽松)", "value": "default"},
                {"label": "我手动指定 (输入 °C)", "value": "manual"},
            ],
            "multiSelect": False,
        },
    ],
    "TEMP:TC": [
        {
            "name": "tc_type",
            "question": "热电偶分度号?",
            "header": "TC type",
            "options": [
                {"label": "J 型 (默认, -210..+1200°C)", "value": "J"},
                {"label": "K 型 (-200..+1370°C, 通用)", "value": "K"},
                {"label": "T 型 (-200..+400°C, 低温)", "value": "T"},
                {"label": "E 型 (-200..+900°C, 高灵敏度)", "value": "E"},
            ],
            "multiSelect": False,
        },
        {
            "name": "pass_window",
            "question": "温度合格区间?",
            "header": "Pass window",
            "options": [
                {"label": "走默认 -210..+1820°C (全量程)", "value": "default"},
                {"label": "我手动指定 (输入 °C)", "value": "manual"},
            ],
            "multiSelect": False,
        },
    ],
}
