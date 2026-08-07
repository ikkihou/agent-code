# Agent 输出流水线（从 loop 到终端）

本文档追踪 agent 运行期间产生的所有输出——流式模型文本、`tool_call` trace、`observation`、diff 预览、hook 结果、slash 执行结果、cron 提示——从产生到最终在终端被打印/渲染的完整旅程。

**核心结论：agent loop 内所有要上终端的输出，最终都汇聚到两个回调之一——`emit()`（追踪行通道）和 `print_output()`（渲染行通道），然后由运行模式决定是直打 Rich 终端，还是经一个线程安全单点搬进 prompt_toolkit 面板。**

```
                    ┌───────────────────────────────────────────────┐
                    │  agent loop（worker 线程，agent_code/agent.py）│
                    │                                               │
  流式模型文本  ────▶  _LineBufferedStreamRenderer → write_stream_line │
  tool_call    ────▶  emit() ──┐  (startswith("observation:") 被掐)  │
  observation  ────▶  emit() ──┴─▶ trace.append + print_output       │
  diff/确认 UI  ────▶  print_output()                                │
  hook 结果    ────▶  print_output() / emit()                        │
                    └──────────────┬────────────────────────────────┘
                                   │
              ┌────────────────────┴──────────────────────┐
  一次性模式   │                            REPL 模式       │
  output=None │                            output=ui_writer│
  print_output│                            print_output     │
  =console.print                              │
  Rich 直打 ─────▶ 终端                       render_console_chunk（Rich 离屏渲染成 ANSI）
                                   → OutputWriter = ui_write
                                   → loop.call_soon_threadsafe（唯一跨线程接缝）
                                   → OutputTranscript（ANSI→样式片段 + 纯文本副本）
                                   → TranscriptLexer → 输出面板
```

---

## 1. 两个核心回调

### `emit(line)` —— 追踪行通道

定义在 `agent_code/agent.py:166-171`：

```python
def emit(line: str) -> None:
    # 工具结果可能很长：完整内容只通过 tool_result 回填给模型，终端只看工具调用/最终回答。
    if line.startswith("observation:"):
        return                      # ← observation 被拦截，不上终端
    trace.append(line)              # ← 双写进 AgentResult.trace
    print_output(line, markup=False, highlight=False)
```

- 带 `observation:` 前缀的行被**过滤**——只回填给模型，不上终端。
- 其余行**双写**：进 `AgentResult.trace`（供会话/调试回放），同时经 `print_output` 上终端。
- 一律 `markup=False, highlight=False`，即 trace 行是**纯文本**，不做 Rich 上色。

### `print_output` —— 渲染行通道

由 `_make_printer(output)` 返回（`agent_code/agent.py:74-86`）：

```python
def _make_printer(output: OutputWriter | None = None) -> PrintFunc:
    if output is None:
        return console.print                       # 一次性模式：Rich 直打

    def print_to_output(*objects: Any, **kwargs: Any) -> None:
        styled = (
            kwargs.get("markup", True) is not False
            or kwargs.get("style") is not None
            or any(not isinstance(obj, str) for obj in objects)
        )
        output(render_console_chunk(*objects, styled=styled, **kwargs))
    return print_to_output
```

- **一次性模式**：`output=None`，`print_output` 字面意义上就是 Rich 的 `console.print`，直接打 stdout。
- **REPL 模式**：包一层，把 `*objects, **kwargs`（Rich print 风格）用 `render_console_chunk` 离屏渲染成 `OutputChunk`，再交给 `OutputWriter` 回调。
- `styled` 是启发式推断：是否带 markup/style，或参数里有非字符串对象 → 渲染成 ANSI；否则纯文本。

### 两种模式的接线点

| 模式 | `output` 参数 | `print_output` 走向 |
|---|---|---|
| 一次性（`cli.py:run_once`） | 不传（None） | Rich `console.print` → stdout |
| REPL（`cli.py:run_turn`） | `ui_writer` | `render_console_chunk` → `OutputChunk` → 线程安全搬移 |

---

## 2. 每种输出各自的旅程

每种输出下面拆三层：**产生点**（谁发射）、**内容格式**（具体字符串长什么样）、**去向**（trace / 终端 / 只给模型）。

### 流式模型文本（回答过程）

