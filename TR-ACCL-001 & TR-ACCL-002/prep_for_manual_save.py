"""为手动保存波形做准备：把示波器冻结在含真实 I2C 突发的单次采集上。

之后 CH1/CH2 的两次保存读的都是这同一条冻结记录（单次触发模式），
所以两次导出的时间轴完全对齐，可以计算 tsu(SDA)/th(SDA)。

探头当前为对调状态：CH1=SDA, CH2=SCL。
"""
from __future__ import annotations
import sys
import time

BW = float(sys.argv[1]) if len(sys.argv) > 1 else 1e9

sys.path.insert(0, "E:/other/ai_project/EEMCP")
sys.stdout.reconfigure(encoding="utf-8")

from mcp_MSO54.instrument import MSO54, MSO54Error  # noqa: E402


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
        print("  [!] 未找到 J-Link 串口")
        return False
    try:
        ser = serial.Serial(port, 115200, timeout=0.1)
    except Exception as e:
        print(f"  [!] 打开 {port} 失败: {e}")
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
                    print(f"  [DUT] continue_read ✓")
                    ser.close()
                    return True
            else:
                time.sleep(0.05)
        time.sleep(2.0)
    ser.close()
    return False


def main() -> int:
    s = MSO54()
    print(f"示波器: {s.connect()}\n")

    s.write("ACQUIRE:STATE STOP")
    time.sleep(0.15)
    for n in (1, 2):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE ON")
        s.write(f"CH{n}:COUPLING DC")
        s.write(f"CH{n}:TERMINATION 1e6")
        s.write(f"CH{n}:BANDWIDTH {BW}")
        s.write(f"CH{n}:SCALE 0.5")
        s.write(f"CH{n}:POSITION -3")
        s.write(f"CH{n}:OFFSET 0")
        s.write(f"CH{n}:LABEL:NAME \"{'SDA' if n == 1 else 'SCL'}\"")
    for n in (3, 4):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE OFF")

    s.write("HORIZONTAL:SCALE 1e-4")        # 100 µs/div → 1 ms 记录，含整段突发（约 90 个 SCL 周期）
    s.write("HORIZONTAL:POSITION 30")
    s.write("TRIGGER:A:MODE NORMAL")
    s.write("TRIGGER:A:TYPE EDGE")
    s.write("TRIGGER:A:EDGE:SOURCE CH2")    # SCL 在 CH2
    s.write("TRIGGER:A:EDGE:SLOPE FALL")
    s.write("TRIGGER:A:EDGE:COUPLING DC")
    s.write("TRIGGER:A:LEVEL:CH2 1.65")
    s.write("ACQUIRE:MODE SAMPLE")
    s.write("ACQUIRE:NUMAVG 1")
    time.sleep(0.4)

    print(f"记录长度={s.q_record_length()}  采样率={s.q_sample_rate():.3g} Sa/s  "
          f"窗口={s.q_record_length()/s.q_sample_rate()*1e3:.3f} ms")
    print(f"时基={s.q_horizontal_scale()*1e6:.0f} µs/div   触发={s.q_trig_edge_source()}↓   "
          f"带宽={s.q_channel_bandwidth(1):.3g} Hz")

    # 装一个 SCL 频率测量用于验证
    for n in range(1, 9):
        try:
            s.meas_delete(n)
        except MSO54Error:
            pass
    time.sleep(0.25)
    s.write("MEASUREMENT:MEAS1:TYPE FREQUENCY"); time.sleep(0.05)
    s.write("MEASUREMENT:MEAS1:SOURCE CH2"); time.sleep(0.05)
    s.write("MEASUREMENT:MEAS2:TYPE PK2PK"); time.sleep(0.05)
    s.write("MEASUREMENT:MEAS2:SOURCE CH1"); time.sleep(0.05)
    s.write("MEASUREMENT:MEAS3:TYPE PK2PK"); time.sleep(0.05)
    s.write("MEASUREMENT:MEAS3:SOURCE CH2"); time.sleep(0.4)

    print("\n启动 DUT 流量…")
    verified = False
    for attempt in range(4):
        pump_traffic()
        s.write("ACQUIRE:STOPAFTER SEQUENCE")
        s.write("ACQUIRE:STATE RUN")
        dl = time.time() + 3
        while time.time() < dl:
            if s.q_trig_state() in ("READY", "ARMED", "AUTO"):
                break
            time.sleep(0.05)
        dl = time.time() + 6
        trig = ""
        while time.time() < dl:
            trig = s.q_trig_state()
            if trig in ("TRIGGER", "TRIGGERED", "SAVE"):
                break
            time.sleep(0.05)
        if trig not in ("TRIGGER", "TRIGGERED", "SAVE"):
            continue
        time.sleep(0.5)
        try:
            f = s.q_meas_value(1)
            pp_sda = s.q_meas_value(2)
            pp_scl = s.q_meas_value(3)
        except Exception:
            f = pp_sda = pp_scl = None
        ok = (f is not None and 100 < f < 9e30
              and pp_sda is not None and pp_sda < 9e30 and pp_sda > 2.0
              and pp_scl is not None and pp_scl < 9e30 and pp_scl > 2.0)
        if ok:
            verified = True
            print(f"✓ 采集验证通过：SCL 频率={f:.1f} Hz, "
                  f"SDA 峰峰值={pp_sda:.3f} V, SCL 峰峰值={pp_scl:.3f} V")
            break
        print(f"  第 {attempt+1} 次不合格：freq={f}, SDA_pp={pp_sda}, SCL_pp={pp_scl} —— 重试…")

    if not verified:
        print("\n[!] 未采到含 I2C 数据的记录 —— 请确认 DUT 已上电后再试")
        s.disconnect()
        return 1

    print(f"\n触发状态 = {s.q_trig_state()}  采集状态 = {s.q_acq_state()}")
    print("示波器已冻结在这一次采集上，现在可以去做了。")
    s.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
