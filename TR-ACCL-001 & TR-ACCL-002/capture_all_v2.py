"""TR-ACCL-001/002 — 截图 + 波形导出（修正版）。

上一版两个 bug：
  1) 改完时基后 TRIGGER:STATE? 还残留上一次的 SAVE，触发等待循环立即退出，
     实际示波器刚重新武装、没有数据 → 截到空屏。
     修正：先等状态进入 READY/ARMED（证明重新武装），再等真实触发，
     最后**用 FREQUENCY 测量值验证这次采集确实含 I2C 数据**才保存。
  2) SAVE:WAVEFORM 的 FILEFORMAT 写法未验证，文件没落到 U 盘。
     修正：一次尝试多种语法变体并各自用不同文件名，便于事后判断哪种生效。

探头对调状态：CH1=SDA, CH2=SCL。
"""
from __future__ import annotations
import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "E:/other/ai_project/EEMCP")
sys.stdout.reconfigure(encoding="utf-8")

from mcp_MSO54.instrument import MSO54, MSO54Error  # noqa: E402

ROOT = Path("E:/other/ai_project/EEMCP/TR-ACCL-001 & TR-ACCL-002")
DATA_DIR = ROOT / "data"
USB = "E:"
CH1_SIG, CH2_SIG = "SDA", "SCL"
SCL_CH = "CH2"

SHOTS = [
    ("01_full_wide",        1e9, 1e-4, "FREQ/HIGH/LOW"),
    ("02_full_zoom",        1e9, 1e-5, "RISE/FALL/MIN"),
    ("03_20mhz_zoom",       2e7, 1e-5, "RISE/FALL/MIN"),
    ("04_full_undershoot",  1e9, 2e-5, "MIN/MAX"),
    ("05_20mhz_undershoot", 2e7, 2e-5, "MIN/MAX"),
]

MEAS_SETS = {
    "FREQ/HIGH/LOW": [("FREQUENCY", SCL_CH), ("HIGHTIME", SCL_CH), ("LOWTIME", SCL_CH),
                      ("HIGH", "CH1"), ("LOW", "CH1"),
                      ("HIGH", "CH2"), ("LOW", "CH2"), ("PK2PK", "CH1")],
    "RISE/FALL/MIN": [("RISETIME", "CH1"), ("FALLTIME", "CH1"),
                      ("MINIMUM", "CH1"), ("MAXIMUM", "CH1"),
                      ("RISETIME", "CH2"), ("FALLTIME", "CH2"),
                      ("MINIMUM", "CH2"), ("MAXIMUM", "CH2")],
    "MIN/MAX":       [("MINIMUM", "CH1"), ("MINIMUM", "CH2"),
                      ("MAXIMUM", "CH1"), ("MAXIMUM", "CH2"),
                      ("HIGH", "CH1"), ("LOW", "CH1"),
                      ("PK2PK", "CH1"), ("PK2PK", "CH2")],
}
# 每个视图里用于"确认采到 I2C 数据"的槽位（必须是 FREQUENCY 或含边沿的量）
VERIFY_SLOT = {"FREQ/HIGH/LOW": 1, "RISE/FALL/MIN": 1, "MIN/MAX": 7}


def pump_traffic() -> bool:
    import serial
    import serial.tools.list_ports
    port = None
    for p in serial.tools.list_ports.comports():
        d = (p.description or "")
        if "JLink" in d or "J-Link" in d or "CDC" in d:
            port = p.device
            break
    if not port:
        return False
    try:
        ser = serial.Serial(port, 115200, timeout=0.1)
    except Exception:
        return False
    time.sleep(0.3)
    ser.reset_input_buffer()
    for cmd, exp in [("sensor", "Sensor>"), ("imu", "IMU>"), ("init", "IMU ready")]:
        ser.reset_input_buffer(); ser.write((cmd + "\n").encode()); ser.flush()
        dl = time.time() + 3
        while time.time() < dl:
            if ser.in_waiting:
                if exp in ser.read(ser.in_waiting).decode("ascii", "replace"):
                    break
            else:
                time.sleep(0.05)
    for _ in range(5):
        ser.reset_input_buffer(); ser.write(b"continue_read\n"); ser.flush()
        dl = time.time() + 3
        buf = ""
        while time.time() < dl:
            if ser.in_waiting:
                buf += ser.read(ser.in_waiting).decode("ascii", "replace")
                if "[ 1/20]" in buf:
                    ser.close()
                    return True
            else:
                time.sleep(0.05)
        time.sleep(2.0)
    ser.close()
    return False


