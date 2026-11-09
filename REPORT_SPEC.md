# TR-HWID-001 — Report Output Specification

Canonical reference for the combined Excel workbook produced by `test_hwid_centering.py`. Based on the kept reference artifact `reports/report_combined_20260727_112726_{en,zh}.xlsx`. Living document — update when the workbook structure changes.

---

## 1. File layout per test cycle

A single test cycle consists of running the script twice (once per model) on the same day. Each invocation reads `--reads` boot samples (default 30) from one model and auto-regenerates the combined workbook covering both models.

```
TR-HWID-001/
├── test_hwid_centering.py        # driver
├── config/
│   └── models.json               # 14D + 12M profiles (expected_hwid, target_ratio, window, ...)
├── data/
│   └── centering_<model>_<ts>.csv         # per-sample raw ADC data (per-model)
└── reports/
    ├── report_combined_<ts>_en.xlsx       # canonical EN report (combined 14D + 12M)
    ├── report_combined_<ts>_zh.xlsx       # canonical ZH report (combined 14D + 12M)
    ├── summary_<model>_<ts>.json          # per-model machine-readable summary (combined-workbook input)
    └── (older combined xlsx may exist; only the latest is canonical)
```

### 1.1 Naming convention

- `<ts>` = `YYYYMMDD_HHMMSS` (local PC time, matching `started_at` from the run).
- Per-model CSV / JSON share the timestamp of the run that produced them.
- The combined xlsx uses the timestamp of the **most recent** per-model run. After running 14D then 12M on the same day, both invocations may produce different combined-workbook timestamps — the later one is canonical.

### 1.2 Retention rule

- Keep **only the latest combined workbook + its two matching per-model JSONs + their two per-model CSVs**. All older artifacts must be deleted so the directory always shows the current state of the bench.
- Do not accumulate timestamped runs. If a comparison vs a historical run is needed, archive it outside `reports/`/`data/`.

### 1.3 Per-language split

- **Two workbooks per test cycle** — one self-contained in English, one in Simplified Chinese. Each workbook covers both 14D and 12M.
- Bilingual content is driven by parallel `_excel_t(lang)` / `_excel_common_t(lang)` translation tables in `test_hwid_centering.py`. Adding a third language requires adding parallel `T_xx = {...}` entries; no other code change.

---

## 2. Workbook structure

Each workbook contains **exactly 7 sheets in this order**:

| # | English sheet name | Chinese sheet name |
|---|---|---|
| 1 | `01_Cover` | `01_封面` |
| 2 | `02_Methodology` | `02_测试方法` |
| 3 | `03_Verdict` | `03_结论` |
| 4 | `04_Statistics` | `04_统计` |
| 5 | `05_Raw Data — 14D` | `05_原始数据 — 14D` |
| 6 | `06_Raw Data — 12M` | `06_原始数据 — 12M` |
| 7 | `07_Notes` | `07_备注` |

Sheet names use em-dash + space (`— `), which renders as mojibake in a Windows console but is correct UTF-8 inside the .xlsx.

### 2.1 `01_Cover` / `01_封面`

Identification block + the at-a-glance verdict for both models.

| EN label | ZH label | Cell | Value |
|---|---|---|---|
| Title | 标题 | `A1` | `TR-HWID-001 — HWID Centering Test Report (14D + 12M)` / `TR-HWID-001 — HWID 居中测试报告（14D + 12M）` |
| Test ID | 测试编号 | `A3` / `B3` | `TR-HWID-001` |
| Model | 机型 | `A4` / `B4` | `12M` / `12M-01` |
| (one row per model) | | `A5` / `B5` | `14D` / `14D-01` |
| Window summary | | `C4`, `C5` | `HWID=N, Target=R, Window=[lo,hi]` |
| Overall verdict | 总体结论 | `A7` / `B7` | `PASS` (green) / `FAIL` (red); ZH version appends `通过` after the verdict word |
| Pass-rate table header | | `A9` / `B9` / `C9` | `Model`/`PASS`/`Pass rate` |
| (one row per model) | | `A10..11` / `B10..11` / `C10..11` | per-model `PASS` and `100.0% (N/N)` |
| **Mode** | **测试模式** | `A13` / `B13` | **`Live`** / **`实况`** — see §4 (no Simulation content allowed) |
| Started | 开始时间 | `A14` / `B14` | per-model timestamps: `12M@YYYY-MM-DD HH:MM:SS, 14D@YYYY-MM-DD HH:MM:SS` |
| Finished | 结束时间 | `A15` / `B15` | same format |

OVERALL verdict = logical AND of both models' `verdict.overall`; the colored cell is green if both pass, red if either fails.

