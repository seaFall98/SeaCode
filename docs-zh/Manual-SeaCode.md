# SeaCode - 使用手册

本手册以实际任务为顺序说明如何安装、配置、运行和排障。它会重复必要的产品和安全概念，让操作时不必在多份文档之间来回猜测；更深入的模块因果链和 14 步设计见 Design。

## 运行契约

本手册描述 SeaCode V1 的公开运行方式。SeaCode 在开发者自己的机器和项目目录中运行，配置、会话、记忆和工作区记录默认保存在本地。

| 项目 | 内容 |
| --- | --- |
| Python | 3.12 或更高版本 |
| 包管理 | `uv` |
| 终端入口 | `sea` |
| 配置目录 | `~/.seacode/` 与项目 `.seacode/` |
| 默认 Browser Remote | `http://localhost:18888` |

## 1. 安装

准备 Python 3.12+、`uv`、Git、支持 UTF-8 的终端和一个可用的模型 Provider 凭据。

在 SeaCode 仓库目录执行：

```bash
uv sync
uv tool install --editable .
uv tool update-shell
```

重新打开终端后，可以在任意项目目录直接使用 `sea`。只想在当前仓库运行时，也可以使用：

```bash
uv run sea
```

## 2. 配置 Provider

SeaCode 使用本地 YAML，不要求 `.env`。配置按以下顺序读取：

1. `~/.seacode/config.yaml`
2. `<当前项目>/.seacode/config.yaml`
3. `<当前项目>/.seacode/config.local.yaml`

后层的 `providers` 列表完整替换前层的列表。项目级共享配置可以提交无密钥示例；真实密钥只放在未跟踪的 `config.local.yaml` 或用户级配置中。

最小配置：

```yaml
providers:
  - name: primary
    protocol: openai-compat
    model: your-model-name
    base_url: https://api.example.com
    api_key: replace-with-your-key
    thinking: false
```

| 字段 | 说明 |
| --- | --- |
| `name` | profile 的可读名称；多个配置时用于选择。 |
| `protocol` | `anthropic`、`openai` 或 `openai-compat`。 |
| `model` | 端点实际支持的模型名称。 |
| `base_url` | 请求基础地址。 |
| `api_key` | 只保存在本地 YAML 中的认证凭据。 |
| `thinking` | 可选；仅在模型和协议支持时启用扩展思考。 |
| `context_window` | 可选；覆盖该 Provider 的上下文窗口估计。 |
| `max_output_tokens` | 可选；设置单次输出上限。 |
| `available_models` | 可选；限制运行时可显式选择的模型列表。 |

协议必须与请求格式匹配：

| `protocol` | 请求路径 |
| --- | --- |
| `anthropic` | Anthropic Messages |
| `openai` | OpenAI Responses |
| `openai-compat` | OpenAI-compatible Chat Completions |

SeaCode 不根据 URL 猜测协议。真实 `api_key` 不应出现在 Git、日志、trace、测试 fixture、会话正文、截图或公开文档中。

## 3. 可选运行配置

### 3.1 MCP Server

`mcp_servers` 支持本地 stdio 子进程和 Streamable HTTP：

```yaml
mcp_servers:
  - name: filesystem
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/path/to/allowed/dir"
    env:
      NODE_PATH: "${NODE_PATH}"
  - name: remote
    url: https://mcp.example.com/mcp
    headers:
      Authorization: "Bearer ${MCP_TOKEN}"
```

Server 连接、工具发现和关闭按 Server 隔离；一个 Server 失败不会阻断其它 Server 或主会话。模型通过 `ToolSearch` 按需发现延迟加载的外部工具。

### 3.2 沙箱

```yaml
sandbox:
  enabled: false
  auto_allow: false
  network_enabled: false
```

macOS 使用 Seatbelt，Linux 使用 bubblewrap；Windows 保持应用层权限和路径检查并明确降级。沙箱开关不能替代危险命令硬拦截、路径边界和人工确认。

### 3.3 生命周期 Hook

Hook 可以绑定 `session_start`、`turn_start`、`pre_tool_use`、`post_tool_use`、`session_end`、`compact` 等生命周期事件。动作类型包括 `command`、`prompt`、`http` 和 `agent`；`pre_tool_use` 可以拒绝工具调用。

```yaml
hooks:
  - id: block-json-write
    event: pre_tool_use
    if: 'tool == "WriteFile" && args.file_path =~ "/\\.json$/"'
    reject: true
    action:
      type: prompt
      message: "禁止写入 JSON 文件：$TOOL_ARGS.file_path"
```

Hook 配置错误会在启动或加载时报告；普通动作失败保持可观察，并按事件类型决定是否影响主流程。

## 4. 启动入口

| 入口 | 命令 | 适用场景 |
| --- | --- | --- |
| TUI | `sea` | 交互式开发、权限审批、长任务和团队进度。 |
| 非交互文本 | `sea -p "检查测试入口"` | 一次性任务、脚本和诊断。 |
| JSON 结果 | `sea -p "..." --output-format json` | 获取单个结构化最终结果。 |
| 流式 JSON | `sea -p "..." --output-format stream-json` | 消费文本、工具、用量和完成事件。 |
| Browser Remote | `sea --remote` | 在浏览器中观察和控制本地 Agent。 |

通用参数：

```text
sea --version
sea --mode default
sea --mode acceptEdits
sea --mode plan
sea --mode bypassPermissions
```

`--mode` 覆盖配置中的默认权限模式。非交互 `-p` 模式没有 TUI 审批对话框，权限请求按非交互入口的运行策略处理；需要细粒度人工决策时使用 TUI 或 Browser Remote。

