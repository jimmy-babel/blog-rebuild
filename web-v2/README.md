# Blog Article Studio

本地微信公众号文章重构工作台（v2）。后端使用 FastAPI，任务元数据使用 SQLite，文章和图片仍输出到项目根目录的 `result/`。web-v2 独立维护自己的 pipeline 和脚本，不依赖 `web/` 或项目 skill。

## 启动

1. 复制 `.env.example` 为 `.env`。
2. 填写第三方 OpenAI-compatible `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
3. 文章会直接请求 `https://mp.weixin.qq.com/s/...` 页面，不需要启动 n8n。
4. 在 PowerShell 执行：

```powershell
cd E:\JIMMY\AI-Proj\blog\web
python -m pip install -r requirements.txt
.\run.ps1
```

打开 http://127.0.0.1:8000 。

`IMAGE_API_MODE=edits` 会把允许的来源图片和角色参考图发送给 `/images/edits`；如果第三方只实现 `/images/generations`，改为 `generations`，但不会保留参考图输入。

每张图片请求都会在 `.recreate-work/<任务ID>/api-debug/` 保存调试副本：`image-XXX.postman.json` 可直接导入 Postman，`image-XXX.curl.txt` 可复制或导入为 cURL，`image-XXX.request.json` 保存实际字段和本地文件路径，`image-XXX.response.body` 保存接口返回的原始 body。请求副本中的 API Key 使用 `{{OPENAI_API_KEY}}` 占位符，不保存真实密钥；multipart 请求不要手动设置 `Content-Type`。

图片生成失败时，任务工作目录还会写入 `image-failures.json`，记录每张图的错误和对应调试文件；任务仍可降级完成时，界面进度会显示失败图片数量。结果目录提交遇到 Windows `WinError 5` 时会进行有限退避重试，重试耗尽后才标记任务失败。

创建任务时可使用 `article_mode: "trans"|"ori"`、`image_mode: "copy"|"rebuild"|"comic"`、`comic_count`、`caption_mode: "ai"|"local"` 和 `brand: "on"|"off"`。`ori` 会保留来源正文的显式换行；`comic` 自动要求 `ori`，只依据文字生成连续叙事图，不读取来源图片；旧字段 `brand_reference` 仍兼容。启用品牌人物时必须使用 `IMAGE_API_MODE=edits`。
