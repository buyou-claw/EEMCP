# TR-ACCL-001 / TR-ACCL-002 测试方案（审查版）

> 依据：`DS_lis2dh12.pdf` Rev 6、`test item.txt`、MSO54 实测能力
> 日期：2026-08-18
> 状态：**方案审查通过后再执行**

---

## 1. 测试项目定义

| 编号 | 测试项 | 被测对象 | 通道 |
|---|---|---|---|
| TR-ACCL-001 | Accelerometer I2C Bus — **clock** signal timing and signal integrity | SCL | CH1 |
| TR-ACCL-002 | Accelerometer I2C Bus — **data** signal timing and signal integrity | SDA | CH2 |

被测总线：MCU（主）↔ LIS2DH12（从，地址 0x33 已由 `WHO_AM_I=0x33 [OK]` 确认）
工作模式：I2C Standard mode，实测 f(SCL) ≈ 96.4 kHz
DUT 流量：`continue_read` 每秒发起一次加速度计读取（约 1 ms 突发，占空比 ≈ 0.1%）

---

## 2. 规格书判据

### 2.1 Table 6 — 电气特性（DS §2.3）

| 符号 | 参数 | Min | Max | 说明 |
|---|---|---|---|---|
| VIH | 数字高电平**输入** | **0.8·Vdd_IO** | — | 从机识别为高的最低电平 |
| VIL | 数字低电平**输入** | — | **0.2·Vdd_IO** | 从机识别为低的最高电平 |
| VOH | 高电平**输出** | **0.9·Vdd_IO** | — | LIS2DH12 输出 |
| VOL | 低电平**输出** | — | **0.1·Vdd_IO** | LIS2DH12 输出 |

> **注意**：I2C 是开漏总线，高电平由**上拉电阻**建立，不是 LIS2DH12 驱动。
> 因此 CH1/CH2 上的 VOH 实际测的是上拉电平，判据应取 **VIH = 0.8·Vdd_IO**（从机必须能识别为高）；
> VOL 才是 LIS2DH12 拉低时的电平，判据 **0.1·Vdd_IO**（输出规格）与 **VIL = 0.2·Vdd_IO**（有效低电平）。

### 2.2 Table 8 — I2C 从机时序（DS §2.4.2，Standard mode）

| 符号 | 参数 | Min | Max | 单位 |
|---|---|---|---|---|
| f(SCL) | SCL 时钟频率 | 0 | 100 | kHz |
| tw(SCLL) | SCL 低电平时间 | 4.7 | — | µs |
| tw(SCLH) | SCL 高电平时间 | 4.0 | — | µs |
| tsu(SDA) | SDA 建立时间 | 250 | — | ns |
| th(SDA) | SDA 保持时间 | 0 | 3.45 | µs |
| th(ST) | START 条件保持时间 | 4 | — | µs |
| tsu(SR) | 重复 START 建立时间 | 4.7 | — | µs |
| tsu(SP) | STOP 条件建立时间 | 4 | — | µs |
| tw(SP:SR) | STOP→START 总线空闲 | 4.7 | — | µs |

> **测量点定义（DS 原文脚注）**：
> *"Measurement points are done at 0.2·Vdd_IO and 0.8·Vdd_IO, for both ports."*
> → 全部时序量的参考电平是 **0.2·Vdd_IO / 0.8·Vdd_IO**，不是 50%。

> **表下注 1（DS 原文）**：
> *"Data based on standard I2C protocol requirement, not tested in production."*
> → 这些数值取自 I2C 标准协议（UM10204），**非产线实测值**，报告中需注明。

### 2.3 Table 9 — 绝对最大额定值（DS §2.5）

| 符号 | 参数 | 限值 |
|---|---|---|
| Vin | 任一控制脚输入电压（含 SCL/SDA） | **-0.3 V ~ Vdd_IO+0.3 V** |
| ESD | HBM | 2 kV |

> 3.3V 供电时过冲上限 = **3.6 V**，下冲下限 = **-0.3 V**。

### 2.4 上升/下降时间：规格书**未定义**

