# EEMCP A4 优化报告（2026-06-18）

## 总评
- 完成 Batch 数: 6/6
- verify_preflight 结果: 8/8 GO
- pytest 结果: 8 passed
- envelope 7 键是否保持: ✓ (test/value/unit/lower/upper/pass/timestamp 顺序一致)

## Batch 1: 项目骨架
| 文件 | 操作 | 风险 | 状态 |
|---|---|---|---|
| pyproject.toml | 新建 | 无 | ✓ |
| requirements.txt | 新建 | 无 | ✓ |
| Makefile | 新建 | 无 | ✓ |
| .github/workflows/verify.yml | 新建 | 无 | ✓ |

要点:
- pyproject.toml 用 `setuptools.packages.find include=["mcp_*"]`，不引入新依赖
- requirements.txt 仅锁最低版本 (`fastmcp>=2.0 pyvisa>=1.13 joulescope>=1.1`)，不写 patch
- Makefile 提供 `verify` / `pytest` / `test` / `clean` / `help` 五个目标
- CI 用 `windows-latest` (项目 Windows-only)

## Batch 2: 文档同步
| 文件 | 操作 | 状态 |
|---|---|---|
| CLAUDE.md | 顶部加 "## Platform" 段；line 126 加 "Platform: Windows-only"；line 143 改 "17 → 14 debug_*.py"、改 "referenced in NOTES.md → documented in state_machine.py 注释（v6.2/v6.6/v6.8 标记处）和 SPEC_mcp_interaction.md §7" | ✓ |
| doubao_thread.md | 文件头加 WARNING 段（3 行）| ✓ |
| SPEC_mcp_interaction.md | "Status: DRAFT v1" 段后加 "**已实现部分**（2026-06-18）" 块（§3 / §5.3 / §7 三处实现引用）| ✓ |
| mcp_34465a/scripts/README.md | 47 行后加 ".stop 机制差异" 注 | ✓ |
| mcp_8852b/scripts/README.md | "All scripts follow the same conventions" 段加 ".stop 未实现" 提示 | ✓ |
| mcp_34465a/NOTES.md | 新建（7 节 gotcha 摘要，从 README §"Gotchas" 抽）| ✓ |
| mcp_js220/NOTES.md | 新建（5 节 gotcha 摘要）| ✓ |
| mcp_8852b/FIX_TIMELINE.md | 新建（3 行 v6.x 表格 + 详细注释）| ✓ |

## Batch 3: 轻量代码
| 文件 | 操作 | 状态 |
|---|---|---|
| verify_preflight.py:21 | sys.path 改 `Path(__file__).resolve().parent` 写法（去掉硬编码 `e:\Centrak\BLE Sticker\test\HWTEST`）| ✓ |
| verify_preflight.py 文档字符串 | "Phase 2 preflight verification" → "Preflight verification"（去掉 phase 编号）| ✓ |
| mcp_34465a/scripts/run_dmm_voltage_log.py | 加 `STOP_FILE` + `_StopFlag` + `signal.signal` 处理 + 循环内 `if stop_flag.stop: break` + KeyboardInterrupt catch（5 处，与 run_dmm_current_log.py 模式一致）| ✓ |

## Batch 4: manual test 改名
| 项 | 状态 |
|---|---|
| 移动 `mcp_8852b/manual test/` → `mcp_8852b/reference/_ble_msm_samples/` | ✓ |
| 改 `state_machine.py:207` docstring `mcp_8852b/manual test/Internal Log.txt` → `mcp_8852b/reference/_ble_msm_samples/Internal Log.txt` | ✓ |
| 改 `state_machine.py:275` docstring `the manual test's Internal Log` → `the reference/_ble_msm_samples/ Internal Log` | ✓ |
| 改 `debug_eut_init2.py:163` print `manual test/` → `reference/_ble_msm_samples/` | ✓ |
| 移 `Bluetooth low energy Measurement Software.msi` (2.4 MB) → `mcp_8852b/reference/_ble_msm_samples/` | ✓ |
| 删除旧的 `mcp_8852b/manual test/` 目录 | ✓ |

