# SeaCode - 使用手册

## 版本说明

本手册描述 SeaCode v1 的稳定运行契约。v1 正在开发中；请先在 [Roadmap](./project_roadmap.md) 确认当前分支已经包含所需能力，再执行安装或运行命令。

| 项目 | 内容 |
| --- | --- |
| Python | 3.12 或更高版本 |
| 包管理 | `uv` |
| 终端入口 | `sea` |
| 配置目录 | `~/.seacode/` 与项目 `.seacode/` |

## 1. 环境准备

准备 Python 3.12+、`uv`、Git、支持 UTF-8 的终端，以及一个可用的模型 Provider 凭据。不要把真实凭据提交到 Git、粘贴到对话框或分享给他人。

当对应运行时版本可用后，在项目目录安装并启动：

```bash
uv sync
uv run sea
```

## 2. 配置模型

SeaCode 的主配置是本地 YAML，不要求使用 `.env`。按以下顺序读取文件，后出现的文件替换前面文件中的完整 `providers` 列表：

1. `~/.seacode/config.yaml`
2. `<项目目录>/.seacode/config.yaml`
3. `<项目目录>/.seacode/config.local.yaml`

公开仓库只提供无密钥的 `.seacode/config.yaml.example`。实际使用时，复制其结构到上述某个未跟踪位置并填写自己的值：

```yaml
providers:
  - name: primary
    protocol: openai-compat
    model: your-model-name
    base_url: https://api.example.com
    api_key: replace-with-your-key
    thinking: false
```

字段说明：

| 字段 | 作用 |
| --- | --- |
| `name` | 配置的可读标识；启动时用于选择。 |
| `protocol` | `anthropic`、`openai` 或 `openai-compat`。 |
| `model` | 端点实际支持的模型名。 |
| `base_url` | 请求的基础地址。 |
| `api_key` | 仅保存在本地 YAML 的认证凭据。 |
| `thinking` | 可选；仅在所选协议和模型支持时启用扩展思考。 |

协议与请求路径必须匹配：`anthropic` 对应 Anthropic Messages，`openai` 对应 OpenAI Responses，`openai-compat` 对应兼容的 Chat Completions。不要让 SeaCode 通过 URL 猜测协议。

本地真实配置和 `config.local.yaml` 必须被 `.gitignore` 忽略。错误、状态栏、日志、测试快照和会话正文都不应输出 `api_key`。

## 3. 第一次对话

启动时只有一个配置会直接进入对话；存在多个配置时，先通过键盘选择要使用的配置。顶栏或状态栏会在一个稳定位置显示当前配置与模型。

输入一条短问题后按 `Enter` 发送，例如：

```text
请简要解释这个项目的测试入口。
```

回复会逐段显示。需要多行输入时按 `Shift+Enter` 换行。等待或流式输出期间，SeaCode 会阻止重复提交；完成或失败后输入框恢复可用。

## 4. 错误恢复

遇到鉴权、限流、网络、超时或响应格式问题时：

1. 阅读界面的脱敏错误摘要；不要把密钥贴到对话中。
2. 检查 `protocol`、`base_url`、`model` 与 Provider 文档是否一致。
3. 修改本地 YAML 后重新启动，或在错误恢复后的同一会话继续输入下一条消息。

失败的助手内容不会作为完整回答写入后续逻辑历史，因此下一次请求不会携带不完整回复。用户先前输入和屏幕错误记录仍可用于理解本轮失败。

## 5. 后续运行能力

随着 v1 里程碑交付，SeaCode 会逐步提供工具、权限、会话、命令、Skills、Hooks、隔离工作区和团队协作。每项能力的使用方式、权限影响和故障处理以同版本的界面说明与 Roadmap 为准；不要假设未交付的命令已经存在。

## 6. 安全检查

- [ ] 真实 `api_key` 只存在于本机的未跟踪 YAML。
- [ ] 当前工作目录是预期项目，配置文件不在仓库根目录。
- [ ] `protocol` 与 Provider 实际支持的请求路径一致。
- [ ] 分享日志、截图或会话前已检查敏感字段。
- [ ] 遇到失败先检查配置和错误摘要，不泄露凭据以换取排障信息。