Table 8 **没有 tr / tf 行**。上升/下降时间的判据只能引用总线规范 **I2C UM10204**：
Standard mode：**tr ≤ 1000 ns，tf ≤ 300 ns**。
报告中必须标明这是总线规范要求，非 LIS2DH12 器件规格。

---

## 3. 上一版测试设计的准确性错误（已定位）

| # | 错误 | 后果 | 修正 |
|---|---|---|---|
| 1 | 用示波器默认 **50% 阈值**测时序 | 与 DS 定义的 0.2/0.8·Vdd_IO 测量点不符，tw/tsu/th 全部偏差 | 正式数值改由 Python 按 **0.66 V / 2.64 V** 阈值从原始波形重算 |
| 2 | 假设示波器默认 RISETIME/FALLTIME 可用 | Tek 默认 **10-90%**，而 DS/I2C 用 **20-80%**，线性边沿下相差约 1 倍，会产生**假失败** | Python 按 20-80% 重算 |
| 3 | `DATA:STOP 10000` = 读记录**开头** 8 µs | 记录长 1.25 M 点、触发点在 30% 处 → 读到的全是触发前的总线空闲段，即"3.27 V + 0.2 V 噪声" | 改用 **MANUAL 模式 + RECORDLENGTH 10000**，记录长度 = 读取长度；或读触发点附近窗口 |
| 4 | AUTO 触发 + 自由采集去"等"流量 | I2C 突发占空比 0.1%，单次采集命中概率 ≈0.1%，90 秒也抓不到 | 改 **NORMAL 触发 + STOPAFTER SEQUENCE 单次捕获**，等真实 SCL 边沿 |
| 5 | 两个通道分别读 CURVE? | RUN 模式下两次读可能来自**不同次采集**，无法做 SDA↔SCL 的相关时序 | 单次捕获后读，两通道来自**同一次冻结记录** |

> 另：`WAVEFORM:LIST?` 是固件不支持的查询，会锁死 USBTMC bulk-IN（NOTES.md §1 触发条件 #2）。已禁用。

---

## 4. 示波器配置方案

### 4.1 垂直（两个通道相同）

| 项 | 值 | 依据 |
|---|---|---|
| SCALE | **0.5 V/div** | 10 div × 0.5 = 5 V 量程，含 0~3.3 V 全摆幅；8-bit ADC → **20 mV/LSB**，可分辨 VOL(≤0.33 V) |
| POSITION | **-3 div** | 0 V 参考落在中心下方 3 div（屏幕中心 = 1.5 V），0 V 与 3.3 V 同时在屏内 |
| OFFSET | 0 | 不使用偏移，避免与 POSITION 语义混淆 |
| COUPLING | DC | 必须 DC，否则低频/直流电平丢失 |
| TERMINATION | 1 MΩ | 高阻，不加载总线 |
| BANDWIDTH | FULL (1 GHz) | 保留边沿细节；探测系统带宽需远高于 100 kHz I2C |

> 探棒要求：**地线尽可能短**（MEASUREMENT_GUIDE：6 cm 地线 + 10-15 pF 会在 100-300 MHz 谐振，产生假振铃）。
> 报告中记录探棒型号/带宽与衰减比。

### 4.2 时基与记录长度（三种配置）

| 配置 | 用途 | HORIZONTAL | 采样率 | 记录时长 | 分辨率 |
|---|---|---|---|---|---|
| **C1 边沿精细** | 上升/下降时间、tsu(SDA)、th(SDA)、th(ST) | MANUAL, 10000 点, **20 µs/div** | 50 MS/s | 200 µs | **20 ns/点** |
| **C2 协议包络** | 整笔事务结构、tsu(SR)、tsu(SP)、tw(SP:SR) | MANUAL, 10000 点, **500 µs/div** | 2 MS/s | 5 ms | 500 ns/点 |
| **C3 统计** | f(SCL)、tw(SCLL)、tw(SCLH) 统计 | MANUAL, 10 000 000 点, **100 ms/div** | 10 MS/s | 1 s | 100 ns/点 |

分辨率校验：
- C1：300 ns 边沿 = 15 点；tsu(SDA) 最小 250 ns = 12.5 点 ✓
- C2：tsu(SP) 最小 4 µs = 8 点（够判定，精度 ±0.5 µs）
- C3：tw(SCLL) 4.7 µs = 47 点，一次记录含约 90 个 SCL 周期 → 统计样本充足 ✓

