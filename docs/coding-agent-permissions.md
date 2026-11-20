# 主流 Coding Agent 权限系统调研

> 调研时间：2026-11-20。信息来源为各产品官方文档 / 官方仓库（文中标注链接），均为当期最新版本的行为。
> 目标：梳理业界权限系统的实现模式，为本项目（agent-code）的权限体系演进提供参考。

---

## 1. 调研范围

| 产品 | 形态 | 开源 | 官方文档 |
|---|---|---|---|
| Claude Code | CLI / IDE 插件 | 否（Agent SDK 开放） | [code.claude.com/docs/en/permissions](https://code.claude.com/docs/en/permissions) |
| OpenAI Codex CLI | CLI / IDE / 云端 | 是（Apache-2.0，Rust） | [learn.chatgpt.com/docs/agent-approvals-security](https://learn.chatgpt.com/docs/agent-approvals-security)、[permissions.md](https://learn.chatgpt.com/docs/permissions) |
| Google Gemini CLI | CLI | 是（Apache-2.0，TS） | [github.com/google-gemini/gemini-cli docs/reference/policy-engine.md](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md) |
| Cursor | IDE Agent | 否 | [cursor.com/docs/agent/security](https://cursor.com/docs/agent/security)、[run-modes](https://cursor.com/docs/agent/security/run-modes) |
| Cline | VS Code / JetBrains / CLI | 是（Apache-2.0，TS） | [docs.cline.bot/features/auto-approve](https://docs.cline.bot/features/auto-approve) |
| Goose（Block/Square） | CLI / Desktop | 是 | [goose-docs.ai/docs/guides/managing-tools/goose-permissions](https://goose-docs.ai/docs/guides/managing-tools/goose-permissions) |
| OpenHands | SDK / 云端 | 是（SDK） | [docs.openhands.dev/sdk/guides/security](https://docs.openhands.dev/sdk/guides/security) |
| Aider | CLI（结对编程） | 是 | [aider.chat/docs/git.html](https://aider.chat/docs/git.html) |

---

## 2. 权限系统的四层架构（业界共性）

各家实现细节差异很大，但几乎所有主流产品都收敛到了同一套分层模型——**从上到下依次判定，命中即止**：

```
①  企业/管理员管控层（managed policy，不可被用户覆盖）
②  显式规则层（deny / ask / allow 规则，用户或团队配置）
③  权限模式层（mode / profile / approval policy，决定"谁来把关"）
④  技术沙箱层（OS 级 sandbox：文件系统、网络边界；规则层绕不过去时的最后防线）
```

再加两个横切机制：

- **只读白名单**：read-only 工具/命令默认放行，不进入审批流程。
- **LLM 辅助审查**（新兴趋势）：Claude Code 的 `auto` 模式分类器、Cursor 的 Auto-review、OpenHands 的 `LLMSecurityAnalyzer`，用一个（通常是更小的）模型对"要执行的动作"做风险评审，替代人肉点击。

下面逐产品展开。

---

## 3. 产品详述

### 3.1 Claude Code —— 规则引擎 + 模式 + 分类器的标杆实现

**三层判定顺序：deny → ask → allow**，第一个命中的规则生效；规则特异性不改变顺序（deny 规则不允许被 allow 规则"挖洞"例外）。

**规则语法**：`Tool` 或 `Tool(specifier)`。

- 裸工具名 deny（如 `Bash`）= 从模型上下文中**整个移除该工具**（模型根本看不到）；带 specifier 的 deny（如 `Bash(rm *)`）= 工具保留、命中调用被拦截。
- 通配符：`Bash(npm run *)`；`*` 必须放在子命令之后（`Bash(git * main)` 会放行一切 git 子命令，启动时会告警）。行尾 `ls *` 等价于 `ls:*`，且空格属于规则的一部分（`ls*` 连 `lsof` 都匹配）。
- 参数匹配：`Tool(param:value)` 可以匹配任意顶层标量参数（如 `Agent(model:opus)`、`Bash(run_in_background:true)`），但**禁止匹配主内容字段**（Bash 的 `command`、Read/Edit 的 `file_path`、WebFetch 的 `url`）——因为这类规则可被复合命令绕过，写了会启动告警。
- 工具名通配：deny/ask 支持 `"*"`、`"mcp__*"`；allow 只接受 `mcp__<server>__*` 形式（server 段不能带通配）。

**Bash 规则的工程细节**（非常值得借鉴的部分）：

- **复合命令感知**：识别 `&& || ; | |& &` 和换行作为分隔符，每个子命令都必须被允许才免审批；deny/ask 规则对子命令（包括子 shell、命令替换、for 循环体内）同样生效。
- **wrapper 剥离**：固定剥离 `timeout/time/nice/nohup/stdbuf/command/builtin/noglob` 和裸 `xargs`，因此 `Bash(npm test *)` 也能匹配 `timeout 30 npm test`；已知安全环境变量赋值（如 `NODE_ENV=test`）同理，而 deny/ask 规则会穿透任意变量赋值。`npx/docker exec/devbox` 等**环境执行器不剥离**（它们可以执行任意参数，粒度必须精确到内层命令）。
- **Bash 规则的诚实声明**：规则匹配的是"模型写的命令文本"，不是安全边界——`Bash(curl *)` 拦不住 `/usr/bin/curl ...`；文档明确建议真正的文件/网络管控交给沙箱。
- **内置只读命令集**：`ls/cat/echo/pwd/head/tail/grep/find/wc/which/diff/stat/du/cd` 及只读 git 形式，任何模式下都免审批（不可配置，需用 ask/deny 规则覆盖）；并有一批精巧的例外检测（未引号 glob + 有写入能力 flag 的命令会审批、`ps auxe` 会泄漏环境变量、Windows UNC 路径会发送凭据等）。
- **审批持久化**："Yes, and don't ask again" 按类别有不同生命周期：Bash/WebFetch 领域规则永久写入 `.claude/settings.local.json`（复合命令会**按子命令拆分保存**，最多 5 条规则）；文件修改批准只在会话内有效。

**配置层级**：`~/.claude/settings.json`（个人）→ 项目共享 `.claude/settings.json`（入 git）→ 项目本地 `.claude/settings.local.json`（个人不入 git）→ 企业 managed settings（最高优先级、不可覆盖）。

**权限模式**：`default`(Manual) / `acceptEdits`（自动接受文件编辑） / `plan`（只读探索） / `auto`（**后台分类器替代人审**） / `dontAsk`（免打扰：本该弹窗的调用直接拒绝） / `bypassPermissions`（跳过全部提示，仅建议容器内使用；企业可用 `disableBypassPermissionsMode` 禁用）。无人值守场景还有 `permissions.denyInsteadOfPrompting`，把等待审批的调用直接拒绝而不是挂起。

**Hooks 扩展**：PreToolUse hook 可拦截或放行，但 hook 返回 allow **不会跳过** deny/ask 规则的评估——显式规则始终在 hook 之上。

### 3.2 OpenAI Codex CLI —— 沙箱为纲、审批为辅

Codex 的设计哲学与 Claude Code 不同：**先给一个 OS 强制的技术边界（sandbox），再在边界上叠审批策略**，规则匹配起辅助作用。

**Sandbox 模式**（OS 级强制）：

- `read-only`：只能看，改文件/跑命令都要审批。**未标记受信任的目录默认就是它**（首次启动 onboarding 决定是否信任）。
- `workspace-write`：可读全部、工作区内可写、可跑常规本地命令；**默认断网**（`[sandbox_workspace_write] network_access = true` 才开）。
- `danger-full-access`：无沙箱。

沙箱实现按平台用系统机制：macOS Seatbelt (sandbox-exec)、Linux Landlock + seccomp、Windows 本地沙箱。

**Approval policy**：`untrusted`（已被废弃）/ `on-request`（沙箱允许的动作免审批，越界时才问）/ `on-failure`（沙箱失败后申请提权重跑）/ `never`。常用预设组合：Read only、Auto（`workspace-write + on-request`，工作区内全自动、越界/联网才问）、Full Access。

**权限 Profiles**（新版，beta）：把沙箱规则升级为**命名策略**，统一声明文件系统 + 网络规则：

```toml
default_permissions = "project-edit"
[permissions.project-edit]
extends = ":workspace"                    # 内置 :read-only / :workspace / :danger-full-access
[permissions.project-edit.filesystem.":workspace_roots"]
"." = "write"
"**/*.env" = "deny"                       # 窄 deny 优先于宽 write，不可被例外
[permissions.project-edit.network.domains]
"api.openai.com" = "allow"
"tracking.example.com" = "deny"
```

内置三个：`:read-only` / `:workspace` / `:danger-full-access`；`extends` 继承，且 `:workspace` 基线**强制把 `.git/.agents/.codex/.aws` 保持只读**除非显式覆盖。旧 `sandbox_mode` 与新 profiles 互斥（二选一），企业可用 managed `requirements.toml` 限制可用 profile 白名单。

**其他**：执行策略规则（exec-policy，命令级 allowlist）；网络默认经代理（network proxy）才能落实域名规则；GPT-6 时代的异步"安全监控"可以在任务运行中检测不安全行为并**暂停任务**；带破坏性 annotation 的 MCP 工具调用总是要审批（read annotation 优先级更高）。

### 3.3 Gemini CLI —— TOML 策略引擎（最早做出完整规则引擎的开源实现）

Gemini CLI 新版引入了**Policy Engine**，是开源产品中少见的"独立、声明式、多层级"规则引擎。

**规则**（`~/.gemini/policies/*.toml`）：`[[rule]]` = 条件（`toolName` 通配、`argsPattern` RE2 正则、`commandPrefix`/`commandRegex` 简写、`mcpName`、`subagent`、`toolAnnotations`）+ 决策（`allow` / `deny` / `ask_user`）+ `priority`（0–999）。

**多层级优先级**（_Admin > User > Workspace > Extension > Default_）：`final_priority = tier_base + toml_priority/1000`，保证管理员策略永远压过用户策略，同层内再细排。

**决策语义**：deny（全局规则命中时**整个工具从模型上下文中剔除**，既安全又省 token——与 Claude Code 裸名 deny 行为一致）；`ask_user` 在非交互模式降级为 deny。

**特色**：

- **按模式生效**：规则可绑定 `modes = ["default","autoEdit","plan","yolo"]`。
- **审批持久化的模式感知**：plan 模式批准的规则覆盖全部模式；其他模式批准只覆盖当前模式及**更宽松的模式**（层级 `plan < default < autoEdit < yolo`），防止宽松环境"泄漏"信任。
- **shell 专项**：默认对**重定向**（`>`、`>>`）和**命令替换**（`$()`、反引号）单独要求确认，除非规则显式 `allowRedirection`/`allowCommandSubstitution`。
- **管理员策略安全检查**：系统策略目录必须 root 所有、不可组写（Linux/macOS），否则**整体忽略**该目录策略，防止提权。
- **模式族**：`default` / `autoEdit` / `plan` / `yolo`（全部自动批准）。

### 3.4 Cursor —— Run Modes + 分类器 + 沙箱的组合拳

Cursor 把选择做成三种 **Run Modes**（Settings > Agents > Approvals & Execution）：

| 模式 | 免审批范围 | 沙箱 | 分类器 |
|---|---|---|---|
| Auto-review（推荐） | 白名单动作直接跑；其余 shell 尽量进沙箱；出沙箱的动作交给分类器 | 是 | 是 |
| Allowlist | 只跑白名单；可选沙箱 | 可选 | 否 |
| Run Everything | 全自动 | 否 | 否 |

**Auto-review 分类器**：跑在 Cursor 后端的小模型（Gemini 3.5 Flash Lite + Haiku 兜底），评审 shell/MCP/Fetch 调用；它可以在用户机器上做**只读**的 ReadFile/Grep/Glob/ListDir 以理解命令上下文（比如读命令要执行的脚本）。分类器拒绝后 agent 可以换路径，或弹人工审批。文档明确写："Auto-review 不是安全边界"。

**配置即自然语言**：`permissions.json`（用户级 + 项目级合并）用**自然语言句子**表达 allow/block 倾向：

```json
{ "autoRun": {
    "allow_instructions": [],
    "block_instructions": ["Every AWS CLI command should go through approval first."] } }
```

**默认安全基线**（不配置时的行为）：读文件/搜代码免审批（`.cursorignore` 可隐藏敏感文件）；工作区文件改动免审批但配置文件要先批准；**终端命令默认全部要审批**；MCP 连接要先批准、连接后每次调用还要单独批准（可用 MCP allowlist 预批）；沙箱内网络默认阻断，`sandbox.json` 管理网络域名与额外读写路径；`.git/config`、`.git/hooks` 等敏感路径受保护。

### 3.5 Cline —— 按类别的开关矩阵 + 模型自评风险

Cline 的权限粒度是**工具类别开关**（Auto Approve 面板），不搞规则语法：

| 开关 | 说明 |
|---|---|
| Read project files / Read all files | 工作区内 / 外（需先开基础开关） |
| Edit project files / Edit all files | 同上 |
| Execute safe commands / Execute all commands | 安全命令 / 全部命令 |
| Use the browser / Use MCP servers | 浏览器、MCP |
| YOLO Mode | 全自动批准一切（明确警告危险） |

**特色点——模型自评 `requires_approval` 标志**：Cline **没有固定的命令安全白名单**，而是让模型自己给每条终端命令打 `requires_approval` 标志（`npm run build`/`git status` 通常标 safe；`npm install`/`rm -rf`/`sed -i`/`mv` 标需审批）。文档坦承"这些是示例而非保证"。这是"低成本高灵活性"路线的典型——安全性完全取决于模型自觉。

其他：Plan/Act 模式切换（Plan 只探索、Act 才执行）；checkpoints 用于回滚；`.clinerules` / `.clineignore` 限制行为与可见文件（`.clineignore` 已标注即将弃用）。

### 3.6 Goose —— 三档模式 + 工具级权限 + LLM 读写分类

- **三档权限模式**：Completely Autonomous（默认！全自动）/ Manual Approval（逐次确认）/ Smart Approval（**按风险自动批低危、高危上问**）。
- 工具级权限（Tool Permissions）在 approve/smart 模式下可细化。
- **读写判定交给 LLM**：goose 文档明说"对工具的 read/write 分类是 best-effort，由 LLM 提供方解读"——即用模型标注决定是否弹审批。
- **有趣的外部集成**：当用 Claude Code 等作为 CLI provider 时，goose 把它们的原生权限请求**路由进自己的确认界面**（机制同 Claude Agent SDK 的 `canUseTool`），统一审批体验。

### 3.7 OpenHands —— 策略对象 + 风险分级（SDK 视角最清晰）

OpenHands SDK 把权限拆成两个正交对象，SDK 视角最干净：

- **Confirmation Policy**（决定"要不要人批"）：`AlwaysConfirm()` / `NeverConfirm()` / `ConfirmRisky()`（需配合风险分析器）。拒绝时可带自然语言原因回传给 agent 让它换路径。
- **Security Analyzer**（决定"动作有多危险"）：给每个动作打 `LOW / MEDIUM / HIGH / UNKNOWN`。默认实现 `LLMSecurityAnalyzer`：**主模型在生成动作时顺手自评风险级别**（也可用独立 LLM 复核），`ConfirmRisky()` 与之配合实现"只批高危"。

另外提供 Docker 沙箱隔离执行环境（云端必用）。

### 3.8 Aider —— 不设权限系统的对照组

Aider 是唯一**没有权限审批系统**的：所有编辑直接落盘，靠 **git 自动提交**做安全网——每次修改自动 commit（`--auto-commits`），`/undo` 回滚上一次变更，`/diff` 查看变更，脏文件先做保护性提交（`--dirty-commits`）。即"**事后可逆**"替代"事前审批"。这对轻量结对工具成立，但对能跑任意 shell 的 agent 明显不够。

---

## 4. 横向对比

| 维度 | Claude Code | Codex CLI | Gemini CLI | Cursor | Cline | Goose | OpenHands | Aider |
|---|---|---|---|---|---|---|---|---|
| 核心机制 | 规则引擎 + 模式 | OS 沙箱 + 审批策略 + Profiles | TOML 策略引擎 + 模式 | Run Modes + 分类器 + 沙箱 | 类别开关矩阵 | 三档模式 + 工具权限 | 策略对象 + 风险分析器 | 无（git 兜底） |
| 决策取值 | allow/ask/deny | 沙箱边界 + 需/免审批 | allow/deny/ask_user | 免审批/分类器审/人工审批 | 免审批/需审批 | 自动/智能/人工 | 免审批/需审批(按风险) | — |
| 只读默认放行 | ✅ 内置只读命令集 | ✅ read-only 沙箱内 | ✅ 默认策略 | ✅ 读+搜+工作区编辑 | ✅ 基础开关 | ✅ | ✅ | ✅ |
| 命令级细粒度规则 | ✅ 强（复合命令/wrapper/参数） | 执行策略规则 | ✅ 强（RE2 + 前缀） | ✅ 但用自然语言 | ❌（模型自评） | 工具级 | ❌（风险级别） | ❌ |
| 工作区边界 | 工作目录 + additionalDirectories | workspace-write 沙箱（OS 强制） | 沙箱（容器/seatbelt） | 沙箱 + .cursorignore | 工作区开关 | — | workspace 参数 + Docker | — |
| 网络管控 | WebFetch 域名规则 + 沙箱 | 默认断网 + 域名 allow/deny | 规则可达 | 沙箱默认断网 + sandbox.json | — | — | 云端隔离 | — |
| LLM 参与审批 | auto 模式分类器 | 自动审批审查 + 异步安全监控 | ❌（纯规则） | Auto-review 分类器 | requires_approval 自评 | read/write 自评 | LLMSecurityAnalyzer 自评 | ❌ |
| 模式族 | default/acceptEdits/plan/auto/dontAsk/bypass | read-only/auto/full-access 预设 | default/autoEdit/plan/yolo | Auto-review/Allowlist/Run Everything | Plan/Act + YOLO | auto/smart_approve/approve | Always/Never/ConfirmRisky | — |
| 审批记忆 | 永久(按仓库)/会话级，按子命令拆分保存 | 目录信任 + 配置 | "所有未来会话"，且模式感知 | permissions.json（NL 规则） | 设置持久 | 会话/设置 | 会话级 | — |
| 企业管控 | managed settings（最高优先级） | requirements.toml + profile 白名单 | Admin 策略层（目录属主校验） | 团队级配置优先 | Enterprise API | — | 云端组织权限 | — |
| 硬约束兜底 | 沙箱（推荐配合规则） | OS 沙箱 | 沙箱 | OS 沙箱 | ❌ | ❌ | Docker | git |

### 共性模式清单

1. **deny > ask > allow 的固定判定序**，命中即止；deny 不可被 allow 例外（Claude Code、Gemini CLI 一致）。
2. **裸名 deny = 从上下文移除工具**（Claude Code、Gemini CLI）——省 token 且从源头堵死，优于"保留工具再拦截"。
3. **只读白名单内建**：读文件/搜索/glob 默认免审批，审批成本集中在写路径。
4. **工作区边界是第一道墙**：目录范围、`.gitignore`/`.cursorignore` 隐藏、敏感路径（`.git`、`.env`）保护。
5. **网络是独立管控面**：默认断网/按域名白名单，且通常需要沙箱/代理支撑（Codex 明说：无代理时域名规则只是摆设）。
6. **模式 = 审批责任的分配**：谁把关（人 / 分类器 / 无人）随模式切换；plan 模式是硬只读约束，多家共用。
7. **审批可记忆但分级**：bash/域名永久记忆（按仓库）、文件编辑会话级；Gemini 的"批准覆盖当前及更宽松模式"是更精细的变体。
8. **规则匹配文本 ≠ 安全边界**：Claude Code、Cursor 都公开承认规则/分类器可被绕过，真正的兜底是 OS 沙箱或容器。
9. **LLM 辅助审批成为新趋势**：三种流派——后台分类器审命令（Claude/Cursor）、模型生成时自评风险标志（Cline/goose/OpenHands）、异步任务级监控暂停（Codex）。共同特点：**小/便宜模型 + 明确声明"不是安全边界"**。
10. **企业管控层永远压过用户配置**，且目录权限/文件属主都要校验（Gemini 甚至校验策略目录是否 root 所有）。

---

## 5. 对 agent-code 的借鉴建议

本项目现状（`agent_code/permissions.py`，157 行）：`decide_permission()` 按 `_DANGEROUS_PATTERNS`（bash 正则黑名单）→ `_ASK_TOOLS`（ask_user、web_*）→ `_READONLY_TOOLS` 白名单 → plan 模式只读硬约束 → `_LOW_RISK_WRITES` → 其余 ask 的顺序做**纯工具级判定**；bash 只有一刀切的危险模式 deny，没有 allow 侧细粒度。

对照业界实践，按投入产出排序：

1. **决策统一为 allow/ask/deny + "命中即止"链**（已基本符合；确认黑名单永远优先于白名单/模式即可，与业界 deny-first 一致）。
2. **bash 规则升级为前缀 allowlist + 复合命令拆分**：引入 `Bash(npm run *)` 式规则；判定前按 `&& || ; |` 拆分子命令，全部命中 allow（或属只读白名单）才免审批；剥离 `timeout/time/nohup` 等常见 wrapper。deny 侧保持"穿透赋值前缀"的宽匹配。
3. **裸名 deny 移除工具**：deny 整个工具时直接从模型工具列表剔除（同时省上下文），等价于 Claude Code/Gemini CLI 行为。
4. **审批记忆分级持久化**：bash/域名"不再询问"按仓库写 `.agent/settings.local.json`（可入/不入 git 由用户定），文件编辑批准仅会话级；复合命令批准时按子命令拆开记忆。
5. **网络与 `.env` 类敏感文件管控**：`web_fetch` 按域名 ask/allow；默认 deny 读 `.env`、`.git/`、密钥类路径（read 级 deny + bash 侧 `cat .env` 也要拦——Claude Code 特意说明单靠 Read deny 拦不住 `grep -r`，bash 侧需要联动）。
6. **（远期）只读沙箱兜底**：Windows 上可考虑 restricted token / AppContainer 或容器方案；在此之前至少保证 bash 阻断列表覆盖路径穿越与编码绕过。
7. **不建议现在做 LLM 分类审批**：需要额外模型调用与成本，且各家都承认它不是安全边界；当前"规则 + 模式 + 确认 UI"的组合已经覆盖 80% 价值。

---

## 6. 参考链接

- Claude Code: <https://code.claude.com/docs/en/permissions> · <https://code.claude.com/docs/en/agent-sdk/permissions>
- Codex: <https://learn.chatgpt.com/docs/agent-approvals-security> · <https://learn.chatgpt.com/docs/permissions> · <https://learn.chatgpt.com/docs/sandboxing>
- Gemini CLI: <https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/policy-engine.md> · <https://github.com/google-gemini/gemini-cli/blob/main/docs/cli/sandbox.md>
- Cursor: <https://cursor.com/docs/agent/security> · <https://cursor.com/docs/agent/security/run-modes>
- Cline: <https://docs.cline.bot/features/auto-approve>
- Goose: <https://goose-docs.ai/docs/guides/managing-tools/goose-permissions>
- OpenHands: <https://docs.openhands.dev/sdk/guides/security>
- Aider: <https://aider.chat/docs/git.html>