备注: `mcp_8852b/reference/_ble_msm_samples/parse_trace.py` 内部用了绝对路径 (`E:\Centrak\BLE Sticker\test\HWTEST\mcp_8852b\manual test\...`) — 按任务要求**不需改**。该脚本是 investigation 工具（输入 `Capture.txt` → 输出 `gpib_trace.py/json/tsv`），不是日常脚本；它的 `gpib_trace.json` / `gpib_trace.py` / `gpib_calls.tsv` 输出已存在于新目录。**只在有人重新运行 parse_trace.py 时才会暴露路径错**。

## Batch 5: pytest 收编
| 项 | 状态 |
|---|---|
| tests/ 目录骨架 (`__init__.py` + `conftest.py`) | ✓ |
| `test_preflight_dmm.py` (3 tests, 1-3) | ✓ |
| `test_preflight_js220.py` (3 tests, 4-6) | ✓ |
| `test_envelope_endtoend.py` (1 test, 7) | ✓ |
| `test_legacy_smoke.py` (1 test, 8) | ✓ |
| Makefile `pytest` 目标 | ✓ |
| pytest 8 passed | ✓ |

要点:
- 测试**断言逐字复制** verify_preflight.py 的内容（不加强、不改语义）
- conftest.py 用与 Batch 3.1 一致的 `Path(__file__).resolve().parent.parent` 写法
- verify_preflight.py **保留** — CLAUDE.md 显式要求"改 server.py 后必跑"

## Batch 6: envelope 抽公共
| 项 | 状态 |
|---|---|
| `common/__init__.py` 新建 | ✓ |
| `common/envelope.py` 新建 (`ok` / `err` / `envelope` 三个 helper) | ✓ |
| `mcp_34465a/server.py` 改 import + 改 `_envelope`/`_ok`/`_err` 函数体 | ✓ |
| `mcp_js220/server.py` 改 import + 改 `_envelope`/`_ok`/`_err` 函数体 | ✓ |
| verify_preflight 8/8 仍通过 | ✓ |
| pytest 8 passed 仍通过 | ✓ |
| envelope 7 键保持（顺序：`test, value, unit, lower, upper, pass, timestamp`）| ✓ |

**关键技术细节**:
- 用**绝对 import** `from common.envelope import ...`（不用 `..common`），因为 `mcp_*` 是顶层包，没有父包
- 命名冲突处理：`from common.envelope import envelope as _common_envelope`（避开本地 `_envelope` 包装函数同名）
- 包内 `_envelope(test, value, unit, lower, upper)` 包装仍存在 — 保留 **DEFAULT_LIMITS fallback**（按 SCPI function 查默认 pass window），然后调用 `_common_envelope(...)` 构造 7 键 dict
- `_ok(d)` → `ok(**d)`；`_err(e)` → `err(str(e))` — 行为完全等价
- mcp_8852b **不动**（用 TxTestResult dataclass，形状不同）

## verify_preflight.py 输出（最后 5 行）
```
[PASS] 8. ԭ tool Կ import + callable (��ƻ�ð��)  6/6 legacy tools callable
========================================================================
Summary: 8/8 tests passed
========================================================================
GO
```

（中文乱码是 Windows bash console 编码问题；UTF-8 文件本身完好）

## pytest 输出（最后 5 行）
```
tests/test_preflight_dmm.py::test_1_dmm_propose PASSED                  [ 37%]
tests/test_preflight_dmm.py::test_2_dmm_hard_gate PASSED                [ 50%]
tests/test_preflight_dmm.py::test_3_dmm_bypass PASSED                   [ 62%]
tests/test_preflight_js220.py::test_4_js_propose PASSED                 [ 75%]
tests/test_preflight_js220.py::test_5_js_hard_gate PASSED               [ 87%]
tests/test_preflight_js220.py::test_6_js_bypass PASSED                  [100%]
============================== 8 passed in 1.90s ==============================
```

## 失败记录
- 无

## 给 A5 验收员的接口

