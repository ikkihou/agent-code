# Agent 输出流水线（从 loop 到终端）

本文档追踪 agent 运行期间产生的所有输出——流式模型文本、`tool_call` 通知、diff 预览、hook 结果、slash 执行结果、cron 提示——从产生到最终在终端被打印/渲染的完整旅程。

**核心结论：agent loop 与前端之间隔着一层 wire 事件通道（`agent_code/wire.py`）。loop 与工具层只 `wire.send()` 结构化事件——不碰终端、不知道前端存在；前端是纯事件消费者，收到事件才决定怎么渲染（着色、行格式、diff 样式全是消费端职责）。借鉴 kimi-cli 的 UI ↔ agent loop 解耦架构。**

```
              ┌────────────────────────────────────────────────┐
              │   agent loop（worker 线程，agent_code/agent.py） │
              │                                                │
 流式模型文本 ────▶ wire.send(TextDelta(原文))   （不攒行，原文直发）  │
 通知行        ────▶ wire.send(Line(text, markup))  trace 双写     │
 工具开始     ────▶ wire.send(ToolCallStarted(name, args_preview))  │
 diff/命令预览 ───▶ wire.send(DiffPreview / CommandPreview)         │
              └──────────────┬─────────────────────────────────────┘
                             │  结构化事件（唯一边界）
              ┌──────────────┴──────────────────────┐
 一次性模式    │                        REPL 模式      │
 Wire→sink    │                        Wire→notify →   │
 同步 flush，  │                        call_soon_threadsafe（跨线程接缝，
 Console 直打 │                        drain → event_to_chunks →
 ───────────▶ 终端                     StreamLineBuffer 攒行 →
                                             OutputTranscript → 输出面板
                                             （session 转录边车持久化 chunk，
                                              resume 重放））
```

---

## 1. wire 通道（`agent_code/wire.py`）

事件类型（frozen dataclass）：

| 事件 | 语义 | 生产点 |
|---|---|---|
| `TextDelta(text)` | 模型流式原文增量（**不攒行**） | `agent.py` stream 循环 |
| `Line(text, markup)` | 一行完整通知（continue/final/hook/compacted/plan rejected） | `agent.py` emit / `tool_execution.py` |
| `PromptEcho(text)` | 用户/cron 输入回显块 | REPL / cron 路径 |
| `ToolCallStarted(name, args_preview)` | 工具调用开始（args_preview 已截断，仅展示） | `tool_execution.py` |
| `DiffPreview(path, old, new)` | 文件写入/编辑的 diff 预览（原文，前端着色） | `tool_execution.py::_handle_ask` |
| `CommandPreview(command)` | 待确认 bash 命令 | `tool_execution.py::_handle_ask` |

`Wire` 的消费模型：

- **合并语义**：队列内相邻的未消费 `TextDelta` 原地拼接——流式期间 UI 消费不及时也不积压几百条 delta（kimi-cli MergeableMixin 同款思路）。
- **同步消费**（one-shot / 测试）：只设 `sink`，`send()` 立刻 flush 事件批给 sink。
- **异步消费**（REPL）：设 `sink` + `notify`，`send()` 只触发 notify；消费端 `drain()` 取走全部待处理事件（保 send 顺序）。REPL 用 `_drain_scheduled` 标志把同一轮 UI 迭代内的多次 notify 合并成一次 `call_soon_threadsafe`。
- 两者都没设时事件落空（fire-and-forget），loop 对"有没有人在听"无感知。

trace（`AgentResult.trace`）仍由 `run_agent` 维护，是结果对象的一部分，不属于事件流。

## 2. 每种输出各自的旅程

| 输出 | 事件 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `TextDelta`（原文） | 否（final 行是） | 是（消费端攒行后渲染） |
| `tool_call: ...` | `ToolCallStarted` | 否 | 是（前端格式化） |
| 工具返回内容 | （无事件） | 否 | 否（只经 tool_result 给模型） |
| diff 预览 | `DiffPreview` | 否 | 是（前端 render_diff 着色） |
| 命令确认预览 | `CommandPreview` | 否 | 是 |
| PostToolUse hook | `Line(markup=True)` | 是 | 是（dim） |
| PreToolUse 拦截 | 构造 error block，无事件 | 否 | 否（进 tool_result） |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| `continue:` / `interrupted` / `final:` | `Line` | 是 | 是 |
| `compacted:` | `Line(markup=True)` | 是 | 是（dim） |
| plan 拒绝 | `Line` | 否 | 是 |
| ask_user_question 选择 | （无事件，回填 tool_result） | 否 | 否 |
| slash 结果（REPL） | 不走 wire，就地渲染 | 否 | 是 |
| 用户输入回显 | `PromptEcho` | 否 | 是（深灰底块） |

