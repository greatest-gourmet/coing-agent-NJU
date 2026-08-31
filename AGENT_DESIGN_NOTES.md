# Coding Agent 实现原理与答辩要点

> 本文是推免答辩底稿：讲清「市面上 coding agent 怎么实现 → 我的代码落在哪一步 → 为什么不是封装 → 每一步的创新点与解决的问题」。

---

## 一、市面上 code agent 的通用架构

所有 coding agent（Claude Code、Codex、Cursor Agent、Devin、OpenHands、SWE-agent、Aider）本质上都跑在同一条循环上，学术界叫 **ReAct（Reason + Act）循环**，工业界叫 **agentic loop**。差异不在「有没有这个循环」，而在**每一环做得多好**。

```
while 未完成:
    ① 组装上下文    —— 把 system prompt + 工具说明 + 记忆 + 检索到的代码拼成 messages
    ② 模型推理/决策  —— LLM 输出：要么调用某个工具(带 JSON 参数)，要么说"我做完了"
    ③ 解析工具调用   —— 把模型的 JSON 参数解析成真正的 Python 参数，校验合法性
    ④ 本地执行工具   —— 读写文件 / 跑命令 / 搜索 / git，在沙箱里真正落地副作用
    ⑤ 观察结果回填   —— 把执行结果(输出/报错/退出码)作为 tool 消息塞回 messages
    ⑥ 终止判定      —— 判断"任务完成 / 出错 / 超时 / 轮数上限"，决定继续还是退出
```

### 每一步在"干什么"，以及主流产品怎么做的

**① 组装上下文（Context Engineering）** —— 被公认最重要的杠杆。核心矛盾：模型上下文窗口有限，但一个仓库可能有几百万行。
- **Cursor**：代码库索引 + embedding 语义检索，把相关代码片段塞进上下文（`@Codebase`）。
- **Aider**：用 tree-sitter 把仓库解析成调用图，生成紧凑的 **repo map**（函数签名 + 依赖关系）放进上下文，让模型「看到全局结构」而不读全文。
- **Claude Code**：读 `CLAUDE.md` + 文件系统探索 + 长期记忆，并在上下文变长时做压缩/摘要。

**② 模型推理/决策** —— 一次带 `tools` 参数的 `chat.completions` 调用，模型返回 `tool_calls`（工具名 + JSON 参数字符串）。这是模型厂商提供的**唯一原语**，也是题目允许依赖的唯一东西。

**③ 解析工具调用** —— 模型给的 JSON 参数字符串可能是非法的（不是合法 JSON、类型不对、缺必填字段）。健壮的 agent 必须自己兜住，而不是直接 crash。

**④ 本地执行工具** —— 真正读写文件、跑 `subprocess`。关键词是**安全**：路径不能越界（防止模型读 `.ssh` 下的密钥）、命令要有超时和输出上限、危险操作要拦截。

**⑤ 观察回填** —— 把结果（包括报错、退出码）原样喂回去。这是 agent「能自我纠错」的来源：模型看到测试失败 → 改代码 → 重跑。

**⑥ 终止判定** —— 最容易被新手忽略、但最关键的工程点。没有它，agent 会无限循环或原地打转。

---

## 二、六步循环 → 我的代码映射（精确到函数）

整个循环的**唯一入口**是 [agent.py](coding_agent/agent.py) 里 `AgentRunner.run()` 的第 110 行 `while True:`。六步都在这个循环体里发生，但「干活」的函数分散在三个文件。

| 循环步骤 | 触发点（agent.py 内） | 真正干活的函数 |
|---|---|---|
| ① 组装上下文 | `run()` 第 119–126 行 | [memory.py](coding_agent/memory.py) `prepare()` + [tools.py](coding_agent/tools.py) `schemas()` |
| ② 模型推理/决策 | `run()` 第 126 行 | [llm_client.py](coding_agent/llm_client.py) `complete()` |
| ③ 解析工具调用 | `run()` 第 171 行 | `_parse_arguments()`（agent.py）+ `_validate_arguments()`（tools.py） |
| ④ 本地执行工具 | `run()` 第 193 行 | `ToolRegistry.execute()`（tools.py） |
| ⑤ 观察结果回填 | `run()` 第 194、209 行 | `observe_tool()`（memory.py）+ `_tool_message()`（agent.py） |
| ⑥ 终止判定 | `run()` 第 111–224 行的多个 `return` | `_stopped()`（agent.py） |

