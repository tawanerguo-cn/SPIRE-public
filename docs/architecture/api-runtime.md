# 独立模型 API 运行入口

现在可以由 SPiRE 自己调用 OpenAI Responses API，再将模型的工具请求交回现有 workflow。
不需要 Codex CLI、MCP 客户端或 ChatGPT 登录。现有 CLI/MCP 继续可用。
当前这三个入口提供的仍然是离线数据分析和 fake 演练，没有新增 live 动作。

## 安装与第一次运行

在仓库根目录的本机 Python 环境中安装可选依赖：

```powershell
python -m pip install -e ".[api]"
```

在本机环境变量 `OPENAI_API_KEY` 中配置自己的 API key。模型 ID 由操作员明确选择，
需要支持 Responses function calling；观察图片还需要视觉输入。下面的 `<模型ID>` 需要替换。
仓库不保存凭据，也没有预设昂贵模型。基础 CLI/MCP 不读取 API key 或导入 OpenAI SDK。

```powershell
python -B -m runtime_layer.agent_cli --api --model "<模型ID>" --read-root ".\tests\fixtures" --output-root ".\tmp\api-agent" --prompt-file ".\config\agent-task.example.txt"
```

此命令会发起真实、计费的模型请求，示例任务只查询已登记的能力，不执行 fake 实验。
API 安装包含 Matplotlib，方便后续传图；MCP 另用 `.[agent]` 安装。
Pydantic 是基础依赖，用同一份函数签名生成参数 schema 并进行本地严格校验。

分析实验数据时，将 `--read-root` 换成允许读取的实际数据目录，可重复指定多个。
在 UTF-8 `--prompt-file` 中写任务和精确文件路径；不提供该选项则从 stdin 读取任务。
SPiRE 会将任务、工具返回的数据和图片发送给配置的模型 API。派生文件写在独立 `--output-root` 下。
没有任意文件浏览、shell、Python 执行、桌面控制或修改安全门的模型工具。

不请求模型也能导出完整工具 schema：

```powershell
python -B -m runtime_layer.agent_cli --list-tools --read-root ".\tests\fixtures" --output-root ".\tmp\api-agent"
```

## 单一实现与分层

| 内容 | 权威实现 | 调用方 |
| --- | --- | --- |
| 工具名称、描述与访问提示 | `knowledge_base/script_manifests/spm_agent_tools.toml` | 公共 registry、能力查询 |
| 参数类型与默认值 | `runtime_layer.spm_tools.SPMTools` 的 typed 方法签名 | `ToolRegistry` 生成 schema 并校验 |
| 白名单分发、参数/执行错误封装 | `runtime_layer.tool_registry.ToolRegistry` | CLI、MCP、API |
| workflow、文件范围、fake 互斥与图片落盘 | 已有 SPMTools/workflow | 所有入口 |
| 模型任务指令 | `knowledge_base.agent_prompt` | API runtime |
| 模型 HTTP 请求、上下文与预算 | `runtime_layer.openai_agent` | CLI `--api` 或 Python 调用者 |
| 会话证据持久化 | 已有 `data_layer.recording.RunStore` | API runtime 与原有 workflows |

没有复制 Nanonis wrapper、SafetyGate、实验循环、MCP 控制栈或数据库格式。
MCP 直接发布公共 registry 的 schema，避免框架的隐式参数转换使 CLI/API/MCP 行为不同。
参数多余字段、数字字符串、把布尔值当整数、非有限值和非 JSON object 都会被拒绝；
实验单位、参数硬边界和动作前提继续由已有 workflow/SafetyGate 校验。
工具 JSON schema 不是另一套实验安全策略。

当前唯一实现的模型协议是 OpenAI Responses。可以向 Python `run_openai_agent` 注入相同协议的客户端，
用于测试或显式配置端点，但这不代表已支持 Anthropic、Gemini 或任意 OpenAI-compatible 服务。
以后接其他协议应适配此 registry 和 `ToolOutput`，不要复制工具清单或仪器实现。

