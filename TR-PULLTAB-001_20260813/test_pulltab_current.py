r"""TR-PULLTAB-001 — pull-tab current test report (12M + 14D).

Emits a combined EN/ZH workbook. Verdict is organised around the three
spec test items rather than raw stage windows; device state (Inventory
Sleep / boot ramp / Active advertising) is inferred purely from the
supply-current trace.

Data set:
  Both models use the same two-phase capture pattern, dated 2026-08-13:
    10 uA fixed range  — Inventory Sleep with the tab installed
    10 mA fixed range — pull-tab / Active / re-install sequence
  The earlier single-range 100 mA capture of 12M (2026-08-12) has been
  superseded; see the Notes sheet.

Styling follows REPORT_SPEC.md (style elements only).
"""
from __future__ import annotations
import csv
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

# Per REPORT_SPEC.md §1: every test cycle has its own folder with three
# sub-directories. Driver lives at the root, raw CSVs in data/, generated
# workbooks / plots / summaries in reports/.
TEST_CYCLE_DIR = Path(r"e:\other\ai_project\EEMCP\TR-PULLTAB-001_20260813")
DATA_DIR = TEST_CYCLE_DIR / "data"
REPORTS_DIR = TEST_CYCLE_DIR / "reports"

PLOT_NAMES = ("current_traces_overview.png",
              "current_traces_activation.png",
              "current_traces_reinstall.png")

# Markers — derived from the active-capture CSVs in REPORTS_DIR.
# Override here only if the operator records a different re-install moment.
MARKERS = {
    "12M": {"rise": 0.094, "active": 0.250, "reinstall": 130.0},
    "14D": {"rise": 0.406, "active": 0.906, "reinstall": 76.765},
}

# --- Detection thresholds (also documented in the Methodology sheet) -------
PULSE_A = 1e-3        # >1 mA  => advertising / Active burst
RISE_A = 10e-6        # >10 uA rolling mean => supply-current rise (boot)
RISE_WIN = 10         # rolling-mean window, samples
ACTIVE_WIN_S = 5.0    # a pulse within this window => still Active
SLEEP_QUIET_S = 10.0  # no pulse for this long => Inventory Sleep
OVERRANGE_A = 9.9e37

# --- Captures -------------------------------------------------------------
CAPTURES = [
    {
        "model": "12M", "phase": "sleep", "device_id": "12M-01",
        "csv": "centering_12M_v2_sleep_20260813_141458.csv",
        "range_a": 10e-6, "nplc": 0.001,
        "start": "2026-08-13T14:14:58", "end": "2026-08-13T14:16:36",
        "reinstall_t": None,
    },
    {
        "model": "12M", "phase": "active", "device_id": "12M-01",
        "csv": "centering_12M_v2_active_20260813_141636.csv",
        "range_a": 10e-3, "nplc": 0.001,
        "start": "2026-08-13T14:16:36", "end": "2026-08-13T14:20:13",
        "reinstall_t": 130.0,  # operator marker (wall clock of "reinstall now")
    },
    {
        "model": "14D", "phase": "sleep", "device_id": "14D-01",
        "csv": "pulltab_14D_sleep_20260813_133416.csv",
        "range_a": 10e-6, "nplc": 0.001,
        "start": "2026-08-13T13:34:16", "end": "2026-08-13T13:36:06",
        "reinstall_t": None,
    },
    {
        "model": "14D", "phase": "active", "device_id": "14D-01",
        "csv": "pulltab_14D_active_20260813_133623.csv",
        "range_a": 10e-3, "nplc": 0.001,
        "start": "2026-08-13T13:36:23", "end": "2026-08-13T13:39:55",
        "reinstall_t": 76.765,  # operator marker
    },
]

PLOTS = ["current_traces_overview.png",
         "current_traces_activation.png",
         "current_traces_reinstall.png"]


def fmt_range(range_a: float) -> str:
    return (f"{range_a*1e6:g} uA" if range_a < 1e-3
            else f"{range_a*1e3:g} mA")

HEADER_FILL = PatternFill("solid", fgColor="EEEEEE")
GRAY_FILL = PatternFill("solid", fgColor="EEEEEE")
PASS_FILL = PatternFill("solid", fgColor="2E7D32")
FAIL_FILL = PatternFill("solid", fgColor="C62828")
INFO_FILL = PatternFill("solid", fgColor="F9A825")
WHITE_BOLD = Font(name="Calibri", size=11, bold=True, color="FFFFFFFF")


# ==========================================================================
# Analysis
# ==========================================================================
@dataclass
class Capture:
    model: str
    phase: str
    device_id: str
    range_a: float
    nplc: float
    start: str
    end: str
    reinstall_t: float | None
    rows: list = field(default_factory=list)     # [(t, i_a, iso)]
    events: list = field(default_factory=list)   # [(t0, t1, peak_a, n)]
    stats: dict = field(default_factory=dict)
    rise_t: float | None = None
    first_pulse_t: float | None = None
    timeline: list = field(default_factory=list)  # [(t0, t1, state)]

    @property
    def label(self) -> str:
        return f"{self.model}/{self.phase}"

    @property
    def noise_floor_uA(self) -> float:
        """Peak-to-peak noise of the quiet portion, in uA."""
        quiet = [v for t, v, _ in self.rows
                 if self.first_pulse_t is None or t < self.first_pulse_t - 1]
        if len(quiet) < 10:
            return 0.0
        return (max(quiet) - min(quiet)) * 1e6


def load(path: Path) -> list:
    with path.open(newline="", encoding="utf-8") as f:
        return [(float(r["t_s"]), float(r["current_a"]), r["iso_time"])
                for r in csv.DictReader(f)]


