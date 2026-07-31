# SeaCode - User Manual

This manual follows real tasks: install, configure, run, and troubleshoot SeaCode. It repeats the product and safety concepts needed during operation so users do not have to infer them across documents; the deeper module causality and 14-step design are in Design.

## Runtime Contract

This manual describes the public SeaCode V1 runtime. SeaCode runs on the developer's machine and inside the developer's project directory; configuration, sessions, memory, and workspace records are local by default.

| Item | Value |
| --- | --- |
| Python | 3.12 or later |
| Package manager | `uv` |
| Terminal entry point | `sea` |
| Configuration directories | `~/.seacode/` and project `.seacode/` |
| Default Browser Remote | `http://localhost:18888` |

## 1. Install

Prepare Python 3.12+, `uv`, Git, a UTF-8-capable terminal, and credentials for a supported model Provider.

From the SeaCode repository directory:

```bash
uv sync
uv tool install --editable .
uv tool update-shell
```

Open a new terminal and use `sea` from any project directory. To run only inside the current checkout:

```bash
uv run sea
```

## 2. Configure A Provider

SeaCode uses local YAML; `.env` is not required. Configuration is read in this order:

1. `~/.seacode/config.yaml`
2. `<current-project>/.seacode/config.yaml`
3. `<current-project>/.seacode/config.local.yaml`

The later file replaces the earlier `providers` list in full. Shared project configuration may contain a credential-free example; real credentials belong only in an untracked `config.local.yaml` or user-level configuration.

Minimal configuration:

```yaml
providers:
  - name: primary
    protocol: openai-compat
    model: your-model-name
    base_url: https://api.example.com
    api_key: replace-with-your-key
    thinking: false
```

| Field | Meaning |
| --- | --- |
| `name` | Readable profile name used for selection. |
| `protocol` | `anthropic`, `openai`, or `openai-compat`. |
| `model` | Model name supported by the endpoint. |
| `base_url` | Request base URL. |
| `api_key` | Authentication credential stored only in local YAML. |
| `thinking` | Optional; enables extended thinking only when supported. |
| `context_window` | Optional override for the Provider context-window estimate. |
| `max_output_tokens` | Optional per-response output limit. |
| `available_models` | Optional allowlist for explicit model selection. |

The protocol must match the request format:

| `protocol` | Request path |
| --- | --- |
| `anthropic` | Anthropic Messages |
| `openai` | OpenAI Responses |
| `openai-compat` | OpenAI-compatible Chat Completions |

SeaCode does not infer a protocol from a URL. Real `api_key` values must never appear in Git, logs, traces, test fixtures, conversation bodies, screenshots, or public documentation.

## 3. Optional Runtime Configuration

### 3.1 MCP Servers

`mcp_servers` supports local stdio processes and Streamable HTTP:

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

Server connection, discovery, and shutdown are isolated per Server. One failed Server does not block other Servers or the main session. The model discovers deferred external tools through `ToolSearch`.

### 3.2 Sandbox

```yaml
sandbox:
  enabled: false
  auto_allow: false
  network_enabled: false
```

macOS uses Seatbelt and Linux uses bubblewrap. Windows keeps application-level permission and path checks with explicit degradation. A sandbox switch does not replace dangerous-command hard blocks, path boundaries, or human approval.

### 3.3 Lifecycle Hooks

Hooks can bind to lifecycle events such as `session_start`, `turn_start`, `pre_tool_use`, `post_tool_use`, `session_end`, and `compact`. Action types include `command`, `prompt`, `http`, and `agent`; `pre_tool_use` can reject a tool call.

```yaml
hooks:
  - id: block-json-write
    event: pre_tool_use
    if: 'tool == "WriteFile" && args.file_path =~ "/\\.json$/"'
    reject: true
    action:
      type: prompt
      message: "JSON writes are blocked: $TOOL_ARGS.file_path"
```

Hook configuration errors are reported during startup or loading. Ordinary action failures remain observable, and event policy determines whether they affect the main flow.

## 4. Entry Points

| Entry | Command | Use |
| --- | --- | --- |
| TUI | `sea` | Interactive development, approvals, long tasks, and team progress. |
| Non-interactive text | `sea -p "inspect the test entry point"` | One-shot tasks, scripts, and diagnostics. |
| JSON result | `sea -p "..." --output-format json` | One structured final result. |
| Streaming JSON | `sea -p "..." --output-format stream-json` | Consume text, tools, usage, and completion events. |
| Browser Remote | `sea --remote` | Observe and control the local Agent from a browser. |

Common options:

```text
sea --version
sea --mode default
sea --mode acceptEdits
sea --mode plan
sea --mode bypassPermissions
```

`--mode` overrides the default permission mode from configuration. Non-interactive `-p` mode has no TUI approval dialog and follows the non-interactive entry policy for permission requests. Use the TUI or Browser Remote when fine-grained human decisions are required.

### Browser Remote security

