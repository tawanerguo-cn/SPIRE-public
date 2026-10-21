# 找原子/逐点打谱与自动修针：代码层次

这批代码实现的是两个 **fake-first workflow 的最小闭环**，不是已经批准的 live 实验 protocol。
所有 Nanonis 命令仍然只有 `OfficialNanonisBackend` 能够转发给官方 `nanonis_spm`；workflow
只依赖 `NanonisDevice` 的 snake_case typed API。

Live single-step operation requires explicit operator authorization through the existing access path. It does not make a fake workflow, a physical-movement check, or end-to-end automated tip conditioning an accepted live workflow.

## 依赖方向

```text
runtime_layer（离线/fake 工具菜单与可选 stdio MCP）
        |
        v
workflow_layer
  |          |             |
  v          v             v
analysis   data        instrument
  |          |             |
  +----------+-------------+
                         NanonisDevice
                         /           \
            FakeNanonisBackend   OfficialNanonisBackend
                                      |
                                nanonis_spm / TCP
```

- `instrument_layer`：官方方法、typed models、fake 状态、capability registry、SafetyGate、
  precheck/postcheck/abort。
- `analysis_layer`：图像候选原子和谱分类；禁止操作仪器。
- `data_layer`：`AtomMap`、逐原子 transaction 和修针 run record；禁止操作仪器。
- `workflow_layer`：组合上述接口，决定步骤顺序；禁止直接调用 backend。
- `runtime_layer`：开放保存文件观察、标点和 fake campaign；没有 live、桌面或任意代码执行工具。详见 [agent 工具](spm-agent-tools.md)。

## Instrument layer 已接入的接口

### 共用读接口

| Typed API | 官方 Nanonis 方法 | 用途 |
|---|---|---|
| `get_scan_speed()` | `Scan.SpeedGet` | 记录扫描条件 |
| `get_scan_xy_position()` | `Scan.XYPosGet` | 记录 scanner 坐标 |
| `get_follow_me_position()` | `FolMe.XYPosGet` | 移动前后 read-back |
| `get_follow_me_speed()` | `FolMe.SpeedGet` | 记录移动条件 |
| `get_bias_spectroscopy_settings()` | `BiasSpectr.Status/Props/Limits/Chs/TimingGet` | 已封装的核心采谱设置快照；未覆盖 advanced 设置 |
| `get_tip_shaper_properties()` | `TipShaper.PropsGet` | 读取当前修针配置 |
| `get_atom_tracking_status/properties()` | `AtomTrack.Status/PropsGet` | 漂移/原子跟踪只读状态 |

### 找原子/逐点打谱写接口

| Typed API | 官方方法 | 代码安全边界 |
|---|---|---|
| `start_scan()` | `Scan.Action` | 只启动既有 frame/buffer/speed/save 配置；feedback 必须开启；读回 running；live 已验收 |
| `acquire_scan()` | `Scan.Action` + `Scan.WaitEndOfScan` | 当前 frame/speed 不变；超时 stop；live 默认关闭 |
| `move_tip_xy()` | `FolMe.XYPosSet` | 目标必须位于当前旋转 scan frame 内；移动后 read-back |
| `acquire_bias_spectrum_transaction()` | `BiasSpectr.Props/Limits/Chs/TimingSet` + `Start` | Linear/HOLD/Reset Bias前检、独立guard、内存回传；采集时关闭保存选项，结束后恢复并回读；接管时留待交接 |
| `stop_bias_spectroscopy()` | `BiasSpectr.Stop` | 保护性 stop |
| `stop_atom_tracking()` | `AtomTrack.CtrlSet(Status=0)` | 只允许关闭，不允许开启 |

`BiasSpectr.Start` 返回的数据保存在内存中。当前 wrapper 不允许通过 base name 或 autosave
隐式写文件；以后如需保存 `.dat`，必须单独走 data layer 的命名、覆盖和审计策略。
`acquire_scan()` 不会改写 Nanonis GUI 中已有的 autosave 配置，因此它在完成文件策略 review 前
保持 live disabled；fake backend 的 scan acquisition 不写文件。

### 自动修针写接口

