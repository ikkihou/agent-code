# agent-code vs kimi-cli：差距分析与改进路线

本文档基于 2026-11-16 对 kimi-cli（MoonshotAI，Python 版 v1.52.0，已归档、由 Kimi Code CLI 接替）的调研，对比 agent-code 在 **agent 功能** 与 **UI 交互** 上的差距，并给出按性价比排序的改进路线。调研覆盖了 kimi-cli 的 UI 层（`src/kimi_cli/ui/`）与 agent 能力层（`packages/kosong`、`packages/kaos`、`src/kimi_cli/`）。

**总体结论：agent-code 的骨架（wire 事件解耦、scrollback+Live、三层权限、JSONL 会话持久化、hooks、cron）与 kimi-cli 同构且更精简，方向正确。差距集中在四块：① 循环健壮性（重试/去重/压缩质量）、② 子代理与后台任务、③ 渲染质量（Markdown/diff/工具块）、④ 确认交互的细度。**

---

## 一、Agent 功能层面

### 1. 子代理（差距最大的一项）

**kimi-cli**：完整的子代理体系。

- `Agent` 工具 + LaborMarket 注册表（`src/kimi_cli/subagents/registry.py`）。默认类型定义在 `src/kimi_cli/agents/default/`（YAML）：coder（全量工程工具）、explore（只读探索）、plan（只读规划），支持 `extend` 继承、`allowed_tools`/`exclude_tools` 工具白名单、`when_to_use`。
- 子代理通过 `Runtime.copy_for_subagent` 克隆运行时：独立上下文，共享 approval / wire hub；跑完只把最后摘要返回父代理（`run_with_summary_continuation`）；嵌套被禁止（子代理均排除 Agent 工具）。
- **后台 agent 任务**：`src/kimi_cli/background/`（manager / store / worker 独立进程 / agent_runner）。bash 任务（Shell 工具 `run_in_background` 字段）与 agent 任务统一管理，`max_running_tasks` 默认 4；状态机（running/awaiting_approval/completed/failed/timed_out/killed）、心跳 + stale 检测、完成时生成通知（下一 step 注入 LLM 并触发 Notification hook）。
- 模型侧有三个工具主动管理：`TaskList` / `TaskOutput` / `TaskStop`。

**agent-code**：完全没有子代理。`tool_execution.py` 的并行批次只是并行执行 read-only 工具调用，不是并行智能体。

**改进路径**：加一个 `agent`/`task` 工具，在独立线程里跑 `run_agent()`——基础设施已现成：`Wire` 解耦让子代理事件可隔离或转发，`MockProvider` 让子代理可测。先做 explore 型只读子代理（工具白名单），再补后台任务管理三件套。

### 2. 上下文管理

**kimi-cli**：

- LLM 摘要压缩（`soul/compaction.py` `SimpleCompaction`，保留最近 2 条消息）。
- 触发条件基于 token：`token_count >= max_context * 0.85`（`should_auto_compact`，比例与预留量可在 LoopControl 配置）。
- 手动 `/compact` 支持自定义指令；压缩后注入仍在运行的后台任务快照。
- token 预算：`llm.py` `compute_max_completion_tokens` / `estimate_request_tokens` 按需计算输出上限。

**agent-code**：`compact_basic.py` 的 `compact()` 是纯机械替换（无 LLM），触发信号是 `len(messages) > 40`——消息数而非 token 数。

**改进**：优先把触发信号换成 token 用量（streaming 响应的 `usage` 现成可得）；其次再做 LLM 摘要压缩。

### 3. 循环健壮性

**kimi-cli**（`soul/kimisoul.py`，约 2000 行）：

- tenacity 指数退避 + jitter 重试（0.3–5s，`max_retries_per_step` 默认 3）；错误分类 `classify_api_error`（rate_limit/auth/overloaded/5xx/network/timeout/context_overflow），401 时刷新 OAuth token 重试。
- **同参工具调用去重**（`soul/toolset.py`）：同一 step 内相同 `(tool, canonical_args)` 直接复用原任务结果；跨 step 重复调用注入 reminder（`_build_repeat_reminder`），连续重复达阈值强制停 turn（`stop_reason="tool_call_repeat"`）。
- `max_steps_per_turn` step guard（默认 1000）。
- steer 队列：用户在 turn 进行中插入消息，立即注入运行中回合的上下文（`_consume_pending_steers`）。
- `/btw` 侧问：本地 LLM 调用、DenyAllToolset，不污染主对话。

