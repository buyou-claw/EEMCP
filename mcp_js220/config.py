"""JS220 connection & default-limit configuration.

The JS220 connects only over USB; there is no VISA resource string.
`scan()` returns a list of `Device` instances — the first match is
the default. Override by passing `device_path` to `js220_connect`
or by exporting `JS220_DEVICE_PATH` (case-insensitive match on the
serial-number suffix, e.g. `JS220-005742`).

The JS220 enumerates as `u/js220/<serial_number>`; `device_path`
accepts any unique suffix of that path.

Default safety limits (mcp_envelope windows for the one-shot
measurement tools) are product-line placeholders for the
CMT BLE Sticker PCBA — tighten to your specific DUT spec.
"""
from __future__ import annotations
import os
from dataclasses import dataclass


# Match by any unique suffix of "u/js220/<serial>". Set to "None" to
# accept the first device scan() finds.
DEFAULT_DEVICE_PATH = os.environ.get("JS220_DEVICE_PATH", None)


@dataclass
class JS220Config:
    # If None: use scan()[0]. If a string: filter scan() to devices whose
    # device_path or serial contains this substring (case-insensitive).
    device_path: str | None = DEFAULT_DEVICE_PATH

    # Default sampling_frequency (input rate). 100 kHz is a good balance
    # for capturing BLE bursts without overflowing the stream_buffer.
    # Valid values are in VALID_SAMPLING_FREQS.
    default_sampling_frequency: str = os.environ.get("JS220_SAMPLING_FREQ", "100 kHz")

    # Default reduction_frequency (statistics output rate). 10 Hz is
    # plenty for tracking average current over multi-second windows
    # without overwhelming the host with stats callbacks.
    default_reduction_frequency: str = os.environ.get("JS220_REDUCTION_FREQ", "10 Hz")

    # Default current range. "auto" lets the JS220 down-range as the
    # current drops — saves you from manual range switching.
    default_current_range: str = os.environ.get("JS220_CURRENT_RANGE", "auto")

    # Default IO voltage. 3.3V matches the BLE Sticker MCU rail.
    default_io_voltage: str = os.environ.get("JS220_IO_VOLTAGE", "3.3V")

    # How long to wait after a parameter_set / start() for the
    # statistics stream to deliver its first valid sample.
    stream_warmup_s: float = float(os.environ.get("JS220_WARMUP_S", "0.5"))

    # How long the one-shot js220_measure_* helpers let the JS220
    # settle before reading the average. Picked so a 1.0 s default
    # duration delivers ~10 stats samples at 10 Hz.
    one_shot_settle_s: float = float(os.environ.get("JS220_SETTLE_S", "0.2"))

    # Per-call USB transfer timeout (ms). The joulescope driver does
    # not have a single "transport timeout" knob — this is used as
    # the statistics-stream callback timeout AND the device.open()
    # retry budget. 5000 ms is conservative for a flaky USB hub.
    usb_timeout_ms: int = int(os.environ.get("JS220_TIMEOUT_MS", "5000"))


CONFIG = JS220Config()


# --- Valid parameter values (mirrors the JS220 firmware options) ---
VALID_SOURCES = (
    "off",      # sensor off, no current flow
    "raw",      # on, normal streaming
    "pattern_usb",
    "pattern_control",
    "pattern_sensor",
)

VALID_CURRENT_RANGES = (
    "auto", "10 A", "2 A", "180 mA", "18 mA",
    "1.8 mA", "180 µA", "18 µA", "off",
)

VALID_SAMPLING_FREQS = (
    "2 MHz", "1 MHz", "500 kHz", "200 kHz", "100 kHz",
    "50 kHz", "20 kHz", "10 kHz", "5 kHz", "2 kHz",
    "1 kHz", "500 Hz", "200 Hz", "100 Hz", "50 Hz", "20 Hz", "10 Hz",
)

VALID_REDUCTION_FREQS = ("100 Hz", "50 Hz", "20 Hz", "10 Hz",
                         "5 Hz", "2 Hz", "1 Hz")

VALID_IO_VOLTAGES = (
    "1.8V", "2.1V", "2.5V", "2.7V", "3.0V", "3.3V", "3.6V", "5.0V",
)


# --- Product-line default PASS/FAIL windows ---
# These are BLE Sticker PCBA HWDVT defaults; tighten for your DUT.
DEFAULT_LIMITS = {
    # VCC rail supply sanity
    "VOLT:AVG":   {"lower": 1.8,  "upper": 5.5},   # V
    # Active-mode current draw on a BLE SoC — generous
    "CURR:AVG":   {"lower": 0.0,  "upper": 0.500}, # A
    # Peak power (10 A range × 5 V = 50 W max; PCBA far below)
    "POW:AVG":    {"lower": 0.0,  "upper": 2.5},   # W
    # Accumulated energy over a capture window
    "ENERGY":     {"lower": 0.0,  "upper": 1.0e3}, # J
    "CHARGE":     {"lower": 0.0,  "upper": 1.0e3}, # C
    # Per-burst peak
    "POW:PEAK":   {"lower": 0.0,  "upper": 5.0},   # W
    "CURR:PEAK":  {"lower": 0.0,  "upper": 1.0},   # A
}


