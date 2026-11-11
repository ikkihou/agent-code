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
 turn 生命周期 ───▶ wire.send(TurnStarted / TurnEnded               │
              └──────────────┬─────────────────────────────────────┘
                             │  结构化事件（唯一边界）
              ┌──────────────┴──────────────────────┐
 一次性模式    │                        REPL 模式      │
 Wire→sink    │                        Wire→sink      │
 同步 flush，  │                        （worker 线程）│
 Console 直打 │                             ↓          │
 ───────────▶ 终端                     call_soon_threadsafe
                                             （唯一跨线程接缝）
                                                  ↓
                                       _consume_events（UI 协程）
                                        ├─ TextDelta → LiveArea.feed_stream
                                        │             └→ 已闭合段落 _commit_chunk
                                        │                （sys.stdout.write → scrollback）
                                        └─ 其余事件 → event_to_chunks → _commit_chunk
                                     LiveArea.formatted()（prompt message 区）
                                     只显示 spinner / 预览 / 未闭合段落尾部
```

**终端所有权模型（scrollback + Live）**：`PromptSession.prompt_async()` 是终端唯一所有者，
只有输入行 + message 区（Live）+ bottom_toolbar 由 pt 重绘。已经发生完的内容不再由 pt
管理——它们被写进原生终端 scrollback，滚动/选区/复制都是终端原生的，也没有"整块 Document
重设"的帧成本。Live 区只装"正在进行中"的东西：spinner + 计时、活动工具名、待批准预览、
当前未闭合段落的尾部几行。

---

## 1. wire 通道（`agent_code/wire.py`）

事件类型（frozen dataclass）：

| 事件 | 语义 | 生产点 |
|---|---|---|
| `TextDelta(text)` | 模型流式原文增量（**不攒行**） | `agent.py` stream 循环 |
| `Line(text, markup)` | 一行完整通知（continue/final/hook/compacted/plan rejected） | `agent.py` emit / `tool_execution.py` |
| `PromptEcho(text)` | 用户/cron 输入回显块 | REPL / cron 路径 |
| `ToolCallStarted(call_id, name, args_preview)` | 工具调用开始（args_preview 已截断，仅展示） | `tool_execution.py` |
| `ToolCallCompleted(call_id, name, is_error)` | 工具调用结束（前端据此打 ✓/✗ 行） | `tool_execution.py` |
| `DiffPreview(path, old, new)` | 文件写入/编辑的 diff 预览（原文，前端着色） | `tool_execution.py::_handle_ask` |
| `CommandPreview(command)` | 待确认 bash 命令 | `tool_execution.py::_handle_ask` |
| `TurnStarted()` / `TurnEnded()` | turn 生命周期边界：启动/关闭 Live 区、收尾提交未闭合段落 | REPL `_work_loop` |

`Wire` 的消费模型：

- **合并语义**：队列内相邻的未消费 `TextDelta` 原地拼接——流式期间 UI 消费不及时也不积压几百条 delta（kimi-cli MergeableMixin 同款思路）。
- **同步消费**（one-shot / 测试 / REPL）：只设 `sink`，`send()` 立刻 flush 事件批给 sink。REPL 的 sink 是 `_worker_sink`，它只做 `call_soon_threadsafe` 把批投进 UI 队列。
- 只设 `notify` 时 `send()` 仅触发 notify，消费端 `drain()` 取走全部待处理事件（保 send 顺序）。
- 两者都没设时事件落空（fire-and-forget），loop 对"有没有人在听"无感知。

trace（`AgentResult.trace`）仍由 `run_agent` 维护，是结果对象的一部分，不属于事件流。

## 2. 每种输出各自的旅程

| 输出 | 事件 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `TextDelta`（原文） | 否（final 行是） | 是（Live 区尾部 → 段落后进 scrollback） |
| `tool_call: ...` | `ToolCallStarted` | 否 | 是（Live 区活动工具名） |
| 工具完成 | `ToolCallCompleted` | 否 | 是（`✓ name` 进 scrollback） |
| 工具返回内容 | （无事件） | 否 | 否（只经 tool_result 给模型） |
| diff 预览 | `DiffPreview` | 否 | 是（Live 区一行提示；确认后整段 diff 进 scrollback） |
| 命令确认预览 | `CommandPreview` | 否 | 是（Live 区一行提示 / 确认后 Command 行） |
| PostToolUse hook | `Line(markup=True)` | 是 | 是（dim） |
| PreToolUse 拦截 | 构造 error block，无事件 | 否 | 否（进 tool_result） |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| `continue:` / `interrupted` / `final:` | `Line` | 是 | 是 |
| `compacted:` | `Line(markup=True)` | 是 | 是（dim） |
| plan 拒绝 | `Line` | 否 | 是 |
| ask_user_question 选择 | （无事件，回填 tool_result） | 否 | 是（问题+提示符在 Live 区，回答提交 scrollback） |
| slash 结果（REPL） | 不走 wire，就地渲染 | 否 | 是 |
| 用户输入回显 | `PromptEcho` | 否 | 是（深灰底块） |

## 3. 两个前端

### 一次性模式（`cli.py::_ConsoleWireSink`）

`run_once` 构造 `_console_wire()`（sink = Console 直打）。`run_agent` 在同线程 send → 每次立刻 flush：`TextDelta` 原样 `console.print(end="")`（终端软折行）、`Line` 按 markup 开关渲染、`DiffPreview` 调 `prompt_ui.render_diff` 着色。无跨线程、无行缓冲。turn 生命周期事件在 one-shot 下不需要（无 Live 区），sink 里直接忽略。

### REPL 模式（`interactive.py::InteractiveShell`）

`run_turn(line, wire)` 每个 turn 拿到专属 `Wire`，worker 线程跑 `run_agent`；消费链路：

1. **worker → UI 线程**：`_worker_sink` 只做 `loop.call_soon_threadsafe(_enqueue_events, batch)`；`_enqueue_events` 把事件灌进 `_event_queue` 并 `_live_dirty.set()`。**这是唯一的跨线程接缝**，sink 里不碰任何 UI。
2. **`_consume_events`（UI 协程）**：等 `_live_dirty`，一次取空队列，逐事件翻译：
   - `TextDelta` → `LiveArea.feed_stream()`：按空行切段，**已闭合的段落立刻返回**并提交 scrollback；未闭合段落只留在 Live 区（含半行）。
   - `TurnStarted` / `TurnEnded` → 开关 `turn_active`、重置/收尾；`TurnEnded` 把未闭合段落整段 `flush_paragraph()` 提交。
   - `ToolCallStarted` → `active_tool`（挂到 spinner 行）；`DiffPreview` / `CommandPreview` → `pending_preview`（Live 区等批准提示）。
   - 其余（`Line` / `PromptEcho` / `ToolCallCompleted` / 确认后的 Diff/Command）→ `event_to_chunks()` → 直接提交。
3. **提交 scrollback**：`_commit_chunks` → transcript 边车 → `_commit_chunk` → `sys.stdout.write(_chunk_to_ansi(chunk))`。`patch_stdout` 把 stdout 换成 `StdoutProxy`，它用自带 flush 线程经 `run_in_terminal` 绘制到 prompt 上方——唯一 thread-safe 且不与 `prompt_async` 抢输入的路径。
4. **Live 区重绘**：每次事件批处理后 `app.invalidate()`；spinner 由 `_animate_spinner` 协程每 0.12s 触发一次重绘（只在 turn 活跃时）。

**批准/提问走 Live 区，不写 stdout**：`_ask_in_app` 把问题正文和提示符挂进 `_render_message`（prompt message），用户输入由 `_submit_text` 喂给 pending future。原因见下方约束 3。

cron 到点 prompt 在 worker 线程内直接 `_worker_sink([PromptEcho(...)])` / `[Line(...)]`，量小低频，复用同一条投递路径。

## 4. 关键设计约束（踩坑提醒）

1. **流式半行只在 Live 区攒**——`LiveArea.stream_pending` 持有未完成半行；只有空行闭合的段落才 `_commit_chunk`（整行保证），`TurnEnded` 才 flush 半行。
2. **工具层不发字符串行**——`tool_execution.py` 只发 `ToolCallStarted/ToolCallCompleted/DiffPreview/CommandPreview` 事件；要改显示格式去 `interactive.py::event_to_chunks` / `_preview_lines`（REPL）或 `cli.py::_ConsoleWireSink`（one-shot）。
3. **提交绝不能走 pt 的 `print_formatted_text`**——app 运行时它会 `loop.call_soon_threadsafe(run_in_terminal(...))` 且调用方无法 await；在"提交完立刻进下一轮 `prompt_async`"的循环里，它会和 prompt 的输入 detach/attach 竞争，表现为**后续输入全部失效**（实测踩过，排查成本很高）。正确路径是 `sys.stdout.write`，由 `patch_stdout` 的 `StdoutProxy` 负责落到 prompt 上方。
4. **`FormattedText` 没有 `.fragments`**——`_render_message` 必须返回新构造的 `FormattedText(list_of_tuples)`，不能修改已有实例。曾经在这里抛 `AttributeError`，导致 prompt 渲染线程死掉、后续输入永不返回（看起来像 worker 死锁，实际是渲染异常）。
5. **提问期间不写 stdout**——`StdoutProxy` 的 `run_in_terminal` 会 detach 输入，和正在运行的 `prompt_async` 抢输入流。问题正文一律进 Live 区。
6. **`tool_result` 必须按 `tool_use` 顺序返回**——并行只读组靠 `ThreadPoolExecutor.map` 保序，这是回填模型的协议约束，与显示无关但改动时最易破坏。
7. **`Wire.send` 线程安全**（内部锁），但 `sink`/`notify` 回调运行在调用方线程——REPL 的 sink 里只做 `call_soon_threadsafe`，不做任何 UI 操作。
8. **slash 结果绕过 wire**——它不经事件流、不进 trace；若要 slash 输出也进事件流，需在 `route_slash_command` 显式处理。
9. **resume 重放**：`_initial_chunks` 在 prompt 启动前直打（此时无 app，`_commit_chunk` 直接写 stdout），之后照旧由 transcript 边车持久化 chunk。

## 5. 调试速查表

| 想找的输出 | 事件 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `TextDelta` | 否（final 行是） | 是 |
| `tool_call: ...` | `ToolCallStarted` | 否 | 是（Live 区） |
| 工具完成 | `ToolCallCompleted` | 否 | 是（✓ 行） |
| 工具返回内容 | （无） | 否 | **否**（只给模型） |
| diff / 命令确认 | `DiffPreview` / `CommandPreview` | 否 | 是 |
| PostToolUse hook | `Line(markup=True)` | 是 | 是 |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| compacted 提示 | `Line(markup=True)` | 是 | 是 |
| slash 结果（REPL） | （不走 wire） | 否 | 是 |
| interrupted | `Line` | 是 | 是 |
| 卡片留在屏幕上不滚进去 | — | — | 检查是否误用了 `print_formatted_text`（约束 3） |
| 提问后输入无反应 | — | — | 检查 `_render_message` 是否抛异常（约束 4） |