## 会话行为与预算

API runtime 保留完整的 response output，包括 reasoning 上下文、function call 和 assistant message。
工具的 JSON 与原生 PNG 一起作为 `function_call_output` 传回，模型能看到图像，而非仅看到本地路径。
请求设置 `store=False`，上下文由本地会话传递；这不等于声明服务商所有日志均不保存。
工具设置 `strict=False` 以保留可省略的 Python 默认参数和开放字典；本地校验始终严格执行。

| 参数 | 默认值 | 语义 |
| --- | --- | --- |
| `--max-turns` | 8 | 最多 8 次模型请求；最后一次如仍要调用工具，停止并标记预算耗尽 |
| `--max-tool-calls` | 16 | 执行前检查整批调用预算，包含参数错误的调用 |
| `--max-output-tokens` | 4096 | 每次模型响应的输出 token 上限 |
| `--max-input-bytes` | 16777216 | 每次完整请求 JSON 的 UTF-8 大小上限，含图像与历史 |
| `--timeout` | 120 秒 | SDK 单次 HTTP 请求超时设置 |

这些是调用预算，不是总金额或实验实时中断保证。工具运行时间仍由各 workflow 约束。
运行器不自动重试 HTTP 请求、不自动 resume 或重放旧 run；重复 call ID 在执行前拒绝。
参数/工具错误作为结构化结果交回模型，可供修正参数或检查失败证据。prompt 要求失败 workflow
先检查证据，不自动重试；这条解释规则不代替 workflow/SafetyGate 的代码边界。
不完整响应不执行其中的工具请求。HTTP 失败、拒答、空响应、Ctrl+C、预算耗尽分别留下终止原因。

## 记录和回读

每次运行在 `output-root/agent_runs/年/月/日/run-.../` 新建已有格式的 `run.sqlite` 和 manifest。
事件包含任务与配置、工具 schema、请求轮次、模型响应/usage、工具开始/结果、PNG 路径与最终报告。
工具调用前先提交开始事件，之后的 API 失败不会抹掉已提交的工具结果。
模型 SDK 异常仅记录异常类型，避免异常字符串带出请求正文或凭据。
日志包含任务、数据与模型上下文；API key 不写入日志。

stdout 返回 JSON：`status`、`reason`、`report`、轮数/调用数、错误计数和数据库位置。
`complete` 只表示得到模型最终答复，不表示实验成功；所有结果带
`report_scope=model_interpretation_not_experimental_validation` 和 `authorizes_live_action=false`。
其余终止状态 CLI 返回非零退出码。

将返回的 `database_path` 放进 JSON 参数文件，通过现有工具回读：

```powershell
python -B -m runtime_layer.agent_cli --tool inspect_workflow_run --arguments-file ".\tmp\inspect-agent-run.json" --read-root ".\tests\fixtures" --output-root ".\tmp\api-agent"
```

## 验证与范围

新增测试用真实 OpenAI Python SDK 加本地 `httpx.MockTransport` 验证请求/响应序列，
覆盖工具 JSON、扫描 PNG、多轮 reasoning 保留、持久化、重复 ID、预算、不完整响应、429、超时和中断。
原有 stdio MCP 集成测试继续运行，并逐项比较 MCP 与公共 registry 的 schema，验证类型错误不触发 workflow。
这些测试不需要凭据、不消耗 API 额度、不连接仪器。

尚未验收真实账号/模型的付费调用、Windows 本机运行或 live 闭环实验；
没有实现长任务恢复、实时流式输出、自动专家知识检索或跨模型协议适配。
本轮重构集中在 agent 入口与共享运行逻辑，未宣称完成整仓的全部技术债清理。

协议依据：[OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling)、
[reasoning 上下文](https://developers.openai.com/api/docs/guides/reasoning)。
