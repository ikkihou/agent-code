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
                                     _render_live() → LiveArea.formatted()
                                     （独立 Live 窗口，位于输入 frame 上方）
                                     首行空行 + spinner / 预览 / 未闭合段落尾部
                                     （spinner 与已提交 prompt 恰好隔一行）
```

**终端所有权模型（scrollback + Live）**：`PromptSession.prompt_async()` 是终端唯一所有者，
只有输入区（frame 内的多行 buffer）+ 独立 Live 窗口（输入 frame 上方）+ bottom_toolbar 由
pt 重绘。输入区是 `multiline=True` 的多行 buffer，frame 高度随行数增长；底部工具栏只显示
permission mode + model。Live 区不是 prompt 的 `message`：`layout._install_live_area_window()`
把它插成 frame 的兄弟窗口，`layout._collapse_input_height()` / `_strip_frame_side_borders()`
把输入框压到内容高度、去掉左右竖线，让整个布局跟着内容走。已经发生完的内容不再由 pt
管理——它们被写进原生终端 scrollback，滚动/选区/复制都是终端原生的，也没有"整块 Document
重设"的帧成本。Live 区只装"正在进行中"的东西：spinner + 计时、活动工具名、待批准预览、
当前未闭合段落的尾部几行；spinner 在 Live 顶部，首行是一个空行，所以它与刚提交的 prompt
之间恰好隔一行。

---

## 1. wire 通道（`agent_code/wire.py`）

事件类型（frozen dataclass）：

| 事件 | 语义 | 生产点 |
|---|---|---|
| `TextDelta(text)` | 模型流式原文增量（**不攒行**） | `agent.py` stream 循环 |
| `Line(text, markup)` | 一行完整通知（continue/final/hook/compacted/plan rejected） | `agent.py` emit / `tool_execution.py` |
| `PromptEcho(text)` | 用户/cron 输入回显块（可多行；REPL 整行满宽高亮） | REPL / cron 路径 |
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
| diff 预览 | `DiffPreview` | 否 | 是（仅 Live 区一行 `⏸ diff … waiting for approval`；完整 diff 不进 scrollback，只有 one-shot 才打印） |
| 命令确认预览 | `CommandPreview` | 否 | 是（仅 Live 区一行 `⏸ bash … waiting for approval`，命令截断到 60 字符） |
| PostToolUse hook | `Line(markup=True)` | 是 | 是（dim） |
| PreToolUse 拦截 | 构造 error block，无事件 | 否 | 否（进 tool_result） |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| `continue:` / `interrupted` / `final:` | `Line` | 是 | 是 |
| `compacted:` | `Line(markup=True)` | 是 | 是（dim） |
| plan 拒绝 | `Line` | 否 | 是 |
| ask_user_question 选择 | （无事件，回填 tool_result） | 否 | 是（问题+提示符在 Live 区，回答提交 scrollback） |
| slash 结果（REPL） | 不走 wire，就地渲染 | 否 | 是 |
| 用户输入回显 | `PromptEcho` | 否 | 是（整行满宽高亮条：bold 白字 + `bg:#444444`，按终端宽度 pad） |

## 3. 两个前端

### 一次性模式（`cli.py::_ConsoleWireSink`）

`run_once` 构造 `_console_wire()`（sink = Console 直打）。`run_agent` 在同线程 send → 每次立刻 flush：`TextDelta` 原样 `console.print(end="")`（终端软折行）、`Line` 按 markup 开关渲染、`DiffPreview` 调 `prompt_ui.render_diff` 着色。无跨线程、无行缓冲。turn 生命周期事件在 one-shot 下不需要（无 Live 区），sink 里直接忽略。

### REPL 模式（`ui/shell.py::InteractiveShell`）

