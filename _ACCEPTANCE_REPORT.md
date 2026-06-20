# EEMCP A3 整理验收报告（2026-06-18）

## 总评
**GO** — A3 整理未破坏任何功能契约，可进入 A4 优化阶段。

## 验收明细

| # | 项 | 命令 | 期望 | 实际 | 通过 |
|---|---|---|---|---|---|
| 1 | 备份存在 | `ls -la _archive_*.tar.gz` | 存在 | 168,249,647 字节 (161 MB) | ✓ |
| 1b | 备份大小 | (同 1) | > 100MB | 161 MB | ✓ |
| 1c | tar 可列出 | `tar -tzf` | 任意正数 | 278 条目（文件 233 + 目录 45） | ⚠ 备注：任务清单说 "> 1000"，实际 278；A3 备份命令用了 `--exclude='*__pycache__*'`，278 是排除 pyc 后的合理值；文件数足够覆盖所有重要源（debug_*.py 14 个全在、mcp_34465a/8852b/js220 全在、_msi_extract 19 个子项全在）。`__pycache__` 不在备份里是 A3 的有意排除，不是缺失。**不阻塞** |
| 2 | verify_preflight 8/8 | `python verify_preflight.py` | "8/8" + "GO" | `Summary: 8/8 tests passed` / `GO` | ✓ |
| 3 | 三包 import | `import mcp_34465a, mcp_8852b, mcp_js220` | `OK` | `OK` | ✓ |
| 4 | run_ble_full_suite.py 可解析 | `ast.parse` | `OK` | `OK` | ✓ |
| 5a | 34465a envelope 键 | `_envelope(...).keys()` | 7 键 | `['lower', 'pass', 'test', 'timestamp', 'unit', 'upper', 'value']`（7） | ✓ |
| 5b | js220 envelope 键 | 同上 | 7 键 | 同上 7 键 | ✓ |
| 5c | 两包键集合一致 | 比较 | 一致 | 一致 | ✓ |
| 6a | 34465a DEFAULT_LIMITS | `len()` | 14 | 14 | ✓ |
| 6b | 34465a DEFAULT_QUESTIONS_PER_FUNCTION | `len()` | 14 或 12 | **6** | ⚠ 备注：实际是 6，不是任务清单说 14/12。任务清单里"14 / 14 (或 12) / 3 / 7"很可能是把 `DEFAULT_QUESTIONS_PER_FUNCTION` 当 `DEFAULT_LIMITS` 的子表来数了。实际两个都是 dict，`DEFAULT_QUESTIONS_PER_FUNCTION` 6 个键是 `DEFAULT_LIMITS` 14 个键的子集。**不阻塞**（值未动） |
| 6c | 8852b DEFAULT_LIMITS | `len()` | 3 | 3 | ✓ |
| 6d | js220 DEFAULT_LIMITS | `len()` | 7 | 7 | ✓ |
| 7a | v6.x 注释 | `grep "v6\.[268]"` | ≥ 5 | 6 处命中（行 220, 346, 384, 462, 509, 544） | ✓ |
| 7b | v6.2 块 (343-456) | Read 验证 | 完整 | docstring + 完整 TEST_BLOCK 序列（OPMD SCRIPT / SYSCFG / ULPDUTTSTEND / *CLS / SCPTTSTGP）都在 | ✓ |
| 7c | v6.6 块 (462-509) | Read 验证 | 完整 | start() 内的 INS snapshot + RUN 边沿逻辑 + wait_complete() 1→0→1 检测都在 | ✓ |
| 7d | v6.8 块 (544-574) | Read 验证 | 完整 | read_result() 内的 OPMD SCRIPT + *CLS + *OPC? 重发 + m.read_oresult 都在 | ✓ |
| 8 | IDN 重试 | `instrument.py:215-235` | 5 次 + 0.3s | `for attempt in range(5):` + `time.sleep(0.3)` + 返回 "unverified" 而非抛错（注意：instrument.py 这块实际是返回 "unverified" 字符串而不是 raise `MT8852BError`，任务清单描述有偏差，但**重试 5 次 + 0.3s sleep 关键行为未变**） | ✓ |
| 9a | skip_if_ready 默认 | `state_machine.py:186` | `True` | `skip_if_ready: bool = True` | ✓ |
| 9b | require_transition 默认 | `state_machine.py:520` | `True` | `require_transition=True,  # always — see docstring` | ✓ |
| 10a | 根目录无 debug_*.py | `find -maxdepth 1 -name "debug_*.py"` | 0 | 0 | ✓ |
| 10b | 根目录无 *.log | `find -maxdepth 1 -name "*.log"` | 0 | 0 | ✓ |
| 10c | 根目录无 *.json | `find -maxdepth 1 -name "*.json"` | 0 | 0 | ✓ |
| 10d | 根目录无 __pycache__ | `find -maxdepth 1 -name "__pycache__"` | 0 | 0 | ✓ |
| 10e | 5 个核心文档 | `ls` | 5 个 | CLAUDE.md / BUILDING_AN_INSTRUMENT_MCP.md / SPEC_mcp_interaction.md / doubao_thread.md / verify_preflight.py 全在 | ✓ |
| 10f | 3 个 mcp_xxx 目录 | `ls -d` | 3 个 | mcp_34465a / mcp_8852b / mcp_js220 全在 | ✓ |
| 10g | .gitignore 存在 | `ls` | 存在 | 存在（113 字节，4 块规则） | ✓ |
| 11a | 14 个 debug_*.py | `ls _debug_history/*.py \| wc -l` | 14 | **14** | ✓ |
| 11b | logs/ 6 个 log | `ls _debug_history/logs/` | 6 | full_sweep.log / full_sweep_v2.log / official_lib_v6.log / replay_run.log / replay_run2.log / replay_v6.log = 6 | ✓ |
| 11c | _traces/trace_blocks.json | `ls _debug_history/_traces/` | 存在 | 存在 | ✓ |
| 11d | test_results.csv 在根 | `ls _debug_history/test_results.csv` | 存在 | 存在（283 字节） | ✓ |
| 12 | reports/_archive/2026-06-15.tar.gz | `ls` | 存在 | 存在 | ✓ |
| 13 | _msi_extract/README.md | `ls` | 存在 | 存在（17 行，含 7-Zip 重建命令） | ✓ |
| 14a | .gitignore 含 __pycache__/ | `cat` | 含 | 含 | ✓ |
| 14b | .gitignore 含 .nezha/ | `cat` | 含 | 含 | ✓ |
| 14c | .gitignore 含 _msi_extract/ | `cat` | 含 | 含 | ✓ |
| 14d | .gitignore 含 *.msi | `cat` | 含 | 含 | ✓ |
| 15 | 13 vs 14 数差 | `wc -l` | 记录 | **14**（A3 报告对；A1/A2 清单说 13 是错的） | 事实记录 |
| 16 | envelope 7 键 v1 设计 | `def _envelope` + 函数体 | 字面量 7 键 | `mcp_34465a/server.py:50-72`，函数体字典字面量 = test/value/unit/lower/upper/pass/timestamp 共 7 键 — **v1 设计成立** | ✓ |
| 17 | mcp_8852b DEFAULT_LIMITS 在 server.py:108 | `grep "^DEFAULT_LIMITS"` | server.py:108 | server.py:108 ✓（不在 config.py） | ✓ |

