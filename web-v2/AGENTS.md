# Web-v2 runner 边界

本目录是独立维护的本地网站 runner，不是任一 Codex skill 的运行时入口。

- 只有启动 Web 服务时才读取本目录的 `.env`。
- web-v2 只读取本目录的脚本与 `.env`，不得运行时引用 `web/` 或 `.agents/skills/` 下的脚本。
- Web-v2 通过第三方 OpenAI-compatible API 编排流程；文章抓取直接请求微信公众号页面，不使用 n8n。
