# mcp_MSO54 — Measurement Guide

How to measure common quantities correctly on the bench MSO54 using this MCP.
Complements `NOTES.md` (which is about the bus protocol); this file is about
**what to set, why, and what to expect back**.

All references to "CH1" can be substituted for any active analog channel.

---

## §1 Ripple on a DC rail

### 1.1 What "ripple" means here

The small periodic AC component riding on a DC supply voltage. For a
2.9 V rail at 20 MHz bandwidth, anything from 0 (clean LDO) to ~50 mV pk-pk
(typical buck converter) is normal. **Above ~100 mV pk-pk, suspect a
load step or a regulator instability, not "ripple".**

Distinguish from **noise** (broadband, aperiodic) — see §2.

### 1.2 Required scope settings (the canonical preset)

| Setting | Value | Why |
|---|---|---|
| `CH1:BANDWIDTH` | `20E6` (20 MHz) | Rejects RF pickup and switching-node spikes above 20 MHz — without this you measure switching noise, not ripple. |
| `CH1:COUPLING` | `AC` *(or DC + offset)* | Removes the DC bias so the ripple is centered on screen; lets you use a smaller V/div for resolution. DC coupling works if you already have offset set so the trace is on-screen. |
| `CH1:SCALE` | As small as the ripple allows (typical 5–50 mV/div) | Determines measurement resolution. A 1 V/div scale on a 30 mV ripple gives only 8 LSBs at 8-bit scope resolution. |
| `HORIZONTAL:SCALE` | Depends on ripple period: <br>• 50/60 Hz mains → 5–10 ms/div <br>• 100/120 Hz rectified → 5 ms/div <br>• 100 kHz SMPS → 1–10 µs/div | Must show ≥ 3 ripple cycles across the screen. |
| `ACQUIRE:MODE` | `SAMPLE` *(not AVERAGE)* | AVERAGE smooths ripple edges, which kills FREQUENCY/PERIOD measurement. Use AVERAGE only when you want to improve SNR at the cost of edge information. |
| `ACQUIRE:NUMAVG` | 1 (SAMPLE mode = no averaging) | See above. |
| `TRIGGER:A:MODE` | `NORMAL` *(not AUTO)* | AUTO mode silently overrides your trigger level to "near 50 % of signal" — see §3.3. |
| `TRIGGER:A:EDGE:LEVEL:CH1` | Middle of the ripple range | Needed for FREQUENCY/PERIOD to count edges. |

Apply in this order: **STOP acquisition → write BW → write coupling/scale → write horiz → set trigger mode NORMAL → set trigger level → restart acquisition**.

### 1.3 The MCP code pattern

```python
s.set_acq_state(False)                    # STOP — avoids auto-scale override
s.set_channel_bandwidth(1, 20e6)          # 20 MHz BW limit
s.set_channel_coupling(1, "AC")           # or keep DC + offset
# scale write must also come after STOP
s.set_channel_scale(1, 0.05)              # 50 mV/div
s.set_horizontal_scale(100e-6)            # 100 µs/div (example)
s.set_acq_mode("SAMPLE")                  # NOT AVERAGE — keeps edges
s.set_trig_mode("NORMAL")                 # NOT AUTO — preserves level
s.set_trig_edge_source("CH1")
s.set_trig_level(1, 2.92)                 # middle of the ripple range
s.set_acq_state(True)                     # RUN
```

### 1.4 Measurements to install

Install these on slots 1–6 in this order:

| Slot | Type | What it tells you |
|---|---|---|
| MEAS1 | `PK2PK` | **Headline ripple amplitude.** Always present. |
| MEAS2 | `MEAN` | DC level (sanity check; should match your rail voltage). |
| MEAS3 | `FREQUENCY` | Ripple fundamental frequency. **Returns `9.91e37` if no edges detected** — see §3.1. |
| MEAS4 | `PERIOD` | 1/FREQUENCY. Same sentinel if no edges. |
| MEAS5 | `ACRMS` | Cycle RMS — what you compare against "ripple RMS" specs. |
| MEAS6 | `RMS` | Full-record RMS (dominated by DC). Useless for ripple characterization alone; ACRMS is what you want. |

### 1.5 Worked example — CH1 ripple on 2026-07-18