- **产生点**：`provider.complete_stream` 产出 `text_delta` 事件 → `stream_renderer.feed()`（`_LineBufferedStreamRenderer`，`agent_code/agent.py:51-71`）把半行攒在 `pending` 里，**凑成完整行**才调用 `write_stream_line` → `print_output(line, ..., soft_wrap=True)`。
- **内容**：模型原生输出的逐段增量文本，不做任何格式化。`feed()` 逐字符拼接，攒到 `\n` 才放行一行：
  ```
  # 上游分批吐来： "让我看" → "一下 agent.py" → "。\n" → "这个文件…"
  # 终端收到的是拼好的完整行：
  让我看一下 agent.py。
  这个文件…（后续行）
  ```
- **去向**：仅终端，**不进 trace**。攒整行是硬约束：prompt_toolkit 只显示整行，半行会被覆盖，这层缓冲是行边界的唯一保证者。
- **收尾**：`final: <text>` 单独经 `emit` 进 trace 并上终端（`agent_code/agent.py:302-305`）。

### `tool_call` 行（工具调用 trace）

- **产生点**：`execute_one_tool_call` 开头（`agent_code/tool_execution.py:175`）：

  ```python
  emit(f"tool_call: {call.name} {_format_call_args(call.arguments)}")
  ```

- **内容**：`tool_call: <工具名> <参数 dict 预览>`。`_format_call_args`（`agent_code/tool_execution.py:154-163`）把每个超过 80 字符的字符串参数截到前 80 字符 + `…`，**其它参数原样**：
  ```
  tool_call: read_file {'path': 'agent_code/agent.py'}
  tool_call: bash {'command': 'uv run pytest tests/test_hooks.py -k test_name'}
  tool_call: file_write {'file_path': 'notes.md', 'content': '# Notes\n\n- 第一行示例，假设…（80 字符后的部分被…省略）…'}
  ```
- **去向**：进 `trace`，同时 `print_output(markup=False, highlight=False)` 纯文本上终端。注意**终端只看精简预览，完整参数照常传给工具**。

### `observation`（工具返回内容）

- **产生点**：工具执行后（`agent_code/tool_execution.py:220`）：

  ```python
  emit(f"observation: {result.content}")
  ```

- **内容**：工具 `run` 返回的完整字符串，即模型通过 `tool_result` 看到的那一份。不同工具格式各异（统一有截断上限，普通 8000 字符 / bash 12000）：

  | 工具 | content 内容示例 |
  |---|---|
  | `read_file` | 文件全文，超限时末尾追加 `\n[truncated N chars]`（`tools/read.py:36`、`fs_safety.py:209`） |
  | `list_files` | 每行一个条目，目录名带 `/` 后缀；空目录返回 `(empty)`（`tools/read.py:53`） |
  | `glob` | 路径每行一个，按 mtime 倒序、最多 200 条；无命中返回 `(no matches)`（`tools/read.py:72-75`） |
  | `grep` | `文件:行号:内容`（走 ripgrep，`--line-number --no-heading`，`tools/read.py:105`） |
  | `bash` 成功 | stdout；空输出返回 `(no output)`（`bash_runner.py:59`） |
  | `bash` 失败 | `exit code 1\n<输出>`（`bash_runner.py:58`） |
  | `bash` 超时 | `error: command timed out after 30s`（`bash_runner.py:47`） |
  | `bash` 有 stderr | 输出末尾追加 `\n[stderr]\n<stderr>`（`bash_runner.py:50-53`） |
  | `bash` 后台 | `Command running in background with ID: bg-abc12345.\nStdout is being written to: .bg/bg-abc12345.out\nStderr is being written to: .bg/bg-abc12345.err`（`tools/shell.py:33-35`） |
  | 路径非法 / 参数缺失 | `error: <具体原因>` 前缀惯例（如 `error: not a directory: x`、`error: missing required argument 'pattern'`） |

- **去向**：被 `emit` 第一行 `startswith("observation:")` **掐掉，永远不上终端**。完整内容只通过返回的 `tool_result` block 回填给模型（`messages.append`，`agent_code/agent.py:340`）。
- **核心策略**：**终端只看"调了什么"（tool_call 行），模型才看"返回了什么"（observation）**。

> 例外：`exit_plan_mode` 拒绝路径故意用大写 `emit(f"Observation: {obs}")`（`agent_code/tool_execution.py:198`）绕开过滤，让用户看到拒绝原因。所有 `_error_observation` 路径（含 PreToolUse hook 拦截）都走小写 `observation:`，对终端隐藏。