# ---------------------------------------------------------------------------
# Interactive preflight — built-in capture presets
# ---------------------------------------------------------------------------
# Each preset bundles the four JS220 user-settable parameters
# (sampling_frequency, reduction_frequency, i_range, io_voltage)
# with a use-when and a human-readable rationale, so the
# `js220_preflight_capture` tool can present them as a
# multiple-choice question to the user.
#
# These are STATIC data — no logic. The dispatcher in server.py picks
# the preset, then `nudge_range` (already in server.py) re-checks the
# i_range against the operator's `expected_current_a` / `peak_current_a`
# to give 5-10× headroom.
#
# Source of truth mirrors the `default_test_recommendations` block
# already in server.py `js220_get_capabilities` — kept in sync manually
# (no auto-import: config.py must remain importable without the rest of
# the package to keep dependency direction clean).
PREFLIGHT_PRESETS: dict[str, dict] = {
    "long_idle_log": {
        "default_config": {
            "sampling_frequency": "1 kHz",
            "reduction_frequency": "2 Hz",
            "i_range": "auto",
            "io_voltage": "3.3V",
        },
        "use_when": "监测 standby / connected-but-idle 状态 (分钟-小时级)",
        "rationale": (
            "1 kHz 采样 + 2 Hz 统计 → 每个 stats 窗口 500 个样本, "
            "足以分辨 µA 级的 sleep current, 又不会撑爆 USB 带宽。"
        ),
    },
    "ble_active_bursts": {
        "default_config": {
            "sampling_frequency": "100 kHz",
            "reduction_frequency": "10 Hz",
            "i_range": "auto",
            "io_voltage": "3.3V",
        },
        "use_when": "捕捉 BLE TX/RX 突发 (advertising / connection events)",
        "rationale": (
            "100 kHz 采样 + 10 Hz 统计 → 每个 stats 窗口 10 000 个样本, "
            "能解析亚毫秒级峰值; i_range=auto 配合典型 50 mA / 峰值 500 mA。"
        ),
    },
    "fine_peak_capture": {
        "default_config": {
            "sampling_frequency": "2 MHz",
            "reduction_frequency": "1 Hz",
            "i_range": "10 A",
            "io_voltage": "3.3V",
        },
        "use_when": "需要看清单次 TX 突发的形状 (inrush / dI/dt)",
        "rationale": (
            "2 MHz 采样 + 1 Hz 统计 → 每个 stats 窗口 2 000 000 个样本, "
            "可以重建微秒级脉冲; i_range 锁 10 A 是为了不丢大电流峰值。"
        ),
    },
    "ultra_low_current": {
        "default_config": {
            "sampling_frequency": "100 Hz",
            "reduction_frequency": "2 Hz",
            "i_range": "18 µA",
            "io_voltage": "3.3V",
        },
        "use_when": "standby < 1 µA, 需要干净的 µA 级分辨率",
        "rationale": (
            "100 Hz 采样 + 2 Hz 统计 → 50 样本/窗口; i_range 锁 18 µA "
            "把量程噪底压到 nA 级, 适合测 deep-sleep 漏电。"
        ),
    },
    "high_current_load": {
        "default_config": {
            "sampling_frequency": "10 kHz",
            "reduction_frequency": "20 Hz",
            "i_range": "10 A",
            "io_voltage": "3.3V",
        },
        "use_when": "DUT 持续 > 100 mA (电机 / 显示屏 / 大功率 RF)",
        "rationale": (
            "10 kHz 采样 + 20 Hz 统计 → 500 样本/窗口, "
            "i_range 锁 10 A 避免反复切量程, 适合稳态大电流。"
        ),
    },
}


# Mapping used by js220_preflight_capture to translate `expected_current_a`
# into a fixed i_range with ~5-10× headroom. Mirrors the table in
# js220_recommend_config (server.py) — kept here so the preflight tool
# can compute the proposed i_range offline (without touching the device).
PREFLIGHT_I_RANGE_TABLE = (
    # (upper_bound_a, i_range_str)
    (18e-6,  "18 µA"),
    (180e-6, "180 µA"),
    (1.8e-3, "1.8 mA"),
    (18e-3,  "18 mA"),
    (180e-3, "180 mA"),
    (2.0,    "2 A"),
    (None,   "10 A"),
)
