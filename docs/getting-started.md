# 首次安装与离线演示

本文带你安装 SPiRE、生成独立的实验室配置、检查软件环境并运行一项离线演示。当前发布版的设备路径面向 STM/Nanonis。自检和演示不连接 Nanonis，也不需要 Codex API key。

## 安装

请使用 Python 3.11 或更高版本，并从仓库根目录运行命令。

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
```

macOS 或 Linux：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
. .venv/bin/activate
```

以下命令在激活后的虚拟环境中运行。若 PowerShell 阻止执行 `Activate.ps1`，不要修改全局执行策略；改用虚拟环境内的直接入口，例如 `& ".\.venv\Scripts\spire.exe" init --directory .local/lab --profile-id my_lab`。

## 建立本站配置

`spire init` 在指定的新目录中创建 Nanonis 连接文件、录制配置和仪器 profile。配置默认选用 `fake`，录制开关关闭，profile 标记为 `draft`。该 profile 可用于离线检查和 fake 演练；它不表示本站已经验收，也不能用于 live 录制。

```sh
spire init --directory .local/lab --profile-id my_lab
spire doctor --site .local/lab
spire-recorder --config .local/lab/recording.toml status
```

请保留 `connection.toml` 的 `backend = "fake"`，直到完成离线适配。本站文件存放在 `.local/lab/`；实验室专属路径和阈值不需要写进仓库。确认它们没有进入 Git：

`spire-recorder ... status` 读取配置和本地磁盘状态，不连接 Nanonis，也不启动录制。本站 `recording.toml` 的 master switch 保持 `enabled = false`。

```sh
git status --short
```

初始化目录已存在时，工具会拒绝覆盖，以免覆盖你已有的实验室配置。每个 checkout 可使用不同目录，例如 `.local/lab-device-a` 和 `.local/lab-device-b`。

## 运行离线演示

演示使用 fake Nanonis backend 和已有 workflow，写入一个新的结果目录，并将运行事件保存在 `RunStore` 中。它展示软件记录一次模拟 workflow 的方式；它没有进行物理测量。

```sh
spire demo --output .local/demo
```

可以让 agent 读取当前支持的工具清单。此命令使用空 JSON 参数，不会等待 stdin 或调用模型 API：

```sh
spire-agent --tool describe_instrument_service --arguments '{}' --read-root tests/fixtures --output-root .local/agent-output
```

`tests/fixtures` 是仓库内用于离线验证的现成目录。agent 工具只会读显式列出的 `--read-root`，并在 `--output-root` 下写入派生结果；连接参数等私有站点配置不必加入读取范围。

## 将 Codex 接入离线工具

如果希望 Codex 查看你选定的数据目录并调用 SPiRE 的离线分析和 fake 工具，安装 MCP 依赖：

```sh
python -m pip install -e ".[agent]"
```

先准备已存在的读取目录。示例中以 `.local/data` 为例；请由实验室用户决定哪些数据可交给 Codex 读取。命令会生成供你检查的 MCP TOML，不会修改 Codex 配置：

```sh
spire mcp-config \
  --read-root .local/data \
  --output-root .local/spire-output \
  --site .local/lab
```

将输出片段手动加入你当前使用的 Codex 配置，再重启或重新载入相应 MCP server。SPiRE MCP 使用本机 stdio 启动命令、参数与环境变量；配置的实际字段以 [OpenAI Codex MCP 官方说明](https://learn.chatgpt.com/docs/extend/mcp#stdio-servers)为准。只授权必要的数据目录，先在 Codex 中检查暴露的工具名称和描述。

MCP 使用 Codex 工具连接，不会自动调用模型 API。只有你显式运行 `spire-agent --api` 并提供 API 凭据时，程序才会通过 OpenAI API 发起请求。

## 连接实验室 Nanonis

Live 部署另需安装仓库声明的驱动依赖：

```sh
python -m pip install -e ".[live]"
```

先在本站配置中核对 Programming Interface 的 host 与 port、Nanonis 版本和实际信号名称。可将 `SPIRE_CONNECTION_CONFIG` 指向 `.local/lab/connection.toml`；`NANONIS_HOST`、`NANONIS_PORT`、`NANONIS_TIMEOUT_S` 可覆盖连接字段。环境变量在当前进程和 Codex MCP 中的传递方式取决于实际启动方式，检查 `spire doctor --site .local/lab` 的输出。

将 profile 中需要的记录信号、单位和 gate 条件与本机 Nanonis 配置核对。只有经负责人员审阅并在 profile 中登记相应证据后，才把 `status = "draft"` 更新为 `status = "reviewed"`。这个状态表示本站 profile 信息经过审阅，不替代仪器验收或动作授权。

请先阅读[仪器授权和安全边界](operations/authorized-control.md)，并使用[Codex 适配指南](codex-adaptation.md)分阶段验证。`--live` 会连接现场设备；任何实际动作都需要实验室操作员针对具体设备和动作单独授权。

## 下一步

- 按[Codex 适配指南](codex-adaptation.md)把实验室接口、profile、目录和验收证据逐项登记。
- 阅读[七层架构和仪器服务契约](architecture/instrument-services.md)，定位代码与唯一设备入口。
- 阅读[实验知识库](../src/knowledge_base/README.md)，确认历史经验和科学阈值的适用范围。
- 开发者可从[贡献指南](../CONTRIBUTING.md)开始。
