---
name: recreate-wechat-article-v3
description: "Run the v3 isolated WeChat Official Account article reconstruction workflow: fetch the article directly from its public page without n8n, clean and rewrite it, recreate retained illustrations with the project's gender-and-age character references, and package a centered offline HTML with its cleaned source archive and image provenance. Use when the user invokes `$recreate-wechat-article-v3` with an `https://mp.weixin.qq.com/s/...` URL or explicitly asks for the v3 workflow. This skill is separate from and does not modify the original new workflow."
---

# 微信文章原创重构（v3）

本目录是 `recreate-wechat-article-new` 的 v3 独立副本。所有脚本、参考资料和配置均由 v3 skill 自己持有；执行本 skill 时不得回写或替换旧 skill 的文件。

把微信公众号文章转换为清洗后的原文或重新创作的中文文章，并将配图重制为项目统一角色风格，或将全文转换为连续 comic 图片，最终输出离线 HTML。

## 固定约束

先读取项目根目录 `AGENTS.md`。所有相对路径均以项目根目录为基准。

- 仅接受 `https://mp.weixin.qq.com/s/...`。
- 直接请求微信公众号文章页面并解析正文，不调用 n8n、`web/` runner 或任何本地 Webhook；不得修改旧 skill 或 `test-2`。
- 不复制原文，不做逐段近义词替换。以事实与核心观点为素材，重新设计标题、结构、论证、例子和语言。
- 不在 HTML 页面展示原作者、来源账号或二维码。来源 URL 放在 `<meta name="source-url">`，并写入内部归档 `source.md`。
- HTML 页面中的 `html` 与 `body` 元素不得设置 `background` 或 `background-color`；页面背景保持浏览器默认透明/白色，避免复制正文时把页面底色带入目标编辑器。
- 最终 HTML 的 `body` 与 `main` 标签不得设置默认 `style` 属性；页面和内容容器保持无内联默认样式。
- HTML 主题变量 `--paper` 固定为 `#fff`，内容容器可使用该变量作为纯白底色。
- 成功的最终配图必须是新生成；失败项只能显示原始图片链接占位，不允许用来源图片或本地模板兜底。
- 不覆盖 `result/` 中已有结果。

## 与本地 Web runner 的边界

在 Codex 中原生调用本 Skill 时，不要导入或调用项目 `web/` 下的网站 runner、HTTP API 或 `web/.env`。原生流程使用本 Skill 的 Python 脚本和 Codex 原生图片生成能力；`web/` 仅供用户单独启动本地网站时使用第三方 API 编排流程。

## 调用参数

支持以下等价长短参数；参数可放在文章链接前后：

```text
--article-mode trans|ori    -a trans|ori
--image-mode rebuild|copy|comic    -m rebuild|copy|comic
--comic-count N             -n N
--caption-mode ai|local   -c ai|local
--brand on|off  -b on|off
```

未提供参数时使用 `trans + copy + ai + brand on`。`ori` 仍做二次语义清洗，但清洗后的正文原文直出，不压缩、不改写；`trans` 使用现有深度原创重构规则。`comic` 等价于 `ori`，默认生成 5 张图，不使用 `-c`，且不读取或参考来源图片；`brand on` 时每个 comic 叙事节点按文字判断角色并传入对应角色图，无法判断时使用 `photos/girl.png`。遇到未知取值、缺少取值，或同一参数被赋予互相冲突的值时，停止执行并提示有效选项，不自行猜测。图片生成规则见 [references/image-generation-modes.md](references/image-generation-modes.md)，开始处理图片前必须读取该文件。

## 执行流程

### 1. 直接获取与规则清洗

创建唯一工作目录，例如 `.recreate-work/0818-143000-文章短名/`，然后执行：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/fetch_article.py "<微信链接>" --work-dir "<工作目录>" --download-images
```

脚本直接读取微信页面中的 `#js_content`，生成 `source.json` 和仅供检查的 `source-images/`。若页面触发反爬、返回空内容或无法解析，停止重构并请用户提供导出的 HTML/PDF；绝不猜测或伪造正文。

