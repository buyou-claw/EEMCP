# EEMCP 文件整理报告（2026-06-18）

A3 执行：`目录与文件重整`

## 备份

- 备份文件: `E:/other/ai_project/EEMCP/_archive_pre_reorg_20260618_113935.tar.gz`
- 备份大小: 168,249,647 字节（161 MB）— tar.gz 全量（排除 `__pycache__/`）
- 备份命令: `tar --exclude='EEMCP/__pycache__' --exclude='EEMCP/**/__pycache__' -czf _archive_pre_reorg_<TS>.tar.gz EEMCP/`
- 注意：第一次 `tar` 报 "file changed as we read it" 但产物完整；之后 import 触发的 `__pycache__` 重新生成属于正常行为，不影响备份。

## 移动清单

| # | 源 | 目标 | 数量 | 风险 | 状态 |
|---|---|---|---|---|---|
| 1 | `EEMCP/debug_*.py` | `EEMCP/mcp_8852b/scripts/_debug_history/` | 14 文件（A2 清单说 13，实际 14） | 低 | ✓ |
| 2 | `EEMCP/*.log` | `EEMCP/mcp_8852b/scripts/_debug_history/logs/` | 6 文件 | 低 | ✓ |
| 3 | `EEMCP/trace_blocks.json` | `EEMCP/mcp_8852b/scripts/_debug_history/_traces/` | 1 文件 | 低 | ✓ |
| 4 | `EEMCP/test_results.csv` | `EEMCP/mcp_8852b/scripts/_debug_history/` | 1 文件 | 低 | ✓ |
| 5 | `EEMCP/**/__pycache__/` × 7 | (deleted) | 7 目录 | 低 | ✓ |
| 6 | `EEMCP/reports/{6/15 子目录}` × 11 | `EEMCP/reports/_archive/2026-06-15.tar.gz` | 11 目录 → 1 压缩包（1.3 MB） | 低 | ✓ |
| 7 | (created) | `EEMCP/.gitignore` | 1 文件 | 低 | ✓ |
| 8 | (created) | `EEMCP/_msi_extract/README.md` | 1 文件 | 低 | ✓ |
| 9 | (skip) `mcp_8852b/manual test/` | (原位) | — | 中 | — 保留原名，理由见下 |

## 引用扫描结果

| 模式 | 命中文件 | 处置 |
|---|---|---|
| `mcp_8852b/manual test`（in .py） | `state_machine.py:207, 275`（docstring）+ `debug_eut_init2.py:163`（print） + `manual test/parse_trace.py` 内部绝对路径 | **不改名** — 见下 |
| `_msi_extract`（in .py） | 0 处 | 保留目录 + gitignore + README |
| `test_results.csv` / `trace_blocks.json`（in .py） | 0 处 | 安全移动 |
| `debug_`（in .py） | 仅在被移走的 `debug_*.py` 内部互引（已是相对路径或 sys.path） | 安全移动 |
| `nezha`（in .py） | 0 处 | gitignore 排除 |

### 为什么保留 `mcp_8852b/manual test/` 原名

- `state_machine.py` 的 docstring 里有 `mcp_8852b/manual test/Internal Log.txt` 路径引用
- `debug_eut_init2.py` 的 print 提示里写 `'manual test/'`
- `manual test/parse_trace.py` 用的是**绝对路径** `E:\Centrak\BLE Sticker\test\HWTEST\...`，与项目内相对位置无关 — 即使重命名也不影响运行
- 但 A2 清单已标"中等风险"，且 `state_machine.py` 是**绝对不动**文件（v6.2/v6.6/v6.8 fix 块在 343-456, 462-509, 544-574 行）— 修改其 docstring 需谨慎
- 决策：保留原名 = 零风险，A4 如需重命名为 `_ble_msm_samples/`，可顺带同步改 `state_machine.py:207` 和 `debug_eut_init2.py:163` 两处注释（这是 A4 的范围，不是 A3）

## 验证结果

- [✓] 三包 import OK — `import mcp_34465a; import mcp_8852b; import mcp_js220` 成功
- [✓] `run_ble_full_suite.py` AST 解析 OK（用 `encoding='utf-8'` 显式编码；默认 GBK 解码失败是 Windows 环境问题，与本次整理无关）
- [✓] `verify_preflight.py` 8/8 通过 — `Summary: 8/8 tests passed` / `GO`
- [⚠] envelope 键 = **7 个**（`['lower', 'pass', 'test', 'timestamp', 'unit', 'upper', 'value']`），不是 A2 任务描述的 8 个（任务描述期望含 `ok` 键）— 但实际 `_envelope` 函数体确认就是 7 键，**这是项目本身的实现**，不是 A3 引入的问题。`_envelope` 的 7 键设计从 v1 一直未变
- [✓] 根目录 log = 0（`_debug_history/logs/` 之外）；pyc = 0（验证时 import 触发的 `__pycache__` 是 Python 正常行为，不算遗留）

### DEFAULT_LIMITS 完整性（A2 要求"不漂移"）