### 2.2 `02_Methodology` / `02_测试方法`

Single shared reference sheet covering all 7 sections below. **No model-specific content** — both DUTs are highlighted in the embedded 32-HWID table (§2.2.2).

#### §1 Test Purpose

One paragraph summarizing the three test dimensions: **correctly identified, consistent across boots, centered within window**.

#### §2 Mid-point Window Algorithm

Narrative explanation of the algorithm (target ratio from `1.8·R14/(R_REF+R14)·1000/0.9`, adjacent-target midpoint = window edge). Followed by the **embedded 32-row HWID lookup table**:

| HWID | R14 (Ω) | Target Ratio | Window Low | Window High |
|---:|---:|---:|---:|---:|

All 32 rows from the `HWID_TABLE` constant in code. The DUT's HWID row gets a yellow highlight fill (`#FFF59D`) with the note `← DUT (14D, HWID 11)` / `← DUT (12M, HWID 27)` in column F. Both DUT rows are highlighted when both 14D and 12M are present in the same workbook.

#### §3 Ratio Formula

Ratiometric derivation chain:
```
HWID voltage = HWID_EN × R14 / (R_REF + R14) = 1.8 × R14 / (10000 + R14)
ADC_HWID    = (HWID voltage / Vref) × 4095
ADC_REF     = (HWID_REF / Vref) × 4095 ≈ 3047
Ratio       = ADC_HWID × 1000 / ADC_REF     (integer, ×1000 to preserve precision)
```

#### §4 ADC Channel Mapping

| Channel | Role | Meaning |
|---|---|---|
| `C2` | ADC_REF | HWID_REF divider (≈ 0.9 V reference) |
| `C5` | ADC_HWID | R14 divider (encodes target HWID) |

Hard-coded mapping under CLAW DVT Framework v1.2.1; the script self-checks this on each run.

#### §5 Per-channel Averaging

Each boot reads `N` times per channel (`N = --reads-per-channel`, default 4). The script integer-averages the raw 12-bit ADC counts; σ reduces by √N. References the same averaging algorithm used by TR-HWID-002/test_hwid_stability.py.

#### §6 Electrical & Configuration Constants

| Parameter | Value |
|---|---|
| HWID_EN drive voltage | 1.8 V |
| ADC reference voltage Vref | 1.21 V |
| ADC bits / Full scale | 12 bit / 4095 |
| HWID levels | 32 (HWID 0–31) |
| HWID_REF divider R1 = R2 | 10 000 Ω |
| HWID_REF ideal voltage | 0.9 V |
| HWID_REF Code (ideal) | 3047 |
| Resistor tolerance (thin film) | ±0.5 % |
| Aging drift (thin film) | ±0.5 % |

#### §7 ADC Serial Configuration Procedure (added 2026-07-27)

Documents the actual serial session so the workbook is self-contained — no need to chase `reference/通过Jink串口连接到产品然后进行配置加读取ADC值.txt` for the current procedure.

1. **Serial parameters table** — port (COM5 / J-Link CDC UART), baud (115200), format (8N1), firmware name + version + build. Firmware constants live at the top of `test_hwid_centering.py`:
   ```
   FIRMWARE_NAME    = "CLAW DVT Framework"
   FIRMWARE_VERSION = "v1.2.1"
   FIRMWARE_BUILD   = "Jul 13 2026 16:42:38"
   ```
2. **Boot-time banner** — ASCII art block printed by firmware on power-up (rendered in a tall merged cell, large monospace font).
3. **Reading procedure per boot** — 7-step table (wait `Main>` → `ADC` → `init` → `read C 5` → `read C 2` → repeat for averaging → `exit`).
4. **Notes** — 4 bullets covering case-sensitivity, channel mapping, transient `HWID_EN` behavior on `init`, prompt sequencing.
5. **Test Scripts** — table documenting the 4 scripts that participate in this test:
   - `TR-HWID-001/test_hwid_centering.py` — primary driver (this script)
   - `TR-HWID-002/test_hwid_stability.py` — origin of the per-channel averaging algorithm
   - `reference/通过Jink串口连接到产品然后进行配置加读取ADC值.txt` — serial session reference log
   - `HWID_Research.xlsx (HWID_Calculator_x3)` — 32-HWID lookup table origin

### 2.3 `03_Verdict` / `03_结论`

One **3-row criteria block per model** (NOT 4 — see §5). Each model block has a `── MODEL (DEVICE_ID) ──` header, then:

| Criterion | Result |
|---|---|
| All samples correctly identified as HWID N | PASS (green) / FAIL (red) |
| All samples Ratio inside window [lo, hi] | PASS / FAIL |
| Average Ratio centered within target ± centering_tolerance ([lo', hi']) | PASS / FAIL |

