# Agent 输出流水线（从 loop 到终端）

本文档追踪 agent 运行期间产生的所有输出——流式模型文本、`tool_call` trace、diff 预览、hook 结果、slash 执行结果、cron 提示——从产生到最终在终端被打印/渲染的完整旅程。

**核心结论：agent loop 内所有要上终端的输出收敛到一个通道——`emit(text, markup=)`（trace 双写），流式模型文本另走 `write_stream_line`（不进 trace）。两者都把完整行的 `OutputChunk` 交给唯一的 `OutputWriter` 回调，由前端决定渲染方式。样式片段（prompt_toolkit 内联样式串）是规范样式表示，live 路径不再经过 ANSI。**

```
                    ┌───────────────────────────────────────────────┐
                    │  agent loop（worker 线程，agent_code/agent.py）│
                    │                                               │
  流式模型文本  ────▶  _LineBufferedStreamRenderer → write_stream_line │
  tool_call /   ────▶  emit(text, markup=) ──▶ trace.append          │
  continue/final      （observation 不再发终端：只经 tool_result 回填） │
  diff/确认预览 ────▶  emit(..., markup=True)                        │
  hook 结果    ────▶  emit / emit(markup=True)                        │
                    └──────────────┬────────────────────────────────┘
                                   │
                          OutputChunk(text, format,
                                      fragments)
               ┌────────────────────┴────────────────────┐
 一次性模式    │                            REPL 模式     │
 output=None  │                            output=ui_writer
 console.print│                            loop.call_soon_threadsafe（唯一跨线程接缝）
 Rich 直打    │                            OutputTranscript（片段 + 纯文本）
 ────────────▶ 终端                          TranscriptLexer → 输出面板
                                             （session 转录边车持久化 chunk，
                                              resume 重放）
```

---

## 1. 唯一发射通道：`emit(text, markup=)`

定义在 `agent_code/agent.py` 的 `run_agent` 内：

```python
def emit(text: str, *, markup: bool = False) -> None:
    trace.append(text)
    if output is not None:
        output(OutputChunk(text + "\n", format="rich" if markup else "plain"))
    elif markup:
        console.print(text, markup=True, highlight=False)
    else:
        console.print(text, markup=False, highlight=False)
```

- **双写**：每行既进 `AgentResult.trace`，又上终端。
- `markup=True` 表示 text 是 Rich markup（如 `render_diff` 的输出、`compacted:` 行），由前端各自渲染。
- 旧的 `"observation:"` 字符串前缀过滤协议已删除：工具返回内容不再发往终端，只经 `tool_result` block 回填给模型（`tool_execution.py` 里 error 直接构造 block）。

流式模型文本走 `write_stream_line`（不进 trace，`emit` 的孪生兄弟）：`_LineBufferedStreamRenderer` 把半行攒成完整行后调用它。

## 2. OutputChunk：唯一输出单元

`agent_code/output.py`：

```python
@dataclass(frozen=True)
class OutputChunk:
    text: str                       # 永远是无 ANSI 纯文本
    format: OutputFormat = "plain"  # plain | rich | ansi
    fragments: tuple[StyleAndText, ...] = ()  # 即用样式片段
```

- `text` 永远是纯文本——持久化、剪贴板复制、纯文本消费方都用它。
- `fragments` 非空时即为最终样式，前端直接采用，**不再做任何解析**。
- `format == "rich"` 表示 text 是 Rich markup，由 `chunk_fragments()`（`output.py`）归一化成片段：Rich 离屏 `render_str` → segments → 翻译成 prompt_toolkit 内联样式串。**不经过 ANSI。**
- `format == "ansi"` 仅旧版 transcript 记录回放出现（`ANSI().__pt_formatted_text__()` 解析）。
- 离屏 Console 进程内共享一个（`_get_offscreen_console`），宽度 4096 不折行——折行交给前端。

样式翻译的词汇差异（`output.py::_pt_style`）：

| Rich | prompt_toolkit | 备注 |
|---|---|---|
| `bold` / `dim` / `red` / `#ff0000` | 相同 | 两边词表同源，直接透传 |
| `on #444444` | `bg:#444444` | 背景色写法差异 |
| `color(238)` | `#444444` | 256 色转等值 hex |
| `not bold` 等关闭态 | （丢弃） | pt 词表无对应，样式缺失优于崩溃 |

## 3. 每种输出各自的旅程