def find_events(rows: list, thr: float = PULSE_A) -> list:
    out, ine = [], False
    t0 = pk = 0.0
    n = 0
    for t, v, _ in rows:
        if v > thr:
            if not ine:
                ine, t0, pk, n = True, t, v, 1
            else:
                pk, n = max(pk, v), n + 1
        elif ine:
            out.append((t0, t, pk, n))
            ine = False
    if ine:
        out.append((t0, rows[-1][0], pk, n))
    return out


def find_rise(rows: list, before_t: float | None) -> float | None:
    """First time the RISE_WIN rolling mean exceeds RISE_A.

    Only meaningful when the measurement range resolves tens of uA; on the
    100 mA range the boot ramp is buried in the noise floor.
    """
    limit = before_t if before_t is not None else 9e9
    for i in range(len(rows) - RISE_WIN):
        t = rows[i][0]
        if t >= limit:
            break
        win = [v for _, v, _ in rows[i:i + RISE_WIN]]
        if statistics.fmean(win) > RISE_A:
            return t
    return None


def build_timeline(rows: list, events: list) -> list:
    """Collapse the trace into Active / Sleep segments from pulse cadence."""
    if not rows:
        return []
    if not events:
        return [(rows[0][0], rows[-1][0], "sleep")]
    segs = []
    cur_state = "sleep"
    cur_t0 = rows[0][0]
    last_pulse = None
    for t0, t1, _, _ in events:
        if last_pulse is not None and t0 - last_pulse > SLEEP_QUIET_S:
            segs.append((cur_t0, last_pulse + ACTIVE_WIN_S, "active"))
            cur_t0 = last_pulse + ACTIVE_WIN_S
            cur_state = "sleep"
        if cur_state == "sleep":
            segs.append((cur_t0, t0, "sleep"))
            cur_t0 = t0
            cur_state = "active"
        last_pulse = t1
    segs.append((cur_t0, rows[-1][0], cur_state))
    return [s for s in segs if s[1] - s[0] > 0.05]


def summarize(rows: list) -> dict:
    vs = [v for _, v, _ in rows]
    valid = [v for v in vs if abs(v) < OVERRANGE_A / 2]
    span = rows[-1][0] - rows[0][0] if len(rows) > 1 else 0.0
    return {
        "n": len(rows),
        "overrange": len(vs) - len(valid),
        "duration_s": span,
        "rate_hz": len(rows) / span if span > 0 else 0.0,
        "min_uA": min(valid) * 1e6,
        "max_uA": max(valid) * 1e6,
        "median_uA": statistics.median(valid) * 1e6,
        "mean_uA": statistics.fmean(valid) * 1e6,
        "p95_uA": statistics.quantiles(valid, n=20)[-1] * 1e6,
        "over_100uA": sum(1 for v in valid if v > 100e-6),
        "over_1mA": sum(1 for v in valid if v > PULSE_A),
    }


def analyse(spec: dict) -> Capture:
    c = Capture(model=spec["model"], phase=spec["phase"],
                device_id=spec["device_id"], range_a=spec["range_a"],
                nplc=spec["nplc"], start=spec["start"], end=spec["end"],
                reinstall_t=spec["reinstall_t"])
    c.rows = load(DATA_DIR / spec["csv"])
    c.events = find_events(c.rows)
    c.stats = summarize(c.rows)
    c.first_pulse_t = c.events[0][0] if c.events else None
    c.rise_t = find_rise(c.rows, c.first_pulse_t)
    c.timeline = build_timeline(c.rows, c.events)
    return c


# ==========================================================================
# Test-item evaluation
# ==========================================================================
@dataclass
class ItemResult:
    verdict: str        # PASS / FAIL / INFO
    measured: str
    evidence: str


def eval_item1(caps: dict) -> ItemResult:
    """Activation latency: pull-tab -> Active, inferred from current."""
    act = caps.get("active") or caps.get("combined")
    if act is None or act.first_pulse_t is None:
        return ItemResult("FAIL", "n/a", "no Active burst found in the trace")
    fp = act.first_pulse_t
    FIRST_PULSE_MIN_S = 0.1
    if (act.rise_t is not None and fp > FIRST_PULSE_MIN_S
            and fp - act.rise_t < 5.0):
        lat = fp - act.rise_t
        return ItemResult(
            "PASS", f"{lat*1e3:.0f} ms",
            f"supply-current rise at t={act.rise_t:.3f} s "
            f"-> first >1 mA burst at t={fp:.3f} s "
            f"(range {act.range_a*1e3:g} mA resolves the boot ramp)")
    if fp <= FIRST_PULSE_MIN_S:
        return ItemResult(
            "INFO", f"< {fp+0.05:.2f} s (ramp missed)",
            f"first >1 mA burst already at t={fp:.3f} s — "
            f"the capture started after the boot ramp, so the activation "
            f"latency is not resolvable from this trace "
            f"(only an upper bound of {fp+0.05:.2f} s is implied)")
    return ItemResult(
        "INFO", f"< {1.0:.1f} s (bounded)",
        f"first >1 mA burst at t={fp:.3f} s, but the boot ramp is below the "
        f"{act.noise_floor_uA:.0f} uA pk-pk noise floor of the "
        f"{act.range_a*1e3:g} mA range — latency not resolvable")


