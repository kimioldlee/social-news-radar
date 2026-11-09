# 站点解析规则与栏目清单

本文件记录各站的页面形态、稿件 URL 格式、可用栏目入口与实测踩坑结论。新增或修复采集源时先读这里。

## 通用约定

- parser 接口：`parse_xxx(body, source) -> list[dict]`。统一用 `make_item(source, title_inner_html, url, date=..., note=...)` 构造结果；它负责标题清洗、长度截断、关键词打标与批次标记。
- 源配置字段：`name` / `url` / `parser` / `column` / `media` / `media_type` / `mode` / `recent_only` / `enabled` / `note`。
  - `mode=keyword`（默认）：只保留命中 `keywords` 的条目；若一条都没命中，留前 8 条便于浏览。
  - `mode=all`：全部保留，适合本身就是调查稿聚合的专题页。
  - `recent_only=false`：跳过时间窗过滤，适合更新慢、但历史稿仍有价值的栏目。
- 请求头带完整 UA / Accept / Accept-Language，`Referer` 按目标域名动态设置；每源间隔 1.2 秒，单线程，失败自动跳过。
- 清洗：标题剥离「来源 + 日期 / 阅读量 / 栏目名」尾串，以及 `#话题#` 前缀；去重按 URL 精确匹配 + 标题去标点后前 18 字模糊匹配（同一事件多家转载只留一条）。

## 澎湃新闻（parser: thepaper）

- 栏目页为服务端渲染（SSR），URL 形如 `https://www.thepaper.cn/list_<id>`，稿件链接 `/newsDetail_forward_<id>`。
- 已验证栏目 ID：

| 栏目 | ID | 栏目 | ID |
|---|---|---|---|
| 直击现场 | 25428 | 澎湃人物 | 25427 |
| 绿政公署 | 25425 | 一号专案 | 25424 |
| 法治中国 | 25426 | 浦江头条 | 25422 |
| 中国政库 | 25462 | 港台来信 | 25463 |
| 暖闻湃 | 26911 | 澎湃评论 | 27224 |
| 时政湃 | 142516 | 国防聚焦 | 137534 |
| 人事风向 | 25423 | 未来2% | 119447 |

- 时事总入口：`https://www.thepaper.cn/channel_25950`；澎湃号·媒体：`https://m.thepaper.cn/channel_135619`（PC 版该路径曾返回服务器错误）。
- 站内搜索 `https://www.thepaper.cn/searchResult?id=<URL编码关键词>` 的每条结果都标注所属栏目，**适合人工反查栏目**，但它是客户端异步加载，Python 直抓的 HTML 里没有结果，不能作为脚本数据源。
- 高频 Python 请求会返回 403，需完整浏览器头 + 限速。
- 平台号稿件（标题后缀「澎湃号·媒体」）是第三方上传，页面带「仅提供信息发布平台」特别声明，不属于任何自采栏目；找同类稿要同时盯官媒原发与平台号转载两端。

## 封面新闻（parser: thecover）

- 首页 `https://www.thecover.cn/` 与专题页均为 SSR，稿件链接 `/news/<加密ID>`。
- **ID 内含 `/`、`+`、`%2B`、`==` 等字符，必须原样拼接，不能 urlencode 或删斜杠**；个别专题页 ID 前多一个 `/`（`/news//xxx`），需规范化为 `/news/xxx`。
- 「封面深镜」调查稿聚合专题（最贴近深度调查选题）：
  - 深度调查 `https://www.thecover.cn/getSubjectById/16060?skId=12601`
  - 封面调查 `https://www.thecover.cn/getSubjectById/15624?skId=12303`
- 特色 IP 栏目：封面深镜（深度调查）、追踪到底（消费）、人间事儿、封面评论、云求助 / 云投诉。
- 首页混有大量央媒外链，正则只取 `/news/` 相对链接即可过滤。

## 上游新闻（parser: cqcb）

- 栏目页 `https://www.cqcb.com/channel/<id>.html` 为 SSR，稿件路径 `/news/<频道id>/<YYYY-MM-DD>/<id>.html`，**日期可直接从 URL 精确提取**。
- 常用栏目：`29` 调查/热点、`14` 法治、`6` 评论、`4` 重庆、`30` 要闻、`24` 健康、`21` 教育。
- 法治栏目更新较慢（常见数月前稿件），建议配 `mode=all` + `recent_only=false` 保留历史样本。