def set_meas(s: MSO54, spec) -> int:
    for n in range(1, 9):
        try:
            s.meas_delete(n)
        except MSO54Error:
            pass
    time.sleep(0.25)
    ok = 0
    for n, (t, src) in enumerate(spec, 1):
        s.write(f"MEASUREMENT:MEAS{n}:TYPE {t}"); time.sleep(0.04)
        s.write(f"MEASUREMENT:MEAS{n}:SOURCE {src}"); time.sleep(0.04)
        try:
            if s.q_meas_type(n).upper() == t.upper():
                ok += 1
        except MSO54Error:
            pass
    return ok


def arm_and_verify(s: MSO54, verify_slot: int) -> tuple[bool, str]:
    """武装 → 等重新武装(READY) → 等真实触发 → 用测量值验证采集含 I2C 数据。

    关键：必须先看到 READY，否则可能读到上一次残留的 SAVE 状态。
    """
    for attempt in range(4):
        s.write("ACQUIRE:STOPAFTER SEQUENCE")
        s.write("ACQUIRE:STATE RUN")
        # 阶段1：确认已重新武装
        dl = time.time() + 3.0
        saw_ready = False
        while time.time() < dl:
            st = s.q_trig_state()
            if st in ("READY", "ARMED", "AUTO"):
                saw_ready = True
                break
            time.sleep(0.05)
        # 阶段2：等真实触发
        st = ""
        dl = time.time() + 5.0
        while time.time() < dl:
            st = s.q_trig_state()
            if st in ("TRIGGER", "TRIGGERED", "SAVE"):
                break
            time.sleep(0.05)
        if st not in ("TRIGGER", "TRIGGERED", "SAVE"):
            continue
        # 阶段3：用测量值确认这次采集真的含 I2C 数据
        time.sleep(0.4)
        try:
            v = s.q_meas_value(verify_slot)
        except Exception:
            v = None
        if v is not None and v < 9e30 and v != 0.0:
            return True, f"{st} / verify={v:.6g}"
        # 数据无效 → 补流量重试
        pump_traffic()
    return False, st


