"""TR-ACCL-001 / TR-ACCL-002 — I2C 时序与信号完整性测试（安全版）

======================================================================
 安全设计原则（2026-08-18 三次 USBTMC 锁死事故后制定）
======================================================================
1. 只用 mcp_MSO54/scripts/validate.py 在这台示波器（FW 1.34.8.1078）
   上逐条验证过的 SCPI 命令。一条都不多。
2. **完全不碰波形下载子系统**：不发 DATA:* / WFMOUTPRE:* / CURVE?
   —— 这三次锁死全部发生在这条路径上（见 NOTES.md §1）。
3. 用示波器自带的自动测量引擎取数：MEASUREMENT:ADDMEAS（写）
   + MEAS<n>:TYPE?/SOURCE?/VALUE?（读）—— 已验证路径。
4. 触发用 NORMAL + 持续 RUN，不反复 STOP/RUN。
   NORMAL 模式下记录里必然是"有 I2C 突发"的那一次采集，
   所以测量值一定有效（不会返回 9.91e37 的"无信号"哨兵值）。
5. 用 ACQUIRE:NUMACQ? 判断是否来了新采集，只在有新数据时取值，
   避免读到重复的旧值。
6. 全程只有两次 STOP：开头配置一次、收尾一次。

======================================================================
 判据（DS_lis2dh12.pdf Rev 6）
======================================================================
Table 8（I2C 从机时序，Standard mode）—— 测量点 0.2·Vdd_IO / 0.8·Vdd_IO：
  f(SCL)        ≤ 100 kHz
  tw(SCLL)      ≥ 4.7 µs      ← 示波器 NEGATIVEWIDTH (CH1)
  tw(SCLH)      ≥ 4.0 µs      ← 示波器 POSITIVEWIDTH (CH1)
  tsu(SDA)      ≥ 250 ns      ← 需波形相关分析（本次不计）
  th(SDA)       0 ~ 3.45 µs   ← 需波形相关分析（本次不计）
Table 6（电气特性）：
  VOH  ≥ 0.9·Vdd_IO = 2.97 V ；从机识别为高需 ≥ 0.8·Vdd_IO = 2.64 V
  VOL  ≤ 0.1·Vdd_IO = 0.33 V ；从机识别为低需 ≤ 0.2·Vdd_IO = 0.66 V
Table 9（绝对最大额定值）：
  Vin 范围 -0.3 V ~ Vdd_IO+0.3 V = -0.3 ~ 3.6 V
上升/下降时间：DS 未定义，引用 I2C 总线规范 UM10204（Std: tr≤1000ns, tf≤300ns）
  注意：示波器 RISETIME/FALLTIME 默认按 10-90% 计算，I2C 规范按 20-80%，
        报告中注明此差异。
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

RUN_DIR = Path("E:/other/ai_project/EEMCP/TR-ACCL-001 & TR-ACCL-002/data")

# --swap：探头对调后运行。此时 CH1 接在 SDA、CH2 接在 SCL。
# 用于判别下冲是"跟着信号"（真实电路问题）还是"跟着通道"（探头伪影）。
SWAP = "--swap" in sys.argv
LBL_CH1 = "SDA" if SWAP else "SCL"
LBL_CH2 = "SCL" if SWAP else "SDA"

# --- 判据 ---
VDD_IO = 3.3
LIMITS = {
    "fSCL":      {"lower": 0.0,        "upper": 100e3,   "unit": "Hz"},
    "tw_SCLL":   {"lower": 4.7e-6,     "upper": None,    "unit": "s"},
    "tw_SCLH":   {"lower": 4.0e-6,     "upper": None,    "unit": "s"},
    "tr":        {"lower": None,       "upper": 1000e-9, "unit": "s"},
    "tf":        {"lower": None,       "upper": 300e-9,  "unit": "s"},
    "VOH":       {"lower": 0.9 * VDD_IO, "upper": None,  "unit": "V"},
    "VOL":       {"lower": None,       "upper": 0.1 * VDD_IO, "unit": "V"},
    "VMAX_ovs":  {"lower": None,       "upper": VDD_IO + 0.3, "unit": "V"},
    "VMIN_und":  {"lower": -0.3,       "upper": None,    "unit": "V"},
}

N_SAMPLES_PER_BATCH = 40      # 每个批次最多取多少个采集样本
POLL_INTERVAL_S = 0.2
BATCH_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
def probe(s: MSO54) -> bool:
    """探活：失败立即返回 False，不发第二条命令。"""
    try:
        s.q_acq_state()
        return True
    except Exception as e:
        print(f"  [探活失败] {type(e).__name__} —— 停止所有操作", flush=True)
        return False


class DutSerial:
    """DUT 串口控制（sensor→imu→init→continue_read）。端口自动探测。"""

    def __init__(self) -> None:
        self.ser = None
        self.port = None

    def open(self) -> tuple[bool, str]:
        import serial
        import serial.tools.list_ports
        for p in serial.tools.list_ports.comports():
            d = (p.description or "")
            if "JLink" in d or "J-Link" in d or "CDC" in d:
                self.port = p.device
                break
        if self.port is None:
            avail = [f"{p.device}({p.description})" for p in serial.tools.list_ports.comports()]
            return False, f"未找到 J-Link 串口；当前端口: {avail or '无'}"
        try:
            self.ser = serial.Serial(self.port, 115200, bytesize=8, parity="N",
                                     stopbits=1, timeout=0.1)
            time.sleep(0.3)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            return True, self.port
        except Exception as e:
            return False, f"打开 {self.port} 失败: {e}"

    def _send_wait(self, cmd: str, expect: str, timeout_s: float = 3.0) -> str:
        if self.ser is None:
            return ""
        self.ser.reset_input_buffer()
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()
        deadline = time.time() + timeout_s
        buf = ""
        while time.time() < deadline:
            n = self.ser.in_waiting
            if n:
                buf += self.ser.read(n).decode("ascii", errors="replace")
                if expect in buf:
                    break
            else:
                time.sleep(0.05)
        return buf

    def init_sequence(self) -> dict:
        out = {}
        for cmd, expect in [("sensor", "Sensor>"), ("imu", "IMU>"), ("init", "IMU ready")]:
            r = self._send_wait(cmd, expect)
            out[cmd] = r.strip()[-120:]
            print(f"  [DUT] {cmd:8s} → {r.strip()[-70:].replace(chr(10), ' | ')}", flush=True)
        return out

    def start_traffic(self) -> bool:
        """确保 DUT 正在发 I2C 突发（continue_read：20 个样本，每秒 1 次）。

        必须以首个样本行 "[ 1/20]" 为准判断启动成功 —— 上一次运行结束时也会
        打印 "Continue_Read: done (20 samples)"，误当成功会导致整批取不到数据。
        运行中发命令会得到 "No command found"，此时等待后重试。
        """
        for attempt in range(5):
            r = self._send_wait("continue_read", "[ 1/20]", timeout_s=3.0)
            if "[ 1/20]" in r:
                print(f"  [DUT] continue_read ✓ → "
                      f"{r.strip()[-70:].replace(chr(10), ' | ')}", flush=True)
                return True
            if "done (20 samples)" in r or "IMU>" in r:
                time.sleep(0.5)
                continue
            time.sleep(2.5)
        print(f"  [DUT] continue_read ✗ 多次重试仍未启动", flush=True)
        return False

    def close(self) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None


# ---------------------------------------------------------------------------
def install_measurements(s: MSO54, spec: list[tuple[str, str]]) -> dict:
    """显式配置测量槽 MEAS1..MEASn 并逐槽回读验证。

    不用 MEASUREMENT:ADDMEAS 的自动分配 —— 实测（FW 1.34.8.1078）在类型名
    被拒绝时槽位会静默保留旧类型，无法判断哪个 ADD 生效、落在哪一槽。
    改为直接写 MEAS<n>:TYPE / MEAS<n>:SOURCE，再回读确认。

    返回 {slot: {type, source, ok}}；ok=False 的槽不参与后续取值。
    """
    for n in range(1, 9):
        try:
            s.meas_delete(n)
        except MSO54Error:
            pass
    time.sleep(0.3)

    for n, (meas_type, src) in enumerate(spec, start=1):
        s.write(f"MEASUREMENT:MEAS{n}:TYPE {meas_type}")
        time.sleep(0.04)
        s.write(f"MEASUREMENT:MEAS{n}:SOURCE {src}")
        time.sleep(0.04)
    time.sleep(0.6)

    installed: dict = {}
    for n, (meas_type, src) in enumerate(spec, start=1):
        try:
            t = s.q_meas_type(n)
            got_src = s.q_meas_source(n)
        except MSO54Error:
            continue
        ok = (t.upper() == meas_type.upper() and got_src.upper() == src.upper())
        installed[n] = {"type": t, "source": got_src, "ok": ok,
                        "requested": f"{meas_type}/{src}"}
    return installed


def collect_batch(s: MSO54, title: str, spec: list[tuple[str, str]],
                  max_samples: int = N_SAMPLES_PER_BATCH,
                  timeout_s: float = BATCH_TIMEOUT_S) -> dict:
    """装入一批测量，用 NUMACQ? 检测新采集，收集多次样本。"""
    print(f"\n{'=' * 72}", flush=True)
    print(f"批次 {title}", flush=True)
    print(f"{'=' * 72}", flush=True)

    installed_all = install_measurements(s, spec)
    installed = {n: i for n, i in installed_all.items() if i.get("ok")}
    rejected = {n: i for n, i in installed_all.items() if not i.get("ok")}

    for n, info in installed.items():
        print(f"  槽{n}: {info['type']:12s} ← {info['source']}  ✓", flush=True)
    for n, info in rejected.items():
        print(f"  槽{n}: 请求 {info['requested']:22s} → 实得 "
              f"{info['type']}/{info['source']}  ✗ 已跳过", flush=True)
    if not installed:
        print("  [!] 没有测量被成功装载", flush=True)
        return {"error": "no_measurement_installed", "readback": installed_all}

    samples: dict[int, list[float]] = {n: [] for n in installed}
    bad: dict[int, int] = {n: 0 for n in installed}

    try:
        last_numacq = s.q_acq_numacq()
    except Exception:
        last_numacq = None

    deadline = time.time() + timeout_s
    while time.time() < deadline and len(samples[next(iter(installed))]) < max_samples:
        try:
            numacq = s.q_acq_numacq()
        except Exception:
            print("  [!] NUMACQ 读取失败 —— 中止本批", flush=True)
            break
        if last_numacq is None or numacq != last_numacq:
            last_numacq = numacq
            for n in installed:
                try:
                    v = s.q_meas_value(n)
                except Exception:
                    v = None
                if v is None or v >= 9e30:      # 9.91e37 = 无信号哨兵
                    bad[n] += 1
                    continue
                samples[n].append(v)
        time.sleep(POLL_INTERVAL_S)

    out = {"installed": installed, "rejected": rejected,
           "samples": {}, "sentinel_counts": bad}
    for n, info in installed.items():
        vs = samples[n]
        key = f"{info['type']}_{info['source']}"
        if vs:
            vs_sorted = sorted(vs)
            out["samples"][key] = {
                "n": len(vs),
                "min": vs_sorted[0],
                "max": vs_sorted[-1],
                "mean": sum(vs) / len(vs),
                "median": vs_sorted[len(vs_sorted) // 2],
                "raw": vs,
            }
            print(f"  {key:26s} n={len(vs):3d}  "
                  f"min={vs_sorted[0]:.6g}  mean={sum(vs)/len(vs):.6g}  "
                  f"max={vs_sorted[-1]:.6g}", flush=True)
        else:
            out["samples"][key] = {"n": 0, "sentinel": bad[n]}
            print(f"  {key:26s} 无有效样本（哨兵 {bad[n]} 次）", flush=True)
    return out


# ---------------------------------------------------------------------------
def main() -> int:
    t_start = datetime.now()
    out: dict = {
        "test_id": "TR-ACCL-001 / TR-ACCL-002",
        "started_at": t_start.isoformat(timespec="seconds"),
        "vdd_io_assumed_v": VDD_IO,
        "limits": LIMITS,
        "probe_config": "swapped (CH1=SDA, CH2=SCL)" if SWAP else "normal (CH1=SCL, CH2=SDA)",
    }
    print("=" * 72, flush=True)
    print("TR-ACCL-001 / TR-ACCL-002 — I2C 时序与信号完整性测试（安全版）", flush=True)
    print("=" * 72, flush=True)

    # ---------- 1. 连接示波器 ----------
    s = MSO54()
    out["idn"] = s.connect()
    print(f"\n[1] 示波器: {out['idn']}", flush=True)

    # ---------- 2. 配置（唯一一次配置用 STOP） ----------
    print("\n[2] 配置示波器", flush=True)
    s.write("ACQUIRE:STATE STOP")
    time.sleep(0.15)

    for n in (1, 2):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE ON")
        s.write(f"CH{n}:COUPLING DC")
        s.write(f"CH{n}:TERMINATION 1e6")
        s.write(f"CH{n}:BANDWIDTH FULL")
        s.write(f"CH{n}:SCALE 0.5")
        s.write(f"CH{n}:POSITION -3")
        s.write(f"CH{n}:OFFSET 0")
        s.write(f"CH{n}:LABEL:NAME \"{LBL_CH1 if n == 1 else LBL_CH2}\"")
    for n in (3, 4):
        s.write(f"DISPLAY:GLOBAL:CH{n}:STATE OFF")

    # 水平：保持 AUTO 模式；100 µs/div → 1 ms 记录，含约 90 个 SCL 周期
    s.write("HORIZONTAL:SCALE 1e-4")
    s.write("HORIZONTAL:POSITION 30")

    # 触发：NORMAL 保证记录里一定有 I2C 突发。
    # 触发源始终跟随 SCL 所在通道 —— 对调探头后 SCL 在 CH2，
    # 这样两次采集都对齐在 SCL 下降沿上，才可比。
    TRIG_CH = 2 if SWAP else 1
    s.write("TRIGGER:A:MODE NORMAL")
    s.write("TRIGGER:A:TYPE EDGE")
    s.write(f"TRIGGER:A:EDGE:SOURCE CH{TRIG_CH}")
    s.write("TRIGGER:A:EDGE:SLOPE FALL")
    s.write("TRIGGER:A:EDGE:COUPLING DC")
    s.write(f"TRIGGER:A:LEVEL:CH{TRIG_CH} 1.65")
    out_trig = TRIG_CH

    s.write("ACQUIRE:MODE SAMPLE")
    s.write("ACQUIRE:NUMAVG 1")
    # 必须显式设回 RUNSTOP（连续采集）。若上一步脚本留下 SEQUENCE，
    # 示波器触发一次就停，NUMACQ 不再变化 → 整批取不到样本。
    s.write("ACQUIRE:STOPAFTER RUNSTOP")
    time.sleep(0.3)

    out["scope_config"] = {
        "horizontal_scale_s_per_div": s.q_horizontal_scale(),
        "horizontal_position_pct": s.q_horizontal_position(),
        "horizontal_mode": s.q_horizontal_mode(),
        "sample_rate_sa_per_s": s.q_sample_rate(),
        "record_length": s.q_record_length(),
        "ch1_scale_v_per_div": s.q_channel_scale(1),
        "ch1_position_div": s.q_channel_position(1),
        "trig_mode": s.q_trig_mode(),
        "trig_source": s.q_trig_edge_source(),
        "trig_level_v": s.q_trig_level(out_trig),
    }
    cfg = out["scope_config"]
    win_ms = cfg["record_length"] / cfg["sample_rate_sa_per_s"] * 1e3
    print(f"  时基={cfg['horizontal_scale_s_per_div']} s/div, "
          f"采样率={cfg['sample_rate_sa_per_s']:.4g} Sa/s", flush=True)
    print(f"  记录长度={cfg['record_length']} 点, 窗口={win_ms:.3f} ms "
          f"（≈{win_ms/10*1000:.0f} 个 100 kHz SCL 周期）", flush=True)
    print(f"  垂直=0.5 V/div, 触发=NORMAL {cfg['trig_source']}↓@{cfg['trig_level_v']} V",
          flush=True)

    # ---------- 3. DUT 流量 ----------
    print("\n[3] 驱动 DUT", flush=True)
    dut = DutSerial()
    ok, info = dut.open()
    out["dut_port"] = info
    print(f"  串口: {info}", flush=True)
    if not ok:
        out["error"] = f"串口不可用: {info}"
        s.write("ACQUIRE:STATE STOP")
        s.disconnect()
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 2
    out["dut_init"] = dut.init_sequence()

    # 开始采集（此后全程 RUN，不再 STOP）
    s.write("ACQUIRE:STATE RUN")
    time.sleep(0.5)
    print(f"  示波器已 RUN，触发模式 NORMAL（记录必然含 I2C 突发）", flush=True)

    # ---------- 4. 三批测量 ----------
    batches = [
        ("A — SCL 时序 (CH1)", [
            ("FREQUENCY", "CH1"), ("PERIOD", "CH1"),
            ("HIGHTIME", "CH1"), ("LOWTIME", "CH1"),
            ("RISETIME", "CH1"), ("FALLTIME", "CH1"),
            ("AMPLITUDE", "CH1"), ("MEAN", "CH1"),
        ]),
        ("B — 电平与信号完整性", [
            ("HIGH", "CH1"), ("LOW", "CH1"),
            ("MAXIMUM", "CH1"), ("MINIMUM", "CH1"),
            ("HIGH", "CH2"), ("LOW", "CH2"),
            ("MAXIMUM", "CH2"), ("MINIMUM", "CH2"),
        ]),
        ("C — SDA 时序 + 峰峰值", [
            ("PK2PK", "CH1"), ("PK2PK", "CH2"),
            ("FREQUENCY", "CH2"),
            ("HIGHTIME", "CH2"), ("LOWTIME", "CH2"),
            ("RISETIME", "CH2"), ("FALLTIME", "CH2"),
            ("MEAN", "CH2"),
        ]),
    ]

    out["batches"] = {}
    for title, spec in batches:
        if not probe(s):
            out["error"] = f"探活失败于批次 {title}"
            break
        dut.start_traffic()           # 每批前续期，保证有流量
        bl = title.split(" ")[0]
        out["batches"][bl] = collect_batch(s, title, spec)

    # ---------- 5. 收尾 ----------
    dut.close()
    s.write("ACQUIRE:STATE STOP")
    s.disconnect()
    out["finished_at"] = datetime.now().isoformat(timespec="seconds")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    ts = t_start.strftime("%Y%m%d_%H%M%S")
    path = RUN_DIR / f"i2c_test_{ts}{'_swap' if SWAP else ''}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[完成] 结果写入 {path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())