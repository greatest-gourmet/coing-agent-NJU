# 自研 Coding Agent

一个从零实现的本地编程智能体。它通过 OpenAI-compatible Chat Completions 与大语言模型交互，由本项目自行完成 Agent 主循环、工具注册、参数校验、上下文管理、错误反馈、记忆、回滚和任务终止，不依赖 LangChain、LlamaIndex、OpenAI Agents SDK、Claude Agent SDK、AutoGen、CrewAI 等 Agent 框架或 SDK。

## 1. 项目定位

本项目不是在现成 Agent 产品上封装界面，而是将模型通信与 Agent 编排明确分离：模型只负责提出下一步动作，所有本地副作用均由受控的 Python 工具执行。系统面向“读取项目—修改代码—运行测试—根据结果继续修复”的编程任务，强调可解释、可验证和可恢复。

## 2. 技术栈

- Python 3.10+
- Python 标准库：`urllib`、`subprocess`、`pathlib`、`json`、`dataclasses`
- OpenAI-compatible Chat Completions 原生 HTTP
- 本地文件系统和子进程工具
- JSONL 记忆与运行轨迹
- 可选本地 OCR Skill（图片/PDF 转结构化文字）

模型客户端只发送 HTTP 请求和工具 Schema，不使用模型厂商的 Agent SDK，也不使用 Code Interpreter、Files API 等服务端托管工具。

## 3. 快速运行

### 配置模型

PowerShell：

```powershell
$env:OPENAI_API_KEY="your-api-key"
$env:OPENAI_BASE_URL="https://api.deepseek.com/v1"
$env:OPENAI_MODEL="deepseek-chat"
```

### 单任务模式

```powershell
python main.py --workdir .\demo_workspace --verbose "检查项目并运行测试，修复发现的问题"
```

### 多轮模式

```powershell
python main.py --chat --workdir .\demo_workspace --verbose --trace
```

进入交互模式后可以连续输入任务；`exit` 或 `quit` 退出。工作目录不存在时会自动创建。`--trace` 将脱敏轨迹写入 `workdir/trace/session.jsonl`。

## 4. 总体架构

```text
用户任务 / 多轮输入
        │
        ▼
┌──────────────────────┐
│ main.py              │  CLI、工作目录、多轮会话、进度输出
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ AgentRunner          │  ReAct 循环、解析、回填、限制、finish
└──────┬───────────────┘
       ├── WorkingMemory / LessonStore
       ├── OpenAICompatibleClient ── 原生 HTTP ── 模型
       └── ToolRegistry
              ├── 文件工具：list/read/write/apply_patch
              ├── 命令工具：run_command
              ├── 恢复工具：rollback
              ├── 结束工具：finish
              └── OCR 工具：ocr_image
```

## 5. 分层设计

### 5.1 模型边界层：`coding_agent/llm_client.py`

`OpenAICompatibleClient` 只负责模型通信，不负责任务编排：

1. 将消息历史和工具 Schema 序列化为 JSON；
2. 通过标准库 `urllib` POST 到 `/chat/completions`；
3. 对网络异常和 408、429、5xx 等临时错误进行重试；
4. 对响应 JSON、`choices`、`message` 和 `tool_calls` 做格式校验；
5. 将供应商响应归一化为 `ModelResponse` 和 `ToolCall`；
6. 错误信息中脱敏 API Key。

模型层没有 `run()`、`agents`、任务状态或本地工具执行逻辑，因此 Agent 行为完全由本项目控制。

### 5.2 Agent 核心层：`coding_agent/agent.py`

`AgentRunner` 是系统唯一的任务编排入口。单轮流程如下：

```text
准备消息与记忆
    ↓
调用模型
    ↓
解析普通回复或 tool_calls
    ↓
逐个校验并执行工具
    ↓
记录观察结果
    ↓
写回 role=tool 消息
    ↓
进入下一轮，直到 finish 或触发限制
```

每个任务具有显式资源上限：

- 最大轮数：50；
- 最大工具调用数：50；
- 总运行时间上限；
- JSON 解析错误次数上限；
- 连续重复工具调用上限。

模型返回普通文本但没有调用 `finish` 时，任务不会被标记为成功。

### 5.3 工具执行层：`coding_agent/tools.py`

`ToolRegistry` 统一管理工具的注册、Schema 导出、参数校验、执行和结果封装。工具处理函数只返回 `ToolResult`，异常会被转换为结构化错误，避免单个工具崩溃整个 Agent。

内置工具：

| 工具 | 作用 |
|---|---|
| `list_files` | 列出工作目录的直接子项 |
| `read_file` | 按可选行号读取文本文件 |
| `write_file` | 创建文件或在显式允许时覆盖 |
| `apply_patch` | 对唯一匹配的文本执行局部替换 |
| `run_command` | 在工作目录执行命令并返回输出、退出码和耗时 |
| `ocr_image` | 调用本地 OCR Skill 读取图片/PDF 文字 |
| `rollback` | 恢复文件修改前的快照 |
| `finish` | 提交任务摘要和测试结果并结束任务 |