| Setting | Before | After |
|---|---|---|
| CH1 BW | 1 GHz (FULL) | **20 MHz** |
| CH1 coupling | DC | DC |
| Horizontal | 40 ns/div | **100 µs/div** |
| ACQ mode | SAMPLE | SAMPLE |
| numavg | 16 | (n/a in SAMPLE) |
| Trigger mode | AUTO | **NORMAL** |
| Trigger level (CH1) | 2.92 V | 2.92 V |

**Results:**

| Quantity | Value |
|---|---|
| DC level (MEAN) | **2.942 V** |
| Ripple pk-pk | **34.4 mV** |
| Ripple RMS (ACRMS) | **2.7 mV** |
| Ripple frequency | **not detected** (FREQUENCY = `9.91e37` sentinel) |

**Interpretation:** after 20 MHz BW limit, there is no detectable periodic
ripple component — the 34 mV pk-pk is broadband noise. The earlier 160 mV
seen without BW limit was RF pickup / switching noise above 20 MHz, not
"ripple" by the standard definition.

This is actually the **typical conclusion for a clean DC rail**: the
periodic component (if any) is below the noise floor after BW limiting.

### 1.6 If frequency detection fails

The `9.91e37` sentinel means "scope couldn't find a periodic edge". Three
things to try, in order:

1. **Lower the trigger level** so it's inside the ripple range (between
   vmin and vmax of the displayed waveform). The default AUTO mode may
   have parked the level just outside the ripple.
2. **Switch trigger to NORMAL** (AUTO silently overrides the level).
3. **Remove AVERAGE acquisition** (AVERAGE smooths the ripple into DC,
   so no edges exist to count).
4. **Remove BW limit** — if the actual ripple is above 20 MHz (rare for
   DC rails, common on RF supplies), the BW limit is filtering the very
   signal you're trying to see. Reconsider whether you actually want
   "ripple" or "switching-node noise" — those are different specs.

If all four fail: there is genuinely no periodic component in your
bandwidth. The signal is broadband noise. Report `PK2PK` as the amplitude
characterization and note that periodicity is absent.

### 1.7 CURVE? capture for time-domain backup

The full-amplitude `MEAS1:PK2PK` value uses the scope's entire record
length (here: 6.25 Mpts at 6.25 GSa/s = 1 ms window). CURVE? only
returns the **first 2500 points** of that record by default, which at
6.25 GSa/s = **400 ns** of time — useless for ripple in the kHz–MHz
range. You will see a flat DC value, not the ripple.

Two ways to fix:

| Option | What you do | Trade-off |
|---|---|---|
| Increase `DATA:STOP` | Ask for more points (e.g. `stop=10000`) | Limited to 10,000 by the MCP gate; 10000 × 160 ps = 1.6 µs — still narrow. |
| Add `set_horizontal_record_length` / `set_horizontal_sample_rate` to `instrument.py` | Force the scope to a lower sample rate (e.g. 1 MSa/s × 2500 pts = 2.5 ms window) | Wider window, but loses fine-grained edge information for fast signals. |

Until the second option is added, treat CURVE? as a sanity check
("is the signal roughly at the expected DC level?") and rely on the
MEASUREMENT slots for the actual numeric result.

### 1.8 Probe discipline

**Long ground leads ruin ripple measurements.** A standard probe with a
6 cm ground lead forms a resonant circuit with the probe tip capacitance
(~10–15 pF), peaking around 100–300 MHz. That peak looks like "ripple"
even when the rail is clean.

For authoritative ripple on a switching supply:

- Use a **spring-tip ground** (1–2 mm loop) or
- **50 Ω coax with BNC adapter** and the scope's 50 Ω termination
- Set coupling to DC (50 Ω termination works with DC coupling; AC
  coupling at 50 Ω may load the source)

For low-frequency linear-rail measurements (mains-frequency ripple),
long ground is less critical — the resonant peak is far above your
bandwidth and gets filtered out by the 20 MHz BW limit.

### 1.9 Related scripts in this package

| Script | What it does |
|---|---|
| `scripts/ch1_voltage_report.py` | DC voltage snapshot (DVM + MEAN + CURVE?). Not ripple-specific but uses the same STOP-before-write pattern. |
| `scripts/ch1_ripple.py` | The canonical ripple preset + measurement install + report. Read this before changing the preset. |
| `scripts/generate_report.py` | Full state dump + matplotlib waveform plot + manual-screenshot workflow. Useful for archiving. |