| Typed API / recipe | 官方方法 | precheck / postcheck |
|---|---|---|
| `apply_bias_pulse()` / `BiasPulseRecipe` | `Bias.Pulse` | scan/谱停止、反馈开、tip state 合法；异常 withdraw |
| `run_z_poke()` / `ZPokeRecipe` | `ZCtrl.ZPosSet` | 反馈关、Z limits 开；move/dwell/finally restore；验证恢复位置 |
| `run_tip_shaper()` / `TipShaperRecipe` | `TipShaper.PropsSet` + `Start` | scan/谱停止、反馈开；验证反馈恢复；异常 withdraw |

这些方法不是“只读占位”。fake backend 会改变状态并记录与官方方法同名的 command log；
Official backend 也已经连接同一调用路径。`follow_me_move` 与
`scan_start_current_frame` 已逐项完成本站 live commissioning；`bias_pulse`、`z_poke`、
`tip_shaper`、`bias_spectroscopy` 和完整 `scan_acquisition` 仍为
`live_enabled=False`，所以真实 backend 会在第一条写命令之前被 `SafetyGate` 拒绝。

`start_scan()` 与阻塞式 `acquire_scan()` 是两个不同动作。前者不接收 frame、buffer、speed
或保存设置，执行前快照这些参数，唯一写命令是 `Scan.Action(Start, direction)`，然后核验
`Scan.StatusGet=running`。Nanonis API 没有当前 start direction getter，调用者必须显式选择
up/down。本站在 200 nm × 200 nm、256 × 256 当前配置上完成了 down 方向验收；操作员确认 UI
启动后人工停止。控制事件通过 `ExistingRunActionEventSink` 以同一 action id 的 before/after
写入正在运行的实验数据库。

### Feedback-managed poke 与粗动服务

`run_feedback_poke(FeedbackPokeRecipe)` 将正的 `approach_depth_m` 映射为 Nanonis
Tip Shaper 的负 `Tip Lift` 和等量正 `Lift Height`。按 2026-09-20 操作员更新，recipe
不再接受偏压参数；读取当前偏压，关闭 Change Bias，并将两个偏压字段均填为当前读回值，
避免转折处夹带电压变化；完成后核验 feedback 与初始 bias 都已恢复。

横向粗动通过 `move_coarse_x_plus/minus()` 和 `move_coarse_y_plus/minus()` 进入
instrument layer，固定映射到 `Motor.StartMove` direction code 0..3。0.0.29 增加
`retract_coarse_z()`，只允许 code 4（Z+）；code 5（Z-）没有公共 wrapper。
两类动作共用马达实现、`CoarseMoveResult` 和审计；不同边界分别进入 `coarse_xy_move` 与
`coarse_z_retract`。调用前后都要求 scan、Bias Spectroscopy、Auto Approach 停止，且
Z controller 稳定关闭。草案 XY 单命令最多 1000 步、退针最多 10 步，范围由 SafetyGate 持有，
不是已验证的安全距离。返回只证明 Nanonis 命令已完成等待，不证明 PMD4 发生了
物理位移；本机 `Motor.PosGet` 当前明确返回模块不可访问。该 bounded action 仍为
`live_enabled=False`，不得用鼠标自动化绕过。通用换点 workflow 和 fake MCP 服务见
[仪器服务](instrument-services.md)。

Auto Approach 也只作为 `run_auto_approach()` 的完整事务存在：precheck 要求 scan、谱和
Z controller 均已停止，随后启动、轮询、超时停止，并以 controller 成功接管作为 postcheck。
`acquire_local_scan()` 不接收任意绝对 frame；它只能从当前旋转 frame 中裁一个完全包含的
子区域，扫描结束或异常时都恢复原 frame。两者均为 fake-first、live-disabled。

`move_tip_xy()` 调用 Nanonis `FolMe.XYPosSet`，随后用 `FolMe.XYPosGet` 核验模块坐标。
正常情况下 Nanonis Follow Me/scan-position UI 会显示同一模块状态，但这不等于鼠标光标或
点击动画会重放。首次 live commissioning 必须同时核对 API read-back、Nanonis GUI 坐标/标记
与后续扫描位置；不为视觉效果引入鼠标自动化。

## Analysis layer

`LocalMaximumAtomDetector` 是一个确定性 baseline：robust threshold、局部极大值、最小间距
抑制、pixel-center 到旋转 scan frame 的显式坐标变换（遵循 Nanonis 正角为顺时针）。它适合离线测试接口，不应被视为已经
commission 的实验室原子检测器。

