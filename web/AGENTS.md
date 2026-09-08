# Web runner 边界

本目录是独立的本地网站 runner，不是 `recreate-wechat-article` Skill 的原生入口。

- 只有启动 Web 服务时才读取本目录的 `.env`。
- Codex 原生调用 `$recreate-wechat-article` 不得导入本目录的模块、调用本目录的 HTTP API 或读取本目录的 `.env`。
- Web 通过第三方 OpenAI-compatible API 编排流程；Skill 原生流程使用 Codex 自身的工具和 Skill 规则。