## 极目新闻（parser: ctdsb）

- 栏目页 `https://www.ctdsb.net/c<id>.html` 为 SSR，稿件路径 `/c<id>_<YYYYMM>/<id>.html` 或 `/t<topic>/<id>.html`。只有月份，日期取抓取日并在备注标注。
- 栏目 ID：`1716` 世相（社会民生主阵地）、`1738` 消费、`1659` 健康、`1673` 科教、`1715` 观点、`1676` 湖北、`1734` 武汉、`1737` 房产、`1740` 城建、`1747` 时政。
- 注意 `/c1476` 这类是**文章路径前缀**，不是栏目页；直接请求会 404。

## 九派新闻（parser: jiupai_sina）

- 官网 `www.jiupainews.com` 是 React 内容后台（create-react-app + wangeditor + echarts），任意路径都返回同一个约 7KB 空壳，**没有面向读者的公开列表页**；`m.jiupainews.com` 不通，微博移动端 API `m.weibo.cn/api/container/getIndex` 返回 432。
- 降级方案：官方微博在新浪新闻的 SSR 聚合页 `https://sina.cn/media/7516679696`，条目结构为 `<a class="post-link" href="/news/detail/<id>.html">` + `<div class="post-head">` 里的 `.time` + `<div class="post-text">` 正文。
- 正文常以 `【#话题#】` 或连续 `#话题#` 开头，需剥离话题标记后取首句作标题；链接补全为 `https://sina.cn/news/detail/<id>.html`。
- 每条结果备注必须标注来源为「官方微博聚合页」，避免误认为官网直抓。

## 官媒 / 权威索引（适合人工找同类稿，不参与脚本默认采集）

- **中国记协「我的代表作」**：每周一更新，集中收录央媒与地方融媒的深度报道、舆论监督稿，是找同类选题性价比最高的单一入口。
- 人民日报「民生一线」「枝叶关情」「有所思」「多棱镜」；人民网社会频道 `http://society.people.com.cn/`（RSS `http://www.people.com.cn/rss/society.xml` 仍可访问但条目停留在 2025 年，属停更源，默认关闭）。
- 央视网评论 `https://opinion.cctv.com/`（深度报道）、央视《焦点访谈》《每周质量报告》；新华网民生；工人日报 / 中工网权益版；中国新闻周刊、新京报、南方都市报、界面调查、大象新闻调查组、海报新闻「独立调查」。
- 典型链路：官媒原发深度稿 → 平台号 / 融媒体同步 → 多家融媒跟进通报与个案。

## 已知坑

| 现象 | 原因 / 对策 |
|---|---|
| Python 请求返回 403 | 高频或 UA 不完整；补浏览器头、每源限速 1.2 秒 |
| 页面能打开但抓不到标题 | 纯 CSR（React/Vue）；改用 SSR 栏目页或官方账号聚合页 |
| 标题尾部带「沸点 2026-11-08」「封面新闻 26天前」「澎湃新闻、某单位」 | 元信息塞在同一个 `<a>`；用 `clean_title` 的 META_RE 与来源词表剥离 |
| 看板里同一事件出现多条 | 跨源转载；按标题前 18 字归一化去重 |
| CSV 写入 PermissionError | 文件被编辑器 / 预览面板占用；`safe_write` 自动退化到 `社会民生新闻样本_最新.csv` 副本 |
| `--days` 过滤后某源为 0 条 | 栏目只有历史稿；给该源设 `recent_only=false` |

## 新增站点步骤

1. 先确认页面是否 SSR：用浏览器工具或 `urllib` 抓 HTML，搜索页面中是否直接出现中文标题与稿件链接。
2. 找出稿件 URL 的稳定模式与标题所在标签，写一个 `parse_<name>(body, source)`，内部逐条调用 `make_item`。
3. 把函数注册到 `PARSERS` 字典。
4. 在 `news_data.json` 的 `sources` 增加配置（含 `parser`、`media`、`column`、`mode`），必要时同步补 `channels` 入口分组。
5. 用 `--out` 指向临时目录实跑一次，检查标题是否干净、日期是否正确、URL 是否可打开，再决定是否设为默认启用。
