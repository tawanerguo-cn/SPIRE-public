# 面向 agent 的仪器服务

SPiRE 提供可发现、可组合、可审计的仪器服务，目标是让 agent 高效、安全地使用 STM，
并扩展到 AFM 及其他实验设备。修针、原子定位、谱学只是使用这些服务的工作流。
目前底层实现是 Nanonis；没有声称已实现其他设备驱动或验收 AFM 的力／频移反馈。

## 层次与服务契约

```text
agent -> runtime (CLI / MCP，共用工具清单)
      -> workflow (前检、编排、后检、中止、持久化运行证据)
      -> NanonisDevice (typed 接口、SafetyGate、动作审计)
      -> OfficialNanonisBackend / FakeNanonisBackend

workflow -> data (文件、RunStore) / analysis (信号、图像、谱)
         -> knowledge (审核知识、协议、manifest) / memory (历史经验，当前待完善)
```

不新增连接、socket、信号读取、安全或存储实现。扩展其他设备时在 instrument layer 添加对应
设备实现及 fake，复用 workflow / runtime 的运行与证据机制；只有真实出现多个实现需要共同
契约时再抽取相应 Protocol，不提前伪造一个声称适用于所有设备的驱动。

每项服务需明确：输入及单位、允许状态、代码边界、能力与验收状态、返回证据、失败后的停止行为。
参数范围由 SafetyGate 持有，工具清单由 `spm_agent_tools.toml` 持有。能力查询读取这些登记，
不另维护一份安全菜单。`Motor.StartMove` 的 `wrapper_bounded_actions` 分别关联 XY 与退针；
它的命令级 `bounded_action` 为 null，调用者应读取各 wrapper 的映射。

STM 的电流／Z 判据不能自动迁移到 AFM。新增模式必须明确反馈信号、单位、探针/样品条件、
设备能力及独立验收，避免以修改知识文本的方式扩大硬件权限。

## 粗动退针与换点的实现边界

- 四个 XY typed 方法保持原方向限制。
- `retract_coarse_z(number_of_steps, group=...)` 固定 Z+，禁止任意方向输入；没有 −Z 方法。
- 两类动作复用同一马达执行与状态检查，返回 `CoarseMoveResult`，事件名分别为
  `coarse_xy_move` / `coarse_z_retract`。旧 `CoarseXYMoveResult` 已直接迁移，不提供历史别名。
- 同一连接返回 `Wait until finished` 后还会重新检查状态；返回不证明真实移动距离。
  站点历史记录显示 `Motor.PosGet` 模块不可访问，方向已有操作员确认和单步实验记录，
  但实际位移/间隙仍不能由命令返回证明；组与驱动设置不能跨会话盲目沿用。
- `ZCtrl.Withdraw` 仍是独立的保护性动作，不能替代指定数量的粗动马达步数。
- `home_z_controller` 使用原生Home现有配置，调用者显式给最大行程；配置读取、行程检查和审计复用同一设备入口，返回命令确认及当前状态。

`CoarseRelocationWorkflow` 是通用换点事务，不发 pulse：

```text
校验全部参数与初始状态
 -> 关反馈并读回
 -> 粗动 Z+ 退针并复查状态
 -> 指定轴/方向 XY 横移并复查状态
 -> 已有 Auto Approach 事务（含超时停止与反馈接管检查）
 -> 新状态快照
 -> awaiting_postcheck：等待新的扫描/谱及质量判断
```

参数全部显式给出；工具不会从知识库自动选方向或设备组。下方10 / 200步仅是历史fake演练参数，
不是当前实验推荐；本站换区经验见[当前workflow](../../src/knowledge_base/tip_conditioning/workflow.md#换位置与检验)。
步数不换算成距离，参数硬边界从 SafetyGate 查询，不从实验经验反推权限。
动作或审计失败后不继续下一步，独立尝试相关保护性 stop，记录失败阶段和每个停止失败。
不自动重试、不自动反向补偿、不自动恢复 feedback、不重新发 pulse。

当前整个组合流程显式 fake-only，即便调用者自行构造允许 live 的 gate，也不能执行这个
workflow。相关单动作默认 live gate 关闭；本站后续已有临时授权下的真实单步操作，
不能再将“当初只做fake开发”表述为项目从未操作硬件。授权及验收范围见[控制说明](../operations/authorized-control.md)，
单步验证不等于组合workflow已完成验收，也不能添加鼠标旁路。

## 共用 CLI / MCP 工具

| 工具 | 用途 |
|---|---|
| `describe_instrument_service` | 从代码登记查询 capabilities、各动作 SafetyGate 和工具菜单，不连接设备 |
| `run_fake_coarse_relocation` | 使用新建 fake device 演练一次换点，结果与失败均保存到 RunStore |
| `inspect_workflow_run` | 读取生成目录内的运行事件，支持分页；读取不会恢复执行 |

`describe_instrument_service` 描述代码已实现的服务，不是设备当前在线能力，也不授予动作权限。
现有 MCP surface 仍为 offline/fake-only。它共享原来的执行互斥锁、路径限制、唯一输出目录和
真实 stdio 传输；未建立第二套 MCP server 或工具 manifest。

示例参数（模拟，不执行 pulse）：

```json
{"xy_direction":"x_plus","retract_steps":10,"xy_steps":200,"group":1}
```

通过 `-m runtime_layer.agent_cli --tool run_fake_coarse_relocation --arguments-file ...`
调用，沿用 `--read-root` 和 `--output-root`。返回 `database_path` 可交给
`inspect_workflow_run`。SQLite run 的 `complete` 表示记录已结束，真正工作流结果在事件中，
目前仍为 `awaiting_postcheck`；不能解读为针尖已修好。原始动作关联 before/after/failed 事件
仍存入同一数据库，可用现有 recording 数据读取接口检查。

## 验证范围与历史依据

测试覆盖固定退针方向、参数越界、真实 backend 拒绝、全部参数先验检查、方向/组编码、
扫描/谱/反馈冲突、命令后状态改变、退针/XY 响应丢失、自动进针超时、独立停止失败、
运行记录回读、路径限制及 MCP 实际 stdio 调用。以上组合流程测试的硬件行为均为 fake。

2026-09-18 验证：`python -B -m unittest discover -s tests -v` 共 231 项，230 项通过、
1 项 opt-in 真机测试跳过。复用正式 recorder review 回放 480,950 行历史数据，耗时约
10.5 秒，61 段扎针候选的完整检测结果与 0.0.28 一致。本次回放用于验证识别逻辑重构，
未重新检查历史 SXM 图像质量。CLI 的 10 步退针／200 步横移 fake 演练也已保存运行数据库。

完整闭环仍需当前设备的组合验收、新扫描/谱的质量判断和闭环策略；不能把服务存在等同于实验完成。
memory 的检索/晋升机制也仍未实现，经验不自动提升为执行规则。