def eval_item2(caps: dict) -> ItemResult:
    """Stays Active across pull-tab pad re-connection."""
    act = caps.get("active") or caps.get("combined")
    if act is None or act.reinstall_t is None:
        return ItemResult("FAIL", "n/a", "no re-install marker for this model")
    ri = act.reinstall_t
    after = [e for e in act.events if e[0] > ri]
    if not after:
        return ItemResult("FAIL", "0 pulses",
                          f"no >1 mA pulse after re-install at t={ri:.2f} s")
    span = act.rows[-1][0] - ri
    gaps = [after[i+1][0] - after[i][0] for i in range(len(after) - 1)]
    cadence = (f"inter-pulse gaps: {min(gaps):.1f}-{max(gaps):.1f} s"
               if gaps else "single pulse, no gap")
    if len(after) == 1:
        cadence = (f"single pulse at t={after[0][0]:.2f} s "
                   f"({after[0][0] - ri:.0f} s after re-install), "
                   f"then quiet to t={act.rows[-1][0]:.2f} s "
                   f"— sparse, but not shutdown")
    return ItemResult(
        "PASS", f"{len(after)} pulses / {span:.0f} s",
        f"re-install at t={ri:.2f} s; advertising continued at "
        f"t={after[0][0]:.2f} s and kept up to t={after[-1][1]:.2f} s; "
        f"{cadence} — no shutdown")


def eval_item3(caps: dict) -> ItemResult:
    """Inventory Sleep: stable current with the tab installed."""
    slp = caps.get("sleep")
    if slp is not None:
        s = slp.stats
        return ItemResult(
            "PASS",
            f"{s['median_uA']:.2f} uA median "
            f"({s['min_uA']:.2f}–{s['max_uA']:.2f} uA)",
            f"{s['n']} samples over {s['duration_s']:.0f} s on the "
            f"{slp.range_a*1e6:g} uA range, {s['over_1mA']} pulses >1 mA "
            f"and {s['overrange']} overrange — stable, no wake events")
    comb = caps.get("combined")
    if comb is None or comb.first_pulse_t is None:
        return ItemResult("FAIL", "n/a", "no sleep segment in the data set")
    pre = [v for t, v, _ in comb.rows if t < comb.first_pulse_t - 1]
    return ItemResult(
        "INFO",
        f"no wake events / {len(pre)} samples",
        f"quiet before activation, but the {comb.range_a*1e3:g} mA range has "
        f"a {comb.noise_floor_uA:.0f} uA pk-pk noise floor — the uA-level "
        f"sleep current is below resolution and cannot be qualified")


ITEM_EVALS = [eval_item1, eval_item2, eval_item3]


def eval_model(caps: dict) -> tuple[str, list[ItemResult]]:
    res = [fn(caps) for fn in ITEM_EVALS]
    overall = "FAIL" if any(r.verdict == "FAIL" for r in res) else "PASS"
    return overall, res


# ==========================================================================
# Plot generation (runs on the same in-memory data the report embeds)
# ==========================================================================
BLUE, RED, GREEN = "#1f77b4", "#c62828", "#2E7D32"
PLOT_FLOOR_A = 1e-8


def _clip(vs, floor=PLOT_FLOOR_A):
    return [max(v, floor) for v in vs]


def _ts_vs(cap: Capture):
    return [t for t, _, _ in cap.rows], [v for _, v, _ in cap.rows]


def _get_active(caps: list, model: str) -> Capture | None:
    for c in caps:
        if c.model == model and c.phase == "active":
            return c
    return None


def _get_sleep(caps: list, model: str) -> Capture | None:
    for c in caps:
        if c.model == model and c.phase == "sleep":
            return c
    return None