| 模块 | 键数 |
|---|---|
| `mcp_34465a.config.DEFAULT_LIMITS` | 14 |
| `mcp_8852b.server.DEFAULT_LIMITS` | 3 |
| `mcp_js220.config.DEFAULT_LIMITS` | 7 |

值未读取/未修改（不读是因为 A2 强调"不动值"）。

## 根目录整理后结构

```
E:/other/ai_project/EEMCP/
├── .gitignore                          (新建 — gitignore __pycache__/ + .nezha/ + _msi_extract/)
├── .nezha/                             (原位，gitignore 排除)
├── _archive_pre_reorg_20260618_113935.tar.gz   (备份，161 MB)
├── _msi_extract/                       (原位 + 新增 README.md 解释来源)
│   └── README.md
├── Bluetooth low energy Measurement Software.msi   (原位)
├── BUILDING_AN_INSTRUMENT_MCP.md
├── CLAUDE.md
├── CMT HWDVT Test Plan -BLE Sticker Tag.xlsx
├── SPEC_mcp_interaction.md
├── _REORG_REPORT.md                    (本文件)
├── doubao_thread.md
├── mcp_34465a/
├── mcp_8852b/
├── mcp_js220/
├── reports/
│   ├── _archive/2026-06-15.tar.gz      (11 个 6/15 报告子目录打包)
│   ├── _smoke_voltage/
│   ├── ble_full_20260617_*/            (6/17 × 5 保留)
│   ├── ble_full_20260618_*/            (6/18 × 7 保留)
│   ├── full_sweep_20260617_*.md        (3 文件)
│   ├── full_sweep_v2_visual.html
│   ├── js220_script_test/  js220_smoke/  js220_test_quick/
│   └── product_modes_test/
├── run_ble_full_suite.py
└── verify_preflight.py
```

## 未做的事 + 原因

1. **`mcp_8852b/manual test/` 改名为 `_ble_msm_samples/`** — 保留原名。原因：`state_machine.py:207, 275` docstring 和 `debug_eut_init2.py:163` print 提示引用了原路径；`state_machine.py` 是 A2 红线文件，且 A4 接手时统一改更合理。
2. **删除 `_msi_extract/`** — 保留目录。原因：grep 0 处代码引用，删除是安全的；但 A2 任务要求"先确认是否有重建脚本" — 现在加了 `README.md` 记录 7-Zip 重建命令，已具备可恢复性。
3. **删除 `.msi` 原始安装包** — 不动，`.gitignore` 已排除。

## 回滚方法

```bash
# 完整回滚到整理前状态
cd E:/other/ai_project
rm -rf EEMCP
tar -xzf EEMCP_backup_or_old/EEMCP_pre_reorg_20260618_113935.tar.gz
# 然后删掉备份
```

备份文件路径: `E:/other/ai_project/EEMCP/_archive_pre_reorg_20260618_113935.tar.gz`
（如果备份文件已删，可解压后 rm `_archive_pre_reorg_*.tar.gz` 清理）

**单步回滚（仅回滚某批移动）**：
- 移动 debug/logs/traces/csv → 把 `mcp_8852b/scripts/_debug_history/` 整个移回根目录
- 归档 reports → 解压 `reports/_archive/2026-06-15.tar.gz` 回 `reports/`
- 删除 __pycache__ → Python 会自动重建，无需回滚

## 给 A4 的接口

**已稳定的结构（A3 完成后）**：
- `EEMCP/mcp_8852b/scripts/_debug_history/` 目录结构稳定（logs/、_traces/）
- `EEMCP/reports/_archive/2026-06-15.tar.gz` 命名约定
- `EEMCP/.gitignore` 排除规则（`__pycache__/`、`.nezha/`、`_msi_extract/`、`*.msi`）
- `EEMCP/_msi_extract/README.md` 重建命令

**仍可安全移动/重命名（A4 范围）**：
- `mcp_8852b/manual test/` → `_ble_msm_samples/`（仅需同步改 `state_machine.py:207, 275` docstring 和 `debug_eut_init2.py:163` print 提示 — 不影响功能）
- 根目录 `*.msi` 文件可考虑移入 `mcp_8852b/reference/_ble_msm_samples/` 或 `_archive/`
- `mcp_8852b/scripts/` 内的 `test_init.py` / `test_init_official.py` 可考虑重命名/合并

**A4 不要动**：
- A3 已移动的 22 个文件（debug/log/trace/csv）
- `mcp_8852b/state_machine.py`（除非做 manual test 重命名时改 2 处 docstring）
- 4 处 `DEFAULT_LIMITS` 值
- v6.2/v6.6/v6.8 fix 块（state_machine.py 343-456, 462-509, 544-574）
- `instrument.py:220-231` `*IDN?` 5 次重试
- `set_eut_interface(skip_if_ready=True)` / `wait_test_complete(require_transition=True)` 默认
- `verify_preflight.py` 8 项测试
- 三包 envelope 键顺序/名字
- `mcp_8852b/reference/*` 任何 PDF/txt
- `mcp_8852b/scripts/replay_capture.py` 等 MT8852B 协议验证脚本

## 等 A5 验收