**agent-code**：只有 max_tokens 截断的两次 "continue" 重试。无网络错误退避重试、无重复调用检测、无 step 上限、无 steer。

**改进**："同参去重 + 连续重复检测强制停止"成本很低而收益高——当前循环没有任何防呆，模型陷入重复调用时会烧光上下文。

### 4. MCP / 插件 / Skills / 多 provider

**kimi-cli**：

- MCP 客户端（fastmcp，stdio/http + OAuth 远程服务器，`soul/toolset.py` 的 `load_mcp_tools`；支持延迟加载 `defer_mcp_tool_loading`，CLI `kimi mcp`）。
- plugin 机制（`src/kimi_cli/plugin/`，install/list/remove，自定义工具动态加载）。
- Agent Skills（`src/kimi_cli/skill/`）：SKILL.md 规范，多目录发现（builtin / 用户 / 项目 / 品牌目录合并），注册为 `skill:<name>` 斜杠命令并注入 system prompt；还支持 flow 型技能（`skill/flow/`，Ralph 式迭代循环）。
- 多 provider：kimi / openai_legacy / openai_responses / anthropic / google_genai / vertexai，每 provider 独立 base_url/api_key/OAuth（`llm.py`）。

**agent-code**：单 provider（Anthropic 兼容协议，可换 base_url 但只有这一种 wire 格式）；无 MCP、无 skills、无插件。`web_fetch`/`web_search` 已实现但尚未注册进 `default_tools()`。

**改进顺序**：web 工具注册（几分钟的事）→ SKILL.md 式 skills（只是目录发现 + system prompt 注入，成本低）→ MCP 客户端（成本高，放最后）。

### 5. Checkpoint / 回滚

**kimi-cli**：

- 上下文级 checkpoint（`soul/context.py`，JSONL 追加 `_checkpoint` 行，`revert_to` 时 rotate 文件重建）。
- `SendDMail` 工具（"来自未来的自己的信"）：模型可回滚到 checkpoint 并注入消息重新决策。
- turn 级 session fork（`session_fork.py`：枚举 turn、截断 wire/context 复制成新会话）。

**agent-code**：`file_history.py` 有**写前文件备份**（`.agent/history/<rel>/<ts>`），但没有恢复入口——备份写了却用不上。

**改进**：加 `/undo` 斜杠命令 + `file_restore` 工具，把已有设施闭环。session fork（resume 后从指定 turn 截断另存新 id）也不难。

### 6. 权限系统

**kimi-cli**（`soul/approval.py` + `approval_runtime/`）：

- 模式：yolo（全自动批准）/ afk（无人值守，隐含 auto-approve）/ plan（只读规划）；approval 状态持久化在 session state，跨重启保留。
- 审批响应四选一（`wire/types.py:293`）：approve / **approve_for_session（会话内缓存）** / reject / **reject + inline feedback（拒绝时直接告诉模型该怎么做）**。子代理收到拒绝会继续换方案而不是终止 turn。
- hooks 可 block：PreToolUse exit code 2 = block；JSON `permissionDecision: "deny"` = block；UserPromptSubmit/Stop hook 也可 block。fail-open（hook 失败/超时 = allow）。
- 无 OS 级 sandbox，安全靠审批 + hooks。

**agent-code**：default / acceptEdits / plan / fullAccess 四种模式，但确认只有二元的"确认/拒绝"。hooks 事件只有 PreToolUse / PostToolUse / Stop。

**改进**："本次会话记住"（session 级 allow 集合）和"拒绝并给模型反馈"（拒绝文案作为 error block 返回）两个选项实现成本低、体验提升明显。hooks 可补充 PostToolUseFailure / PreCompact / SessionStart / Notification 等事件。