**输入层**：`PromptSession(multiline=True)`——buffer 可含换行，frame 高度随行数增长
（`_collapse_input_height` 不限制最大高度，只在补全菜单打开时临时让出 `MENU_ROWS` 行）。
Enter（ControlM）提交；Ctrl+J、Alt+Enter（多数终端编码为 `Esc, Enter`）、Esc+Enter 插入
换行。Shift+Enter 依赖 `shell.py::_install_shift_enter_mapping()`：import 时把
`ANSI_SEQUENCES` 里的三个扩展序列（kitty CSI-u `\x1b[13;2u`、kitty legacy
`\x1b[27;2;13u`、xterm modifyOtherKeys `\x1b[27;2;13~`）覆盖为 `Keys.ControlJ`。**限制**：
只有终端本来就发送这些序列时才生效；pt 3.0.52 没有 kitty keyboard protocol 支持，应用不能
主动 enable（开启后 Esc/Ctrl+C 的编码会变，而 pt 解析不了，等于把这两个键弄坏）。因此可靠
的换行方式是 Ctrl+J / Alt+Enter。`session.app.timeoutlen = 0.2`：Esc 既是 abort 又是
Esc+Enter 的前缀，短超时让单独 Esc 仍能及时中断，同时 Esc+Enter 仍可匹配。

`run_turn(line, wire)` 每个 turn 拿到专属 `Wire`，worker 线程跑 `run_agent`；消费链路：

1. **worker → UI 线程**：`_worker_sink` 只做 `loop.call_soon_threadsafe(_enqueue_events, batch)`；`_enqueue_events` 把事件灌进 `_event_queue` 并 `_live_dirty.set()`。**这是唯一的跨线程接缝**，sink 里不碰任何 UI。
2. **`_consume_events`（UI 协程）**：等 `_live_dirty`，一次取空队列，逐事件翻译：
   - `TextDelta` → `LiveArea.feed_stream()`：按空行切段，**已闭合的段落立刻返回**并提交 scrollback；未闭合段落只留在 Live 区（含半行）。
   - `TurnStarted` / `TurnEnded` → 开关 `turn_active`、重置/收尾；`TurnEnded` 把未闭合段落整段 `flush_paragraph()` 提交。turn 活跃时 `LiveArea.formatted()` 先输出一个空行再输出 spinner，这个空行就是它与已提交 prompt 之间的唯一 gap。
   - `ToolCallStarted` → `active_tool`（挂到 spinner 行）；`ToolCallCompleted` → 清 `active_tool`，随后落成 `✓ name` 行。
   - `DiffPreview` / `CommandPreview` → `ui/live.py::_preview_lines()` 生成一行 `⏸ … waiting for approval` 塞进 `pending_preview`，然后 **`continue`**——`ui/events.py::event_to_chunks()` 里虽写了完整 diff / Command 的渲染分支，但 REPL 永远走不到；只有 one-shot 的 `_ConsoleWireSink` 会打印完整内容。
   - 其余（`Line` / `PromptEcho` / `ToolCallCompleted` / slash 结果等）→ `event_to_chunks()` → 直接提交。
3. **提交 scrollback**：`_commit_chunks` → transcript 边车 → `_commit_chunk` → `sys.stdout.write(_chunk_to_ansi(chunk))`。`_chunk_to_ansi` 先按 `chunk.full_width` 调 `_pad_full_width` 补到终端宽度，再把片段样式经 `_rich_style` 翻成 Rich 词汇，最后交给 `width=4096` 的 offscreen Console 渲染。`patch_stdout(raw=True)` 把 stdout 换成 `StdoutProxy`，它用自带 flush 线程经 `run_in_terminal` 绘制到 prompt 上方——唯一 thread-safe 且不与 `prompt_async` 抢输入的路径。
4. **Live 区重绘**：每次事件批处理后 `app.invalidate()`；spinner 由 `_animate_spinner` 协程每 0.12s 触发一次重绘（只在 turn 活跃时）。
5. **bottom_toolbar**：只显示 permission mode（`default` / `accept edits` / `plan`）和 model，不再拼 todo / 活动状态。

**提交 prompt 的整行高亮**：`ui/events.py::render_user_prompt` 返回 `full_width=True` 的 chunk，样式 `bold ansiwhite bg:#444444`（多行输入每行都加 `> ` 前缀；结尾只有单个 `\n`）。提交链路：