`ReferenceSpectrumClassifier` 做归一化最近参考谱匹配，并用 `maximum_distance` 显式拒绝为
`unknown`。0.0.21 起必须给出有物理偏压轴与 provenance 的 reference，核对通道单位、单调偏压采样和适用条件；不匹配、空数据或类别歧义均拒绝。similarity confidence 不是校准概率；真正上线前需要冻结参考库、采集条件和验证集。

`TipQualityAnalyzer` 是 workflow 使用的 Protocol；`DeterministicTipQualityAnalyzer` 提供
robust contrast 和 scan-line jump 的可解释 baseline。它必须显式传入
`TipQualityThresholds`，仓库没有偷偷内置某个实验室阈值。原因是修针文献调研尚未产生本实验室
reviewed 的好针/坏针判据，这个 baseline 只能用于离线 shadow/fake 验证，不能直接当成 protocol。

## Workflow layer

### `AtomSpectroscopyWorkflow`

```text
saved scan + source SHA-256 + explicit frame
  -> numerical/visual candidate selection + excluded regions
  -> source-bound fake-only target plan
  -> for each candidate:
       bounded Follow Me move
       bounded BiasSpectr transaction
       durable raw acquisition in RunStore
       unknown-aware classification
       AtomSpectrumRecord
  -> AtomMap
```

入口要求显式 `frame` 与 `run_store`，不再用当前 frame 解释任意历史图。异常时独立尝试 `BiasSpectr.Stop` 和 `FolMe.Stop`，原始谱在分类前写入 SQLite；分类器故障也不会丢掉已提交的数据。`AtomMap` 保存 scan frame 和完整
`CoordinateTransform`（原始 shape、方向、frame、漂移占位、uncertainty 和版本）；每个原子保存
move read-back 和完整 acquisition transaction，因此可以追溯 recipe、原设置、SafetyGate 参数和
前后 tip state。批次的最大原子数与最大时长通过 `recipe_execution` SafetyGate 校验。

当前未解决的 live 问题包括 drift model、原子点击中心误差、谱前后漂移、运动路径与 conditioning 区域检查、信号级中止阈值、advanced 采谱设置和现场验收。已有排除矩形只筛选历史像素落点，不能证明针尖路径安全；时长检查也不能强行中断底层阻塞命令。

### `AutomaticTipConditioningWorkflow`

```text
reviewed precheck assessment
  -> 必须是 degraded 且置信度达标
  -> scan 必须停止
  -> bounded move to conditioning site
  -> 恰好一个 BiasPulse / ZPoke / TipShaper recipe
  -> AWAITING_POSTCHECK
  -> 独立 postcheck
  -> SUCCEEDED 或 FAILED_POSTCHECK
```

动作执行成功只会产生 `AWAITING_POSTCHECK`，不能直接宣称修针成功。一次不确定或低置信度
precheck 不会触发动作。workflow record 保存底层 `ConditioningActionResult`，包含动作前后 tip
state 和实际通过 SafetyGate 的归一化参数。

`run_once()` 提供完整 fake-first cycle：它通过 bounded `acquire_scan()` 强制获得新的 precheck
scan；执行至多一个修针动作；若动作完成，再强制获取一张新的 postcheck scan。任何 postcheck
采集或分析异常都会进入 `ABORTED`，不会沿用旧图像判定成功。
`minimum_precheck_confidence` 和 `TipQualityThresholds` 都必须由调用方显式提供；仓库不把论文参数
或任意默认值伪装成本实验室判据。

## 从 fake 到 live 还缺什么

1. 用本实验室数据 review 并冻结原子检测、谱分类和 tip-quality 判据。
2. 对每个实验场景建立独立 SafetyGate bounds；当前默认值只是非生产 fake-first 草案。
3. 补 conditioning site/no-go mask、drift budget、最大原子数、最大累计时间和 abort policy。
4. 单 recipe、单位置、人工在环 commissioning。
5. opt-in live integration tests 和 Nanonis GUI/read-back 对照。
6. 上述证据通过 review 后，才单独把某个 bounded action 的 `live_enabled` 改为 `True`。