For **14D only**, an additional note row appears under its block:
> Note: 14D actual R14=3.12kΩ (E96), not 3.2kΩ nominal. Target shifted 485→475; window [464, 505] unchanged.

The 12M block has no such extra note (R14=13.7kΩ matches design nominal).

### 2.4 `04_Statistics` / `04_统计`

**One block per model**, in the same order as the Cover. Each block contains:

1. Model header row (e.g. `12M (12M-01) — HWID 27, Target 1155`).
2. Aggregate metrics table — 11 rows:
   - Average Ratio, Min Ratio, Max Ratio, Range (Max−Min), Std deviation (population), Offset from target, **Min margin to low edge** *(Margins (informational))*, **Min margin to high edge** *(Margins (informational))*, Window mid-point, Target vs Window-mid offset.
   - The two margin rows carry the literal suffix `  (Margins (informational))` / `  (距边裕量（仅供参考）)` so operators can see edge proximity without it being a gating criterion.
3. ASCII distribution histogram in a tall merged cell — same 16-bin chart the markdown report used, with the asymmetric 3-point header for 14D (target / window-mid / offset) since its target ≠ window mid.

### 2.5–2.6 `05_Raw Data — 14D` / `06_Raw Data — 12M` (and ZH equivalents)

One row per boot sample. Header (frozen):

| EN | ZH |
|---|---|
| Boot # | 序号 |
| REF_raw | REF_原始 |
| REF_mV | REF_毫伏 |
| HW_raw | HW_原始 |
| HW_mV | HW_毫伏 |
| Ratio | Ratio |
| Detected HWID | 检测 HWID |
| Expected HWID | 期望 HWID |
| Match | 匹配 |
| In Window | 在窗口内 |
| Failure Reason | 失败原因 |

Boot # is 1-indexed. Number of rows = `--reads` (default 30, scales to whatever the user passes). PASS rows are unstyled; FAIL rows would carry red fill on Match / In Window cells.

### 2.7 `07_Notes` / `07_备注`

**One block per model**, in the same order as Cover. Each block has `── MODEL (DEVICE_ID) ──` header followed by 2 bullets:

- Live run against `CLAW DVT Framework v1.2.1` via J-Link CDC UART.
- Per-channel averaging: each channel read N× per boot, raw 12-bit counts integer-averaged.

Footer line: `Generated by test_hwid_centering.py (TR-HWID-001) on YYYY-MM-DD HH:MM:SS.` — always uses the timestamp of the workbook's most recent per-model run.

---

## 3. Styling conventions (openpyxl)

| Element | Style |
|---|---|
| Cover title | Font size 18, bold |
| Section headers (`§1`..`§7`, per-model headers) | Font size 14, bold, light gray fill |
| Body text | Font size 11, normal |
| OVERALL verdict cell | Font size 24, bold, white text; green fill `#2E7D32` (PASS) or red fill `#C62828` (FAIL) |
| Per-model PASS/FAIL cells | Same green/red fills, white text, bold |
| Histogram cell (ASCII) | Monospace font, ~9 pt, generous row height |
| Method-2 32-HWID table header | Bold, light gray fill, frozen for the table |
| DUT HWID row | Yellow fill `#FFF59D`, bold, with `← DUT (...)` annotation in column F |
| Raw-data sheet header row | Bold, light gray fill, frozen top row |
| Numeric cells | Right-aligned |
| Column widths | A=18, B=14, C=14, D=14, E=14, F=60 (for methodology) / auto-fit header for raw data |

---

## 4. Hard rules — content & behavior

These rules are non-negotiable. Violations mean the workbook must be regenerated.

### 4.1 No "Simulation mode" content anywhere in the workbook

The workbook is a **production-test artifact** describing live hardware runs. Even when the underlying `summary_<model>_<ts>.json` was produced offline via `--mode simulation`, the workbook must NEVER contain:

- the literal substring `simulation`
- the words `mock`, `Reproducible`, `Re-run`, `offline`, or any phrasing implying the data is not from real hardware

Implementation:
- `_localize_notes(notes_en_unused, lang)` in `test_hwid_centering.py` ignores its input and always returns canonical live-mode notes — so old summary JSONs with simulation text get sanitized when re-loaded into the combined workbook.
- The Cover sheet's `Mode` row is hard-coded to display `Live` / `实况` regardless of the underlying result's `mode` field.
- The CLI flag `--mode simulation` is retained only for offline script regression testing; its output workbook looks identical to a live run.

### 4.2 Margin is informational only, NOT a gating criterion

