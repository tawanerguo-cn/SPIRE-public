# 用 Codex 适配本站 Nanonis

本指南让 Codex 帮你检查已有的 Nanonis 适配点，并将本站信息写入独立的私有配置。SPiRE 目前提供 STM/Nanonis 实现；其他仪器型号、信号目录、固件和操作方式须逐项核对。Codex 的回答、离线测试或 profile 文件都不能单独证明硬件已验收。

## 先跑离线检查

在仓库根目录安装基础软件并创建本站配置：

```sh
python -m pip install -e .
spire init --directory .local/lab --profile-id my_lab
spire doctor --site .local/lab
spire demo --output .local/demo
```

`spire init` 仅能创建新目录。profile 起始状态为 `draft`，阈值是待核对的示例值。默认 Nanonis backend 为 `fake`；离线自检与演示没有设备连接步骤。

如果你还没拿到真实的 host、Signals Manager 名称、单位或 Nanonis 版本，先将缺项列入适配矩阵，不要根据文件名或历史复盘补猜。对于 host、账号、地址、样品身份和真实数据目录，使用私有本地文件或实验室的私有配置存储，勿写入 issue、公开源代码、提交消息或发给外部 API。

## 让 Codex 开始工作的提示词

在仓库内开启一个 Codex 任务，可提供下面这段要求并填写你的本地配置路径：

```text
请协助适配这个 checkout 中的 STM/Nanonis 设备接口。

开始前请阅读 AGENTS.md、docs/getting-started.md、docs/operations/authorized-control.md、
docs/architecture/instrument-services.md 以及与本设备有关的 src/knowledge_base 条目。
先运行 `spire doctor --site .local/lab`、`spire demo --output .local/demo-check` 和离线测试。

当前授权范围：仅代码检查与离线 fake 测试。尚未授权连接真实设备或发出任何仪器动作。
请不要用模型记忆或旧站点 profile 猜 host、端口、信号、阈值或设备能力；把缺失的现场事实
列成需操作员提供的项目。所有站点值保存在被 .gitignore 忽略的独立目录，禁止提交真实
地址、帐号、样品/操作者身份、原始数据、RunStore 数据库或本地路径。

请沿现有实现寻找唯一设备接口，先输出适配矩阵，逐项记录：本站已提供的事实及来源、
当前代码入口、需要的改动、离线/现场验收方法和仍未知的事项。优先检查 Nanonis 版本/API
解析、PI host/port（只保存在本地）、Signals Manager 名称和单位、recording profile、
当前样品对应的电流和 Z 范围、数据读取/输出目录。不得复制设备控制层、安全门、工具注册
或记录器。

只实施站点配置能够表达的适配。如果驱动/API 确有缺口，先说明本地回读证据、官方接口
来源和失败案例，再用 fake 增加同一协议路径的回归测试。不要扩大任何 SafetyGate 边界、
live_enabled、profile 的适用范围，或写出可以直接替用户启用 live 的配置。

完成后运行相关 fake 测试并报告具体命令和结果。即使离线测试通过，本任务也没有授权
真机操作。需要只读连接验证或受限动作验收时，请按具体设备、动作、参数和停止方式说明
下一步所需的人工授权；在授权到达前不进行连接、扫描、测谱、移动、进退针或 pulse。
将通过验证的事实与推断分别记录，并标明实验室、Nanonis版本、配置范围和未验收项目。
```

改成 `[已授权事项]` 后，授权只覆盖被明确写出的设备、动作、参数和时段。Codex 应在执行该范围之前读回实际参数，并在执行后复核返回值、动作审计及设备状态。

## 适配矩阵

下表的源码路径指示事实归属。不要将某台设备的数值复制为所有用户的默认值。

