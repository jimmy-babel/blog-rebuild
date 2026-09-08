# Blog Article Studio

本地微信公众号文章重构工作台。后端使用 FastAPI，任务元数据使用 SQLite，文章和图片仍输出到项目根目录的 `result/`。

## 启动

1. 复制 `.env.example` 为 `.env`。
2. 填写第三方 OpenAI-compatible `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
3. 确认 n8n 运行在 `http://localhost:5678`。
4. 在 PowerShell 执行：

```powershell
cd E:\JIMMY\AI-Proj\blog\web
python -m pip install -r requirements.txt
.\run.ps1
```

打开 http://127.0.0.1:8000 。

`IMAGE_API_MODE=edits` 会把来源图片和角色参考图发送给 `/images/edits`；如果第三方只实现 `/images/generations`，改为 `generations`，但不会保留参考图输入。

创建任务时使用 `brand: "on"|"off"` 控制是否使用 `photos/` 中按情绪选择的品牌人物参考图；旧字段 `brand_reference` 仍兼容。启用品牌人物时必须使用 `IMAGE_API_MODE=edits`。
