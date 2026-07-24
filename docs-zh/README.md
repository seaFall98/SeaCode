# SeaCode 文档

English: [../docs-en/README.md](../docs-en/README.md)

SeaCode 是面向软件开发工作的本地终端 AI 编程 Agent。它以清晰的模型协议、可观察的对话和逐步加入的工程能力，帮助开发者在自己的工作目录中完成可验证的任务。

当前 v1 正在开发中。以下文档描述已确认的产品与运行契约；具体能力的可用状态以 Roadmap 为准。

| 文档 | 适合谁读 | 内容 |
| --- | --- | --- |
| [PRD-SeaCode.md](./PRD-SeaCode.md) | 产品读者、评审者和第一次了解项目的人 | 产品定位、用户场景、能力边界和验收标准。 |
| [Design-SeaCode.md](./Design-SeaCode.md) | 开发者和架构评审者 | 运行模型、模块归属、关键流程和故障边界。 |
| [Manual-SeaCode.md](./Manual-SeaCode.md) | 使用者和维护者 | 安装前提、YAML 配置、终端交互和排障方式。 |
| [project_roadmap.md](./project_roadmap.md) | 维护者和评审者 | 14 个工程里程碑、模块交付顺序、质量门槛和后续演进。 |

## 阅读顺序

第一次阅读建议先看 PRD，再看 Design；需要配置和运行时查 Manual；需要理解功能交付顺序时查 Roadmap。

## 文档约定

- `docs-zh/` 与 `docs-en/` 的结构和产品契约保持对应。
- 文档只记录公开的产品、设计、使用和路线信息。
- 命令、配置名和目录名使用代码格式；示例中的密钥始终是占位符。