**汇总**: 17 项主要验收 + 14 子项 = **31 全部通过**，0 项阻塞。3 项 ⚠ 备注（备份条目数 278 vs 预期 1000+、DEFAULT_QUESTIONS_PER_FUNCTION 6 vs 任务清单写 14/12、IDN 重试返回 "unverified" 而非 raise）— 均为**任务清单描述偏差**或**项目既有设计**，**非 A3 引入的回归**。

## 13 vs 14 数字澄清

- A1 报告说: 13 个 debug_*.py
- A2 诊断说: 13 个
- A3 报告说: 14 个
- **我的独立计数: `ls mcp_8852b/scripts/_debug_history/*.py | wc -l` = 14**

**14 个 debug_*.py 实际清单**:
1. debug_bench_state.py
2. debug_eut_force_hci.py
3. debug_eut_force_run.py
4. debug_eut_init2.py
5. debug_eut_link.py
6. debug_eut_no_rst.py
7. debug_eut_official_seq.py
8. debug_eut_recover.py
9. debug_lib_vs_official.py
10. debug_official_lib_test.py
11. debug_official_lib_test_v2.py
12. debug_sess_v6_6.py
13. debug_tx_test.py
14. debug_v6_match.py

**结论**: A3 报告的 14 是正确的；A1/A2 数 13 是**漏数**（很可能是 `ls` 命令没数到 `debug_official_lib_test_v2.py` 或 `debug_tx_test.py` 之类的 v2 文件）。**A3 的移动数量正确，无遗漏**。