| 输出 | 产生点 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | stream_renderer → write_stream_line | 否（final 行是） | 是 |
| `tool_call: ...` | `tool_execution.py` execute_one_tool_call 开头 | 是 | 是 |
| 工具返回内容 | （不再发终端） | 否 | 否（只经 tool_result 给模型） |
| diff / 命令确认预览 | `_handle_ask` → `emit(markup=True)` | 是（markup 原文） | 是（样式渲染） |
| PostToolUse hook | `tool_execution.py` → `emit(markup=True)` | 是 | 是（dim） |
| PreToolUse 拦截 | 构造 error block，不发射 | 否 | 否（进 tool_result） |
| Stop hook 强制续写 | `continue_with` → `emit` | 是 | 是 |
| `continue:` / `interrupted` / `final:` | `agent.py` 各分支 → `emit` | 是 | 是 |
| `compacted:` | `agent.py` → `emit(markup=True)` | 是 | 是（dim） |
| ask_user_question 选择 | `_handle_ask` → `emit(f"choice: ...")` | 是 | 是 |
| plan 拒绝 | `execute_one_tool_call` → `emit(f"plan rejected: ...")` | 是 | 是 |
| slash 结果（REPL） | `interactive.py` `_route_slash_command` | 否 | 是 |
| cron 提示行 | `interactive.py` `_handle_cron_prompt` | 否 | 是 |
| 用户输入回显 | `interactive.py` `_submit_text` → `render_user_prompt` | 否 | 是（深灰底块） |

`render_user_prompt` 直接构造 `(style, text)` 片段（`"ansiwhite bg:#444444"`），不再内嵌 ANSI 转义。背景铺满整行仍由 `TranscriptLexer` 渲染时补位实现。

## 4. 最终渲染：两个后端

### 一次性模式（Rich 直打）

`run_once` 调 `run_agent` 时不传 `output` → `emit`/`write_stream_line` 直接 `console.print`。markup 行交给 Rich 渲染，纯文本行关闭 markup/highlight。无跨线程、无样式转换。

### REPL 模式（线程安全搬移 + 片段面板）

`run_turn` 把 `output`（即 `ui_writer`）传给 `run_agent`。剩余链路：

1. **跨线程**：`output(chunk)` = `ui_write` = `loop.call_soon_threadsafe(append_output, chunk)`——从 worker 线程跳到 prompt_toolkit 事件循环线程。**这是唯一的跨线程接缝**。
2. **落 transcript**：`append_output` → `append_output_text`（`interactive.py`）→ `chunk_fragments`（`output.py`，归一化入口）：
   - fragments 非空 → 直用；
   - rich → `rich_markup_fragments` 翻译；
   - ansi → 旧记录回放解析。
   同时保留 `text` 纯文本，喂给 session 转录边车（chunk 含 fragments 时一并持久化，resume 重放还原样式）。
3. **重绘**：`output_buffer.set_document(...)` + `output_follow_position`（视口停在底部才自动滚动，上滚阅读历史时不被拽走）+ `app.invalidate()` 触发刷新。
4. **渲染**：prompt_toolkit 的 `BufferControl` + `TranscriptLexer` 把片段按行渲染进输出面板。

## 5. 关键设计约束（踩坑提醒）

1. **`OutputWriter` 只收完整行**——流式半行由 `_LineBufferedStreamRenderer` 攒行，`emit` 发射的行自带行尾 `\n`（chunk.text 里的）。新增写入路径时务必保证整行。
2. **observation 不再走终端通道**——工具返回内容只经 tool_result block 回填给模型；终端只看"调了什么"（`tool_call` 行）。要改显示策略去找 `tool_execution.py` 的 block 构造，别在工具里拼字符串。
3. **`tool_result` 必须按 `tool_use` 顺序返回**——并行只读组靠 `ThreadPoolExecutor.map` 保序，这是回填模型的协议约束，与显示无关但改动时最易破坏。
4. **`emit(text, markup=)` 是唯一 keyword 签名**——`emitted.append` 这类只收单参数的回调满足不了它（`TypeError`），测试里用 `_make_emit` 包一层。
5. **`str` 输入恒为 plain**——需要样式必须 `markup=True` 或直接给 `fragments`；裸 str 进 transcript 只有纯文本。
6. **slash 结果绕过 loop**——它不经 `emit`/`trace`；若要 slash 输出也进 trace，需在 `route_slash_command` 显式处理。

## 6. 调试速查表

| 想找的输出 | 产生点 | 进 trace? | 终端可见? |
|---|---|---|---|
| 流式回答文本 | `agent.py` stream_renderer → write_stream_line | 否（final 行是） | 是 |
| `tool_call: ...` | `tool_execution.py` | 是 | 是 |
| 工具返回内容 | tool_result block | 否 | **否**（只给模型） |
| diff / 命令确认 | `tool_execution.py` `_handle_ask` | 是 | 是（样式渲染） |
| PostToolUse hook | `tool_execution.py` | 是 | 是（dim） |
| PreToolUse 拦截 | error block | 否 | 否 |
| Stop hook 强制续写 | `agent.py` → `continue:` | 是 | 是 |
| compacted 提示 | `agent.py` | 是 | 是（dim） |
| slash 结果（REPL） | `interactive.py` | 否 | 是 |
| cron 提示行 | `interactive.py` | 否 | 是 |
| interrupted | `agent.py` | 是 | 是 |
