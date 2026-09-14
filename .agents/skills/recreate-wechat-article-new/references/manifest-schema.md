# Manifest 自动构建规范

## 精简 article-plan.json

Codex 只创建精简文章计划，不手工复制原文 blocks：

```json
{
  "articleMode": "trans",
  "imageMode": "copy",
  "title": "重新创作后的标题",
  "summary": "可选的原创导语",
  "removedSourceIndexes": [7, 18],
  "blocks": [
    {"type": "heading", "level": 2, "text": "新章节标题"},
    {"type": "text", "text": "原创段落。"},
    {"type": "image", "sourceIndex": 12, "path": "cards/image-001.png", "alt": "新场景说明"},
    {"type": "sourceImageLink", "sourceIndex": 25, "alt": "生成失败的配图"}
  ]
}
```

comic 示例：

```json
{
  "articleMode": "ori",
  "imageMode": "comic",
  "comicCount": 5,
  "title": "文章标题",
  "blocks": [
    {"type": "text", "text": "清洗后的原文正文"},
    {"type": "image", "path": "cards/comic-001.png", "alt": "第一幕"},
    {"type": "generatedImageLink", "alt": "第二幕", "error": "生成失败"}
  ]
}
```

- `removedSourceIndexes` 只写二次语义审查从 `source.json.blocks` 删除的索引；省略时视为空数组。
- `articleMode=trans` 时，`text`、`heading`、`quote` 是重新创作后的文章内容；`articleMode=ori` 时正文必须是二次清洗后保留的原文内容，并保留原文显式换行（`text` 中使用 `\n`，不得合并连续原文行）。
- 每张二次清洗后保留的来源图片必须按 `sourceIndex` 顺序出现一次且仅一次。
- 生图成功使用 `image` 并提供本地 `path`；普通模式生图失败使用 `sourceImageLink`，无需手抄 URL。
- `imageMode=comic` 时，生成图片使用不带 `sourceIndex` 的 `image`；失败使用 `generatedImageLink`。图片结果总数必须等于 `comicCount`。
- 自动构建器从 `source.json` 查找原图 URL。URL 无效时保留无 URL 的失败块并返回告警。

图片型文章例外：`source.json` 可以没有 text block，但必须至少有一个 image block；此时自动生成 `sourceCharacterCount: 0`，`sourceArchive.blocks` 只包含图片也合法。渲染器对该类型使用 800 个非空白字符的原创正文上限，不按 0.7 倍字符数计算。

运行：

```powershell
python .agents/skills/recreate-wechat-article-new/scripts/build_manifest.py `
  --source "<工作目录>/source.json" `
  --article "<工作目录>/article-plan.json" `
  --output "<工作目录>/manifest.json"
```

## 自动生成的 manifest.json

```json
{
  "title": "重新创作后的标题",
  "sourceUrl": "https://mp.weixin.qq.com/s/...",
  "sourceCharacterCount": 620,
  "sourceArchive": {
    "title": "原始标题",
    "blocks": [
      {"type": "text", "sourceIndex": 1, "text": "二次清洗后保留的原文正文"},
      {
        "type": "image",
        "sourceIndex": 12,
        "url": "https://mmbiz.qpic.cn/...",
        "path": "C:/.../.recreate-work/.../source-images/image-001.jpg",
        "alt": "原图说明"
      }
    ]
  },
  "summary": "可选的原创导语",
  "blocks": [
    {"type": "heading", "level": 2, "text": "新章节标题"},
    {"type": "text", "text": "原创段落。"},
    {
      "type": "image",
      "sourceIndex": 12,
      "path": "C:/.../.recreate-work/.../cards/image-001.png",
      "alt": "新场景说明"
    },
    {
      "type": "sourceImageLink",
      "sourceIndex": 25,
      "url": "https://mmbiz.qpic.cn/...",
      "alt": "生成失败的配图"
    }
  ]
}
```

构建器自动完成以下工作：

- 从 `source.json` 复制二次清洗后保留的原始图文并保持原始措辞与顺序；
- 将 `localPath` 解析为可靠的本地绝对路径；
- 计算归档文字的非空白字符数并写入 `sourceCharacterCount`；
- 为失败图片补入原始 HTTPS URL；
- 验证所有保留图片都得到一个成功图片或失败链接结果；
- 以临时文件原子写入 manifest，避免留下半成品。

渲染要求：

- 旧 manifest 缺少 `sourceCharacterCount`、`sourceArchive` 或图片 `sourceIndex` 时仍兼容渲染。
- 新文章 `heading.level` 仅允许 2 或 3；`text`、`heading`、`quote` 均不得为空。
- `image.path` 必须指向本地重制 PNG；`sourceImageLink.url` 必须为 HTTPS 或省略。
- `articleMode=trans` 时，`summary`、`text`、`heading` 和 `quote` 的非空白字符总数不得超过 `min(800, ceil(sourceCharacterCount × 0.7))`；`ori` 不执行该限制。
- 正文、导语和引用按 `，。！？；,.!;?` 分行并居中；`ori` 正文同时优先保留已有的显式 `\n` 行边界；数字中的小数点和千位逗号不拆分。

## 输出

```text
result/
└── MMDD-新标题/
    ├── MMDD-新标题.html
    ├── source.md
    ├── images/
    │   └── image-001.png
    └── source-images/
        └── image-001.jpg
```

- 仅成功生成的图片复制到 `images/` 并连续编号；失败结果在 HTML 对应位置显示原图链接，不创建空图片文件。
- 同名文章目录存在时追加 `-HHmmss`，内部 HTML 与最终目录同名，禁止覆盖旧结果。
- `source.md` 按归档 blocks 顺序保存原始图文；本地原图后紧接对应 HTTPS URL。
- 仅在存在成功保存的本地原图时创建 `source-images/`；原图本地归档失败时保留 URL 和告警。
