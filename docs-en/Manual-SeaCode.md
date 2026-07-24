# SeaCode - Manual

## Version Information

| Item | Content |
| --- | --- |
| Runtime | Local Python application |
| Python | 3.12 or newer |
| Package manager | `uv` |
| Main entry point | `sea` |
| Configuration | Project and user configuration |

## 1. Prerequisites

Prepare:

- Python 3.12+
- `uv`
- Git
- A model Provider and its credentials
- A UTF-8 capable terminal

On Windows, macOS, or Linux, make sure `python`, `uv`, and `git` are available from the terminal.

## 2. Install And Start

Run from the project directory:

```bash
uv sync
uv run sea --help
uv run sea
```

After installing the local command, the short form is also available:

```bash
sea
```

The interface shows the current workspace, active model, session state, and input area. On the first run, use `/status` to inspect configuration and tool state.

## 3. Configure A Model Provider

Keep credentials in a local environment file or system environment. Do not commit them to Git. External Provider keys retain their protocol names; SeaCode-owned runtime settings use the `SEA_` prefix.

Example:

```dotenv
ANTHROPIC_API_KEY=replace-with-your-key
ANTHROPIC_BASE_URL=https://api.example.com/anthropic
SEA_LLM_DEFAULT_MODEL=replace-with-your-model
SEA_MAX_STEPS=100
```

Notes:

- The example values are placeholders, not usable credentials.
- Do not put real keys in logs, screenshots, test fixtures, or session content.
- Set `ANTHROPIC_BASE_URL` only when the Provider needs a custom endpoint.
- The model name must be supported by the configured endpoint.

## 4. First Task

Start with a small, verifiable task:

```text
Inspect the current project's test entry points. Explain which files you will read, and do not modify files yet.
```

After reviewing the plan, submit a bounded change:

```text
Fix the specified failing test. Read the relevant implementation and tests first, explain the reason before editing, then run the smallest relevant test and report the result.
```

SeaCode applies the current permission mode to file writes and commands. Tasks that explicitly say “read, change, then verify” are easier to review.

## 5. Common Commands

| Command | Purpose |
| --- | --- |
| `/help` | List available commands. |
| `/status` | Show model, permissions, tools, directory, and usage. |
| `/plan` | Enter read-only planning mode. |
| `/do` | Execute from the current plan. |
| `/compact` | Compact older conversation context. |
| `/resume` | Choose and resume a prior session. |
| `/clear` | End the current session and create a new one. |
| `/session` | Show current session identity and storage information. |
| `/permission` | Show the active permission mode. |
| `/exit` | Exit safely. |

Typing `/` opens the completion menu. Multi-line tasks can be edited in the input area before submission.

## 6. Permission Modes

| Mode | Suitable use |
| --- | --- |
| `default` | Everyday development: reads are automatic, writes and commands require approval. |
| `acceptEdits` | Trusted file changes while keeping command approval. |
| `plan` | Explore code and produce a plan without making changes. |
| `bypassPermissions` | Controlled automation where workspace and command risk have already been reviewed. |

Dangerous-command protection and workspace boundaries should remain effective in every mode. “Allow once” affects one call; “allow persistently” writes a rule for the current workspace; denial leaves the session running.

## 7. Sessions And Runtime Records

SeaCode stores sessions, tool results, and runtime metadata in the user runtime directory. These records support recovery, troubleshooting, and context governance; they should not be committed as source code.

Recommendations:

- Use `/session` to confirm the current session identity.
- Check usage and iteration state with `/status` during long tasks.
- Keep the relevant test output and final change summary after a task finishes.
- Remove sensitive information before sharing runtime records.

## 8. Git Workspaces And Parallel Tasks

When changes need isolation, ask the Agent to create a workspace with a meaningful name before starting. Before exit or cleanup, SeaCode checks uncommitted changes and new commits; a workspace with changes is kept by default to prevent accidental loss.

After a subagent or team task completes, inspect its workspace, test evidence, and pending changes before cleanup. Automation does not replace code review.

## 9. Troubleshooting

### Startup reports a missing key

Check the environment variable name, environment file location, and whether the current terminal has reloaded configuration. Do not paste credentials into the chat input.

### The model reports authentication or an unknown model

Check the endpoint, model name, and Provider protocol. Use `/status` to confirm the active configuration, then retry with a short message.

### A tool call is denied

Read the displayed reason. It may be the permission mode, a rule, a workspace boundary, or dangerous-operation protection. Narrow the task or approve it explicitly instead of hiding the action inside an ambiguous command.

### Context is getting large

Use `/compact`, or ask the Agent to summarize the current goal, completed work, failures, and next steps. Compaction keeps recent text and readable full tool results.

### A workspace cannot be removed

It usually contains uncommitted changes or new commits. Inspect the diff and confirm the result is saved before using a cleanup operation that discards changes.

### The TUI renders incorrectly

Confirm UTF-8 support, increase the terminal width, and restart `sea`. If the issue only appears during a Provider request, inspect the error event and network configuration.

## 10. Security Checklist

- [ ] Credentials exist only in local secure configuration.
- [ ] The current working directory is the intended project.
- [ ] Important writes use `default` or `plan` mode until the scope is clear.
- [ ] Uncommitted changes have been tested or explicitly recorded.
- [ ] Sensitive data has been removed from logs, screenshots, and sessions before sharing.