### 已做修改清单（按"做了哪些事"）
- 新建 `pyproject.toml`, `requirements.txt`, `Makefile`, `.github/workflows/verify.yml`
- 新建 `tests/` 目录（含 5 个文件）
- 新建 `common/envelope.py` + `common/__init__.py`
- 新建 `mcp_34465a/NOTES.md`, `mcp_js220/NOTES.md`, `mcp_8852b/FIX_TIMELINE.md`
- 改 `CLAUDE.md` (顶部 + 126 行 + 143 行), `doubao_thread.md` (头部), `SPEC_mcp_interaction.md` (头部)
- 改 `mcp_34465a/scripts/README.md` (line 47 注), `mcp_8852b/scripts/README.md` (中部)
- 改 `verify_preflight.py` (sys.path + 文档字符串)
- 改 `mcp_8852b/state_machine.py` (line 207, 275 docstring)
- 改 `mcp_8852b/scripts/_debug_history/debug_eut_init2.py` (line 163 print)
- 改 `mcp_34465a/scripts/run_dmm_voltage_log.py` (加 .stop 机制, 5 处)
- 改 `mcp_34465a/server.py` 和 `mcp_js220/server.py` (envelope/_ok/_err 委托到 common.envelope)
- 删除 `mcp_8852b/manual test/` 目录
- 移动 `Bluetooth low energy Measurement Software.msi` → `mcp_8852b/reference/_ble_msm_samples/`

### 仍要独立跑的命令清单（A5 重验）
```bash
cd E:/other/ai_project/EEMCP
python verify_preflight.py
# 期望: Summary: 8/8 tests passed / GO

pytest tests/ -v
# 期望: 8 passed

python -c "import mcp_34465a, mcp_8852b, mcp_js220"
# 期望: 无输出

python -c "
from common.envelope import envelope
e = envelope('VOLT:DC', 3.3, 'V', 3.0, 3.6)
assert list(e.keys()) == ['test', 'value', 'unit', 'lower', 'upper', 'pass', 'timestamp']
print('OK')
"

ls mcp_8852b/reference/_ble_msm_samples/ | head
# 期望: 1.png, 2.png, 3.png, BLE Test Report.html, Bluetooth low energy Measurement Software.msi, ...

# 确认旧的 manual test/ 目录已删
ls mcp_8852b/manual* 2>&1
# 期望: No such file or directory

# 确认根目录无 .msi
ls *.msi 2>&1
# 期望: No such file or directory
```

### 仍不要动的红线（重申 A2 + A5 红线）
- 4 处 `DEFAULT_LIMITS` 的**值**（键位置可移，**值**和**键名**不动）
- `mcp_8852b/state_machine.py:343-456, 462-509, 544-574` v6.2/v6.6/v6.8 fix 块
- `mcp_8852b/instrument.py:220-231` `*IDN?` 5 次重试
- `set_eut_interface(skip_if_ready=True)` / `wait_test_complete(require_transition=True)` 默认
- `verify_preflight.py` 8 项测试**语义**（可重写为 pytest 包装，不能改测试含义）
- 三包 envelope 输出键顺序/名字
- `mcp_8852b/reference/*` 任何 PDF/txt（已移到 `_ble_msm_samples/` 子目录的 13 个 investigation 文件不动）
- `_msi_extract/` 内容
- `mcp_8852b/scripts/replay_capture.py`
- A3 已移动的 22 个文件位置（debug/log/trace/csv）
- 14 个 `debug_*.py` 文件**内容**

### A4 任务外的发现（提示给后续 agent）
- `verify_preflight.py:21` 之前的硬编码 `e:\Centrak\BLE Sticker\test\HWTEST` 路径在 conftest.py 里也复制了一次相同的 `Path(__file__).resolve().parent.parent` fix — 后续如加更多测试脚本，**用同一模式**。
- `mcp_34465a/scripts/run_dmm_voltage_log.py` 现在和 `run_dmm_current_log.py` 用相同的 `.stop` 模式，但代码**没有抽公共 helper**（按任务要求"不抽公共 helper"）。如未来要在多脚本复用，建议抽到 `mcp_34465a/scripts/_stop.py`。
- `mcp_8852b` 的 envelope 形状（带 `header/channel/capture_type/raw_fields/raw`）与 `_common_envelope` 不兼容 — 这是**有意**的，不要强行合并到 common。
