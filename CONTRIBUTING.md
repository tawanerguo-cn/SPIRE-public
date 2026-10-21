# 贡献 SPiRE

SPiRE 当前支持 STM/Nanonis。改动应进入唯一负责它的 `src/` 层，并复用既有 Nanonis device、SafetyGate、workflow、RunStore 和 `ToolRegistry`。适配另一间 STM 实验室通常先使用独立 profile 和本地连接/路径配置，不需要改核心源码。

## 本地开发

Python 3.11 或更高版本：

```sh
python -m venv .venv
python -m pip install -e ".[agent,validation]"
python -B -m unittest discover -s tests -v
```

只改分析、数据解析或基础设备模块时，按 `pyproject.toml` 的 optional dependency 分组按需安装。常规测试使用 fake backend，不能在测试或提交时连接现场仪器。

## 改动要求

- 修改前先读 [Codex 适配指南](docs/codex-adaptation.md) 和相关架构/知识入口，检查现有调用方、文件格式和保存路径。
- Nanonis 命令与回包解析留在 `instrument_layer`；科学计算放 `analysis_layer`；实验顺序及停止/失败行为放 `workflow_layer`；CLI/MCP 通过 `runtime_layer` 使用已有接口。
- 新仪器分支在当前驱动路径外增加 fake 覆盖，测试成功回包、字段缺失或类型改变、物理量单位、上下边界及停止行为。涉及 live 时新增明确 opt-in 测试；完整离线套件不得连接设备。
- 更新协议/schema 时检查读取旧格式、单位、index 身份、数据来源和中断后的部分结果。不重复计算或持久化同一字段含义。
- 站点历史参数、样品评价和现场记录标明型号、版本、信号定义及条件。公开材料不能引用随仓库一并撤下的原始数据；保留方法时说明证据范围。
- 提交前执行 `git diff --check`，复核 `git status --short` 和文件 diff，确认没有私有 TOML、credential、绝对路径、实验数据、RunStore、原始日志或发布包。

## 安装可选功能

- Nanonis live 驱动：`python -m pip install -e ".[live]"`
- Codex MCP 工具：`python -m pip install -e ".[agent]"`
- OpenAI Responses API：`python -m pip install -e ".[api]"`；真实 API 调用需操作者自行提供 API 凭据，并由使用者承担请求成本。

optional extra 只为用户明确选择的功能安装。不要要求 Codex MCP 接入实验数据一定使用模型 API key。
