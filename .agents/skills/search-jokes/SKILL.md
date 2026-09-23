---
name: search-jokes
description: 全网检索可公开访问的中文笑话，按原文保真、长度或对话轮数门槛和长期去重档案筛选。用于“每日一笑”、搜索 N 条笑话或寻找反转笑话；不用于创作、改写或压缩笑话。
---

# 全网笑话检索

输出可追溯的中文笑话原文，并持续维护本 skill 的去重档案。先读取项目根目录 `AGENTS.md`。默认目标为 10 条；下列命令从项目根目录执行。

## 固定规则

- 只采纳能打开并核对的公开来源。每条保留来源标题、HTTPS URL 和访问日期。
- 原文不改写、不删句、不概括、不替换标点或措辞；只允许为了阅读调整换行。来源、序号和标签必须放在原文之外。
- 新推送必须满足至少一项：含标点、不计换行的原文字数不少于 100，或对话轮数不少于 8。未达标不入库、不展示。
- 仅收录轻松干净内容；排除色情、暴力、政治、歧视、仇恨和明显人身攻击。
- 先过滤再展示。原文指纹、88% 以上措辞相似度、以及“人物关系 + 场景 + 前提/包袱”相同的语义重复，都必须拦截。
- 所有发送给用户的条目以 `shown` 状态入库；候选筛选本身不得写入档案。用户明确选用后才改为 `adopted`。已有 `legacy-v1` 条目只用于去重，不视为符合新门槛。

## 执行流程

1. 先校验本地档案：

   ```powershell
   python .agents/skills/search-jokes/scripts/daily_jokes.py validate
   ```

2. 建立本轮唯一 `batch_id`，并从多个独立来源分批检索。逐条打开来源页，逐字提取单个笑话；不要把搜索摘要、文章导语、作者署名或相邻笑话混进原文。
3. 在 `.recreate-work/` 准备候选 JSON。每条必须包含 `id`、`text`、`source.title`、`source.url`、`accessed_at`、`tags`、`twist_type`、`state`、`batch_id`、`dialogue_turn_count` 和四字段 `semantic_signature`；将 `state` 设为 `shown`。
4. 每批先无副作用地筛选，再只将本轮仍需要展示的条目入库。`<剩余数量>` 是目标数量减去当前批次已入库数量：

   ```powershell
   python .agents/skills/search-jokes/scripts/daily_jokes.py screen "<候选JSON>" --limit <剩余数量> --state shown --accepted-output ".recreate-work/<批次>-accepted.json"
   python .agents/skills/search-jokes/scripts/daily_jokes.py add ".recreate-work/<批次>-accepted.json" --state shown
   ```

5. 每批筛选后重新计算缺口；有拒绝项时继续检索下一批，不得把一次拒绝视为整轮失败。单次最多逐条打开并核验 50 个候选；达到目标数量立即停止。因来源的转载/交付条件无法满足而不能使用的候选，记录为“来源不可交付”并继续搜索。
6. 只用 `render` 输出已入库的本批条目：

   ```powershell
   python .agents/skills/search-jokes/scripts/daily_jokes.py render --batch "<批次ID>" --limit <目标数量> --require <目标数量>
   ```

   若核验满 50 个候选仍不足目标，改用不带 `--require` 的 `render` 输出实际通过数量，并报告缺口及各类拦截数量；不得用短笑话、改写版或重复内容补足。

`data/joke_registry.json` 是唯一长期记忆库。不要覆盖或清空它；手工调整原文或语义签名后，运行 `python .agents/skills/search-jokes/scripts/daily_jokes.py refresh` 重算并写回长度与指纹。