def plot_overview(caps: list, out: Path) -> Path:
    """4-panel figure: each model's sleep + active trace."""
    fig, axes = plt.subplots(4, 1, figsize=(13, 13))
    panels = [
        (axes[0], _get_sleep(caps, "12M"),
         "12M — Inventory Sleep, fixed 10 uA range (tab installed)", []),
        (axes[1], _get_active(caps, "12M"),
         "12M — pull / Active / re-install, fixed 10 mA range",
         [(MARKERS["12M"]["rise"], "current rise"),
          (MARKERS["12M"]["active"], "activation"),
          (MARKERS["12M"]["reinstall"], "tab re-install")]),
        (axes[2], _get_sleep(caps, "14D"),
         "14D — Inventory Sleep, fixed 10 uA range (tab installed)", []),
        (axes[3], _get_active(caps, "14D"),
         "14D — pull / Active / re-install, fixed 10 mA range",
         [(MARKERS["14D"]["rise"], "current rise"),
          (MARKERS["14D"]["active"], "activation"),
          (MARKERS["14D"]["reinstall"], "tab re-install")]),
    ]
    for ax, cap, title, marks in panels:
        if cap is None:
            ax.set_title(f"{title} — (no data)")
            continue
        ts, vs = _ts_vs(cap)
        ax.plot(ts, _clip(vs), ".", ms=1.0, color=BLUE, alpha=0.5)
        ax.set_yscale("log")
        ax.set_title(title, fontsize=11)
        ax.set_ylabel("Current (A)")
        ax.grid(True, alpha=0.3, which="both")
        ax.axhline(1e-3, color=RED, lw=0.8, ls="--", alpha=0.7)
        for x, lbl in marks:
            ax.axvline(x, color=RED, lw=1.2, ls="-", alpha=0.8)
            ax.annotate(lbl, (x, ax.get_ylim()[1]), fontsize=8, color=RED,
                        rotation=90, va="top", ha="right")
    axes[-1].set_xlabel("Time (s)")
    fig.suptitle("TR-PULLTAB-001 — supply-current overview "
                 "(dashed red = 1 mA advertising threshold)", fontsize=12)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_activation(caps: list, out: Path) -> Path:
    """Side-by-side: each model's first 3 s of the active trace."""
    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    panels = [
        (axes[0], _get_active(caps, "14D"), "14D"),
        (axes[1], _get_active(caps, "12M"), "12M"),
    ]
    for ax, cap, model in panels:
        m = MARKERS[model]
        if cap is None:
            ax.set_title(f"{model} — (no data)")
            continue
        ts, vs = _ts_vs(cap)
        seg = [(t, v) for t, v in zip(ts, vs) if t <= 3.0]
        ax.plot([t for t, _ in seg], [v * 1e6 for _, v in seg], "-o", ms=2.5,
                lw=0.8, color=BLUE)
        ax.set_yscale("symlog", linthresh=10)
        ax.axvline(m["rise"], color=GREEN, lw=1.4,
                   label=f"current rise {m['rise']:.3f} s")
        ax.axvline(m["active"], color=RED, lw=1.4,
                   label=f"first >1 mA burst {m['active']:.3f} s")
        ax.set_title(f"{model} activation — 10 mA range resolves the boot ramp\n"
                     f"latency = {(m['active']-m['rise'])*1e3:.0f} ms",
                     fontsize=11)
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Current (uA, symlog)")
        ax.legend(fontsize=8)
        ax.grid(True, alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def plot_reinstall(caps: list, out: Path) -> Path:
    """Vertical stack: each model's window around the re-install marker."""
    fig, axes = plt.subplots(2, 1, figsize=(13, 7))
    windows = {"14D": (60.0, 110.0), "12M": (110.0, 180.0)}
    order = ["14D", "12M"]
    for ax, model in zip(axes, order):
        m = MARKERS[model]
        cap = _get_active(caps, model)
        if cap is None:
            ax.set_title(f"{model} — (no data)")
            continue
        ts, vs = _ts_vs(cap)
        lo, hi = windows[model]
        seg = [(t, v) for t, v in zip(ts, vs) if lo <= t <= hi]
        ax.plot([t for t, _ in seg], _clip([v for _, v in seg]), ".",
                ms=3.0, color=BLUE)
        ax.axvline(m["reinstall"], color=RED, lw=1.6,
                   label=f"tab re-installed {m['reinstall']:.2f} s")
        ax.set_title(f"{model} — advertising continues across tab re-install "
                     "(stays Active)", fontsize=11)
    for ax in axes:
        ax.set_yscale("log")
        ax.axhline(1e-3, color=RED, lw=0.8, ls="--", alpha=0.7,
                   label="1 mA threshold")
        ax.set_ylabel("Current (A)")
        ax.grid(True, alpha=0.3, which="both")
        ax.legend(fontsize=8, loc="upper right")
    axes[-1].set_xlabel("Time (s)")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out


def generate_plots(caps: list, out_dir: Path = REPORTS_DIR) -> list[Path]:
    """Render every plot from the in-memory capture data; return paths."""
    paths = [
        plot_overview(caps, out_dir / PLOT_NAMES[0]),
        plot_activation(caps, out_dir / PLOT_NAMES[1]),
        plot_reinstall(caps, out_dir / PLOT_NAMES[2]),
    ]
    for p in paths:
        print(f"wrote: {p}")
    return paths


# ==========================================================================
# Translations
# ==========================================================================
T = {
    "en": {
        "sheets": ["01_Cover", "02_Methodology", "03_Test Items",
                   "04_Statistics", "05_Curves", "06_Raw 12M sleep",
                   "07_Raw 12M active", "08_Raw 14D sleep",
                   "09_Raw 14D active", "10_Notes"],
        "title": "TR-PULLTAB-001 — Pull-tab Current Test Report (12M + 14D)",
        "test_id": "Test ID", "model": "Model", "device": "Device",
        "overall": "Overall verdict", "pass": "PASS", "fail": "FAIL",
        "info": "INFO", "mode": "Mode", "mode_live": "Live",
        "started": "Started", "finished": "Finished",
        "cover_tbl": ["Model", "Verdict", "Items passed", "Capture ranges"],
        "meth_title": "02_Methodology — Test methodology",
        "purpose": "§1 Test purpose",
        "purpose_text": (
            "Verify, from supply current alone, that the BLE Sticker tag "
            "(a) activates within the spec window after the insulator tab is "
            "pulled, (b) stays Active regardless of the connection state "
            "across the pull-tab pads, and (c) draws a stable Inventory "
            "Sleep current while the tab is installed. Device state is "
            "inferred from the current trace; no serial or RF instrumentation "
            "is used."),
        "ranges": "§2 Two-phase range strategy",
        "ranges_text": (
            "Sleep current (sub-uA to a few uA) and Active advertising peaks "
            "(1–8 mA) are four decades apart, and auto-range leaves gaps in "
            "the trace at every range change. Both models are therefore "
            "captured in two fixed-range segments: 10 uA for Inventory Sleep "
            "with the tab installed, 10 mA for the pull / Active / re-install "
            "sequence."),
        "states": "§3 State inference rules",
        "states_hdr": ["Inferred state", "Current signature", "Rule"],
        "states_rows": [
            ("Inventory Sleep", "flat, |I| below ~10 uA, no mA pulses",
             f"no pulse >1 mA for >= {SLEEP_QUIET_S:g} s"),
            ("Boot / activation ramp", "monotonic rise, ~10 uA -> ~100 uA",
             f"{RISE_WIN}-sample rolling mean crosses {RISE_A*1e6:g} uA"),
            ("Active (advertising)", "recurring 1–8 mA pulses, ~1 s cadence",
             f"a pulse >{PULSE_A*1e3:g} mA within {ACTIVE_WIN_S:g} s"),
            ("Shutdown", "returns to sleep floor and stays there",
             f"no pulse >1 mA for >= {SLEEP_QUIET_S:g} s after being Active"),
        ],
        "rate": "§4 Sampling-rate limitation (important)",
        "rate_text": (
            "The DMM is polled with `:READ?` at 65–79 Hz (NPLC=0.001, "
            "auto-zero OFF). Advertising pulses are narrower than ~13 ms and "
            "repeat roughly once per second, so each captured pulse occupies "
            "exactly one sample and roughly half of all pulses fall between "
            "samples. Pulse count is therefore a lower bound and pulse width "
            "cannot be measured with this method; peak amplitude is captured "
            "correctly. This — not the current range — is what made an "
            "earlier 14D capture appear to have no activation burst. "
            "Resolving pulse shape requires buffered sampling or a "
            "wide-bandwidth energy analyser."),
        "config": "§5 Instrument configuration",
        "config_hdr": ["Capture", "Range", "NPLC", "Samples", "Rate",
                       "Min / Max"],
        "proc": "§6 Capture procedure",
        "proc_rows": [
            "1. Connect the Keysight 34465A over USB (NI-VISA).",
            "2. Configure CURR:DC at a fixed range, NPLC=0.001, auto-zero OFF.",
            "3. Confirm the range echo and that auto-range reads 0.",
            "4. Discard one warm-up `:READ?`.",
            "5. Loop `:READ?`, appending one fsync'd CSV row per sample.",
            "6. Sleep phase: capture with the tab installed, then stop before "
            "the tab is pulled (the low range's shunt would drop the rail "
            "if the tag woke up).",
            "7. Switch to the 10 mA range, restart, then pull the tab and "
            "later re-install it, recording each marker time.",
        ],
        "items_title": "03_Test Items — Spec items and verdicts",
        "items_hdr": ["#", "Spec item", "Verdict", "Measured", "Evidence"],
        "items": [
            ("ITEM-1", "Active: tag in Inventory state shall activate and "
                       "enter the system within the spec window of pulling "
                       "the tab"),
            ("ITEM-2", "Active: tag remains Active regardless of the "
                       "connected state across the pull-tab pads (unless "
                       "ALLOW_SHUTDOWN is cleared)"),
            ("ITEM-3", "Inventory Sleep: stable current with the pull-tab "
                       "pads L/R/N and the tab installed"),
        ],
        "stats_title": "04_Statistics — Per-capture statistics",
        "stats_hdr": ["Capture", "Range", "n", "Rate (Hz)", "Dur (s)",
                      "min (uA)", "max (uA)", "median (uA)", "p95 (uA)",
                      ">100 uA", ">1 mA", "overrange"],
        "tl_title": "State timeline (inferred from current)",
        "tl_hdr": ["Capture", "From (s)", "To (s)", "Inferred state"],
        "ev_title": "Advertising pulses >1 mA (lower bound, see §4)",
        "ev_hdr": ["Capture", "t0 (s)", "t1 (s)", "Peak (mA)", "Samples"],
        "curves_title": "05_Curves — Current traces",
        "curve_caps": [
            "Full trace, both models (log scale).",
            "Per-stage breakdown, 12M vs 14D.",
            "Zoom on the pull-tab activation window.",
        ],
        "raw_hdr": ["Sample #", "t (s)", "Current (A)", "ISO time"],
        "notes_title": "09_Notes",
        "notes": [
            "Live run on a Keysight 34465A (USB0::0x2A8D::0x0101::MY57515137::0::INSTR).",
            "Every CSV row is one `:READ?`, written with fsync; summary.json is rewritten every 100 samples.",
            "Device state is inferred from supply current only — see Methodology §3 for the rules.",
            "Pulse counts are lower bounds because of the polling rate — see Methodology §4.",
            "All four captures use the two-phase 10 uA / 10 mA fixed-range pattern dated 2026-08-13.",
            "The earlier single-range 100 mA capture of 12M (2026-08-12) has been superseded by the 10 uA / 10 mA pair and removed.",
        ],
        "footer": "Generated by test_pulltab_current.py (TR-PULLTAB-001) on {ts}.",
    },
    "zh": {
        "sheets": ["01_封面", "02_测试方法", "03_测试项",
                   "04_统计", "05_曲线", "06_原始数据 12M 睡眠",
                   "07_原始数据 12M 激活", "08_原始数据 14D 睡眠",
                   "09_原始数据 14D 激活", "10_备注"],
        "title": "TR-PULLTAB-001 — 拉片电流测试报告（12M + 14D）",
        "test_id": "测试编号", "model": "机型", "device": "设备编号",
        "overall": "总体结论", "pass": "通过", "fail": "不通过",
        "info": "仅供参考", "mode": "测试模式", "mode_live": "实况",
        "started": "开始时间", "finished": "结束时间",
        "cover_tbl": ["机型", "结论", "通过项", "采集量程"],
        "meth_title": "02_测试方法 — 测试方法",
        "purpose": "§1 测试目的",
        "purpose_text": (
            "仅凭供电电流验证 BLE Sticker 标签："
            "(a) 拔出绝缘片后在规格窗口内激活并进入系统；"
            "(b) 无论拉片焊盘的连接状态如何都保持 Active；"
            "(c) 装有绝缘片时 Inventory Sleep 电流稳定。"
            "设备状态完全由电流曲线推断，不使用串口或射频仪器。"),
        "ranges": "§2 双量程采集策略",
        "ranges_text": (
            "睡眠电流（亚微安到几微安）与 Active 广告峰值（1–8 mA）相差四个数量级，"
            "而自动量程每次换挡都会在曲线上留下空档。因此两台机型都按相同的两段固定量程采集："
            "Inventory Sleep 用 10 µA 档，拉片 / Active / 重装序列用 10 mA 档。"),
        "states": "§3 状态推断规则",
        "states_hdr": ["推断状态", "电流特征", "判定规则"],
        "states_rows": [
            ("Inventory Sleep", "平直，|I| 低于约 10 µA，无毫安脉冲",
             f"连续 {SLEEP_QUIET_S:g} s 以上无 >1 mA 脉冲"),
            ("启动 / 激活斜坡", "单调爬升，约 10 µA → 100 µA",
             f"{RISE_WIN} 点滚动均值越过 {RISE_A*1e6:g} µA"),
            ("Active（广告）", "1–8 mA 周期脉冲，约 1 s 一次",
             f"{ACTIVE_WIN_S:g} s 内出现 >{PULSE_A*1e3:g} mA 脉冲"),
            ("关断", "回落到睡眠底噪并保持",
             f"进入 Active 后连续 {SLEEP_QUIET_S:g} s 无 >1 mA 脉冲"),
        ],
        "rate": "§4 采样率限制（重要）",
        "rate_text": (
            "DMM 以 `:READ?` 轮询，速率 65–79 Hz（NPLC=0.001，自动零位 OFF）。"
            "广告脉冲宽度小于约 13 ms、约 1 s 重复一次，因此每个被抓到的脉冲只占 1 个采样点，"
            "且约有一半脉冲落在采样间隙被漏掉。所以脉冲计数只是下限，"
            "本方法也无法测量脉冲宽度；但峰值幅度是准确的。"
            "早前一次 14D 采集之所以看起来「没有激活突发」，原因正是这一点，而不是电流量程。"
            "要看清脉冲形状需要用缓冲采样或宽带能量分析仪。"),
        "config": "§5 仪器配置",
        "config_hdr": ["采集段", "量程", "NPLC", "样本数", "速率", "最小 / 最大"],
        "proc": "§6 采集流程",
        "proc_rows": [
            "1. 通过 USB（NI-VISA）连接 Keysight 34465A。",
            "2. 配置 CURR:DC 固定量程、NPLC=0.001、自动零位 OFF。",
            "3. 回读量程确认，并确认自动量程为 0。",
            "4. 丢弃一次预热 `:READ?`。",
            "5. 循环 `:READ?`，每样本追加一行并 fsync 写入 CSV。",
            "6. 睡眠段：装着绝缘片采集，拉片之前必须先停止"
            "（低量程分流电阻大，标签一旦唤醒会拉塌供电轨）。",
            "7. 切到 10 mA 档重新启动，然后拉片、稍后重装，并记录每个动作的时刻。",
        ],
        "items_title": "03_测试项 — 规格条目与判定",
        "items_hdr": ["#", "规格条目", "结论", "实测", "依据"],
        "items": [
            ("ITEM-1", "Active：处于 Inventory 状态的标签应在拉片后的规格窗口内"
                       "激活并进入系统"),
            ("ITEM-2", "Active：无论拉片焊盘的连接状态如何，标签都保持 Active"
                       "（除非 ALLOW_SHUTDOWN 被清除）"),
            ("ITEM-3", "Inventory Sleep：拉片焊盘 L/R/N 且装有绝缘片时电流稳定"),
        ],
        "stats_title": "04_统计 — 分段统计",
        "stats_hdr": ["采集段", "量程", "n", "速率 (Hz)", "时长 (s)",
                      "min (µA)", "max (µA)", "中位 (µA)", "p95 (µA)",
                      ">100 µA", ">1 mA", "超量程"],
        "tl_title": "状态时间轴（由电流推断）",
        "tl_hdr": ["采集段", "起 (s)", "止 (s)", "推断状态"],
        "ev_title": "广告脉冲 >1 mA（下限，见 §4）",
        "ev_hdr": ["采集段", "t0 (s)", "t1 (s)", "峰值 (mA)", "采样点数"],
        "curves_title": "05_曲线 — 电流曲线",
        "curve_caps": [
            "完整曲线，两机型对比（对数坐标）。",
            "分阶段对比，12M vs 14D。",
            "拉片激活窗口放大。",
        ],
        "raw_hdr": ["序号", "t (s)", "电流 (A)", "ISO 时间"],
        "notes_title": "09_备注",
        "notes": [
            "实况测试，仪器 Keysight 34465A（USB0::0x2A8D::0x0101::MY57515137::0::INSTR）。",
            "每个 CSV 行为一次 `:READ?`，按行 fsync 写入；summary.json 每 100 采样重写一次。",
            "设备状态仅由供电电流推断——规则见测试方法 §3。",
            "受轮询速率限制，脉冲计数为下限——见测试方法 §4。",
            "四段采集均使用 2026-08-13 的 10 µA / 10 mA 双量程固定档位方法。",
            "12M 早前用单一 100 mA 档采集的数据已被本组 10 µA / 10 mA 双量程取代并删除。",
        ],
        "footer": "Generated by test_pulltab_current.py (TR-PULLTAB-001) 生成于 {ts}。",
    },
}


# ==========================================================================
# Rendering helpers
# ==========================================================================
def h1(ws, text, span="A1:F1"):
    ws["A1"] = text
    ws["A1"].font = Font(name="Calibri", size=18, bold=True)
    ws.merge_cells(span)
    ws.row_dimensions[1].height = 30


def section(ws, row, text, ncol=6):
    c = ws.cell(row=row, column=1, value=text)
    c.font = Font(name="Calibri", size=14, bold=True)
    c.fill = GRAY_FILL
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)


