"""交互式 REPL 的 UI 构建块。

scrollback + Live 式 REPL（借鉴 kimi-cli 的终端所有权模型）：

- pt PromptSession（prompt_async）是终端唯一所有者：输入行 + message 区
  （Live：spinner / 流式尾部 / 待确认预览）+ bottom_toolbar 状态栏。
- 输入区带 Frame（show_frame）并与底部对齐：``layout._install_bottom_
  anchored_layout`` 就地改造布局根——顶部插一个吸收剩余高度的 spacer，
  buffer 窗口 dont_extend_height，输入框便始终钉在终端底边
  （与 Claude Code 一致），而不是贴着 scrollback 尾部漂移。
- 已完成的内容经 ``sys.stdout.write``（在 patch_stdout 内）落到原生终端
  scrollback——原生滚动/选区/复制，pt 不再管理历史区，也没有全量重设
  Document 的成本（正是输出面板性能档案里"正解方向"的落地）。
- wire 事件：worker 线程 send → call_soon_threadsafe 桥 → UI 协程消费。
  已完成事件立刻提交 scrollback；流式 TextDelta 只保留尾部几行显示在
  Live 区，turn 结束时整段提交。

按职责细分的子模块：
- completer:  slash 命令补全器
- scrollback: OutputChunk → ANSI → 原生终端 scrollback 的提交路径
- live:       Live 区状态（spinner / 流式尾部 / 待确认预览）
- events:     已完成 wire 事件 → OutputChunk 的翻译
- prompts:    提问（confirm/choice）请求的渲染与答案解析
- layout:     底部状态栏与"输入区钉底"布局改造
- shell:      InteractiveShell REPL 主体（线程模型 + 输入循环）
"""