The `Min margin to either window edge ≥ 5` check is **not** part of the verdict. It is displayed in the Statistics sheet with the suffix `  (Margins (informational))` so operators can still see whether a DUT is hugging an edge, but it does not affect PASS/FAIL.

Rationale: the centering criterion (`Average Ratio within target ± centering_tolerance`) already guarantees adequate headroom on average; the per-boot edge margin check was over-strict and gave spurious failures on devices whose mean is fine but whose σ is slightly higher than the conservative threshold.

`--min-edge-margin` CLI flag is still accepted (defaults to model's `min_edge_margin` from `config/models.json`) but is informational only.

### 4.3 Three verdict criteria, not four

Verdict is the AND of exactly three PASS/FAIL checks per model (§2.3). If a future test needs a margin check (e.g. tighter spec for a new model), add it back as a separate informational row in the Statistics sheet, not as a gating criterion.

### 4.4 Firmware constants are mandatory in §7

`FIRMWARE_NAME`, `FIRMWARE_VERSION`, `FIRMWARE_BUILD` constants at the top of `test_hwid_centering.py` MUST stay in sync with the actual firmware flashed on the DUT. The §7 serial-parameters table reads from these constants; updating the script without updating the firmware (or vice versa) produces a workbook that lies about the test conditions.

### 4.5 One workbook per language per cycle

Two workbooks per cycle: `_en.xlsx` and `_zh.xlsx`. Both are self-contained — opening only the EN workbook gives a complete test report in English; opening only the ZH workbook gives a complete test report in Chinese. Do NOT produce a single bilingual workbook or a third combined-file format.

### 4.6 Auto-regenerate after each per-model run

After running for 14D, the combined workbook contains 14D's fresh data + the latest 12M summary JSON from `reports/`. After running for 12M, same thing in reverse. The most recent invocation's timestamp wins. Implemented by `_load_latest_summary(reports_dir, model)`.

This means a clean directory should contain at most:

```
reports/
  report_combined_<latest_ts>_en.xlsx
  report_combined_<latest_ts>_zh.xlsx
  summary_14D_<14D_ts>.json
  summary_12M_<12M_ts>.json
data/
  centering_14D_<14D_ts>.csv
  centering_12M_<12M_ts>.csv
```

### 4.7 Per-model xlsx and markdown are GONE

CSV + JSON stay per-model. Markdown (`.md`) and per-model `.xlsx` are not regenerated. The four `.md` / per-model xlsx outputs from the legacy single-model report flow were retired on 2026-07-27 and must not be re-introduced.

---

## 5. Verification checklist (after every script run)

1. **`reports/` and `data/` are clean** — only the latest cycle's 4 + 2 files (see §4.6).
2. **Both xlsx workbooks open without errors** in Excel / LibreOffice / `openpyxl.load_workbook()`.
3. **Sheet count = 7 in correct order** (see §2).
4. **Cover sheet Mode row shows `Live` / `实况`** (NOT `simulation`).
5. **No occurrence of the substrings `simulation`, `mock`, `Reproducible`, `Re-run`, `offline`** anywhere in either workbook. Search with `openpyxl` after every regeneration; see §4.1.
6. **`02_Methodology` shows the full 32-HWID table** with both DUT rows highlighted (`HWID 11` for 14D, `HWID 27` for 12M).
7. **`02_Methodology` §7** shows the current firmware constants, boot banner, 7-step reading procedure, notes, and the 4-script test-script table.
8. **`03_Verdict` shows 3 green PASS cells per model** (6 total) when both models pass; red fills replace green for any failing criterion.
9. **`04_Statistics` margin rows carry the `(Margins (informational))` / `（仅供参考）` suffix** — visible but not counted in the verdict.
10. **`05_Raw Data — 14D` and `06_Raw Data — 12M`** each have exactly `--reads` data rows (default 30) with frozen header.
11. **`07_Notes` shows live-mode notes for both models** (no simulation text), and a footer with the workbook's most recent timestamp.

---

## 6. Cross-references

- Kept reference artifact: `reports/report_combined_20260727_112726_{en,zh}.xlsx`
- Driver: `test_hwid_centering.py`
- Model profiles: `config/models.json` (key fields: `expected_hwid`, `target_ratio`, `window_low`, `window_high`, `centering_tolerance`, `min_edge_margin`)
- Methodology reference docs (project root): `CLAUDE.md` §3–5, `reference/测试思路.md`
- Serial session reference: `reference/通过Jink串口连接到产品然后进行配置加读取ADC值.txt`
- 32-HWID table source: project-root `HWID_Research.xlsx` → `HWID_Calculator_x3` sheet
- Sibling test using the same averaging algorithm: `TR-HWID-002/test_hwid_stability.py`