### Browser Remote 安全说明

`sea --remote` 默认监听 `0.0.0.0:18888`，访问地址通常为 `http://localhost:18888`。该入口适合受信任的本机或受控网络；它不应直接暴露到公网。部署在共享网络前，应在外部网络边界提供认证、TLS 和访问控制。

## 5. TUI 基本操作

1. 在目标项目目录启动 `sea`。
2. 只有一个 Provider 时直接进入对话；多个 Provider 时用键盘选择 profile。
3. 输入任务并按 `Enter` 提交；用 `Shift+Enter` 插入换行。
4. 流式输出期间查看工具、用量和权限状态；需要时取消当前回合或拒绝审批。
5. 回合结束后继续输入，或使用本地命令管理运行时状态。

常用命令：

| 命令 | 用途 |
| --- | --- |
| `/help` | 列出命令或查看单个命令详情。 |
| `/status` | 查看会话、模型、token、工具、记忆和工作目录状态。 |
| `/clear` | 清空当前对话并创建新会话。 |
| `/compact` | 手动触发上下文压缩。 |
| `/plan [task]` | 进入或使用 Plan 模式。 |
| `/session list\|resume\|new\|delete` | 列出、恢复、新建或删除会话。 |
| `/memory list\|clear\|edit` | 查看、清理或编辑记忆。 |
| `/permission mode\|rules\|add\|reset` | 查看和管理权限模式与规则。 |
| `/review [focus]` | 让 Agent 对当前变更执行代码审查。 |
| `/mcp` | 查看 MCP 连接和工具状态。 |
| `/sandbox` | 查看沙箱状态和能力。 |
| `/rewind` | 查看文件编辑快照并选择代码、对话或两者回滚。 |
| `/tasks`、`/trace` | 查看后台任务和 Agent 调用树。 |
| `/worktree` | 创建、列出、进入、退出和检查隔离工作区。 |

项目或用户级 Skill 可以注册额外命令；输入 `/help` 查看当前运行时实际可用的完整列表。

## 6. 权限模式

| 模式 | 适用场景 |
| --- | --- |
| `default` | 默认谨慎模式；敏感工具根据规则和 HITL 请求确认。 |
| `acceptEdits` | 适合允许常规文件编辑，同时保留命令和高风险操作检查。 |
| `plan` | 只做规划和安全读取，不执行未经允许的修改。 |
| `bypassPermissions` | 适合受控自动化环境；危险命令硬拦截仍然有效。 |

权限规则可以放在用户级 `~/.seacode/permissions.yaml`、项目级 `.seacode/permissions.yaml` 和本地级 `.seacode/permissions.local.yaml`。使用 `/permission add` 写入本地规则前，确认匹配范围不会超出预期。

## 7. 会话、记忆与工作区

- 会话记录位于项目 `.seacode/sessions`，可以通过 `/session` 恢复。
- 项目和用户指令文件用于表达稳定约定；长期记忆位于 `.seacode/memory`，通过 `/memory` 管理。
- `/compact` 或自动上下文治理会压缩较早消息；大工具结果可以保留在本地并按需重新读取。
- SubAgent 使用独立上下文；`/tasks` 查看后台状态，`/trace` 查看调用树。
- Worktree 为并行任务提供独立目录；退出或清理会检查未提交修改、未推送提交和当前会话保护。
- `/rewind` 可以选择恢复代码、对话或两者；执行前先确认目标快照和工作区状态。

## 8. Skills、Hooks 与 Teams

Skills 支持项目级 `.seacode/skills/` 和用户级 `~/.seacode/skills/`，项目级内容优先。Skill 可以使用 `SKILL.md` 或 `skill.yaml` + `prompt.md` 描述名称、用途、模式、上下文范围和允许工具。

Agent Teams 由 Lead、teammate、共享任务板和 Mailbox 组成。可用后端包括 tmux、iTerm2 和 in-process；Windows 或非交互环境使用 in-process 路径。Coordinator Mode 适合让 Lead 专注拆分任务、接收消息和汇总结果。

## 9. 排障

| 现象 | 检查顺序 |
| --- | --- |
| 找不到 Provider | 确认配置文件路径、YAML 缩进和 `providers` 非空。 |
| 鉴权或请求失败 | 核对 `protocol`、`base_url`、`model` 和本地凭据，不要把 key 放进对话。 |
| 工具被拒绝 | 查看 `/permission`、路径边界、危险命令规则和当前模式。 |
| 上下文过大 | 查看 `/status`，使用 `/compact`，让模型按预览引用重新读取大结果。 |
| MCP 工具不可用 | 查看 `/mcp`，确认命令、URL、环境变量和 Server 独立连接状态。 |
| 会话无法恢复 | 保留原目录，查看 `.seacode/sessions`，不要手工拼接 JSONL；必要时新建会话。 |
| Worktree 无法清理 | 先处理未提交或未推送变更，确认不是当前会话，再显式选择清理方式。 |
| Windows 沙箱差异 | 依赖应用层权限和路径检查，不把 macOS/Linux 的 OS 沙箱行为当作 Windows 保证。 |

## 10. 安全检查清单

- [ ] 真实 `api_key` 只存在于未跟踪的本地 YAML。
- [ ] 当前工作目录和配置文件属于预期项目。
- [ ] `protocol` 与 Provider 实际支持的请求路径一致。
- [ ] Browser Remote 只绑定在受信任网络边界内。
- [ ] 分享日志、截图、会话或 Worktree 前已检查敏感字段和未提交变更。
- [ ] 遇到失败先阅读脱敏错误和状态，不用泄露凭据换取排障信息。