### 4.3 触发

| 配置 | 模式 | 设置 |
|---|---|---|
| C1 / C2 | **NORMAL** | EDGE / CH1 / **FALL** / LEVEL **1.65 V** / `ACQUIRE:STOPAFTER SEQUENCE` |
| C3 | **NORMAL** | 同上；记录 1 s 内必含触发点（= 突发中第一个 SCL 下降沿） |

触发位置（`HORIZONTAL:POSITION`）：
- C1：**30%** → 触发前 60 µs / 触发后 140 µs（覆盖 START 条件 + 后续 20 个 bit）
- C2：**10%** → 触发前 0.5 ms / 触发后 4.5 ms（覆盖整笔事务含 STOP）
- C3：**30%** → 触发前 300 ms / 触发后 700 ms（突发剩余部分全在后段）

### 4.4 单次捕获工作流（关键：保证两通道数据对齐）

```
1. ACQUIRE:STATE STOP              ← 仅配置时用一次
2. 写 C1/C2/C3 全部参数
3. ACQUIRE:STOPAFTER SEQUENCE
4. TRIGGER:A:MODE NORMAL
5. ACQUIRE:STATE RUN               ← 武装
6. 轮询 TRIGGER:STATE? 直到 "SAVE" 或 "TRIGGER"（DUT 每秒一发，等待 ≤2 s）
7. 读 MEAS<n>:VALUE?（此时记录已冻结，测量值有效）
8. 读 CH1 CURVE?  → 读 CH2 CURVE?（同一次记录，时间轴一致）
9. ACQUIRE:STOPAFTER RUNSTOP
10. 需要下一次捕获时回到步骤 3
```

> 全程只在步骤 1 主动 STOP 一次；其余 STOP 都由示波器自身的 `STOPAFTER SEQUENCE` 完成。
> 每次捕获后必须确认 `TRIGGER:STATE?` 已触发，未触发则数据无效，重新武装。

---

## 5. 测量参数映射表

### 5.1 TR-ACCL-001（SCL / CH1）

| 参数 | 判据 | 示波器测量（快速判据） | Python 重算（正式数值） | 配置 |
|---|---|---|---|---|
| f(SCL) | ≤ 100 kHz | FREQUENCY (CH1) | 由 SCL 上升沿间隔求周期 | C3 |
| tw(SCLL) | ≥ 4.7 µs | NEGATIVEWIDTH (CH1) | SCL 低于 0.66 V 的持续时间 | C3 |
| tw(SCLH) | ≥ 4.0 µs | POSITIVEWIDTH (CH1) | SCL 高于 2.64 V 的持续时间 | C3 |
| tr(SCL) | ≤ 1000 ns（I2C 规范） | RISETIME (CH1) | **20%→80%** 转换时间 | C1 |
| tf(SCL) | ≤ 300 ns（I2C 规范） | FALLTIME (CH1) | **80%→20%** 转换时间 | C1 |
| VOH | ≥ 2.64 V (0.8·Vdd_IO) | HIGH (CH1) | 高电平直方图众数 | C3 |
| VOL | ≤ 0.33 V (0.1·Vdd_IO) | LOW (CH1) | 低电平直方图众数 | C3 |
| 过冲 | ≤ 3.6 V (Vdd_IO+0.3) | MAXIMUM (CH1) | 峰值 | C1 |
| 下冲 | ≥ -0.3 V | MINIMUM (CH1) | 谷值 | C1 |
| 单调性/振铃 | 无异常 | — | 每边沿检查单调段数、边沿后过冲幅度 | C1 |

### 5.2 TR-ACCL-002（SDA / CH2）

| 参数 | 判据 | 示波器测量 | Python 重算 | 配置 |
|---|---|---|---|---|
| tsu(SDA) | ≥ 250 ns | — （需相关分析） | SDA 变化 → 下一个 SCL 上升沿（两信号均取 0.66/2.64 V 交点） | C1 |
| th(SDA) | 0 ~ 3.45 µs | — （需相关分析） | SCL 下降沿 → SDA 变化 | C1 |
| VOH | ≥ 2.64 V | HIGH (CH2) | 高电平众数 | C3 |
| VOL | ≤ 0.33 V | LOW (CH2) | 低电平众数 | C3 |
| 过冲/下冲 | ≤3.6 V / ≥-0.3 V | MAXIMUM / MINIMUM (CH2) | 峰值/谷值 | C1 |
| 单调性/振铃 | 无异常 | — | 同 CH1 | C1 |