当文章正文完全由图片组成时，直接解析结果可能只有 image blocks。项目端以下载后的原图作为语义来源，不启用 OCR；模型直接阅读图片中的画面和文字后完成重写。此时 `sourceCharacterCount` 为 0，原创正文仍受 800 个非空白字符的绝对上限约束。

不要在上述失败场景改用浏览器直接抓取。只有用户提供导出的 HTML/PDF 后，才能以该文件作为新的正文输入。

### 2. 二次语义审查

按顺序阅读 `source.json` 的 blocks；`copy/rebuild` 需要用图片查看工具检查来源图片，`comic` 只检查文字 blocks，不读取来源图片。删除规则未识别的以下内容：

- 作者、编辑、摄影、来源、出品等纯文本署名；
- 关注/点赞/在看/分享引导、赞赏、广告、优惠、购买、社群、客服、联系方式、往期推荐和版权尾注等纯文本内容；
- 普通图片默认保留并生成，即使图片与正文主题关系不明确或看起来像装饰图，也不要仅凭这一点删除；
- 只有当整张图片主要内容是二维码、广告、关注引导、联系方式或推广时，才从图片计划中删除。图片角落的二维码、署名或水印视为来源水印，保留该图片并在生成提示中要求忽略，不得复制到新图。

记录被二次清洗删除的 `sourceIndex` 及理由，稍后写入 `article-plan.json.removedSourceIndexes`。保留图片之间及其与相关章节的相对顺序；不要手工复制 blocks 构造 `sourceArchive`，该归档由构建脚本自动生成。

### 3. 文章处理

`article-mode=ori`：在二次清洗后按原文顺序输出正文，不进行压缩、转义或重写；保留来源 HTML 中由 `<br>` 和块级节点形成的显式换行，计划中的 `text` 块必须保留这些 `\n`，不得把连续原文行合并成一段；标题仍可根据成品需要重新命名。`article-mode=trans`：执行下述深度原创重构。

完整理解文章后再写，不按来源段落逐句处理。遵循 [references/rewriting-standard.md](references/rewriting-standard.md)：

1. 统计二次清洗后所有正文 text block 的非空白字符数，作为 `sourceCharacterCount`。
2. 提炼事实、中心论点和读者价值，优先删除重复观点、冗余铺垫及非必要案例。
3. 将成稿控制在清洗后正文的 50%～70%，且最多 800 个非空白字符；短原文不得扩写。
4. 设计不同的新标题与章节逻辑，调整论证顺序并使用新的解释或必要例子。
5. 从空白页写作，完成后检查是否残留来源文章的标志性长句或连续相似表达。
6. 除不可避免的专名、数字、短语外，不保留来源句式。

### 4. 生成图片

`image-mode=copy|rebuild` 按来源图片逐张处理。`image-mode=comic` 时只根据清洗后的文字理解全文，将内容拆成 `comicCount` 个连续叙事节点；每张图包含少量中文标题、旁白或对白，按节点判断 `sceneType` 并选择对应的 `characters` 角色参考图，不查看、不分析、不传入来源图片。`comic` 生成的图片不带 `sourceIndex`，成功使用 `image` block，失败使用 `generatedImageLink` block。

### 5. 重制每张图片

角色参考图只约束对应角色，不代表同一画面中的所有人物。准备每张图片任务时，先记录 `sceneType`、`characters`、`sceneCharacterCount`、`secondaryCharacterNotes` 和 `allowIntentionalDuplicate`；每个 `characters` 项还要记录 `ageGroup` 与简短的 `ageEvidence`。`sceneType` 只能是 `single`、`mixed_pair`、`romantic_pair`、`family_trio` 或 `other_group`；`ageGroup` 只能是 `child`、`adult`、`elder` 或 `unknown`。明确夫妻/情侣、普通一男一女同框时使用男角色+女角色；夫妻/情侣+孩子补充 baby 角色。小孩/儿童使用 `girl-baby` 或 `boy-baby`，青年/成年人/父母/夫妻使用 `girl` 或 `boy`，只有明确爷爷奶奶辈、祖父母或老年身份才使用 `girl-old` 或 `boy-old`。父母关系优先于白发、皱纹、服装和坐姿等外观线索，不能仅凭显老外观使用 `elder`；孩子性别不明使用 `girl-baby`。年龄不明的主角使用 `role=girl` 和 `ageGroup=unknown`，多人场景中性别明确但年龄不明的次要人物使用对应青年角色。人物数量缺失时按 1 处理；只有来源文字或画面明确表达双胞胎、分身或镜像时才允许重复外貌，其他情况固定为 `false`。