- `_pad_full_width(fragments, columns)`：按**视觉行**累计宽度，`get_cwidth()` 保证 CJK 宽字符算 2 列，tab 先 `expandtabs(8)`；行到 `\n` 时补空格到终端宽度，padding 继承该行最后一个非空样式，所以背景是连续横条；超宽行不 pad，交给终端软折行。
- `_rich_style(style)`：把 pt 词汇翻成 Rich——`ansiwhite` → `white`、`bg:#444444` → `on #444444`、`fg:` 前缀、`hidden` → `conceal` 等；Rich 不认识的 token 直接丢弃。**不翻译就会静默丢样式**（Rich 不认识 `ansi*` 与 `bg:`）。
- `_ansi_console` 固定 `width=4096`：Rich 在非 tty 场景会回退到 80 列，宽度不够时 pad 出来的行会被截断/折行。
- 只 pad 渲染结果：`OutputChunk.text/fragments` 始终未 pad；transcript 持久化 `full_width` 意图，旧记录（无该字段）靠片段里是否含 `bg:#444444` 推断，resume 时按当前终端宽度重新 pad。

**批准/提问走 Live 区，不写 stdout**：`_ask_in_app` 把问题正文（`ui/prompts.py::render_prompt_request`）挂进 `LiveArea.pending_preview`，提示符（`prompt_for_request`）由 `_render_live` 加到 Live 区顶部；用户输入由 `_submit_text` 解析后喂给 pending future。原因见下方约束 3 与 5。

cron 到点 prompt 在 worker 线程内直接 `_worker_sink([PromptEcho(...)])` / `[Line(...)]`，量小低频，复用同一条投递路径。

## 4. 关键设计约束（踩坑提醒）

1. **流式半行只在 Live 区攒**——`LiveArea.stream_pending` 持有未完成半行；只有空行闭合的段落才 `_commit_chunk`（整行保证），`TurnEnded` 才 flush 半行。
2. **工具层不发字符串行**——`tool_execution.py` 只发 `ToolCallStarted/ToolCallCompleted/DiffPreview/CommandPreview` 事件；要改显示格式去 `ui/events.py::event_to_chunks` / `ui/live.py::_preview_lines`（REPL）或 `cli.py::_ConsoleWireSink`（one-shot）。
3. **提交绝不能走 pt 的 `print_formatted_text`**——app 运行时它会 `loop.call_soon_threadsafe(run_in_terminal(...))` 且调用方无法 await；在"提交完立刻进下一轮 `prompt_async`"的循环里，它会和 prompt 的输入 detach/attach 竞争，表现为**后续输入全部失效**（实测踩过，排查成本很高）。正确路径是 `sys.stdout.write`，由 `patch_stdout` 的 `StdoutProxy` 负责落到 prompt 上方。
4. **`FormattedText` 没有 `.fragments`**——`_render_live`（作为 `_install_live_area_window` 的 `get_text` 回调）必须返回新构造的 `FormattedText(list_of_tuples)`，不能修改已有实例。曾经在这里抛 `AttributeError`，导致渲染线程死掉、后续输入永不返回（看起来像 worker 死锁，实际是渲染异常）。
5. **提问期间不写 stdout**——`StdoutProxy` 的 `run_in_terminal` 会 detach 输入，和正在运行的 `prompt_async` 抢输入流。问题正文一律进 Live 区。
6. **`tool_result` 必须按 `tool_use` 顺序返回**——并行只读组靠 `ThreadPoolExecutor.map` 保序，这是回填模型的协议约束，与显示无关但改动时最易破坏。
7. **`Wire.send` 线程安全**（内部锁），但 `sink`/`notify` 回调运行在调用方线程——REPL 的 sink 里只做 `call_soon_threadsafe`，不做任何 UI 操作。
8. **slash 结果绕过 wire**——它不经事件流、不进 trace；若要 slash 输出也进事件流，需在 `_route_slash_command` 显式处理。
9. **resume 重放与 `-c` 现状**——`_initial_chunks` 在 prompt 启动前直打（此时无 app，`_commit_chunk` 直接写 stdout），之后照旧由 transcript 边车持久化 chunk（`full_width` 存意图，旧记录靠 `bg:#444444` 推断）。但 `Session.load_latest` 的 `glob("*.jsonl")` 会把更新的 `<id>.transcript.jsonl` 当成会话文件：`-c`/`--continue` 因此加载到 `<id>.transcript`，`history` 抛 `KeyError: 'role'`、回放为空；`--resume <id>` 不受影响。修好前别把 `-c` 当可用路径。
10. **`patch_stdout` 必须 `raw=True`**——proxy 默认的 `write()` 会把每个 ESC 替换成 `?`，已经渲好的 ANSI 变成字面量 `?[...m`，整屏高亮/颜色全毁（实测 P0）。`raw=False` 时 `StdoutProxy` 走 `output.write()` 而不是 `write_raw()`；这和"别用 `print_formatted_text`"是同一类问题的两个面。
11. **`full_width` 只在提交时 pad，样式必须翻译**——存储层保持未 pad（否则 transcript 会把某个终端宽度写死），`_pad_full_width` 用 `get_cwidth` + `expandtabs`、超宽行不 pad，`_ansi_console` 的 `width=4096` 不能调小（Rich 按 80 列回退会截断长行）。片段的 pt 词汇（`ansiwhite`、`bg:#444444`）必须先经 `_rich_style()` 翻成 Rich（`white`、`on #444444`），否则 Rich 静默丢弃 token，表现为"文字在、背景没了"。翻译放在 `scrollback.py` 一处，别改各个生产者——旧 transcript 记录也要能渲染。
12. **多行输入的边界**——`multiline=True` 下 Enter 绑定到 `validate_and_handle`，换行必须显式绑定；Shift+Enter 只在终端主动发 kitty / modifyOtherKeys 序列时可达（pt 3.0.52 无 kitty protocol，不能主动 enable，否则 Esc/Ctrl+C 编码改变）。Esc 同时是 abort 前缀，`session.app.timeoutlen = 0.2` 是这两者能共存的前提。
13. **gap 只由 Live 提供**——prompt echo 以单个 `\n` 结尾，`LiveArea.formatted()` 在 turn 活跃时先补一个 `\n`。两边都加会变成两个空行；Live 不在 turn 活跃时输出这行，所以空闲时窗口高度为 0。

