当前项目是<处理自媒体文章>


recreate-wechat-article-v3

默认:-a trans -m copy -c ai -b on

各参数支持的值如下：
- -a / --article-mode
  - trans：压缩并原创重写
  - nearby：使用原标题逐段近义改写，保留正文块顺序和换行，约 80% 长度仅作参考
  - ori：二次语义清洗后保留原文，不压缩、不改写
- -m / --image-mode
  - rebuild：根据语义重新设计配图
  - copy：参考原图信息结构重制配图
  - comic：根据文章内容生成连续叙事漫画图
- -c / --caption-mode
  - ai：AI 直接生成带中文文案的完整卡片
  - local：AI 生成无文字插画，再由本地脚本排版文案
- -b / --brand
  - on：启用 photos/ 品牌人物参考图
  - off：不使用品牌人物参考图
补充：comic 模式等价于 ori，默认生成 5 张图，并且不使用 -c 参数。