## 3. 两个前端

### 一次性模式（`cli.py::_ConsoleWireSink`）

`run_once` 构造 `_console_wire()`（sink = Console 直打）。`run_agent` 在同线程 send → 每次立刻 flush：`TextDelta` 原样 `console.print(end="")`（终端软折行）、`Line` 按 markup 开关渲染、`DiffPreview` 调 `prompt_ui.render_diff` 着色。无跨线程、无行缓冲。

### REPL 模式（`interactive.py::InteractiveShell`）

`run_turn(line, wire)` 每个 turn 拿到专属 `Wire`，worker 线程跑 `run_agent`；消费链路：

1. **notify → UI 线程**：`_schedule_wire_drain` 用 `_drain_scheduled` 标志合并 notify，`call_soon_threadsafe` 排一次 `_drain_wire`。**这是唯一的跨线程接缝。**
2. **drain 翻译**：`_drain_wire` 逐事件处理——`TextDelta` 喂 `StreamLineBuffer`（**攒完整行才落面板**，pending 半行绝不 finish 截断，pt 不能被覆写半行）；其余事件走 `event_to_chunks()` 翻译成 `OutputChunk`（着色、行格式全在这里）。
3. **turn 收尾**：`_schedule_turn_finish` 兜底排一次 `_turn_finish_cb`（drain 剩余事件 + `finish()` flush 半行）——保证 turn 结束后画面完整，且全部在 UI 线程执行。
4. **落面板**：`_append_output` → `append_output_text`（片段合并）→ session 转录边车 → `set_document` + 自动跟随 + `app.invalidate()`。

cron 到点 prompt 走 `CronPromptWire`：量小低频，`send` 直接逐事件线程安全投递，不开完整 notify/drain 管线。

## 4. 关键设计约束（踩坑提醒）

1. **流式半行只在消费端攒**——`StreamLineBuffer` 持有 pending 半行，`_drain_wire` 绝不调 `finish()`（会截断成两行），只有 turn 收尾 `_turn_finish_cb` 才 flush。
2. **工具层不发字符串行**——`tool_execution.py` 只发 `ToolCallStarted/DiffPreview/CommandPreview` 事件；要改显示格式去 `interactive.py::event_to_chunks`（REPL）或 `cli.py::_ConsoleWireSink`（one-shot）。
3. **`tool_result` 必须按 `tool_use` 顺序返回**——并行只读组靠 `ThreadPoolExecutor.map` 保序，这是回填模型的协议约束，与显示无关但改动时最易破坏。
4. **`Wire.send` 线程安全**（内部锁），但 `sink`/`notify` 回调运行在调用方线程——REPL 的 notify 里只做 `call_soon_threadsafe`，不做任何 UI 操作。
5. **slash 结果绕过 wire**——它不经事件流、不进 trace；若要 slash 输出也进事件流，需在 `route_slash_command` 显式处理。
6. **OutputChunk 仍是面板的显示单元**——wire 事件经 `event_to_chunks` 翻译成 chunk 后，落 transcript / 持久化 / resume 重放的路径与之前完全一致（见 `output.py` 的 `chunk_fragments`）。

## 5. 调试速查表

| 想找的输出 | 事件 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `TextDelta` | 否（final 行是） | 是 |
| `tool_call: ...` | `ToolCallStarted` | 否 | 是 |
| 工具返回内容 | （无） | 否 | **否**（只给模型） |
| diff / 命令确认 | `DiffPreview` / `CommandPreview` | 否 | 是 |
| PostToolUse hook | `Line(markup=True)` | 是 | 是 |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| compacted 提示 | `Line(markup=True)` | 是 | 是 |
| slash 结果（REPL） | （不走 wire） | 否 | 是 |
| interrupted | `Line` | 是 | 是 |