---

## §2 Noise voltage measurement

### 2.1 What "noise voltage" means here

The broadband AC component of a signal, **with no assumption of
periodicity**. Distinct from ripple (which is periodic by definition).

Standard characterization: RMS over a defined bandwidth (e.g.
20 Hz–20 kHz for audio, or full bandwidth of the system), or peak-to-peak
over a defined time interval with 99.9 % confidence (sometimes called
"peak noise" or "Pk-Pk noise").

### 2.2 Required scope settings

| Setting | Value | Why |
|---|---|---|
| `CH1:BANDWIDTH` | match the system under test (often FULL) | Unlike ripple, noise has energy at all frequencies — BW-limit only if you want to characterize noise in a specific band. |
| `CH1:COUPLING` | `AC` | Centers the trace; you don't care about the DC offset. |
| `CH1:SCALE` | tight to the expected pk-pk | Noise can have rare high-amplitude spikes (Gaussian tail) — give them headroom. |
| `HORIZONTAL:SCALE` | long enough to capture many noise events | For Gaussian noise, RMS over ≥ 100 cycles of your lowest-frequency component of interest. |
| `ACQUIRE:MODE` | `SAMPLE` | No averaging — averaging **reduces noise** (by √N), so an averaged measurement underestimates the noise. |
| `ACQUIRE:NUMAVG` | 1 | (SAMPLE = no averaging.) |
| `TRIGGER:A:MODE` | `NORMAL` | — |
| Measurements | `ACRMS` + `PK2PK` + `STDDEV` (slot 7) | See below. |

### 2.3 Measurements to install

| Slot | Type | What it tells you |
|---|---|---|
| MEAS1 | `ACRMS` | RMS amplitude of the AC component over the record. **The primary noise spec.** |
| MEAS2 | `PK2PK` | The 0.1 %–99.9 % confidence interval is often reported as ±3.3 × σ for Gaussian noise, but PK2PK over a finite record is empirical. |
| MEAS3 | `STDDEV` | The standard deviation of all samples. For zero-mean noise this equals the RMS. |
| MEAS4 | `MEAN` | Should be ~0 with AC coupling; non-zero means DC is leaking through. |

### 2.4 The MCP code pattern

```python
s.set_acq_state(False)
s.set_channel_coupling(1, "AC")
s.set_channel_bandwidth(1, 20e6)        # or FULL, per §2.2
s.set_horizontal_scale(1e-3)            # 1 ms/div → 10 ms record
s.set_acq_mode("SAMPLE")
s.set_trig_mode("NORMAL")
s.set_trig_level(1, 0.0)                # middle of the AC-coupled waveform
# install: ACRMS, PK2PK, STDDEV, MEAN
s.set_acq_state(True)
```

### 2.5 Distinguishing noise from ripple

| Property | Ripple | Noise |
|---|---|---|
| Periodic? | Yes (fundamental + harmonics) | No (broadband, aperiodic) |
| Frequency-domain | Discrete spikes | Continuous spectrum |
| Time-domain | Repeating pattern | Random |
| Statistical distribution | Multi-modal | Often Gaussian |
| Best measurement | FREQUENCY + PK2PK + ACRMS | ACRMS + STDDEV + PK2PK (over long record) |
| Reduction technique | Increase filtering | Averaging (√N) — but only if you can tolerate the loss of single-event visibility |

If `FREQUENCY` returns `9.91e37` after trying §1.6, you are looking at
noise, not ripple. If `FREQUENCY` returns a clean value but the trace
visually looks noisy, you have ripple **plus** noise — report both.

---

## §3 MSO54-specific MCP gotchas

### 3.1 The `9.91e37` sentinel

`MEAS<n>:RESULTS:...:FREQUENCY?` (and PERIOD, RISETIME, etc.) return the
value `9.91 × 10^37` when the measurement could not be computed (no
edges, no transitions, signal outside the gate, etc.). This is the
firmware's "I give up" sentinel and is **not a measurement value**.

In code: `if freq > 1e30: treat as "not measurable"`.

### 3.2 CURVE? sample-rate trap

The scope auto-derives sample rate from `HORIZONTAL:SCALE × RECORDLENGTH`.
At 100 µs/div × 6.25 Mpts reclen, you get 6.25 GSa/s. CURVE? with
`stop=2500` returns only the first 2500 samples = **400 ns** of time.