### 7. 会话系统

**kimi-cli**：每工作目录 `sessions/<id>/` 目录，context（消息 JSONL）/ wire（UI 事件日志）/ state（approval 设置、plan mode、custom_title、additional_dirs）分文件存；`--resume [ID]`、`--continue`、fork、通知队列。

**agent-code**：单 JSONL + 渲染 sidecar。**已知 bug 要先修**：`-c`/`--continue` 坏了——`Session.load_latest` glob `*.jsonl` 会挑到更新的 `.transcript.jsonl` sidecar，随后 `history` 抛 `KeyError: 'role'`，replay 为空。

### 8. 其他

- **配置系统**：kimi-cli 用 TOML（tomlkit）+ Pydantic 的 `config.toml`（models/providers、loop_control、background、mcp、hooks、skills、telemetry），支持环境变量覆盖。agent-code 无配置文件。
- **遥测**：kimi-cli 有可选 telemetry（turn/api_error/tool_call/compaction 等）。
- **ACP / wire server**：kimi-cli 支持 `kimi acp`（Agent Client Protocol，适配 Zed/JetBrains）与 WebSocket wire server——对外集成面比 agent-code 宽得多，但属于锦上添花。

---

## 二、UI 层面

两者的底层设计一致：prompt_toolkit 3.0.52 + Rich、scrollback+Live、wire 事件解耦、相邻 TextDelta 合并、已提交内容进 scrollback 尾部留 Live。agent-code 的骨架是对的，差距在渲染质量与交互细节。

### 1. 输出渲染（差距明显）

- **Markdown 流式渲染**：kimi-cli fork Rich Markdown 自研渲染器（`utils/rich/markdown.py`，markdown-it-py 解析），并用 `_blocks.py::_ContentBlock` 做块级流式提交——解析块级 token，确认边界（`_find_committed_boundary`）外的块 flush 到 scrollback，尾部留在 Live。**流式期间就有 Markdown 结构**。agent-code 只按空行分段提交，流式期间是纯文本。
- **Diff 渲染**：kimi-cli 有专门 `utils/rich/diff_render.py`——带行号 Table、主题化背景色、行内改动高亮、+/- 统计 header、多 hunk 分隔；区分审批预览（`render_diff_preview`）与结果展示（`render_diff_panel`）。agent-code 只有 `DiffPreview` 事件，且 REPL 路径把它压缩成一行 "waiting for approval"。
- **Thinking 展示**：kimi-cli 把 thinking 收成一行 "Thought for Xs · N tokens"（灰字，token 估算含 CJK 特判），可选恢复滚动预览。不刷屏。

### 2. 工具调用展示

kimi-cli 每个工具调用一个 `_ToolCallBlock`：运行中转圈 bullet → 完成变绿/红（错误），按结果类型渲染（Diff → diff 面板、Todo → Markdown 列表、Shell → 语法高亮）；子代理内部调用折叠到最近 4 条，更多显示 "N more tool calls ..."。

agent-code 的 `ToolCallStarted`/`ToolCallCompleted` 事件已有，展示粒度更粗。**这块只需在 `ui/events.py` 按工具名分发渲染，wire 层不用动。**

### 3. 输入体验

| 能力 | kimi-cli | agent-code |
|---|---|---|
| 输入历史持久化 | ✅ JSONL 按目录存 + 连续去重，↑ 回溯 | ❌ |
| @文件引用补全 | ✅ `LocalFileMentionCompleter` | ❌ |
| 粘贴大文本占位符化 | ✅ `[Pasted text #N]` + AttachmentCache | ❌（大粘贴瞬间撑爆 Live 区）|
| 流式中输入路由 | ✅ queue / steer / btw / send 单一决策点（`classify_input`） | 只有排队 |
| 外部编辑器 | ✅ Ctrl+O 打开 `$EDITOR` | ❌ |
| Shell 模式 | ✅ Ctrl+X 切换（`$` 提示符，cd 特判） | ❌ |
| 底栏信息 | ✅ 状态标签 + CWD + git 分支（dirty/ahead/behind，TTL 缓存）+ 后台任务徽章 + context 用量% + 轮换快捷键 tips；窄终端逐级降级 | 只有 mode + model |
| toast 系统 | ✅（如 "plan mode ON/OFF"） | ❌ |
| 主题 | ✅ dark/light 集中定义，`/theme` 切换 | ❌ |
| resume 回放 | ✅ 只重放最近轮次 | 全量重放 |