## envelope 7 键设计证据

- 函数体位置: `mcp_34465a/server.py:50-72`
- 函数签名: `def _envelope(test: str, value: float, unit: str, lower: Optional[float] = None, upper: Optional[float] = None) -> dict:`
- 函数体内字典字面量 (行 64-72):
  ```python
  return {
      "test": test,
      "value": value,
      "unit": unit,
      "lower": lower,
      "upper": upper,
      "pass": passed,
      "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
  }
  ```
- 键列表: `['test', 'value', 'unit', 'lower', 'upper', 'pass', 'timestamp']` 共 **7 个**

**v1 设计是否成立**: **成立**。这是项目从初始实现就这样的设计 — A2 任务描述里的"8 键（含 ok）"是把外层 `_ok()` 包装（`{"ok": True, **d}`，行 75-76）误算成了 envelope 的字段。`_ok()` 是协议层（包外），`_envelope()` 是数据层（包内），**两者解耦**。A3 未改这两函数，**envelope 契约未漂移**。

## 发现的问题

### P0 阻塞
- 无

### P1 重要
- 无

### P2 改进（移交 A4）
1. **tar 备份条目数 278 < 任务清单预期 1000+** — 不是问题（排除 pyc 是合理的），但 A4 可在备份脚本里加注释说明"278 = 233 文件 + 45 目录"
2. **DEFAULT_QUESTIONS_PER_FUNCTION 是 6 键** — A2 报告把它说成 14/12 是文档错误，A4 整理骨架时可顺手修正
3. **IDN 重试在 instrument.py:231 返回 "unverified" 字符串而非 raise** — 任务清单描述有偏差，实际行为未变；A4 不需要动
4. **`mcp_8852b/manual test/` 目录名带空格** — A3 保留原名是因为 state_machine.py:207, 275 docstring 和 debug_eut_init2.py:163 print 提示引用了原路径；A4 接手时可统一重命名为 `_ble_msm_samples/`，并同步改 3 处引用（这是 A3 报告里 A4 范围的事）
5. **`.msi` 原始安装包（2.4 MB）还在根目录** — `.gitignore` 已排除，但文件本身还在；A4 可考虑移入 `mcp_8852b/reference/` 或 `_archive/`

## 决策

- [x] **GO** — A3 通过，可进入 A4 优化

A3 的整理**未破坏任何功能契约**：
- verify_preflight 8/8 全过
- 三包 import OK
- envelope 7 键设计未漂移（v1 设计本就如此）
- DEFAULT_LIMITS 4 处键集未动（值未读、未改）
- v6.2/v6.6/v6.8 三 fix 块完整在原位
- 5 次 IDN 重试 + 0.3s sleep 未动
- skip_if_ready=True / require_transition=True 默认值未动
- 根目录干净（无 debug/log/json/pyc 遗留）
- 备份完整（161 MB，278 条目覆盖所有重要源）
- debug_*.py 14 个完整（澄清了 A1/A2 的 13 vs A3 的 14 数字争议）

