# Web-v3 runner 边界

本目录是独立维护的本地网站 runner，不是任一 Codex skill 的运行时入口。

- 只有启动 Web 服务时才读取本目录的 `.env`。
- web-v3 只读取本目录的脚本与 `.env`，不得运行时引用 `web/`、`web-v2/` 或 `.agents/skills/` 下的脚本。
- Web-v3 通过第三方 OpenAI-compatible API 编排流程；文章抓取直接请求微信公众号页面，不使用 n8n。
- Web-v3 使用六张性别/年龄角色参考图；年龄计划必须区分 `child`、`adult`、`elder`、`unknown`，父母关系优先归为 adult，只有明确爷爷奶奶辈或老年身份才使用 old。明确夫妻/情侣或普通一男一女同框时按角色组合传入男、女参考图，夫妻/情侣+孩子追加 baby；其他场景按主角年龄使用 `girl`、`girl-baby` 或 `girl-old`，无法判断时使用 `photos/girl.png`。`emotion` 仅用于 local 卡片配色。