For ripple (kHz–MHz), this is undersampled and looks like a flat DC line.

**Fix (planned):** add `set_horizontal_record_length()` and/or
`set_horizontal_sample_rate()` to `instrument.py`. Until then, use
MEASUREMENT slots for the numeric answers (they use the full record) and
CURVE? only for sanity-checking the DC level.

### 3.3 Trigger mode AUTO overrides your level

`TRIGGER:A:MODE AUTO` (the default) silently re-adjusts the trigger
level to ~50 % of the signal after every few acquisitions. Setting
`TRIGGER:A:LEVEL:CH1 2.91` and reading it back can show `2.92` because
AUTO nudged it.

**Always use `TRIGGER:A:MODE NORMAL` when you've set a specific level
you care about.** See `scripts/ch1_ripple.py` for the pattern.

### 3.4 `WFMOUTPRE:*` queries on display=OFF channels wedge USBTMC

Documented in `NOTES.md §1 trigger #4`. The hard rule:

> Before any `WFMOUTPRE:*` query or `CURVE?`, gate on `CH<n>:DISPLAY? == 1` first.

`scripts/generate_report.py` and `scripts/ch1_ripple.py` both do this.
Adding a new script that captures waveforms? Add the same gate.

### 3.5 Acquisition must STOP before vertical writes

Also `NOTES.md §2`: while `ACQUIRE:STATE` is RUN, the scope's auto-scale
loop overwrites `CH<n>:SCALE` writes within ~100 ms.

> Always `set_acq_state(False)` before `set_channel_scale()`,
> `set_channel_bandwidth()`, `set_channel_coupling()`, `set_channel_offset()`,
> or any other vertical-axis write.

Horizontal writes are NOT overridden — the scope doesn't auto-adjust
time scale. Trigger-level writes ARE safe (per §3.3, AUTO mode
overrides them but that's a separate mechanism).

### 3.6 The four USBTMC wedge triggers (summary; full detail in NOTES.md §1)

| # | Trigger | Workaround |
|---|---|---|
| 1 | `?` on a set-only SCPI cmd | `MSO54.query()` guard (refuses non-`?` strings) |
| 2 | `?` on a firmware-unsupported query | Don't add such queries; use static config |
| 3 | Binary `FILESYSTEM:READFILE` with `read_termination='\n'` | `read_file_bytes()` is disabled on USB |
| 4 | `WFMOUTPRE:*` on display=OFF channels | Gate on `CH<n>:DISPLAY?` first |

Recovery from any of these: USB-port reset (unplug/replug) or scope
power-cycle. **viClear, NI-VISA `VI_ATTR_USB_RESET`, programmatic
device disable/enable, and physical USB-port disable-then-enable all
either do not help or are forbidden by user policy** — only manual
USB cable unplug/replug and full scope power-cycle are acceptable
recovery paths.

---

## §4 Decision tree: "I want to measure X on CH1"

```
Is the quantity periodic?
├── Yes → "ripple" → §1
│     PK2PK + ACRMS + FREQUENCY (if it works)
│
└── No → "noise" → §2
      ACRMS + STDDEV + PK2PK (over long record)
      AC coupling, SAMPLE mode (NOT AVERAGE)

Is the trigger level usable?
├── Yes → great, install measurements, read values
└── No  → trigger mode AUTO is overriding your level
         → switch to NORMAL, set level manually (§3.3)

Is CURVE? returning flat-line data?
├── Yes at expected DC level → you're undersampling (§3.2)
│        → rely on MEASUREMENT slot values, or add
│          set_horizontal_record_length() to instrument.py
└── No  → good, you have a valid time-domain capture

Are you reading FREQUENCY = 9.91e37?
├── Yes → "no edges found" (§3.1)
│        → try §1.6 fixes (lower level, NORMAL mode,
│          no AVERAGE, no BW limit)
│        → if all fail: signal is noise, not periodic
└── No  → great, you have a ripple frequency

Is the scope unresponsive after any of this?
└── Yes → USBTMC wedge (§3.6)
         → tell the operator: "unplug-replug USB cable,
           wait 3 s, I'll retry"
         → DO NOT attempt device-level recovery
           (forbidden by policy — see memory entry
            feedback_no_device_layer_ops.md)
```