`sea --remote` listens on `0.0.0.0:18888` by default, usually accessed at `http://localhost:18888`. Use it only on a trusted local or controlled network; do not expose it directly to the public internet. A shared-network deployment needs authentication, TLS, and access control at the external network boundary.

## 5. TUI Basics

1. Start `sea` in the target project directory.
2. With one Provider, the conversation opens directly; with several, select a profile using the keyboard.
3. Enter a task and press `Enter`; use `Shift+Enter` for a newline.
4. Watch tools, usage, and permission state during streaming. Cancel the turn or deny approval when needed.
5. Continue after a completed turn, or use local commands to manage runtime state.

Common commands:

| Command | Use |
| --- | --- |
| `/help` | List commands or show one command's details. |
| `/status` | Show session, model, token, tool, memory, and work-directory state. |
| `/clear` | Clear the conversation and create a new session. |
| `/compact` | Trigger context compaction manually. |
| `/plan [task]` | Enter or use Plan mode. |
| `/session list\|resume\|new\|delete` | List, resume, create, or delete sessions. |
| `/memory list\|clear\|edit` | Inspect, clear, or edit memory. |
| `/permission mode\|rules\|add\|reset` | Inspect and manage permission modes and rules. |
| `/review [focus]` | Ask the Agent to review current changes. |
| `/mcp` | Inspect MCP connection and tool state. |
| `/sandbox` | Inspect sandbox state and capabilities. |
| `/rewind` | Inspect edit snapshots and choose code, conversation, or both. |
| `/tasks`, `/trace` | Inspect background tasks and the Agent call tree. |
| `/worktree` | Create, list, enter, exit, and inspect isolated workspaces. |

Project or user Skills can register additional commands. Use `/help` to see the complete list available in the current runtime.

## 6. Permission Modes

| Mode | Use |
| --- | --- |
| `default` | Cautious default; sensitive tools follow rules and request HITL approval. |
| `acceptEdits` | Allows ordinary file edits while retaining command and high-risk checks. |
| `plan` | Planning and safe reads only; no unapproved modifications. |
| `bypassPermissions` | Controlled automation; dangerous-command hard blocks remain active. |

Permission rules can live in `~/.seacode/permissions.yaml`, project `.seacode/permissions.yaml`, and local `.seacode/permissions.local.yaml`. Before using `/permission add`, confirm that the match scope is no broader than intended.

## 7. Sessions, Memory, And Workspaces

- Session records live under project `.seacode/sessions` and can be resumed with `/session`.
- Project and user instruction files express stable conventions; long-term memory lives under `.seacode/memory` and is managed with `/memory`.
- `/compact` and automatic context governance compact older messages. Large tool results can remain local and be reread on demand.
- Subagents use independent contexts. Use `/tasks` for background status and `/trace` for the call tree.
- Worktrees give parallel tasks separate directories. Exit and cleanup check uncommitted changes, unpushed commits, and current-session protection.
- `/rewind` can restore code, conversation, or both. Confirm the target snapshot and workspace state first.

## 8. Skills, Hooks, And Teams

Skills can live under project `.seacode/skills/` or user `~/.seacode/skills/`; project content takes precedence. A Skill can use `SKILL.md` or `skill.yaml` plus `prompt.md` to describe its name, purpose, mode, context range, and allowed tools.

Agent Teams consist of a Lead, teammates, a shared task board, and Mailboxes. Available backends include tmux, iTerm2, and in-process; Windows and non-interactive environments use the in-process path. Coordinator Mode is useful when the Lead should focus on task decomposition, messages, and synthesis.

## 9. Troubleshooting

| Symptom | Check |
| --- | --- |
| No Provider found | Check file paths, YAML indentation, and that `providers` is non-empty. |
| Authentication or request failure | Check `protocol`, `base_url`, `model`, and local credentials without putting a key in the conversation. |
| Tool denied | Inspect `/permission`, path boundaries, dangerous-command rules, and the active mode. |
| Context too large | Check `/status`, run `/compact`, and let the model reread large results through their preview references. |
| MCP tool unavailable | Inspect `/mcp`, command, URL, environment variables, and the Server's independent connection state. |
| Session will not resume | Keep the original directory, inspect `.seacode/sessions`, do not hand-edit JSONL, and create a new session if necessary. |
| Worktree will not clean up | Resolve uncommitted or unpushed changes, confirm it is not the current session, then choose cleanup explicitly. |
| Windows sandbox difference | Rely on application-level permissions and paths; do not treat macOS/Linux OS sandbox behavior as a Windows guarantee. |

## 10. Security Checklist

- [ ] A real `api_key` exists only in untracked local YAML.
- [ ] The working directory and configuration belong to the intended project.
- [ ] `protocol` matches the request path supported by the Provider.
- [ ] Browser Remote stays inside a trusted network boundary.
- [ ] Sensitive fields and uncommitted changes are checked before sharing logs, screenshots, sessions, or Worktrees.
- [ ] On failure, read redacted errors and state instead of disclosing credentials for troubleshooting.
