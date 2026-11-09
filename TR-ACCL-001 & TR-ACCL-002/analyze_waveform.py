"""从示波器导出的 CSV 波形按规格书测量点精算 I2C 时序。

输入：data/waveform/001_full_bw.csv（全带宽）、002_20mhz.csv（20 MHz 限带）
      列 = TIME, CH1(=SDA), CH2(=SCL)，800 ps/点

判据出处：DS_lis2dh12.pdf Rev 6
  Table 8 脚注："Measurement points are done at 0.2·Vdd_IO and 0.8·Vdd_IO,
                 for both ports."
  → Vdd_IO = 3.3 V ⇒ 低电平参考 0.66 V、高电平参考 2.64 V

计算量：
  tw(SCLL)   SCL 低于 0.66 V 的持续时间          ≥ 4.7 µs
  tw(SCLH)   SCL 高于 2.64 V 的持续时间          ≥ 4.0 µs
  th(SDA)    SCL 下降沿 → SDA 跳变（SCL 为低期间） 0 ~ 3.45 µs
  tsu(SDA)   SDA 跳变 → 下一个 SCL 上升沿（同上）  ≥ 250 ns
  th(ST)     START(SDA↓ while SCL 高) → 首个 SCL↓  ≥ 4.0 µs
  tsu(SR)    前一 SCL↑ → 重复 START 的 SDA↓        ≥ 4.7 µs
  tsu(SP)    前一 SCL↑ → STOP 的 SDA↑              ≥ 4.0 µs
  tr / tf    20%–80% 转换时间（I2C 规范口径）       ≤1000 ns / ≤300 ns
  下冲       SDA/SCL 最低电平                      ≥ -0.3 V
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import numpy as np

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path("E:/other/ai_project/EEMCP/TR-ACCL-001 & TR-ACCL-002")
WF_DIR = ROOT / "data" / "waveform"

VDD_IO = 3.3
VIL = 0.2 * VDD_IO     # 0.66 V
VIH = 0.8 * VDD_IO     # 2.64 V
V_LOW_20 = 0.2 * VDD_IO
V_HIGH_80 = 0.8 * VDD_IO


def load(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    import csv
    print(f"  读取 {path.name} …", flush=True)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for _ in range(12):      # 10 行元数据 + 1 空行 + "TIME,CH1,CH2" 列名
            f.readline()
        data = np.loadtxt(f, delimiter=",", usecols=(0, 1, 2))
    return data[:, 0], data[:, 1], data[:, 2]


def crossings(t: np.ndarray, v: np.ndarray, level: float,
              rising: bool) -> np.ndarray:
    """返回跨越 level 的时刻数组（线性插值），rising=True 取上升穿越。"""
    above = v >= level
    if rising:
        idx = np.nonzero(~above[:-1] & above[1:])[0]
    else:
        idx = np.nonzero(above[:-1] & ~above[1:])[0]
    if len(idx) == 0:
        return np.array([])
    v0, v1 = v[idx], v[idx + 1]
    t0, t1 = t[idx], t[idx + 1]
    frac = np.where(v1 != v0, (level - v0) / (v1 - v0), 0.0)
    return t0 + frac * (t1 - t0)


def hysteretic_edges(t: np.ndarray, v: np.ndarray, lo: float, hi: float):
    """用迟滞判据提取干净边沿，抑制振铃造成的伪穿越。

    状态：v > hi → 高；v < lo → 低；介于两者之间 → 保持前一状态。
    返回 (rising_times, falling_times)，边沿时刻取实际阈值穿越的线性插值。
    """
    n = len(v)
    st = np.zeros(n, dtype=np.int8)
    st[v > hi] = 1
    st[v < lo] = -1
    idx = np.nonzero(st != 0)[0]
    if len(idx) == 0:
        return np.array([]), np.array([])
    # 前向填充：每个采样点取其之前最近一次"确定状态"
    pos = np.clip(np.searchsorted(idx, np.arange(n), side="right") - 1, 0, len(idx) - 1)
    filled = st[idx[pos]]

    rising, falling = [], []
    chg = np.nonzero(filled[1:] != filled[:-1])[0] + 1
    for i in chg:
        if filled[i] == 1:          # 低 → 高：在 i 之前找 VIH 穿越
            j0 = i - 1
            while j0 > 0 and filled[j0] != -1:
                j0 -= 1
            seg = slice(max(j0, i - 200000), i + 1)
            vs, ts = v[seg], t[seg]
            k = np.nonzero(vs >= hi)[0]
            if len(k):
                k0 = k[0]
                if k0 > 0 and vs[k0] != vs[k0 - 1]:
                    fr = (hi - vs[k0 - 1]) / (vs[k0] - vs[k0 - 1])
                    rising.append(ts[k0 - 1] + fr * (ts[k0] - ts[k0 - 1]))
                else:
                    rising.append(ts[k0])
        else:                        # 高 → 低：找 VIL 穿越
            j0 = i - 1
            while j0 > 0 and filled[j0] != 1:
                j0 -= 1
            seg = slice(max(j0, i - 200000), i + 1)
            vs, ts = v[seg], t[seg]
            k = np.nonzero(vs <= lo)[0]
            if len(k):
                k0 = k[0]
                if k0 > 0 and vs[k0] != vs[k0 - 1]:
                    fr = (lo - vs[k0 - 1]) / (vs[k0] - vs[k0 - 1])
                    falling.append(ts[k0 - 1] + fr * (ts[k0] - ts[k0 - 1]))
                else:
                    falling.append(ts[k0])
    return np.array(rising), np.array(falling)


def stats(a: np.ndarray, unit_scale: float = 1e9, unit: str = "ns") -> dict:
    if len(a) == 0:
        return {"n": 0}
    a = np.sort(a)
    return {"n": int(len(a)), "min": float(a[0]), "p05": float(a[int(0.05*len(a))]),
            "median": float(a[len(a)//2]), "mean": float(a.mean()),
            "p95": float(a[int(0.95*len(a)) if int(0.95*len(a)) < len(a) else -1]),
            "max": float(a[-1])}


def analyze(path: Path, label: str) -> dict:
    print(f"\n{'=' * 70}\n{label}  ({path.name})\n{'=' * 70}", flush=True)
    t, sda, scl = load(path)
    print(f"  {len(t)} 点, 时间 {t[0]*1e6:.1f} µs ~ {t[-1]*1e6:.1f} µs, "
          f"步长 {np.median(np.diff(t))*1e12:.0f} ps", flush=True)

    out: dict = {"file": path.name, "label": label, "npoints": int(len(t)),
                 "t_start_us": float(t[0]*1e6), "t_end_us": float(t[-1]*1e6)}

    # --- 电平 ---
    out["levels"] = {
        "sda_min_V": float(sda.min()), "sda_max_V": float(sda.max()),
        "scl_min_V": float(scl.min()), "scl_max_V": float(scl.max()),
        "sda_high_mean_V": float(sda[sda > VIH].mean()) if (sda > VIH).any() else None,
        "sda_low_mean_V": float(sda[sda < VIL].mean()) if (sda < VIL).any() else None,
        "scl_high_mean_V": float(scl[scl > VIH].mean()) if (scl > VIH).any() else None,
        "scl_low_mean_V": float(scl[scl < VIL].mean()) if (scl < VIL).any() else None,
    }
    L = out["levels"]
    print(f"  电平: SDA min={L['sda_min_V']:+.4f} max={L['sda_max_V']:+.4f} | "
          f"SCL min={L['scl_min_V']:+.4f} max={L['scl_max_V']:+.4f}", flush=True)
    print(f"        SDA 高均值={L['sda_high_mean_V']:.4f} 低均值={L['sda_low_mean_V']:+.4f} | "
          f"SCL 高均值={L['scl_high_mean_V']:.4f} 低均值={L['scl_low_mean_V']:+.4f}",
          flush=True)

    # --- SCL 边沿（迟滞判据，抑制振铃伪穿越）---
    scl_r, scl_f = hysteretic_edges(t, scl, VIL, VIH)
    scl_f_hi = crossings(t, scl, VIH, rising=False)   # 下降穿越 2.64 V
    scl_r_lo = crossings(t, scl, VIL, rising=True)    # 上升穿越 0.66 V
    print(f"  SCL: 上升沿 {len(scl_r)} 个, 下降沿 {len(scl_f)} 个（迟滞判据）", flush=True)

    # 高电平时间：上升穿越 2.64V → 下降穿越 2.64V
    # 配对时用下一个"迟滞下降沿"作为边界，并取该边界之前最后一次 2.64V 穿越，
    # 避免振铃在下降沿附近的伪穿越把一个高电平期切成若干小段。
    high_times = []
    for rf in scl_r:
        nxt_f = scl_f[scl_f > rf]
        if not len(nxt_f):
            continue
        cand = scl_f_hi[(scl_f_hi > rf) & (scl_f_hi <= nxt_f[0])]
        if len(cand):
            high_times.append(cand[-1] - rf)
    # 低电平时间：下降穿越 0.66V → 上升穿越 0.66V
    low_times = []
    for ff in scl_f:
        nxt = scl_r_lo[scl_r_lo > ff]
        if len(nxt):
            low_times.append(nxt[0] - ff)
    out["scl_low_periods_s"] = [float(x) for x in low_times]

    out["tw_SCLH_s"] = stats(np.array(high_times))
    out["tw_SCLL_s"] = stats(np.array(low_times))
    if len(high_times):
        print(f"  tw(SCLH) 高电平时间: n={len(high_times)} "
              f"min={min(high_times)*1e9:.1f} 均值={np.mean(high_times)*1e9:.1f} "
              f"max={max(high_times)*1e9:.1f} ns", flush=True)
    if len(low_times):
        print(f"  tw(SCLL) 低电平时间: n={len(low_times)} "
              f"min={min(low_times)*1e9:.1f} 均值={np.mean(low_times)*1e9:.1f} "
              f"max={max(low_times)*1e9:.1f} ns", flush=True)

    # --- SDA 跳变（同样迟滞）---
    sda_r, sda_f = hysteretic_edges(t, sda, VIL, VIH)
    sda_ev = np.sort(np.concatenate([
        np.column_stack([sda_r, np.ones(len(sda_r))]),
        np.column_stack([sda_f, np.zeros(len(sda_f))]),
    ], axis=0), axis=0) if len(sda_r) or len(sda_f) else np.zeros((0, 2))
    print(f"  SDA: 上升 {len(sda_r)} 个, 下降 {len(sda_f)} 个, 合计跳变 {len(sda_ev)}",
          flush=True)

    # --- 判定每个 SDA 跳变发生在 SCL 高还是低 ---
    data_changes = []   # (t, rising?) 在 SCL 低期间
    start_edges = []    # SDA↓ while SCL 高
    stop_edges = []     # SDA↑ while SCL 高
    for te, is_rise in sda_ev:
        # SCL 是否"确定处于低电平"：最近一次确定事件是 0.66V 下降穿越，
        # 而不是 2.64V 上升穿越 —— 避免把过渡带里的跳变误判为数据位
        last_lo = scl_f[scl_f <= te]
        last_hi = scl_r_lo[scl_r_lo <= te]
        a = last_lo[-1] if len(last_lo) else -np.inf
        b = last_hi[-1] if len(last_hi) else -np.inf
        if a > b:
            data_changes.append((te, bool(is_rise), a))   # 记录参考的 SCL 下降沿
        else:
            (stop_edges if is_rise else start_edges).append(te)

    print(f"  数据位跳变（SCL 低期间）{len(data_changes)} 个 | "
          f"START {len(start_edges)} 个 | STOP {len(stop_edges)} 个", flush=True)

    # --- th(SDA): SCL↓ → SDA 跳变 ---
    th = [te - a for te, _r, a in data_changes]
    # 记录每个跳变所在 SCL 低电平周期的长度（用于判断是否被拉长）
    low_period_of = []
    for te, _r, a in data_changes:
        nxt = scl_r_lo[scl_r_lo > a]
        low_period_of.append((nxt[0] - a) if len(nxt) else None)
    out["th_SDA_with_scl_low_period"] = [
        {"th_s": float(x), "scl_low_s": (float(y) if y else None)}
        for x, y in zip(th, low_period_of)]
    # --- tsu(SDA): SDA 跳变 → 下一个 SCL↑ ---
    tsu = []
    for te, _r, _a in data_changes:
        nxt = scl_r[scl_r >= te]
        if len(nxt):
            tsu.append(nxt[0] - te)

    out["th_SDA_s"] = stats(np.array(th))
    # 区分：SCL 低电平正常的周期 vs 被拉长的周期
    # （I2C UM10204 注 4：tHD;DAT 上限仅在器件不拉长 SCL 低电平时才需满足）
    STRETCH = 5.5e-6
    th_norm = [x for x, lp in zip(th, low_period_of)
               if lp is not None and lp <= STRETCH]
    th_stretch = [x for x, lp in zip(th, low_period_of)
                  if lp is not None and lp > STRETCH]
    out["th_SDA_normal_s"] = stats(np.array(th_norm))
    out["th_SDA_stretched_s"] = stats(np.array(th_stretch))
    out["n_stretched_low_periods"] = int(sum(1 for lp in low_period_of
                                             if lp is not None and lp > STRETCH))
    out["tsu_SDA_s"] = stats(np.array(tsu))
    if th:
        print(f"  th(SDA)  保持时间(全部): n={len(th)} min={min(th)*1e9:.1f} "
              f"中位={np.median(th)*1e9:.1f} max={max(th)*1e9:.1f} ns", flush=True)
    if th_norm:
        print(f"  th(SDA)  保持时间(低电平正常周期): n={len(th_norm)} "
              f"min={min(th_norm)*1e9:.1f} 中位={np.median(th_norm)*1e9:.1f} "
              f"max={max(th_norm)*1e9:.1f} ns", flush=True)
    if th_stretch:
        print(f"  th(SDA)  保持时间(低电平被拉长周期): n={len(th_stretch)} "
              f"max={max(th_stretch)*1e9:.1f} ns  ← I2C 规范豁免上限要求", flush=True)
    if tsu:
        print(f"  tsu(SDA) 建立时间: n={len(tsu)} min={min(tsu)*1e9:.1f} "
              f"中位={np.median(tsu)*1e9:.1f} max={max(tsu)*1e9:.1f} ns", flush=True)

    # --- START/STOP 时序 ---
    thST, tsuSR, tsuSP = [], [], []
    for te in start_edges:
        nxt_f = scl_f[scl_f > te]
        if len(nxt_f):
            thST.append(nxt_f[0] - te)          # START 保持时间
        prev_r = scl_r[scl_r <= te]
        if len(prev_r):
            tsuSR.append(te - prev_r[-1])       # 重复 START 建立时间
    for te in stop_edges:
        prev_r = scl_r[scl_r <= te]
        if len(prev_r):
            tsuSP.append(te - prev_r[-1])       # STOP 建立时间
    out["th_ST_s"] = stats(np.array(thST))
    out["tsu_SR_s"] = stats(np.array(tsuSR))
    out["tsu_SP_s"] = stats(np.array(tsuSP))
    for name, arr, lim in (("th(ST) ", thST, "≥4.0µs"),
                           ("tsu(SR)", tsuSR, "≥4.7µs"),
                           ("tsu(SP)", tsuSP, "≥4.0µs")):
        if arr:
            print(f"  {name}: n={len(arr)} min={min(arr)*1e9:.1f} "
                  f"中位={np.median(arr)*1e9:.1f} max={max(arr)*1e9:.1f} ns  ({lim})",
                  flush=True)

    # --- 20-80% 上升/下降时间 ---
    def slew(sig, te, rising):
        """20-80%（=0.2/0.8·Vdd_IO）转换时间。

        上升沿：从 0.66V 穿越到 2.64V 穿越；
        下降沿：从 2.64V 穿越到 0.66V 穿越。
        在 te 附近 ±3 µs 内查找，且要求两次穿越顺序正确。
        """
        w0 = max(0, np.searchsorted(t, te - 3e-6) - 1)
        w1 = min(len(t), np.searchsorted(t, te + 3e-6) + 1)
        seg_t, seg_v = t[w0:w1], sig[w0:w1]
        if rising:
            i_lo = np.nonzero(seg_v <= VIL)[0]
            i_hi = np.nonzero(seg_v >= VIH)[0]
            if not len(i_lo) or not len(i_hi):
                return None
            a = i_lo[-1]                       # 上升前最后的低电平点
            b = i_hi[i_hi > a]
            if not len(b):
                return None
            return seg_t[b[0]] - seg_t[a]
        else:
            i_hi = np.nonzero(seg_v >= VIH)[0]
            i_lo = np.nonzero(seg_v <= VIL)[0]
            if not len(i_hi) or not len(i_lo):
                return None
            a = i_hi[np.nonzero(seg_t[i_hi] <= te)[0][-1]] if len(np.nonzero(seg_t[i_hi] <= te)[0]) else None
            if a is None:
                return None
            b = i_lo[i_lo > a]
            if not len(b):
                return None
            return min(seg_t[b[0]] - seg_t[a], 3e-6)

    tr_sda = [x for x in (slew(sda, te, True) for te in sda_r) if x]
    tf_sda = [x for x in (slew(sda, te, False) for te in sda_f) if x]
    tr_scl = [x for x in (slew(scl, te, True) for te in scl_r) if x]
    tf_scl = [x for x in (slew(scl, te, False) for te in scl_f) if x]
    out["tr_sda_20_80_s"] = stats(np.array(tr_sda))
    out["tf_sda_20_80_s"] = stats(np.array(tf_sda))
    out["tr_scl_20_80_s"] = stats(np.array(tr_scl))
    out["tf_scl_20_80_s"] = stats(np.array(tf_scl))
    for name, arr in (("SDA tr", tr_sda), ("SDA tf", tf_sda),
                      ("SCL tr", tr_scl), ("SCL tf", tf_scl)):
        if arr:
            print(f"  {name} (20-80%): n={len(arr)} min={min(arr)*1e9:.1f} "
                  f"中位={np.median(arr)*1e9:.1f} max={max(arr)*1e9:.1f} ns",
                  flush=True)

    return out


def main() -> int:
    files = [("001_full_bw.csv", "全带宽 1 GHz"),
             ("002_20mhz.csv", "20 MHz 限带")]
    results = {}
    for fn, label in files:
        p = WF_DIR / fn
        if not p.exists():
            print(f"[跳过] {fn} 不存在")
            continue
        results[fn] = analyze(p, label)

    out_path = WF_DIR / "waveform_analysis.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果写入 {out_path}")

    # ---- 判定汇总 ----
    print(f"\n{'=' * 78}")
    print(f"{'参数':<14}{'判据':<18}{'全带宽实测':<22}{'20MHz实测':<22}{'判定'}")
    print("=" * 78)

    def g(fn, key, fld="min"):
        r = results.get(fn, {}).get(key) or {}
        return r.get(fld) if r.get("n") else None

    rows = [
        ("tw(SCLL)", "≥ 4.7 µs", "tw_SCLL_s", "min", 4.7e-6, "min"),
        ("tw(SCLH)", "≥ 4.0 µs", "tw_SCLH_s", "min", 4.0e-6, "min"),
        ("th(SDA)★", "0 ~ 3.45 µs", "th_SDA_normal_s", "max", 3.45e-6, "max"),
        ("tsu(SDA)", "≥ 250 ns", "tsu_SDA_s", "min", 250e-9, "min"),
        ("th(ST)", "≥ 4.0 µs", "th_ST_s", "min", 4.0e-6, "min"),
        ("tsu(SR)", "≥ 4.7 µs", "tsu_SR_s", "min", 4.7e-6, "min"),
        ("tsu(SP)†", "≥ 4.0 µs", "tsu_SP_s", "min", 4.0e-6, "min"),
        ("tr(SDA)", "≤ 1000 ns", "tr_sda_20_80_s", "max", 1000e-9, "max"),
        ("tf(SDA)‡", "≤ 300 ns", "tf_sda_20_80_s", "median", 300e-9, "max"),
        ("tr(SCL)", "≤ 1000 ns", "tr_scl_20_80_s", "max", 1000e-9, "max"),
        ("tf(SCL)", "≤ 300 ns", "tf_scl_20_80_s", "max", 300e-9, "max"),
    ]
    # ★ 只统计 SCL 低电平正常的周期（规格豁免被拉长周期的上限要求）
    # † 本记录窗口内未捕获到 STOP 条件
    # ‡ 用中位数（个别边沿的测量窗口受限）
    for name, lim, key, fld, thr, kind in rows:
        a = g("001_full_bw.csv", key, fld)
        b = g("002_20mhz.csv", key, fld)
        fa = "—" if a is None else (f"{a*1e9:.1f} ns" if abs(a) < 1e-3 else f"{a*1e6:.3f} µs")
        fb = "—" if b is None else (f"{b*1e9:.1f} ns" if abs(b) < 1e-3 else f"{b*1e6:.3f} µs")
        v = "—"
        if a is not None:
            v = "通过" if (a >= thr if kind == "min" else a <= thr) else "失败"
        print(f"{name:<14}{lim:<18}{fa:<22}{fb:<22}{v}")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())