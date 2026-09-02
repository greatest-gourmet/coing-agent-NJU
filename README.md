从零实现的本地编程智能体，模型通信与 Agent 编排分离，不依赖 Agent SDK。核心是一条 **ReAct 循环**，唯一入口是 `AgentRunner.run()` 的 `while True`，六步如
下，及：
While(未完成) {
1. **组装上下文** — `memory.prepare()` 确定性压缩：超限保留 system + 首条 user + 最近 8 条，中间插 `summary_message()` 工作记忆摘要；工具 Schema 由 `ToolRegistry.schemas()` 提供。
2. **模型推理** — `llm_client.complete()` 带 `tools` 调 `/chat/completions`，`_parse_response()` 归一化成 `ToolCall`，只发请求不做编排。
3. **解析工具调用** — `_parse_arguments()` 做 JSON 层校验（`json.loads` + 正则兜底），失败只把错误回给模型修正本次调用，不 crash。
4. **本地执行** — `ToolRegistry.execute()`：`_validate_arguments()` Schema 校验、`_safe_path()` 沙箱防越界、写前快照、异常转 `ToolResult`、打耗时元数据。
5. **观察回填** — `_tool_message()` 写回 `role=tool` 消息，`observe_tool()` 更新 WorkingMemory，对话历史与工作记忆双层。
6. **终止判定** — 最大轮数 / 时间 / 工具数、重复调用、解析错误过多、`finish` 成功等分支收口 `_stopped()`，提前退出用 `_seal_tool_calls()` 补 tool 回复防 HTTP 400。}

## 总体架构

```text
用户任务 / 多轮输入
        │
        ▼
┌──────────────────────┐
│ main.py              │  CLI、工作目录、多轮会话、进度输出
└──────────┬───────────┘
           ▼
┌──────────────────────┐
│ AgentRunner          │  ReAct 循环、解析、回填、限制、finish
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




## 创新点与解决的问题

**① 执行反馈驱动的自我反省**：不接受模型「我完成了」的自我声明，强制读真实执行反馈（stdout / stderr / 退出码 / 测试 / 文件状态）作为下一轮输入，形成「提出方案→本地执行→观察→反省→修复→再验证」闭环。解决：模型幻觉完成、不验证就交差。

**② 双层记忆与跨任务经验复用**：对话历史存决策过程；WorkingMemory 用结构化状态记录程序确认的事实（改过的文件 / 测试 / 当前错误），以紧凑状态代替重复读盘、节省 token；LessonStore 持久化已验证的「错误→修复→测试」经验并按键词召回，让新任务直接吸取历史教训。解决：每轮从零开始、重复踩坑、上下文无谓膨胀。

**③ 可干预、可恢复、可审计**：执行必经参数 Schema + 路径沙箱 + 生命周期 Hook；改前快照、危险命令拦截、全流程脱敏轨迹、出错可 `rollback` 恢复。解决：Agent 副作用不可控、难回滚、难追溯。

## 快速运行

### 配置模型（PowerShell）

```powershell
$env:OPENAI_API_KEY="your-api-key"
$env:OPENAI_BASE_URL="https://api.deepseek.com/v1"
$env:OPENAI_MODEL="deepseek-chat"
```

### 多轮模式与网页端

```powershell
python main.py --chat --workdir .\demo_workspace --verbose --trace
python -m webui.server --open
```
进入交互模式后可连续输入任务；`exit` 或 `quit` 退出。工作目录不存在时自动创建。`--trace` 将脱敏轨迹写入 `workdir/trace/session.jsonl`。


## OCR 读图

`ocr_image` 工具调用项目内离线 OCR Skill（`ocr/scripts/ocr_tool.py`），把图片 / PDF 转成结构化文字交给纯文本模型，让文本模型也能「读图做题」（截图 / Traceback / 扫描 PDF）。 

引擎按 `win → rapid → tess` 自动择优，均离线：

- `win`：Windows 原生 OCR 
- `rapid`：基于 PaddleOCR
 同时可选 `preprocess`（放大 / 灰度 / 二值化 / 降噪）提升低清图识别率。

 

