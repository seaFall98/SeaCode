# SeaCode 文档

English: [../docs-en/README.md](../docs-en/README.md)

SeaCode 是一个面向软件开发工作的本地 AI 编程 Agent 运行时。它把模型连接、工具执行、权限控制、上下文治理和可恢复的工作状态组织成一个可观察的开发环境。

| 文档 | 适合谁读 | 内容 |
| --- | --- | --- |
| [PRD-SeaCode.md](./PRD-SeaCode.md) | 产品、面试官、第一次了解项目的人 | 产品定位、用户场景、能力边界和验收标准。 |
| [Design-SeaCode.md](./Design-SeaCode.md) | 开发者和架构评审者 | 分层架构、核心模型、关键流程、扩展点和故障边界。 |
| [Manual-SeaCode.md](./Manual-SeaCode.md) | 使用者和维护者 | 安装、配置、运行、权限操作和常见排障。 |
| [project_roadmap.md](./project_roadmap.md) | 维护者和评审者 | 14 个工程里程碑、交付顺序、质量门槛和演进方向。 |

## 阅读顺序

第一次阅读建议先看 PRD，再看 Design；想运行项目时查 Manual；需要理解交付顺序和工程取舍时查 Roadmap。

## 文档约定

- `docs-zh/` 与 `docs-en/` 内容结构保持对应。
- 这些文档只记录稳定的产品、设计、使用和路线信息。
- 命令、配置名和目录名使用代码格式，示例中的密钥始终使用占位符。