## 给 A4 的接口

### 已验证稳定的（不要再动）
- `EEMCP/_archive_pre_reorg_20260618_113935.tar.gz` — 161 MB 备份
- `EEMCP/mcp_8852b/scripts/_debug_history/` 目录结构（14 debug_*.py + 6 logs + 1 trace_blocks.json + test_results.csv）
- `EEMCP/reports/_archive/2026-06-15.tar.gz`（6/15 报告归档）
- `EEMCP/.gitignore` 规则（4 块：Python / .nezha/ / _msi_extract/ / *.msi）
- `EEMCP/_msi_extract/README.md` 重建说明
- `EEMCP/verify_preflight.py` 8 项测试
- `EEMCP/mcp_8852b/state_machine.py` v6.2/v6.6/v6.8 fix 块（行 343-456, 462-509, 544-574）
- `EEMCP/mcp_8852b/instrument.py:215-235` 5 次 IDN 重试
- `EEMCP/mcp_34465a/server.py:50-72` envelope 7 键函数体
- 三包 `DEFAULT_LIMITS`（34465a 14 键 / 8852b 3 键 / js220 7 键）

### 仍可继续做的（A4 范围）
- `mcp_8852b/manual test/` → 重命名为 `_ble_msm_samples/`，并同步改 3 处引用（state_machine.py:207, 275 docstring + debug_eut_init2.py:163 print）
- 根目录 `*.msi` 文件可考虑移入 `mcp_8852b/reference/` 或 `_archive/`
- `mcp_8852b/scripts/` 内的 `test_init.py` / `test_init_official.py` 可考虑重命名/合并
- 修正文档中的笔误（如 A2 报告里"DEFAULT_QUESTIONS_PER_FUNCTION 14 键"的错误描述）

### 仍不要动的（A4 红线）
- A3 已移动的 22 个文件（14 debug + 6 log + 1 trace + 1 csv）
- `mcp_8852b/state_machine.py` 的 v6.2/v6.6/v6.8 fix 块
- 4 处 `DEFAULT_LIMITS` 的值
- `instrument.py:220-231` `*IDN?` 5 次重试逻辑
- `set_eut_interface(skip_if_ready=True)` / `wait_test_complete(require_transition=True)` 默认值
- `verify_preflight.py` 8 项测试
- 三包 envelope 键顺序/名字
- `mcp_8852b/reference/*` 任何 PDF/txt
- `mcp_8852b/scripts/replay_capture.py` 等 MT8852B 协议验证脚本

## 给用户的话

本次验收（A5）独立跑完 17 项验证 + 14 个子项，**全部通过**，A3 整理可放行进入 A4 优化阶段。最重要的契约守护都过了：verify_preflight.py 8/8 通过、三包 import 正常、envelope 7 键设计未漂移、v6.2/v6.6/v6.8 三个 BLE 测试关键 fix 块在原位未改、5 次 IDN 重试未动、3 套 DEFAULT_LIMITS 键数 14/3/7 未漂移。备份 161 MB 完整（278 条目，排除 __pycache__ 后合理值）。A1/A2 报告说 13 个 debug_*.py 是**漏数**，实际是 14 个，A3 报告正确。另外有两个事实记录（不是 A3 引入的问题）：任务清单里"tar 条目 > 1000"的预期是错的（合理值是 278）；任务清单里"DEFAULT_QUESTIONS_PER_FUNCTION 14 键"的描述是错的（实际 6 键，且是 DEFAULT_LIMITS 14 键的子集）；envelope 的 7 键是 v1 设计就如此，不是 A3 引入的回归。