def main() -> int:
    out: dict = {"started_at": datetime.now().isoformat(timespec="seconds"),
                 "usb": USB, "probe_config": "swapped (CH1=SDA, CH2=SCL)",
                 "shots": [], "waveform_exports": []}
    s = MSO54()
    out["idn"] = s.connect()
    print(f"示波器: {out['idn']}\n")

    s.write("ACQUIRE:STATE STOP")
    time.sleep(0.15)
    for n in (1, 2):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE ON")
        s.write(f"CH{n}:COUPLING DC")
        s.write(f"CH{n}:TERMINATION 1e6")
        s.write(f"CH{n}:SCALE 0.5")
        s.write(f"CH{n}:POSITION -3")
        s.write(f"CH{n}:OFFSET 0")
        s.write(f"CH{n}:LABEL:NAME \"{CH1_SIG if n == 1 else CH2_SIG}\"")
    for n in (3, 4):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE OFF")
    s.write("HORIZONTAL:POSITION 30")
    s.write("TRIGGER:A:MODE NORMAL")
    s.write("TRIGGER:A:TYPE EDGE")
    s.write(f"TRIGGER:A:EDGE:SOURCE {SCL_CH}")
    s.write("TRIGGER:A:EDGE:SLOPE FALL")
    s.write("TRIGGER:A:EDGE:COUPLING DC")
    s.write(f"TRIGGER:A:LEVEL:{SCL_CH} 1.65")
    s.write("ACQUIRE:MODE SAMPLE")
    s.write("ACQUIRE:NUMAVG 1")
    time.sleep(0.3)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ok_count = 0

    for name, bw, scale, meas_key in SHOTS:
        print(f"=== {name}  (带宽 {bw:.3g} Hz, {scale*1e6:.0f} µs/div) ===")
        s.write("ACQUIRE:STATE STOP"); time.sleep(0.1)
        for n in (1, 2):
            s.write(f"CH{n}:BANDWIDTH {bw}")
        s.write(f"HORIZONTAL:SCALE {scale}")
        time.sleep(0.4)
        n_ok = set_meas(s, MEAS_SETS[meas_key])
        pump_traffic()
        good, detail = arm_and_verify(s, VERIFY_SLOT[meas_key])
        print(f"  测量装载 {n_ok}/8，采集验证: {'✓' if good else '✗'} ({detail})")
        if good:
            ok_count += 1
        path = f"{USB}/{name}_{ts}.png"
        s.write(f'SAVE:IMAGE "{path}"')
        time.sleep(2.5)
        print(f"  截图 → {path}")
        out["shots"].append({"name": name, "bandwidth_hz": bw, "scale_s_per_div": scale,
                             "capture_verified": good, "detail": detail,
                             "path": path, "meas_loaded": n_ok})

    # ---------- 波形导出：多种语法变体 ----------
    print("\n=== 波形导出尝试 ===")
    s.write("ACQUIRE:STATE STOP"); time.sleep(0.1)
    for n in (1, 2):
        s.write(f"CH{n}:BANDWIDTH 1e9")
    s.write("HORIZONTAL:SCALE 1e-4")
    set_meas(s, MEAS_SETS["FREQ/HIGH/LOW"])
    pump_traffic()
    good, detail = arm_and_verify(s, 1)
    print(f"  采集验证: {'✓' if good else '✗'} ({detail})")

    variants = [
        ("A_spreadsheet", ["SAVE:WAVEFORM:FILEFORMAT SPREADSHEET"]),
        ("B_csv",         ["SAVE:WAVEFORM:FILEFORMAT CSV"]),
        ("C_default",     []),
    ]
    for tag, pre in variants:
        for src, sig in (("CH1", CH1_SIG), ("CH2", CH2_SIG)):
            path = f"{USB}/wf_{tag}_{sig}_{ts}.csv"
            s.write(f"SAVE:WAVEFORM:SOURCE {src}")
            time.sleep(0.15)
            for c in pre:
                s.write(c)
                time.sleep(0.15)
            s.write(f'SAVE:WAVEFORM "{path}"')
            time.sleep(8.0)
            print(f"  已发 {tag} {src}({sig}) → {path}")
            out["waveform_exports"].append({"variant": tag, "source": src,
                                            "signal": sig, "path": path})

    # 同时往示波器本地也存一份，便于用 File Utilities 手动拷贝
    for src, sig in (("CH1", CH1_SIG), ("CH2", CH2_SIG)):
        path = f"/tmp/wf_{sig}_{ts}.csv"
        s.write(f"SAVE:WAVEFORM:SOURCE {src}")
        time.sleep(0.15)
        s.write("SAVE:WAVEFORM:FILEFORMAT SPREADSHEET")
        time.sleep(0.15)
        s.write(f'SAVE:WAVEFORM "{path}"')
        time.sleep(6.0)
        out["waveform_exports"].append({"variant": "local", "source": src,
                                        "signal": sig, "path": path})

    s.write("ACQUIRE:STATE STOP")
    s.disconnect()

    out["finished_at"] = datetime.now().isoformat(timespec="seconds")
    out["shots_verified"] = ok_count
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    p = DATA_DIR / f"screenshots_v2_{ts}.json"
    with open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] {ok_count}/{len(SHOTS)} 张截图确认采到真实 I2C 数据")
    print(f"元数据: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())