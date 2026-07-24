# SeaCode - User Manual

## Version Note

This manual describes the stable SeaCode v1 runtime contract. v1 is under development; consult the [Roadmap](./project_roadmap.md) to confirm that the current branch contains the capability you need before installing or running it.

| Item | Value |
| --- | --- |
| Python | 3.12 or later |
| Package manager | `uv` |
| Terminal entry point | `sea` |
| Configuration directories | `~/.seacode/` and project `.seacode/` |

## 1. Prerequisites

Prepare Python 3.12+, `uv`, Git, a UTF-8 capable terminal, and credentials for a supported model provider. Never commit real credentials, paste them into a conversation, or share them with others.

When the relevant runtime version is available, install and start it from the project directory:

```bash
uv sync
uv run sea
```

## 2. Configure a Model

SeaCode's primary configuration is local YAML; `.env` is not required. Files are read in this order, and a later file replaces the complete `providers` list from an earlier file:

1. `~/.seacode/config.yaml`
2. `<project>/.seacode/config.yaml`
3. `<project>/.seacode/config.local.yaml`

The public repository contains only a credential-free `.seacode/config.yaml.example`. Copy its shape to one of the untracked locations above and fill in your own values:

```yaml
providers:
  - name: primary
    protocol: openai-compat
    model: your-model-name
    base_url: https://api.example.com
    api_key: replace-with-your-key
    thinking: false
```

| Field | Purpose |
| --- | --- |
| `name` | Human-readable profile identifier used during selection. |
| `protocol` | `anthropic`, `openai`, or `openai-compat`. |
| `model` | A model name supported by the endpoint. |
| `base_url` | The request base URL. |
| `api_key` | Authentication credential stored only in local YAML. |
| `thinking` | Optional; enables extended thinking only when the selected protocol and model support it. |

The declared protocol must match its request path: `anthropic` is Anthropic Messages, `openai` is OpenAI Responses, and `openai-compat` is compatible Chat Completions. SeaCode does not infer a protocol from a URL.

Real local configuration and `config.local.yaml` must be ignored by `.gitignore`. Errors, status bars, logs, test snapshots, and conversation bodies must not print `api_key`.

## 3. First Conversation

One configured profile opens the conversation directly. With several profiles, choose one using the keyboard first. The title or status area displays the active profile and model once in a stable location.

Type a short question and press `Enter`, for example:

```text
Briefly explain this project's test entry point.
```

The answer appears incrementally. Use `Shift+Enter` for a multiline prompt. During a pending or streaming request SeaCode prevents duplicate submissions; the input becomes available again after completion or failure.

## 4. Recover from Errors

For an authentication, rate-limit, network, timeout, or response-shape error:

1. Read the redacted error summary; never place a credential in the conversation.
2. Check that `protocol`, `base_url`, and `model` match the provider's documentation.
3. Update local YAML and restart, or continue with a new message once the error state recovers.

Failed assistant output is not stored as a completed answer in later logical history, so a next request does not carry an incomplete response. Earlier user input and the on-screen error remain available for diagnosing the failed turn.

## 5. Later Runtime Capabilities

As v1 milestones are delivered, SeaCode adds tools, permissions, sessions, commands, Skills, Hooks, isolated workspaces, and team collaboration. Consult the matching version's interface guidance and Roadmap for operation, permission impact, and failure handling; do not assume a command exists before its milestone is delivered.

## 6. Security Checklist

- [ ] A real `api_key` exists only in untracked local YAML.
- [ ] The working directory is the intended project and configuration is not at the repository root.
- [ ] `protocol` matches the request path actually supported by the provider.
- [ ] Logs, screenshots, and conversations have been checked before sharing.
- [ ] On failure, configuration and the redacted error are inspected without disclosing a credential.