根据来源图片的画面、可读文案和邻近章节，先判断是否为普通一男一女、夫妻/情侣、夫妻/情侣+孩子或一家三口，再为每个人填充 `purpose`、`ageGroup`、`ageEvidence` 和对应 `role`。`parent`、`mother`、`father` 强制使用 `adult`；`grandparent`、`grandmother`、`grandfather` 强制使用 `elder`；`child`、`son`、`daughter` 强制使用 `child`。普通单主角或其他非男女组合只按主角年龄选择女性角色；主角年龄无法判断时固定使用品牌人物 `girl`，不使用 `girl-old`。`emotion` 仍可独立判断，用于 local 模式的配色与文字排版，不用于选择角色参考图。

不确定主角年龄时运行稳定选择器：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/choose_character.py --url "<微信链接>" --index <从1开始的图片序号>
```

当 `brand on` 时，按 `characters` 选择 `photos/girl.png`、`photos/girl-baby.png`、`photos/girl-old.png`、`photos/boy.png`、`photos/boy-baby.png` 或 `photos/boy-old.png`；普通单主角场景只选一个女性年龄角色，男女组合按男角色+女角色，夫妻/情侣+孩子再追加 baby 角色；关闭时不准备或传入品牌人物参考图，但仍保留角色计划供 prompt 约束。为每张图片先确定一条准确且不复刻原文的目标文案。只有 `local` 模式要求去除空白后最多 20 个可见字符，标点也计入；`ai` 不做字符数限制、截断或超长拦截。

根据调用参数和 [图片生成模式规范](references/image-generation-modes.md) 构造每次独立的 imagegen 请求：

- `rebuild` 只传所选品牌人物参考图；根据来源图的抽象语义与新文章章节自由重构动作、道具、背景、镜头、构图和卡片设计。
- `copy` 同时传来源图片与 `characters` 中的角色参考图；来源图负责整张的信息结构、人物数量、场景关系、色调与卡片排版，角色图按角色映射分别使用；不得复制水印、署名或像素级布局。
- `rebuild` 和 `copy` 的品牌参考图按 `characters` 一一对应；来源图只提供人物数量、动作和空间关系，不提供可复制的身份外观。普通单主角场景只传一个女性年龄角色；男女组合传男、女角色；夫妻/情侣+孩子传男、女和 baby 角色。次要人物不得复制其他角色的脸型、发型、发色、体型、年龄、服装、配饰和姿态。除明确的双胞胎、分身或镜像外，普通多人画面禁止出现两个外貌近乎相同的人物。
- `ai` 让 imagegen 一次生成包含目标中文文案的完整纵向卡片。提示词必须要求接近 1080:1350 的竖向画布、使用 `#F7F6F6` 淡灰白背景，并让插画与文案充分利用画面、避免过量上下留白；不得附加本地模式的情绪底色、分隔线、字体或 20 字规则，也不做 OCR、错字检查或重试。
- `local` 让 imagegen 只生成无文字的上半部分插画，再执行下方 `compose_card.py` 命令；`brand off` 时省略 `--reference`，只要 AI 插画成功即可排版。`--reference` 使用归一化后的 `characters[0]` 主角色图，`--emotion` 仍只控制本地文案配色。

先完成全部图片任务的语义、文案、提示词和参考图路径准备，再在同一个并发编排调用中一次性发出全部 imagegen 请求。使用 `Promise.allSettled` 或等价的独立结算方式；禁止先等待一部分图片完成后再发下一批。`copy/rebuild` 每张来源图片对应一个独立请求；`comic` 每个叙事节点对应一个独立请求；任一失败不得取消其他请求。