## 5. 调试速查表

| 想找的输出 | 事件 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `TextDelta` | 否（final 行是） | 是 |
| `tool_call: ...` | `ToolCallStarted` | 否 | 是（Live 区） |
| 工具完成 | `ToolCallCompleted` | 否 | 是（✓ 行） |
| 工具返回内容 | （无） | 否 | **否**（只给模型） |
| diff / 命令确认 | `DiffPreview` / `CommandPreview` | 否 | 是（仅 Live 一行 `⏸ … waiting for approval`；完整 diff/命令只在 one-shot） |
| PostToolUse hook | `Line(markup=True)` | 是 | 是 |
| Stop hook 强制续写 | `Line`（continue:） | 是 | 是 |
| compacted 提示 | `Line(markup=True)` | 是 | 是 |
| slash 结果（REPL） | （不走 wire） | 否 | 是 |
| interrupted | `Line` | 是 | 是 |
| 卡片留在屏幕上不滚进去 | — | — | 检查是否误用了 `print_formatted_text`（约束 3） |
| 高亮变成字面量 `?[...m` | — | — | 检查 `patch_stdout(raw=True)`（约束 10） |
| 提交的 prompt 没有整行背景 | — | — | 检查 `full_width=True` 与 `_rich_style` 翻译（约束 11） |
| Shift+Enter 无反应 | — | — | 终端未发扩展序列；改用 Ctrl+J / Alt+Enter（约束 12） |
| spinner 与 prompt 的空行不对 | — | — | 空行只由 `LiveArea.formatted()` 前导 `\n` 提供（约束 13） |
| `-c` 回放为空 / `KeyError: 'role'` | — | — | `Session.load_latest` 命中 `<id>.transcript.jsonl`（约束 9） |
| 提问后输入无反应 | — | — | 检查 `_render_live` 是否抛异常（约束 4） |