| 适配项 | 本站需要提供的证据 | 主要实现位置 | 可检查结果 |
| --- | --- | --- | --- |
| 软件与接口 | Nanonis/驱动版本、PI 接口版本、具体失败回包或文档章节 | `src/instrument_layer/backends.py::OfficialNanonisBackend`、`src/instrument_layer/nanonis_device.py::NanonisDevice` | 驱动方法绑定、原始数据类型与单位解析结果 |
| PI 连接 | 私有 host、port、timeout 与启动方式 | `src/instrument_layer/config.py::load_connection_config`；`.local/lab/connection.toml` | `spire doctor` 解析出的 backend 和配置问题；offline 仍是 fake |
| 通道 | Signals Manager 中逐字 signal 名称、单位、是否为实时值、是否经过缩放 | `NanonisDevice.resolve_signals`、`NanonisDevice.read_resolved_signals` | 解析到的索引、通道名、单位与回读值；代码必须保留未知项 |
| 记录与保护判据 | 本实验确切的 I/X/Y/Z 与锁相信号、I 与 Z 量程、settling 和 gate 证据 | `.local/lab/profiles/my_lab.toml`；`src/runtime_layer/recording_control.py::load_recording_settings`；`src/workflow_layer/recording_gate.py::TipRecordingGateMonitor` | 单位一致、输入范围验证、接近样品/远离样品边界 fake 回归 |
| 路径和来源 | 获准读取的根目录、独立输出根目录、本站是否保存 session path | `spire-agent --read-root/--output-root`；recording root；`src/data_layer/recording.py` | 输入只限显式根目录；输出仅在指定根下；原始数据来源可查 |
| 操作能力 | 哪些已包装方法可用、由谁授权、SafetyGate 的当前默认值 | `src/instrument_layer/capabilities.py`、`src/instrument_layer/safety.py`、`src/instrument_layer/nanonis_device.py` | capability 输出列出的软件能力与边界；不代表真机可用 |
| 科学判据 | 样品、测量量、单位、样本和独立复核方式 | `src/analysis_layer/`、`src/workflow_layer/`、对应 `src/knowledge_base/` 条目 | 算法回归、适用范围和观测结论；保留假设与不确定性 |

如果新增实时通道或协议分支，需要确认全部实际调用方仍通过唯一 typed interface；对应 fake 应覆盖成功、缺失/畸形回包、边界值和中止情形。硬件读回内容与科学解释分别陈述。

## 分阶段验收

**阶段一：离线适配。** 检查 site TOML/schema、fake backend 和路径限制；跑 `spire doctor`、`spire demo` 及相关测试，最后运行完整离线测试。开发测试环境需要 `agent`、`api`、`validation` 和 `live` 依赖：`python -m pip install -e ".[agent,api,validation,live]"`。普通 API 测试使用 mock，真机 smoke test 默认关闭。输出适配矩阵及每项结果。通过只表示代码和配置符合离线契约。

**阶段二：受授权的只读现场核对。** 操作员明确授权只读连接后，确认 host、port 和 Nanonis 版本。再解析一组确切的 Signals Manager 名称，读取一次 Current 和 Z，并将读回单位、上下文、设备时间与预期比较。不得启动 scan、spectroscopy、feedback、Auto Approach、移动或写配置。记录 API 名称、错误码、时间、回读和未解决字段；断连或回读不确定时停止并由操作员检查设备。

**阶段三：单项受限动作验收。** 只有操作员针对某个动作及其参数、范围、停止方式和恢复方式再次明确授权后，才允许验收这一个动作。执行前核对实际 SafetyGate 和 `live_enabled`；执行后独立读取状态并检查动作审计。通过一项单步不代表整条 workflow 或其他设备已验收，也不自动授予后续动作权限。

如果使用 live 录制 workflow，profile 的 status 必须从 `draft` 经证据审阅后改为 `reviewed`；此状态检查针对录制链。其他 typed live 读取或动作仍受各自的 SafetyGate 与操作员授权管理，profile 状态不会替它们授权，也不能替代其独立验证。不要为解除录制链的 `draft` 错误而删除检查。

## 问题定位

- `No module named 'mcp'`：MCP 工具需要可选依赖，运行 `python -m pip install -e ".[agent]"` 后重试。
- `No module named 'nanonis_spm'`：live Nanonis backend 需要 `python -m pip install -e ".[live]"`。只做离线分析或 fake 演练无需安装它。
- `live recording requires a reviewed instrument profile; draft profiles are limited to offline and fake rehearsals`：这个检查属于 live 录制 workflow。核对本站 profile 和该录制的适用条件，留存现场审阅证据后再把本站 status 更新为 `reviewed`；其他现场连接与动作仍需各自权限。
- `profile.id does not match its requested id`：`recording.toml` 中的 `instrument_profile` 与 profile 文件名、`[profile].id` 必须一致。
- live 提示连接失败：先在本站核验 PI server 是否启用、host/port 是否正确、运行 Codex/MCP 的用户进程是否可访问设备网络，以及适用的 Python/驱动组合。不要输出或提交真实地址。
- `required telemetry signals are unavailable: ... signal_not_found`：逐字核对 Signals Manager 名称及单位，区分物理信号与索引；不要靠历史 index 猜当前通道。
- 官方驱动回包结构与 fake 预期不同：保存匿名化的回包形状/字段和官方文档出处，再在 backend/parser 归一层加 fake 回归测试。不要改 SafetyGate 容差来掩盖解析错误。
- profile 被加载到旧设备或路径指向默认值：检查 `SPIRE_CONNECTION_CONFIG`、CLI 参数、cwd、配置相对路径解析位置和私有 profile 目录，运行 `spire doctor --site` 并检查 JSON 输出。

自检只读取软件状态和配置契约；它不会证明 Programming Interface 当前在线，也不会验证实验物理结果。