### diff 预览 / 确认 UI

- **产生点**：不经过 `emit`，直接 `print_output`（`agent_code/tool_execution.py:124-130`）。
- **内容**：带 Rich markup 的预览文本，走 `print_output` 即被渲染成 ANSI 彩色：

  ```python
  print_output(f"\n[bold]Diff for {path_str}:[/bold]")
  print_output(render_diff(old_content, new_content, path_str))
  print_output(f"\n[bold yellow]Command:[/bold yellow] {command}")
  ```

  `render_diff`（`prompt_ui.py:33-56`）用 `difflib.unified_diff` 生成带颜色标记的 diff，markup 规则：文件头 `[bold]`、删除行 `[red]`、新增行 `[green]`、`@@` 块 `[cyan]`：

  ```
  [bold]Diff for src/foo.py:[/bold]
  --- a/src/foo.py
  +++ b/src/foo.py
  @@ -1,3 +1,3 @@
  [red]-  def old():[/red]
  [green]+  def new():[/green]

  [bold yellow]Command:[/bold yellow] uv run pytest
  ```

  确认交互本身（`confirm_edit` / `confirm_command` / `confirm_plan` / `prompt_single_choice`，`prompt_ui.py`）走 asker 借回终端弹提示，文字预览走本条通道。

### hook 结果

| hook 类型 | 具体内容 | 终端可见性 |
|---|---|---|
| `PostToolUse` | `hook: PostToolUse bash ok` / `hook: PostToolUse file_write warning: <hook 输出>`，整行 `[dim]` 灰（`tool_execution.py:230`） | 可见 |
| `PreToolUse` 拦截 | observation 内容为 `tool blocked by PreToolUse hook:\n  [hook] <命令>: <输出>`（`tool_execution.py:189-192`）→ 进小写 observation | **隐藏**，只进模型的 tool_result 错误信息 |
| `Stop` 强制续写 | 失败 hook 的非空 output 成为 `forced` → `continue_with(forced, ...)` → `emit(f"continue: {forced}")`（`agent.py:285-301`） | 可见，并追加一条 continue 用户消息多跑一轮 |

### `continue:` / `interrupted` / `compacted` 行

- **产生点**：`continue_with`（`agent.py:189-196`）和 loop 顶部的几个分支。
- **内容**：

  ```
  continue: max_tokens reached — asking the model to continue
  continue: empty response — asking the model to retry
  continue: interrupted by user
  continue: <Stop hook 强制续写的内容>
  compacted: 18 messages remaining
  ```

  其中 `continue:` / `interrupted` 经 `emit`（`agent.py:193, 174`）→ **进 trace + 终端可见**；`compacted:` 走 `print_output(f"[dim]...[/dim]")`（`agent.py:206`）→ **不进 trace**，仅终端（dim 灰）。

### slash 执行结果（REPL 旁路）

- **产生点**：`dispatch_slash`（`slash.py:72-91`）→ `SlashResult(handled, message, markup, should_query, prompt)`。**完全不进 agent loop**。
- **内容**（各 handler 返回的 message 字符串，`markup` 控制是否当 Rich markup 渲染）：

  ```
  /help  →  "[bold]可用命令：[/bold]\n  [bold]/help[/bold]  列出所有已注册 slash command…"（markup=True，逐条 Rich 上色）
  /model →  provider: anthropic  model: deepseek-v4-flash（markup 默认 False，纯文本）
  /未知  →  Unknow command: /foo
  /语法错 →  Invalid command syntax: <shlex 报错>
  ```

- **去向**：REPL 里 `route_slash_command`（`interactive.py:293-303`）→ `render(render_message(result.message, styled=result.markup))`，`render` 就是 `ui_writer` → 直接进线程安全路径；`should_query` 时把展开的 prompt 放进 `job_queue`，下一轮才走 run_turn → run_agent。一次性模式则 `console.print(slash_result.message, markup=...)`（`cli.py:145-147`）Rich 直打。

### cron 到点 prompt

- **产生点**：`handle_cron_prompt`（`interactive.py:305-314`）。
- **内容**：`render_user_prompt(text, source="cron")`（`interactive.py:207-218`）构造一个 ANSI chunk——每行加 `> ` 前缀、白字深灰底（`\x1b[97;48;5;238m...`）反白高亮：

  ```
  > 你的 cron 到点提示内容（白色字、深灰背景、整行铺满）
  ```