### ① 组装上下文

入口 [agent.py:119-126](coding_agent/agent.py#L119-L126)：

```python
context_info = self.memory.context_info(messages)
request_messages = self.memory.prepare(messages)      # 压缩后的 messages
response = self.model.complete(request_messages, tools=self.registry.schemas())
```

- **system prompt**：`run()` 第 80 行 `messages.append({"role": "system", ...})` 拼进第一条。
- **记忆/压缩**：[memory.py](coding_agent/memory.py) `prepare()`（71–84 行）——没超限就原样返回全部 messages；超限就保留 `system + 第一条 user + 最近 8 条`，中间插一条 `summary_message()`（47–52 行，把工作记忆序列化成一条 system 消息）。
- **工具说明**：[tools.py](coding_agent/tools.py) `schemas()`（67–68 行）——把注册时存的每个工具的 JSON Schema 列表返回，作为 `tools=` 参数发给模型。

> 我的实现里**没有「检索代码」这一环**（`prepare` 只做压缩，不做语义检索）。这是相对 Cursor/Aider 的简化点：上下文来自「系统提示 + 模型自己用 `list_files`/`read_file` 按需读」，属于**探索式**而非**检索式**的设计选择。

### ② 模型推理/决策

入口 [agent.py:126](coding_agent/agent.py#L126)，实现是 [llm_client.py](coding_agent/llm_client.py) `OpenAICompatibleClient.complete()`（64–120 行）：

- 把 `messages` + `tools` 打包，调 `client.chat.completions.create(...)`（82 行）。
- 把 HTTP JSON 响应**归一化**成本地类型 `ModelResponse`，其中 `tool_calls` 被解析成 `ToolCall(id, name, arguments_json)` 元组。
- 这一层**只发请求、只归一化，不做任何编排**（文件头注释写明 `without agent orchestration`）。

### ③ 解析工具调用

入口 [agent.py:171](coding_agent/agent.py#L171)，实际有两道校验：

1. **JSON 层**：[agent.py](coding_agent/agent.py) `_parse_arguments()`（240–248 行）——`json.loads(call.arguments_json)`，失败返回 `(空, 错误信息)`；成功且是 dict 返回 `(参数, None)`。
2. **Schema 层**：[tools.py](coding_agent/tools.py) `_validate_arguments()`（107–129 行）——执行前校验必填字段、`additionalProperties`（未知字段）、每个参数的类型。

两道在 `run()` 第 189–193 行汇合：解析失败 → 造 `ToolResult(ok=False)` 回给模型，让模型「只修正本次调用」；通过 → 才真正 `registry.execute()`。

### ④ 本地执行工具

入口 [agent.py:193](coding_agent/agent.py#L193) `result = self.registry.execute(call.name, arguments)`，实现是 [tools.py](coding_agent/tools.py) `ToolRegistry.execute()`（70–105 行）：

1. 查 handler 是否存在（未知工具 → 报错）。
2. 调 `_validate_arguments` 兜底校验。
3. **快照**：若工具是 `write_file`/`apply_patch`，先 `_safe_path` 校验路径并读旧内容存 `_snapshots`（81–88 行）。
4. 调 handler 落地副作用，用 `try/except` 兜住异常转成 `ToolResult`（91–99 行）。
5. 打 `duration_ms`、`revision`、`rollback_available` 元数据。

七个 handler 都在 tools.py：`_list_files`、`_read_file`、`_write_file`、`_apply_patch`、`_run_command`、`_finish`、`_make_rollback`。路径安全统一走 `_safe_path()`（139–147 行）。

### ⑤ 观察结果回填

这一步在**两处**同时发生，入口都在 `run()`：

- **写回对话**：[agent.py:209](coding_agent/agent.py#L209) `messages.append(self._tool_message(call, result))`，由 `_tool_message()`（250–257 行）构造 `role: "tool"` 消息，`content` 是 `json.dumps(result.to_dict())`——模型下一轮能「看到」输出、报错、退出码。
- **写回工作记忆**：[agent.py:194](coding_agent/agent.py#L194) `self.memory.observe_tool(call.name, result.to_dict())`，由 [memory.py](coding_agent/memory.py) `observe_tool()`（29–45 行）用**代码**更新结构化状态：改过的文件、跑过的测试、当前错误、下一步。

> 这两条线对应「对话历史」和「工作记忆」双层：一个喂给模型看，一个给程序记账。

### ⑥ 终止判定

没有单一函数，而是 `run()` 里**散落的多个 `return` 分支**，统一收口在 `_stopped()`（[agent.py:259-277](coding_agent/agent.py#L259-L277)）：

| 判定条件 | 位置 | 结果 |
|---|---|---|
| 达到最大轮数 | [agent.py:111-112](coding_agent/agent.py#L111-L112) | `_stopped(..., "达到最大轮数")` |
| 超过总运行时间 | [agent.py:113-114](coding_agent/agent.py#L113-L114) | `_stopped(..., "超过总运行时间")` |
| 模型调用抛异常 | [agent.py:127-129](coding_agent/agent.py#L127-L129) | `_stopped(..., "模型调用失败")` |
| 模型没调工具（也没 finish） | [agent.py:147-158](coding_agent/agent.py#L147-L158) | `ok=False`，`stop_reason="模型未调用工具"` |
| 工具调用数超限 | [agent.py:162-163](coding_agent/agent.py#L162-L163) | `_stopped(..., "达到最大工具调用数")` |
| 连续重复调用超限 | [agent.py:165-169](coding_agent/agent.py#L165-L169) | `_stopped(..., "检测到重复工具调用")` |
| **成功结束**（finish 工具） | [agent.py:211-222](coding_agent/agent.py#L211-L222) | `ok=True`，`stop_reason="finish"` |
| 解析错误次数过多 | [agent.py:223-224](coding_agent/agent.py#L223-L224) | `_stopped(..., "解析错误次数过多")` |

`_stopped()` 只做两件事：构造 `AgentResult(ok=False, ...)`，调 `_record_finished()` 打日志/写 trace。

---

## 三、为什么这不是「在现成 agent 上封装界面」

**一句话**：封装 = 把别人的 agent 进程/API 包一层壳，让它在我的界面上跑；**我做的是——只拿走模型厂商提供的「一次 chat 调用 + 原生 tool_calls 字段」这一个原语，把决定 agent 行为的所有东西（主循环、工具、记忆、终止、错误处理）全部自己写。**

对照题目原文边界，逐条自证：

| 题目规定 | 我的代码 | 结论 |
|---|---|---|
| 禁用 agent 框架/SDK（LangChain、OpenAI Agents SDK、Claude Agent SDK…） | `llm_client.py` 使用 Python 标准库直接 POST `/chat/completions`，不依赖任何模型或 Agent SDK | ✅ 合规 |
| 禁用「在现成 agent 产品上封装界面」 | 没有 import/调用任何 agent 产品 | ✅ 合规 |
| 禁用服务端托管的代码执行（Code Interpreter、Files API） | 文件操作是 `pathlib` 本地读写，命令是本地 `subprocess.run` | ✅ 合规 |
| 必须自写：**对话历史与上下文管理** | [memory.py](coding_agent/memory.py) | ✅ |
| 必须自写：**工具定义与本地执行** | [tools.py](coding_agent/tools.py) | ✅ |
| 必须自写：**模型输出解析** | [agent.py](coding_agent/agent.py) `_parse_arguments` | ✅ |
| 必须自写：**循环终止条件** | [agent.py](coding_agent/agent.py) `AgentLimits` + `_stopped` | ✅ |
| 必须自写：**错误处理** | 三个文件里遍布的 `ok/error` + 解析错误限次 + 异常兜底 | ✅ |

**反证法**：如果只是封装，我的 `agent.py` 会是一句 `subprocess.run(["claude", ...])` 或 `client.agents.run(...)`，而不会是一个 300 行的 `while True` 循环里手写「组装上下文 → 调模型 → 解析 → 执行 → 回填 → 判终止」。`llm_client.py` 文档字符串第一句就写明了边界：*"A thin OpenAI-compatible chat client **without agent orchestration**"*——它刻意只做「发请求 + 归一化」，不包含任何编排逻辑。这恰恰证明：编排是我自己写的，模型层只是被调用的一个函数。

---

## 四、每一步的创新点 + 解决了当前 coding agent 的什么真实问题

每一项都是通用 coding agent 都踩过的坑，对应我代码里的具体设计。

1. **手写主循环 + 显式资源上限** —— 解决「agent 失控/死循环」。`AgentLimits` 同时约束最大轮数、最大工具调用数、总时长、最大解析错误数，触发即 `_stopped` 并报告明确 `stop_reason`。这是 `while True` 的「刹车系统」。

2. **工具参数校验 + 错误反馈限次修复** —— 解决「模型吐非法参数直接崩」。`_parse_arguments` 兜 JSON 错误；`_validate_arguments` 执行前统一校验必填/类型/未知字段；失败回给模型「只修正本次调用」，并设 `max_parse_errors=3` 防无限重试。

3. **路径沙箱（`resolve` + `relative_to`）** —— 解决「越界读写/路径穿越」。`_safe_path` 把任何路径 `resolve()` 后 `relative_to(workdir)`，越界立即抛错，所有文件工具都走这道闸门。

4. **写入前快照 + rollback** —— 解决「不可逆破坏」。`write_file`/`apply_patch` 执行前自动存快照，`rollback` 工具可撤销。这是**自己实现的版本控制**，不依赖 git——又一例证「不是封装」。

5. **三层上下文 + 确定性压缩** —— 解决「长任务上下文溢出/关键信息丢失」。`prepare()` 超限保留 `system + 首条 user + 最近 8 条`，`summary_message()` 注入程序维护的工作记忆；且 `prepare()` 是纯函数、确定性，可测试可复现。

6. **显式工作记忆（changed_files/current_error/next_step）** —— 解决「模型健忘」。`observe_tool()` 用代码（而非模型自己记）维护结构化状态——「程序知道的确定事实」比让模型靠回忆可靠。与 Aider repo map、Claude Code memory 同一思路。

7. **命令沙箱（危险命令黑名单 + 环境变量清洗）** —— 解决「恶意命令 + 密钥泄露」。`_DANGEROUS_COMMANDS` 拒绝格式化/关机/递归删除；`_clean_environment()` 把含 KEY/TOKEN/PASSWORD/SECRET/AUTH 的环境变量从子进程剥掉再执行。

8. **重复调用检测** —— 解决「模型原地打转」。`(call.name, call.arguments_json)` 作 key 连续计数，`max_repeated_calls=3` 触发终止。用「规范化参数」而非原文，避免换空格就绕过。

9. **脱敏 trace** —— 解决「可复现调试 vs 密钥安全」的矛盾。`redact()` 递归脱敏（key 名匹配 + `sk-...`/`Bearer ...` 值模式），trace 落 `workdir/trace/` 不入库。

10. **OpenAI 兼容 + 可注入 HTTP transport** —— 解决「供应商锁定 + 不可测试」。支持 `OPENAI_BASE_URL`/`OPENAI_MODEL` 走任意兼容端点；构造器接受 `http_post` 注入，让测试使用假 HTTP 响应而不发真实请求。

11. **`finish` 作为显式终止信号** —— 解决「模型何时算完成」。把「结束」做成工具 `finish(summary, tests)`，模型必须显式调用并附测试结果才算成功（`stop_reason="finish"`），比「看模型没调工具就假设完成」严谨。

12. **跨任务会话状态隔离** —— 解决「连续多任务上下文污染」。`run()` 检测上一任务结尾的 `finish` 交换并 `messages[:-2]` 剥离，再注入「新用户请求」提示。只有真正写过连续会话 agent 的人才会碰到的细节。

---

## 五、答辩话术

> **「你这不是就是套壳 Claude Code 吗？」**
> 不是。封装是对别人的 agent 做二次包装，我只用了模型厂商的**原生 tool calling 这一个原语**——一次 `chat.completions` 调用加返回的 `tool_calls` 字段。决定 agent 行为的**主循环、工具定义与本地执行、参数校验、上下文管理、终止条件、错误处理、回滚、脱敏**，全部是我手写的。题目要求的五项「必须自写」逻辑，每一处都有对应源码文件，且有单元测试覆盖。

> **「为什么要自己写主循环，不直接用 SDK 的 agent？」**
> SDK 的 agent 是黑盒，我无法控制它的循环终止条件、上下文怎么压缩、工具怎么校验——而这恰恰是 agent 质量的核心。自写让我能对每一步负责，也能解释为什么它这样运转。这正符合题目「为设计决策给出辩护」的要求。

> **「你最大的设计决策是什么？为什么？」**
> ① **三层上下文 + 确定性压缩**（上下文工程是公认第一杠杆，我把它做成了可测试的纯函数）；② **路径沙箱 + 快照回滚**（自写文件工具的安全责任全在自己，不能依赖任何托管执行）。