性价比排序：**输入历史持久化**（pt `FileHistory`，一行的事）→ **底栏加 context 用量 + git 分支** → **@文件补全** → **粘贴占位符化**。

### 4. 权限确认 UI

kimi-cli 是模态面板（`visualize/_approval_panel.py`）：黄色边框 Panel，↑↓ 或数字键 1-4 四选一（含 session 记住 / 拒绝+反馈，选中反馈项直接在菜单里输入文字带光标渲染）；diff 预览带行数预算，截断显示 "ctrl-e to expand"，**Ctrl+E 把完整内容送 pager**（暂停 Rich Live，退出后恢复渲染）；多个审批请求 deque 排队轮转。

agent-code 的核心问题：**diff/命令全文在 REPL 里根本看不到**（`_consume_events` 短路径截掉），无法做知情确认。先把"审批时展示完整 diff（截断到合理行数）"做了，四选项面板随后。

---

## 三、改进路线（按性价比排序）

1. **修 `-c` bug** + **输入历史持久化** —— 两个小修复，立刻改善日常使用。
2. **审批体验**：完整 diff 预览（行数预算）+ "approve for session" / "reject with feedback" 两选项。
3. **循环防呆**：同参工具调用去重 + 连续重复检测强制停 turn + token 用量触发压缩。
4. **web 工具注册** + SKILL.md 式 skills。
5. **Markdown/diff 流式渲染升级**（`ui/events.py` + `output.py`，wire 层不动）。
6. **子代理**（explore 型只读子代理起步，复用 `run_agent` + `Wire`）→ 后台任务管理（TaskList/TaskOutput/TaskStop）。
7. MCP 客户端（最后，工作量大）。

另有两个顺手项：`/undo`（利用已有的 `file_history.py` 备份）与 `/compact` 手动压缩指令。

---

## 附：kimi-cli 关键文件索引（供深挖参考）

- Agent 循环：`src/kimi_cli/soul/kimisoul.py`；agent/AGENTS.md 加载 `soul/agent.py`；agent 规范解析 `agentspec.py`
- 工具基类：`packages/kosong/src/kosong/tooling/__init__.py`（`CallableTool2[Params]` 用 Pydantic 自动生成 JSON Schema；`ToolOk/ToolError` 区分模型输出与 UI 展示 `DisplayBlock`）；`kosong.step` 在 `packages/kosong/src/kosong/__init__.py`
- 工具集 / MCP：`src/kimi_cli/soul/toolset.py`
- 权限：`src/kimi_cli/soul/approval.py`、`src/kimi_cli/approval_runtime/`
- 压缩 / 上下文 checkpoint：`soul/compaction.py`、`soul/context.py`
- 会话：`session.py`、`session_state.py`、`session_fork.py`
- 子代理：`src/kimi_cli/subagents/`、`src/kimi_cli/agents/default/*.yaml`
- 后台任务：`src/kimi_cli/background/`
- Hooks：`src/kimi_cli/hooks/`；配置：`src/kimi_cli/config.py`；LLM/provider：`llm.py`
- Skills：`src/kimi_cli/skill/`；插件：`src/kimi_cli/plugin/`
- UI：`ui/shell/__init__.py`（Shell 主循环）、`ui/shell/prompt.py`（输入体验）、`ui/shell/visualize/_live_view.py` / `_blocks.py`（流式渲染）、`visualize/_approval_panel.py` / `_question_panel.py`（模态面板）、`visualize/_input_router.py`（输入路由）、`utils/rich/markdown.py` / `diff_render.py`（渲染库）、`ui/theme.py`（主题）、`wire/types.py`（事件协议）