- **去向**：slash 就地分发；普通 prompt 先 `writer(render_user_prompt(...))` 把提示行渲染进面板，再把原文放进 `job_queue` 走正常 run_turn（→ run_agent）。

---

## 3. 最终渲染：两个后端

### 一次性模式（Rich 直打）

`run_once` 调 `run_agent` 时不传 `output`（`cli.py:77-86`）→ `print_output` 就是 `console.print`。流式行、trace、diff 全部 Rich 直打 stdout，**无跨线程、无缓冲、无样式转换**。纯文本 trace 与 ANSI 内容混排由 Rich 统一处理。

### REPL 模式（线程安全搬移 + 样式化面板）

`run_turn` 把 `output`（即 `ui_writer`）传给 `run_agent`（`cli.py:196-208`）。剩余链路：

1. **离屏渲染**：`print_output` → `render_console_chunk` 把 Rich 对象渲染成 ANSI 字符串，包成 `OutputChunk(text, format="ansi"|"plain")`（`output.py:21-38`）。
2. **跨线程**：`output(chunk)` = `ui_write` = `loop.call_soon_threadsafe(append_output, chunk)`（`interactive.py:417-418`）——从 worker 线程跳到 prompt_toolkit 事件循环线程。**这是唯一的跨线程接缝**。
3. **落 transcript**：`append_output`（`interactive.py:396-415`）→ `append_output_text`（`interactive.py:86-99`）：
   - `_chunk_fragments`（`interactive.py:80-82`）：`format == "ansi"` 才解析成 prompt_toolkit 的 `StyleAndText` 样式片段；裸 `str` 永不解析——**格式标志即安全边界**。
   - 同时保留一份剥掉 ANSI 的 `display_text`，喂给 session transcript 边车，resume 时重放。
4. **重绘**：`output_buffer.set_document(...)` + `output_follow_position`（视口停在底部才自动滚动，上滚阅读历史时不被拽走）+ `app.invalidate()` 触发刷新。
5. **渲染**：prompt_toolkit 的 `BufferControl` + `TranscriptLexer`（`interactive.py:160+`）把片段按行渲染进输出面板。

---

## 4. 关键设计约束（踩坑提醒）

1. **`observation:` 过滤在 `emit`，不在工具层**——改动显示策略时先找 `emit` 的过滤，别在工具里拼字符串。
2. **行边界契约是隐式的**——`OutputWriter` 的类型不表达"只收完整行"，保证它的是上游 `_LineBufferedStreamRenderer`。新增写入路径时务必保证整行。
3. **`tool_result` 必须按 `tool_use` 顺序返回**——并行只读组靠 `ThreadPoolExecutor.map` 保序（`agent.py:329-338`），这是回填模型的协议约束，与显示无关但改动时最易破坏。
4. **`str` 输入恒为 plain**——需要上色必须显式走 `render_console_chunk(styled=True)` 或 Rich markup 的 `print_output`；直接传 `str` 进 `ui_writer` 只会得到纯文本。
5. **slash 结果绕过 loop**——它不经 `emit`/`trace`，也不会被 `observation:` 过滤影响；若要 slash 输出也进 trace，需在 `route_slash_command` 显式处理。

---

## 5. 调试速查表

| 想找的输出 | 产生点 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `agent.py` stream_renderer → write_stream_line | 否（final 行是） | 是 |
| `tool_call: ...` | `tool_execution.py:175` | 是 | 是 |
| `observation: ...` | `tool_execution.py:220` 等 | 否 | **否**（只给模型） |
| diff / 命令确认 | `tool_execution.py:124-130` | 否 | 是（ANSI 彩色） |
| PostToolUse hook | `tool_execution.py:230` | 否 | 是（dim） |
| PreToolUse 拦截 | `tool_execution.py:190` | 否 | 否 |
| Stop hook 强制续写 | `agent.py:295-301` → `continue:` | 是 | 是 |
| compacted 提示 | `agent.py:206` | 否 | 是 |
| slash 结果（REPL） | `interactive.py:293-303` | 否 | 是 |
| cron 提示行 | `interactive.py:313` | 否 | 是 |
| interrupted | `agent.py:174` | 是 | 是 |