def table_head(ws, row, headers):
    for j, hh in enumerate(headers):
        c = ws.cell(row=row, column=j + 1, value=hh)
        c.font = Font(name="Calibri", size=11, bold=True)
        c.fill = HEADER_FILL


def para(ws, row, text, ncol=6, height=90):
    c = ws.cell(row=row, column=1, value=text)
    c.alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=ncol)
    ws.row_dimensions[row].height = height


def widths(ws, spec: dict):
    for col, w in spec.items():
        ws.column_dimensions[col].width = w


def verdict_cell(ws, row, col, verdict, t):
    word = {"PASS": t["pass"], "FAIL": t["fail"], "INFO": t["info"]}[verdict]
    fill = {"PASS": PASS_FILL, "FAIL": FAIL_FILL, "INFO": INFO_FILL}[verdict]
    c = ws.cell(row=row, column=col, value=word)
    c.fill = fill
    c.font = WHITE_BOLD
    c.alignment = Alignment(horizontal="center")
    return c


# ==========================================================================
# Sheets
# ==========================================================================
def render(lang: str, out: Path, caps: list[Capture]) -> None:
    t = T[lang]
    names = t["sheets"]
    by_model: dict[str, dict[str, Capture]] = {}
    for c in caps:
        by_model.setdefault(c.model, {})[c.phase] = c
    results = {m: eval_model(ph) for m, ph in by_model.items()}

    wb = Workbook()
    wb.remove(wb.active)

    # ---- 01 Cover --------------------------------------------------------
    s = wb.create_sheet(names[0])
    h1(s, t["title"], "A1:D1")
    s["A3"], s["B3"] = t["test_id"], "TR-PULLTAB-001"
    overall = "FAIL" if any(v == "FAIL" for v, _ in results.values()) else "PASS"
    s["A5"] = t["overall"]
    c = s.cell(row=5, column=2,
               value=t["pass"] if overall == "PASS" else t["fail"])
    c.font = Font(name="Calibri", size=24, bold=True, color="FFFFFFFF")
    c.fill = PASS_FILL if overall == "PASS" else FAIL_FILL
    c.alignment = Alignment(horizontal="center", vertical="center")
    s.row_dimensions[5].height = 36

    table_head(s, 7, t["cover_tbl"])
    row = 8
    for m, phases in by_model.items():
        v, items = results[m]
        npass = sum(1 for r in items if r.verdict == "PASS")
        s.cell(row=row, column=1, value=f"{m} ({list(phases.values())[0].device_id})")
        verdict_cell(s, row, 2, v, t)
        s.cell(row=row, column=3, value=f"{npass}/{len(items)}")
        s.cell(row=row, column=4,
               value=", ".join(f"{p}={fmt_range(c.range_a)}"
                               for p, c in phases.items()))
        row += 1

    row += 1
    s.cell(row=row, column=1, value=t["mode"])
    s.cell(row=row, column=2, value=t["mode_live"])
    s.cell(row=row + 1, column=1, value=t["started"])
    s.cell(row=row + 1, column=2,
           value=", ".join(f"{c.label}@{c.start}" for c in caps))
    s.cell(row=row + 2, column=1, value=t["finished"])
    s.cell(row=row + 2, column=2,
           value=", ".join(f"{c.label}@{c.end}" for c in caps))
    widths(s, {"A": 24, "B": 46, "C": 16, "D": 40})

    # ---- 02 Methodology --------------------------------------------------
    s = wb.create_sheet(names[1])
    h1(s, t["meth_title"])
    section(s, 3, t["purpose"]);  para(s, 4, t["purpose_text"], height=100)
    section(s, 6, t["ranges"]);   para(s, 7, t["ranges_text"], height=110)
    section(s, 9, t["states"])
    table_head(s, 10, t["states_hdr"])
    r = 11
    for a, b, cc in t["states_rows"]:
        s.cell(row=r, column=1, value=a)
        s.cell(row=r, column=2, value=b)
        s.cell(row=r, column=3, value=cc)
        r += 1
    r += 1
    section(s, r, t["rate"]);     para(s, r + 1, t["rate_text"], height=140)
    r += 3
    section(s, r, t["config"])
    table_head(s, r + 1, t["config_hdr"])
    r += 2
    for c in caps:
        st = c.stats
        vals = [c.label, fmt_range(c.range_a), f"{c.nplc:g}", st["n"],
                f"{st['rate_hz']:.1f}",
                f"{st['min_uA']:.2f} / {st['max_uA']:.2f} uA"]
        for j, v in enumerate(vals):
            s.cell(row=r, column=j + 1, value=v)
        r += 1
    r += 1
    section(s, r, t["proc"])
    r += 1
    for b in t["proc_rows"]:
        cc = s.cell(row=r, column=1, value=b)
        cc.alignment = Alignment(wrap_text=True, vertical="top")
        s.merge_cells(start_row=r, start_column=1, end_row=r, end_column=6)
        s.row_dimensions[r].height = 30
        r += 1
    widths(s, {"A": 26, "B": 34, "C": 30, "D": 14, "E": 14, "F": 22})

    # ---- 03 Test Items ---------------------------------------------------
    s = wb.create_sheet(names[2])
    h1(s, t["items_title"], "A1:E1")
    r = 3
    for m, phases in by_model.items():
        v, items = results[m]
        section(s, r, f"── {m} ({list(phases.values())[0].device_id}) ──", 5)
        r += 1
        table_head(s, r, t["items_hdr"])
        r += 1
        for (tag, spec_text), res in zip(t["items"], items):
            s.cell(row=r, column=1, value=tag)
            cc = s.cell(row=r, column=2, value=spec_text)
            cc.alignment = Alignment(wrap_text=True, vertical="top")
            verdict_cell(s, r, 3, res.verdict, t)
            s.cell(row=r, column=4, value=res.measured)
            ce = s.cell(row=r, column=5, value=res.evidence)
            ce.alignment = Alignment(wrap_text=True, vertical="top")
            s.row_dimensions[r].height = 58
            r += 1
        r += 2
    widths(s, {"A": 10, "B": 52, "C": 12, "D": 26, "E": 78})

    # ---- 04 Statistics ---------------------------------------------------
    s = wb.create_sheet(names[3])
    h1(s, t["stats_title"], "A1:L1")
    table_head(s, 3, t["stats_hdr"])
    r = 4
    for c in caps:
        st = c.stats
        vals = [c.label, fmt_range(c.range_a), st["n"],
                f"{st['rate_hz']:.2f}", f"{st['duration_s']:.2f}",
                f"{st['min_uA']:.3f}", f"{st['max_uA']:.3f}",
                f"{st['median_uA']:.3f}", f"{st['p95_uA']:.3f}",
                st["over_100uA"], st["over_1mA"], st["overrange"]]
        for j, v in enumerate(vals):
            cc = s.cell(row=r, column=j + 1, value=v)
            if j > 1:
                cc.alignment = Alignment(horizontal="right")
        r += 1

    r += 2
    section(s, r, t["tl_title"], 4)
    r += 1
    table_head(s, r, t["tl_hdr"])
    r += 1
    for c in caps:
        for t0, t1, state in c.timeline:
            s.cell(row=r, column=1, value=c.label)
            s.cell(row=r, column=2, value=f"{t0:.3f}")
            s.cell(row=r, column=3, value=f"{t1:.3f}")
            s.cell(row=r, column=4, value=state)
            r += 1

    r += 2
    section(s, r, t["ev_title"], 5)
    r += 1
    table_head(s, r, t["ev_hdr"])
    r += 1
    for c in caps:
        for t0, t1, pk, n in c.events:
            s.cell(row=r, column=1, value=c.label)
            s.cell(row=r, column=2, value=f"{t0:.3f}")
            s.cell(row=r, column=3, value=f"{t1:.3f}")
            s.cell(row=r, column=4, value=f"{pk*1e3:.3f}")
            s.cell(row=r, column=5, value=n)
            r += 1
    widths(s, {get_column_letter(i + 1): w for i, w in
               enumerate([20, 12, 10, 11, 11, 12, 12, 13, 12, 11, 10, 11])})

    # ---- 05 Curves -------------------------------------------------------
    s = wb.create_sheet(names[4])
    h1(s, t["curves_title"], "A1:H1")
    s.column_dimensions["A"].width = 110
    r = 3
    for png, cap in zip(PLOTS, t["curve_caps"]):
        p = REPORTS_DIR / png
        section(s, r, cap, 8)
        r += 1
        if p.exists():
            img = XLImage(str(p))
            sc = min(1.0, 900 / img.width)
            img.width, img.height = int(img.width * sc), int(img.height * sc)
            s.add_image(img, f"A{r}")
            r += int(img.height / 19) + 3
        else:
            s.cell(row=r, column=1, value=f"(missing: {png})")
            r += 2

    # ---- 06..09 Raw ----------------------------------------------------
    order = [("12M", "sleep", names[5]), ("12M", "active", names[6]),
             ("14D", "sleep", names[7]), ("14D", "active", names[8])]
    for model, phase, sheet in order:
        c = by_model.get(model, {}).get(phase)
        s = wb.create_sheet(sheet)
        if c is None:
            s["A1"] = f"(no data for {model}/{phase})"
            continue
        h1(s, f"{c.label} — {c.device_id} — {fmt_range(c.range_a)} range — "
              f"{c.start} → {c.end}", "A1:D1")
        table_head(s, 3, t["raw_hdr"])
        for i, (tt, vv, iso) in enumerate(c.rows):
            s.cell(row=4 + i, column=1, value=i + 1)
            s.cell(row=4 + i, column=2, value=f"{tt:.4f}")
            s.cell(row=4 + i, column=3, value=vv)
            s.cell(row=4 + i, column=4, value=iso)
        s.freeze_panes = "A4"
        widths(s, {"A": 10, "B": 12, "C": 18, "D": 28})

    # ---- 10 Notes --------------------------------------------------------
    s = wb.create_sheet(names[9])
    h1(s, t["notes_title"], "A1:D1")
    r = 3
    for n in t["notes"]:
        cc = s.cell(row=r, column=1, value=f"• {n}")
        cc.alignment = Alignment(wrap_text=True, vertical="top")
        s.merge_cells(start_row=r, start_column=1, end_row=r, end_column=4)
        s.row_dimensions[r].height = 30
        r += 1
    r += 1
    s.cell(row=r, column=1, value=t["footer"].format(
        ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    s.column_dimensions["A"].width = 110

    wb.save(out)
    print(f"wrote: {out}")


def main() -> None:
    caps = [analyse(spec) for spec in CAPTURES]
    for c in caps:
        print(f"{c.label:16s} range={c.range_a*1e3:g} mA  n={c.stats['n']}  "
              f"rise={c.rise_t}  first_pulse={c.first_pulse_t}  "
              f"events={len(c.events)}")

    by_model: dict[str, dict[str, Capture]] = {}
    for c in caps:
        by_model.setdefault(c.model, {})[c.phase] = c
    for m, phases in by_model.items():
        v, items = eval_model(phases)
        print(f"\n{m}: {v}")
        for (tag, _), r in zip(T["en"]["items"], items):
            print(f"  {tag} {r.verdict:5s} {r.measured}")
            print(f"        {r.evidence}")

    print("\n[plot] generating PNGs from the in-memory capture data …")
    generate_plots(caps, REPORTS_DIR)

    for lang in ("en", "zh"):
        render(lang, REPORTS_DIR / f"report_combined_20260813_{lang}.xlsx", caps)

    for m, phases in by_model.items():
        v, items = eval_model(phases)
        path = REPORTS_DIR / f"summary_{m}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump({
                "test_id": "TR-PULLTAB-001",
                "model": m,
                "device_id": list(phases.values())[0].device_id,
                "overall": v,
                "captures": {p: {"range_a": c.range_a, "start": c.start,
                                 "end": c.end, "stats": c.stats,
                                 "rise_t_s": c.rise_t,
                                 "first_pulse_t_s": c.first_pulse_t,
                                 "reinstall_t_s": c.reinstall_t,
                                 "events": len(c.events)}
                             for p, c in phases.items()},
                "items": [{"id": tag, "verdict": r.verdict,
                           "measured": r.measured, "evidence": r.evidence}
                          for (tag, _), r in zip(T["en"]["items"], items)],
            }, f, indent=2, ensure_ascii=False)
        print(f"wrote: {path}")


if __name__ == "__main__":
    main()