`ai` 模式把首次成功返回的原始 PNG 保存为 `<工作目录>/raw-cards/image-001.png`。全部 imagegen 结果结算后，一次性并发归一所有成功图片：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/normalize_ai_card.py --input "<工作目录>/raw-cards/image-001.png" --output "<工作目录>/cards/image-001.png"
```

归一脚本会裁除过量外围留白、把与边缘相连的背景统一为示例取样色 `#F7F6F6`，并输出精确的 1080×1350 PNG；不得直接把 `raw-cards/` 图片写入 manifest。

`local` 模式的 imagegen 结果全部结算后，对成功插画一次性并发执行以下命令；不得逐张串行合成：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/compose_card.py --illustration "<AI插画>" [--reference "<角色参考图>"] --caption "<新文案>" --emotion <喜|乐|怒|哀|通用> --output "<工作目录>/cards/image-001.png"
```

脚本会用情绪浅色铺满整个下半部分，取消内部边框和白色留边，并使用放大为原规格 1.5 倍的轻艺术中文字体，做确定性的逐字轻微旋转与上下错落。

每张请求只尝试一次。`ai`、`comic` 和 `local` 成功结果必须是 `cards/` 下可读且尺寸精确为 1080×1350 的 PNG。不得逐图 OCR、重复视觉复查或重试。imagegen、AI 归一或 `local` 合成任一步失败时，都不调用模板卡片，也不嵌入来源图片；普通模式写入 `sourceImageLink`，comic 写入 `generatedImageLink`，由渲染器输出对应占位。

### 5. 渲染 HTML

按 [references/manifest-schema.md](references/manifest-schema.md) 创建精简 UTF-8 `article-plan.json`，写入文章/图片模式、标题、可选导语、二次清洗删除的索引、新文章 blocks，以及每张图片的成功路径或失败结果。不得手工整理完整 `sourceArchive` 或计算 `sourceCharacterCount`。

运行自动构建器；它会从 `source.json` 补齐清洗后的原文归档、原始图片 URL/路径、字符数，并把普通模式失败结果转换为 `sourceImageLink`。comic 的独立图片结果使用 `generatedImageLink`，不绑定来源图片：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/build_manifest.py --source "<工作目录>/source.json" --article "<工作目录>/article-plan.json" --output "<工作目录>/manifest.json"
```

构建成功后运行：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/render_article.py --manifest "<工作目录>/manifest.json" --result-dir result
```

脚本会在 `result/` 下创建单篇文章目录，目录内包含同名 HTML、`source.md`、`images/`，并在有可用原图时创建 `source-images/`。`source.md` 按原文图文顺序嵌入本地原图，并在每张图后记录原始 URL；下载失败的原图保留 URL 和失败标记，不阻断成品。脚本输出文章目录、HTML、Markdown、图片目录与归档告警。随后校验：

```powershell
python .agents/skills/recreate-wechat-article-v3/scripts/validate_output.py "<最终HTML路径>" --expected-image-size 1080x1350
```

最后只做一轮结构与资源校验。若当前环境支持本地页面渲染，再视觉检查移动端宽度、标题与段落、图片顺序和中文断行；不得反复尝试已被环境安全策略阻止的 `file://` 浏览器预览。

渲染器会把正文、导语和引用按 `，。！？；,.!;?` 分行并保留行尾标点；连续标点视为同一结尾，数字中的小数点和千位逗号不拆分。每个拆分行都是独立、居中的 `<p>`；标题和章节标题只居中、不按标点拆分。最终 HTML 的 `<head>` 不包含 `<style>`，不使用 `class`、`span` 或 `figure`，所有视觉样式直接写在对应元素的 `style` 属性中。

## 完成交付

向用户报告文章目录和最终 HTML 的可点击绝对路径、成功新图数、失败链接占位数、归档原图数量、原图归档告警及被清理内容概况。不要在回答中粘贴来源全文，也不要把内部 manifest 当作用户成品。