所有工具执行前都经过统一参数检查：必填字段、字段类型、未知字段和路径有效性。`apply_patch` 要求旧文本恰好匹配一次，避免误修改多个位置。

### 5.4 记忆层：`coding_agent/memory.py`、`lessons.py`

系统采用短期记忆和长期经验两层结构。

#### 短期记忆

`WorkingMemory` 维护当前任务的确定性状态：

```json
{
  "task": "当前任务",
  "constraints": [],
  "changed_files": ["a.py"],
  "tests": [{"exit_code": 0, "result": "..."}],
  "current_error": null,
  "next_step": ""
}
```

每次工具执行后由程序更新，而不是让模型自行维护。这样修改文件、测试结果和当前错误等事实具有稳定的数据来源。

#### 长期经验

`LessonStore` 将任务结果保存到：

```text
<workdir>/.agent_memory/lessons.jsonl
```

每条经验包含任务、成功状态、最终摘要和工具观察。新任务启动时按关键词检索相关经验，并作为参考上下文提供给模型。该机制是本地运行时检索，不会训练或修改模型参数。

### 5.5 观测层：`coding_agent/trace.py`

`TraceRecorder` 以 JSONL 记录：

- 任务开始和结束；
- 每轮上下文统计；
- 模型请求和响应；
- 工具参数和结果；
- 退出码、耗时和截断信息；
- 记忆状态和停止原因。

写入前递归脱敏 API Key、Token、Password、Authorization 等字段，并限制单条记录长度，轨迹只用于本地调试和答辩展示。

## 6. 三项核心创新

### 6.1 执行反馈驱动的自我反省

系统不接受模型“已经完成”的自我声明，而要求模型读取真实执行反馈：标准输出、标准错误、退出码、测试结果和文件状态都会成为下一轮 Prompt 的一部分。

```text
提出方案 → 本地执行 → 观察结果 → 模型反省 → 修复/重试/回退 → 再验证
```

因此，即使命令退出码为 0，但输出乱码或结果不符合预期，模型仍可以在下一轮识别问题并修改代码。

### 6.2 双层记忆与跨任务经验复用

对话历史保存完整决策过程，WorkingMemory 保存程序确认的任务事实，LessonStore 保存已经验证过的错误—修复—测试经验。三者结合后，Agent 不仅能在当前任务中纠错，也能在后续相似任务中参考历史经验。

### 6.3 可干预、可恢复、可审计的执行层

工具执行不是模型直达系统，而是经过参数 Schema、路径沙箱和生命周期 Hook。文件修改前创建快照，危险命令被拒绝，执行结果带退出码和耗时，所有过程写入脱敏轨迹；出现错误时可以通过 `rollback` 恢复。

## 7. 上下文工程

上下文未超限时发送完整历史；超限后执行确定性压缩：

1. 保留系统规则和原始任务；
2. 注入结构化 WorkingMemory；
3. 保留最近工具调用及其结果；
4. 将更早消息压缩为过程摘要；
5. 保留最近修改文件、测试记录和当前错误。

压缩不会修改原始历史，只生成本轮发送给模型的有限视图，因此可以同时兼顾可复现性和上下文预算。

## 8. 安全边界

- 文件路径必须解析到 `workdir` 内，阻止 `../` 和绝对路径越界；
- 命令在指定工作目录执行，清理敏感环境变量并拒绝部分高风险命令；
- 工具参数集中校验，未知工具和非法参数返回结构化错误；
- 写入和局部修改前保存快照，支持回滚；
- 工具输出、模型响应和轨迹均进行长度限制；
- 日志和错误信息不打印 API Key；
- OCR 只调用本地脚本，不上传原始图片。

## 9. OCR 扩展

`ocr_image` 调用项目内的 `ocr/scripts/ocr_tool.py`，把图片或 PDF 转成 JSON 文本结果，再交给文本模型分析。它适合：

- Python Traceback 截图；
- 终端输出截图；
- 编程题目截图；
- 扫描 PDF；
- 图片中的中英文文字。

该能力属于本地 OCR 辅助，不等同于通用多模态视觉理解。模型看到的是 OCR 结果和元数据，而不是图片像素。

## 10. 测试

仓库当前只保留运行所需的 Agent 核心、OCR 扩展和文档；演示代码与临时测试案例已移除，后续测试可按上述模块补充。建议至少覆盖 Agent 主循环、工具参数校验、路径沙箱、回滚、记忆压缩、经验召回、HTTP 响应解析和 trace 脱敏。测试应使用 Fake Model 与模拟 HTTP，不依赖真实 API Key。

## 11. 合规说明

题目允许使用模型厂商 API 客户端、OpenAI 兼容网关和原生 tool calling；本项目进一步采用标准库直接发送 HTTP。Agent 主循环、工具定义与本地执行、上下文管理、模型输出解析、错误处理、记忆和终止条件均由本项目自行实现，不调用任何现成 Agent 产品、Agent 框架、Agent SDK、Code Interpreter 或 Files API。