### 5.3 START / STOP 条件（总线协议，两测试项共用）

| 参数 | 判据 | Python 判定方法 | 配置 |
|---|---|---|---|
| th(ST) | ≥ 4.0 µs | 识别 START（SCL 高时 SDA 下降）→ 量到首个 SCL 下降沿 | C1 |
| tsu(SR) | ≥ 4.7 µs | 重复 START：SCL 高时 SDA 下降 → 前一个 SCL 上升沿 | C2 |
| tsu(SP) | ≥ 4.0 µs | STOP：SCL 高时 SDA 上升 ← 最近一个 SCL 上升沿 | C2 |
| tw(SP:SR) | ≥ 4.7 µs | STOP 的 SDA 上升 → 下一次 START 的 SDA 下降 | C2 |

> START/STOP 的识别规则（I2C 协议）：
> **START** = SCL 为高期间 SDA 由高变低；**STOP** = SCL 为高期间 SDA 由低变高。

---

## 6. 阈值与 Vdd_IO 的确定

- Vdd_IO 标称 **3.3 V**（总线空闲实测 3.27 V，符合 0.9·Vdd_IO 以上的上拉电平）
- 判据换算（Vdd_IO = 3.3 V）：

| 用途 | 电平 |
|---|---|
| 低参考（0.2·Vdd_IO） | **0.66 V** |
| 高参考（0.8·Vdd_IO） | **2.64 V** |
| VOH 下限（0.9·Vdd_IO） | 2.97 V |
| VOL 上限（0.1·Vdd_IO） | 0.33 V |
| 过冲上限（Vdd_IO+0.3） | 3.6 V |

> **待确认**：若能在板上量到 LIS2DH12 的 Vdd_IO 测试点（可用空闲的 CH3 接上），
> 可把上表换成实测值，比标称 3.3 V 更准确。默认按 3.3 V 计算。

---

## 7. 执行流程

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 | 连接 + 显式配置（CH1/CH2 + 关 CH3/CH4 + 触发 + 水平），不先读 IDN | 示波器进入已知状态 |
| P1 | 串口驱动 DUT：`sensor`→`imu`→`init`→`continue_read` | 确认 I2C 流量存在 |
| P2 | **C3 单次捕获** → 读测量 + CURVE? 触发点窗口 | 频率/占空/电平统计 + 完整突发波形 |
| P3 | **C1 单次捕获 ×3** → 读 CURVE? 双通道 | 边沿时间 + 建立/保持 + 过冲 |
| P4 | **C2 单次捕获 ×1** → 读 CURVE? 双通道 | START/STOP 条件时序 |
| P5 | Python 汇总：按 0.66/2.64 V 阈值重算全部时序量 | 判定表 |
| P6 | 生成 EN + ZH 报告（格式参照 REPORT_SPEC.md 七页结构） | `reports/` 输出 |

捕获次数合计约 5 次单次捕获，每次 STOP/RUN 都由示波器自身完成，风险最低。

---

## 8. 风险与限制（写入报告）

1. Table 8 数值来自 I2C 标准协议，**非产线实测值**（DS 原文注 1）。
2. **tr/tf 规格书未定义**，判据引用 I2C UM10204，非器件规格。
3. 示波器测量引擎默认 50%（宽度）/10-90%（边沿）阈值，与 DS 的 0.2/0.8·Vdd_IO 测量点不同；
   报告正式数值一律用 Python 按 DS 阈值从原始波形重算，示波器值仅作交叉验证。
4. 探棒地线长度与探头电容会影响 tr/tf 与过冲读数；报告需记录探棒配置。
5. 8-bit ADC @ 0.5 V/div → 20 mV/LSB；C1 的 20 ns/点 → 边沿时间分辨率约 ±20 ns。
