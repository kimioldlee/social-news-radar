# -*- coding: utf-8 -*-
"""
中国社会民生新闻采集 + 可视化看板（WorkBuddy Skill: social-news-radar）

用法：
    python scripts/collect_news.py --out <输出目录>              抓取并重建看板与 CSV
    python scripts/collect_news.py --out <目录> --no-fetch       只重建，不联网
    python scripts/collect_news.py --out <目录> --init           用模板重置数据文件

产出（全部写入 --out 目录，不污染技能包内部）：
    news_data.json               单一数据源（示例样本 + 实时抓取 + 源/关键词/渠道配置）
    社会民生新闻样本.csv          UTF-8 BOM，Excel 可直接打开
    社会民生新闻抓取看板.html     单文件离线看板，双击即可浏览

依赖：仅 Python 标准库。源与关键词配置在 assets/news_data.template.json，
各站点解析规则见 references/sources.md。

说明：
    - 采集走各站公开的栏目页 / RSS，带完整浏览器头、单线程限速（每源间隔 1.2 秒）。
    - 失败源自动跳过并在控制台提示，不影响其它源。
    - 遵守目标站点的 robots 协议与版权声明，仅用于选题浏览与合规的低频采集。
"""
import argparse
import csv
import difflib
import html
import json
import os
import re
import ssl
import sys
import time
import urllib.request
import shutil
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_FILE = os.path.join(SKILL_DIR, "assets", "news_data.template.json")

# 输出目录默认当前工作目录，可用 --out 覆盖；绝不把结果写进技能包内部
DATA_FILE, CSV_FILE, HTML_FILE = "", "", ""


def configure_output(out_dir):
    global DATA_FILE, CSV_FILE, HTML_FILE
    os.makedirs(out_dir, exist_ok=True)
    DATA_FILE = os.path.join(out_dir, "news_data.json")
    CSV_FILE = os.path.join(out_dir, "社会民生新闻样本.csv")
    HTML_FILE = os.path.join(out_dir, "社会民生新闻抓取看板.html")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "close",
}
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def fetch(url, timeout=15, retry=1):
    for attempt in range(retry + 1):
        try:
            hdr = dict(HEADERS)
            parsed = urlparse(url)
            hdr["Referer"] = "{}://{}/".format(parsed.scheme, parsed.netloc)
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
                raw = resp.read()
            for enc in ("utf-8", "gb18030"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            return raw.decode("utf-8", "ignore")
        except Exception as exc:
            if attempt >= retry:
                print("  ! 抓取失败 {} ({})".format(url, str(exc)[:60]))
                return ""
            time.sleep(1.5)
    return ""


def clean(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return html.unescape(text).strip()


TODAY = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

# 列表页常把「来源 + 日期 + 阅读量」塞在同一个 <a> 里，统一从这些元信息处截断
META_RE = re.compile(
    r"\s+(?:封面新闻|沸点|极目新闻|上游新闻|九派新闻|新华社|人民日报|央视新闻|央视网|大皖新闻|潮新闻)?\s*"
    r"(?:\d{4}-\d{2}-\d{2}|\d{2}-\d{2}|\d+(?:\.\d+)?万|\d+(?:天|小时|分钟)前|\d+图|昨天|前天|今天).*$"
)


def clean_title(inner):
    """把 <a> 内部文本还原成干净标题，剥离尾部来源/时间/阅读量等噪音。"""
    text = html.unescape(re.sub(r"<[^>]+>", " ", inner or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    m = META_RE.search(text)
    if m:
        text = text[:m.start()]
    # 列表页常在标题后直接跟转载来源，形如「… 澎湃新闻、河南财经政法大学」
    text = re.sub(
        r"\s+[\u4e00-\u9fa5]{2,10}(?:[、，,][\u4e00-\u9fa5]{2,12}){1,3}\s*$", "", text
    )
    text = re.sub(
        r"\s+(?:法治|新闻|沸点|热点|评论|世相|封面新闻|上游新闻|极目新闻|九派新闻|"
        r"央视新闻客户端|央视新闻|新华社|人民日报|澎湃新闻|扬子晚报|经视直播|"
        r"大皖新闻|潮新闻|荔枝新闻|封面深镜|1818黄金眼|封面新闻|沸点)\s*$",
        "", text,
    )
    return text.strip()


# 社会民生聚焦：排除政务通知 / 政策发布 / 会议部署 / 机构宣传类稿件。
# HARD 命中即排除；SOFT 命中但标题里没有任何具体人物时才排除（避免误删人物报道）。
FOCUS_HARD = (
    "习近平", "总书记", "中全会", "学习贯彻", "重要讲话", "会见", "致电", "复信",
    "政治局", "国务院", "发文", "印发", "出台", "条例", "草案", "二审", "常务会议",
    "召开会议", "工作专班", "挂牌督办", "举一反三", "宣传周", "拉开帷幕", "启动仪式",
    "亮相", "获授", "授予", "送达", "开幕", "揭牌", "表彰", "评选", "逝世", "秋招",
    "正式落地", "专项计划", "专项规划", "征求意见", "集中整治", "排查隐患", "部署",
    "联合发文", "新图景", "正式实施",
)
FOCUS_SOFT = ("活动", "会议", "发布", "通知", "方案", "规划", "措施", "政策", "走进校园", "开展")
FOCUS_PEOPLE = (
    "男子", "女子", "老人", "学生", "家长", "患者", "家属", "消费者", "商家", "商户",
    "骑手", "教师", "患儿", "小伙", "大爷", "婆婆", "硕士", "大学生", "工人",
    "农民工", "受害者", "嫌疑人", "孩子", "市民", "村民", "护士", "医生", "民警",
    "司机", "摊主", "游客", "乘客", "博主", "网友", "毕业生", "主播", "班主任",
    "当事人", "住户", "居民", "孕妇", "学子",
)
# 暖闻 / 校园趣闻 / 健康科普：都是软内容，不属于「社会民生强相关事件报道」。
# 只作用于 form=栏目更新 的兜底稿，避免误伤深度调查 / 舆论监督等正式报道。
FOCUS_FLUFF = (
    # 暖闻、机构宣传、表彰
    "暖侬心", "生命通道", "护送", "平安就医", "风华正茂", "青春之力", "守护每一颗心",
    "以热血之名", "见义勇为", "暖心事", "致敬", "好人好事", "千里驰援",
    # 校园趣闻、校园软新闻
    "花样游泳", "零基础学", "拿下4枚金牌", "吐火术", "非遗登台", "惊艳全场",
    "苦练千次", "国旗下", "滑翔班", "思政课", "校史", "推迟上学时间",
    "让孩子多睡会", "美人鱼", "健身学舞", "为演好",
    # 健康科普、养生警示
    "专家提醒", "刷短视频超", "注意力被反复重塑", "课业拖延", "隔夜菜", "肝脏抽出",
    "脓液", "医生谈", "久坐加班", "血栓风险", "肺栓塞", "血脂高", "自己开药",
    "险丧命", "肝功能", "科普",
)
FOCUS_ENABLED = True


def is_social_focus(x):
    """判定是否为社会民生强相关的事件报道；政务通知类与软内容返回 False。"""
    if not FOCUS_ENABLED:
        return True
    t = x.get("title", "")
    if any(k in t for k in FOCUS_HARD):
        return False
    if any(k in t for k in FOCUS_SOFT) and not any(k in t for k in FOCUS_PEOPLE):
        return False
    if x.get("form") == "栏目更新" and any(k in t for k in FOCUS_FLUFF):
        return False
    return True


# 报道形态自动归类：按标题特征顺序匹配，越具体的规则越靠前
FORM_RULES = [
    ("评论", ("马上评", "锐评", "时评", "社论", "零度时评", "言短意长", "评丨", "评｜",
              "热点快评", "封面评论", "极目锐评", "评论员", "岂能明目张胆")),
    ("舆论监督", ("暗访", "起底", "曝光", "乱象", "黑产", "揭秘", "卧底", "彻查", "查处",
                "涉嫌", "记者调查", "调查丨", "调查｜", "调查|", "围猎", "猫腻", "被查实",
                "诈骗", "跑路", "白干", "甩客", "提级调查", "被查扣")),
    ("深度调查", ("深度", "追踪", "调查报告", "纪实", "记者手记", "弦外", "探访", "对话",
                "回访", "复盘", "访谈", "背后的", "疑云")),
    ("通报回应", ("通报", "回应", "情况说明", "致歉", "辟谣", "澄清", "公告", "声明",
                "发布", "被抓获", "已被依法控制")),
    ("个案维权", ("维权", "索赔", "退款", "获刑", "判刑", "判决", "赔偿", "被判", "起诉",
                "申诉", "立案", "刑拘", "拘留", "处罚", "调查组", "投诉", "封禁",
                "预存", "讨薪", "救助")),
    ("政策服务", ("政策", "规划", "部署", "出台", "措施", "通知", "医保", "补贴", "发文",
                "条例", "办法", "方案", "印发", "六部门", "卫健委", "草案", "专项计划",
                "常务会议", "排查隐患", "正式落地", "征求意见", "二审")),
]


def classify_form(title):
    """按标题关键词给实时抓取稿打形态标签；无命中时落到「栏目更新」兜底。"""
    for form, keywords in FORM_RULES:
        if any(k in title for k in keywords):
            return form
    return "栏目更新"


def make_item(source, title, url, date="", note=""):
    """统一构造一条抓取结果，供各站点 parser 复用。"""
    title = clean_title(title)
    if not title or len(title) < 8 or not re.search(r"[\u4e00-\u9fa5]", title):
        return None
    if len(title) > 88:
        title = title[:88] + "…"
    return {
        "date": date or TODAY,
        "media": source.get("media", source.get("name", "")),
        "type": source.get("media_type", "融媒"),
        "column": source.get("column", ""),
        "title": title,
        "form": classify_form(title),
        "topics": match_topics(title),
        "url": url,
        "note": note or ("实时抓取自 " + source.get("name", "")),
        "bucket": "实时抓取",
        "source": source.get("name", ""),
    }


def parse_thepaper(body, source):
    """澎湃栏目页为服务端渲染，标题与 newsDetail 链接可直接提取。"""
    items = []
    pairs = re.findall(
        r'href="(/newsDetail_forward_\d+)"[^>]*>\s*(?:<[^>]+>\s*)*([^<]{6,120})',
        body,
    )
    seen = set()
    for path, title in pairs:
        title = clean(title)
        url = "https://www.thepaper.cn" + path
        if not title or url in seen:
            continue
        seen.add(url)
        items.append({
            "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
            "media": "澎湃新闻",
            "type": "融媒",
            "column": source.get("column", ""),
            "title": title,
            "form": "栏目更新",
            "topics": match_topics(title),
            "url": url,
            "note": "实时抓取自 " + source.get("name", ""),
            "bucket": "实时抓取",
            "source": source.get("name", ""),
        })
    return items


def parse_rss(body, source):
    items = []
    for block in re.findall(r"<item>(.*?)</item>", body, re.S):
        def tag(name):
            m = re.search(r"<" + name + r"[^>]*>(.*?)</" + name + r">", block, re.S)
            if not m:
                return ""
            v = m.group(1).strip()
            if v.startswith("<![CDATA[") and v.endswith("]]>"):
                v = v[9:-3]
            return clean(v)
        title, link, pub = tag("title"), tag("link"), tag("pubDate")
        if not title or not link:
            continue
        date = pub[:10] if re.match(r"\d{4}-\d{2}-\d{2}", pub) else ""
        items.append({
            "date": date,
            "media": "人民网",
            "type": "官媒",
            "column": source.get("column", ""),
            "title": title,
            "form": "栏目更新",
            "topics": match_topics(title),
            "url": link,
            "note": "RSS 抓取自 " + source.get("name", ""),
            "bucket": "实时抓取",
            "source": source.get("name", ""),
        })
    return items


def parse_homepage(body, source):
    """通用兜底：提取页面中的中文长标题链接。规则宽松，结果仅供浏览。"""
    items = []
    pairs = re.findall(r'href="(https?://[^"]+)"[^>]*>\s*([^<]{12,80})\s*<', body)
    seen = set()
    for url, title in pairs:
        title = clean(title)
        if url in seen or not re.search(r"[\u4e00-\u9fa5]{8,}", title):
            continue
        seen.add(url)
        items.append({
            "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
            "media": source.get("name", ""),
            "type": "融媒",
            "column": source.get("column", ""),
            "title": title,
            "form": "栏目更新",
            "topics": match_topics(title),
            "url": url,
            "note": "首页链接抓取，需人工复核",
            "bucket": "实时抓取",
            "source": source.get("name", ""),
        })
    return items[:25]


def parse_thecover(body, source):
    """封面新闻：首页与 getSubjectById 专题页均为服务端渲染，稿件链接 /news/<加密ID>。

    注意 ID 内可能含 / + %2B == 等字符，必须原样拼接，不能 urlencode 或去斜杠。
    """
    items, seen = [], set()
    pat = r'href="(/news/[^"]+)"[^>]*>(.*?)</a>'
    for path, inner in re.findall(pat, body, re.S):
        if path.startswith("/news//"):
            path = "/news/" + path[7:]   # 部分专题页 ID 前多了一个斜杠
        url = "https://www.thecover.cn" + path
        if url in seen:
            continue
        it = make_item(source, inner, url)
        if it:
            seen.add(url)
            items.append(it)
    return items


def parse_cqcb(body, source):
    """上游新闻：/channel/<id>.html 为服务端渲染，稿件 URL 自带 YYYY-MM-DD。"""
    items, seen = [], set()
    pat = r'href="(/news/\d+/(\d{4}-\d{2}-\d{2})/\d+\.html)"[^>]*>(.*?)</a>'
    for path, date, inner in re.findall(pat, body, re.S):
        url = "https://www.cqcb.com" + path
        if url in seen:
            continue
        it = make_item(source, inner, url, date=date)
        if it:
            seen.add(url)
            items.append(it)
    return items


def parse_ctdsb(body, source):
    """极目新闻：/c<id>.html 栏目页为服务端渲染，稿件路径 /c<id>_<YYYYMM>/<id>.html。"""
    items, seen = [], set()
    pat = r'href="(/(?:c\d+_(\d{6})|t\d+)/\d+\.html)"[^>]*>(.*?)</a>'
    for path, ym, inner in re.findall(pat, body, re.S):
        url = "https://www.ctdsb.net" + path
        if url in seen:
            continue
        note = "实时抓取自 " + source.get("name", "")
        if ym:
            note += "（栏目月份 {}，日期取抓取日）".format(ym)
        it = make_item(source, inner, url, note=note)
        if it:
            seen.add(url)
            items.append(it)
    return items


def parse_jiupai_sina(body, source):
    """九派新闻：官网 www.jiupainews.com 是 React 内容后台（所有路径返回同一空壳，
    无面向读者的公开列表页），因此改用其官方微博在新浪新闻的聚合页，条目为
    <a class="post-link"> + <div class="post-text"> + <div class="time">。
    """
    items, seen = [], set()
    pat = r'<a class="post-link" href="(/news/detail/\d+\.html)">(.*?)</a>'
    for path, block in re.findall(pat, body, re.S):
        url = "https://sina.cn" + path
        if url in seen:
            continue
        tm = re.search(r'<div class="time">(\d{4}-\d{2}-\d{2})', block)
        date = tm.group(1) if tm else TODAY
        tx = re.search(r'<div class="post-text">(.*?)</div>', block, re.S)
        if not tx:
            continue
        text = re.sub(r"^(?:#[^#]{2,40}#\s*)+", "", clean_title(tx.group(1))).strip()
        if text.startswith("【") and "】" in text:
            head = text[1:text.index("】")].replace("#", "").strip()
            rest = re.sub(
                r"^(?:#[^#]{2,40}#\s*)+", "", text[text.index("】") + 1:].strip()
            ).strip()
            text = head + ("｜" + rest[:38] if rest else "")
        it = make_item(
            source, text, url, date=date,
            note="九派新闻官方微博（新浪新闻聚合页）；官网为 React SPA，无公开列表页",
        )
        if it:
            seen.add(url)
            items.append(it)
    return items


def parse_link_list(body, source):
    """通用列表页 parser：按 url_filter 抽稿件链接，标题交 clean_title 清洗。

    拓展来源时只需在 news_data.json 配置 link_pattern / url_filter / base /
    date_from_url / media / column，不必再写站点专用函数。
    """
    pat = source.get("link_pattern", r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>')
    url_re = re.compile(source.get("url_filter", r".+"))
    title_re = re.compile(source["title_include"]) if source.get("title_include") else None
    title_ban = re.compile(source["title_exclude"]) if source.get("title_exclude") else None
    max_len = source.get("title_max_len", 70)
    base = source.get("base", "")
    date_pat = source.get("date_from_url")
    items, seen = [], set()
    for href, inner in re.findall(pat, body, re.S):
        if not url_re.search(href):
            continue
        url = href if href.startswith("http") else base + href
        title = clean_title(inner)
        if not title or len(title) < 8:
            continue
        # 部分列表页把「标题 + 摘要 + [全文]」塞进同一个 <a>，只取纯标题
        if re.search(r"\[全文\]|阅读全文|\[详情\]", title) or len(title) > max_len:
            continue
        if title_re and not title_re.search(title):
            continue
        if title_ban and title_ban.search(title):
            continue
        date = ""
        if date_pat:
            m = re.search(date_pat, href)
            if m and len(m.groups()) >= 3:
                date = "{}-{}-{}".format(m.group(1), m.group(2), m.group(3))
        it = make_item(source, title, url, date=date)
        if it and url not in seen:
            seen.add(url)
            items.append(it)
    return items


PARSERS = {
    "thepaper": parse_thepaper,
    "rss": parse_rss,
    "homepage": parse_homepage,
    "thecover": parse_thecover,
    "cqcb": parse_cqcb,
    "ctdsb": parse_ctdsb,
    "jiupai_sina": parse_jiupai_sina,
    "link_list": parse_link_list,
}


def match_topics(title):
    hits = []
    for group, words in KEYWORDS.items():
        if any(w in title for w in words):
            hits.append(group)
    return hits


def normalize_topics(x):
    """把主题标签统一到 keywords 的类别级分组，消除「用血紧张」这类事件级碎标签。

    精选样本早期是人工自由标（76 种），实时抓取是关键词组名；两套体系混在
    看板标签区会形成只对应一两条稿的孤立标签。统一用「标题 + 原始标签」
    重新匹配 6 大类，原始细标签保留在 tags_raw 便于追溯。
    """
    raw = x.get("topics", [])
    text = x.get("title", "") + " " + " ".join(raw) + " " + " ".join(x.get("tags_raw", []))
    return match_topics(text)


WINDOW_DAYS = 30


def window_cutoff(days=None):
    """近 N 天的起始日期（含当天）；days<=0 表示不设时间窗。"""
    n = WINDOW_DAYS if days is None else days
    if not n or n <= 0:
        return ""
    return (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=n)).strftime("%Y-%m-%d")


def within_window(item, cutoff):
    """无日期的条目保留；有日期则必须不早于窗口起点。"""
    return not cutoff or not item.get("date") or item["date"] >= cutoff


def norm_title(title):
    """去掉标点后取前 18 字，用于跨源同题去模糊去重。"""
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", title)[:18]


DEDUP_THRESHOLD = 0.48
ANCHOR_FLOOR = 0.28

# 微博「警情通报」类模板串会让不同事件看起来很像，含这些标记的公共子串不算实体锚点
TEMPLATE_MARKERS = (
    "公安局", "警情通报", "发布警情", "接报辖区", "2026年", "11月", "时许",
    "央视新闻", "新华社客户端",
)


def longest_common_substring(a, b):
    m = difflib.SequenceMatcher(None, a, b).find_longest_match(0, len(a), 0, len(b))
    return a[m.a:m.a + m.size]


def strong_anchor(a, b):
    """两条标题共享的具体实体串（地名 / 机构 / 事件对象），用于低相似度同题判定。"""
    lcs = longest_common_substring(norm_full(a), norm_full(b))
    if len(lcs) < 3 or any(m in lcs for m in TEMPLATE_MARKERS):
        return ""
    return lcs


def norm_full(title):
    return re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", title or "")


def title_bigrams(title):
    s = norm_full(title)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity_from_grams(A, B):
    """字符 bigram 的 Jaccard 与重叠系数取较大者；重叠系数能抓住「短标题被长标题包住」。"""
    if not A or not B:
        return 0.0
    jaccard = len(A & B) / len(A | B)
    overlap = len(A & B) / min(len(A), len(B))
    return max(jaccard, overlap * 0.85)


def date_gap(a, b):
    try:
        da = datetime.strptime(a.get("date", ""), "%Y-%m-%d")
        db = datetime.strptime(b.get("date", ""), "%Y-%m-%d")
        return abs((da - db).days)
    except (ValueError, TypeError):
        return None


def is_duplicate(a, b, A, B, threshold):
    # 标题完全一致：跨批次、跨日期无条件合并
    if norm_full(a["title"]) == norm_full(b["title"]):
        return True
    gap = date_gap(a, b)
    if gap is not None and gap > 1:
        return False
    score = similarity_from_grams(A, B)
    if score >= threshold:
        return True
    # 中相似度：同一事件的不同侧面（人数进展 / 救治情况 / 追责通报）字面差异很大，
    # 靠共享的具体实体子串兜底；模板串（如「公安局发布警情通报…」）不算锚点
    if score >= ANCHOR_FLOOR and strong_anchor(a["title"], b["title"]):
        return True
    return False


def dedup_items(items, threshold):
    """事件级去重：并查集聚类，每簇保留一条主稿，其余聚合为主稿的同题报道列表。

    保留优先级：精选样本 > 官媒 > 其它；同级取日期更新、标题信息更全的一条。
    """
    if threshold <= 0 or len(items) < 2:
        return items, 0
    grams = [title_bigrams(x["title"]) for x in items]
    parent = list(range(len(items)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if is_duplicate(items[i], items[j], grams[i], grams[j], threshold):
                union(i, j)

    clusters = {}
    for i in range(len(items)):
        clusters.setdefault(find(i), []).append(i)

    kept, removed = [], 0
    for idxs in clusters.values():
        best = max(idxs, key=lambda i: (
            items[i].get("bucket") == "精选样本",
            items[i].get("type") == "官媒",
            items[i].get("date", ""),
            len(re.findall(r"\d+", items[i].get("title", ""))),  # 含数字多的通常是更新、更全的进展稿
            len(items[i].get("title", "")),
        ))
        main = dict(items[best])
        others = [items[i] for i in idxs if i != best]
        if others:
            medias = list(dict.fromkeys(
                x.get("media", "") for x in others if x.get("media")))
            main["dup_count"] = len(others)
            main["dup_media"] = medias
            main["dup_urls"] = [
                {"media": x.get("media", ""), "title": x.get("title", ""), "url": x.get("url", "")}
                for x in others
            ][:6]
            tail = "同题另有 {} 家媒体报道：{}".format(len(others), "、".join(medias[:5]))
            main["note"] = (main.get("note", "") + "｜" + tail).strip("｜")
        kept.append(main)
        removed += len(others)
    return kept, removed


def crawl(data, days, max_per_source):
    fresh = []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    legacy = {"thepaper_list": "thepaper", "rss": "rss", "homepage": "homepage"}
    for src in data.get("sources", []):
        if not src.get("enabled"):
            continue
        kind = src.get("parser") or legacy.get(src.get("type"), "homepage")
        parser = PARSERS.get(kind, parse_homepage)
        print("→ 抓取 {} [{}]".format(src["name"], kind))
        body = fetch(src["url"])
        if not body:
            continue
        got = [x for x in parser(body, src) if x]
        if src.get("recent_only", True):
            got = [x for x in got if not x["date"] or x["date"] >= cutoff]
        if src.get("mode", "keyword") == "keyword":
            hit = [x for x in got if x["topics"]]
            got = hit if hit else got[:8]   # 无命中时留少量样本便于浏览
        got = got[:src.get("max_items", max_per_source)]
        print("   {} 条".format(len(got)))
        fresh.extend(got)
        time.sleep(1.2)
    # 去重：URL 精确去重 + 标题归一化模糊去重（同一事件多家转载只留一条）
    seen_url, seen_title, out = set(), set(), []
    for x in fresh:
        key = norm_title(x["title"])
        if x["url"] in seen_url or (key and key in seen_title):
            continue
        seen_url.add(x["url"])
        if key:
            seen_title.add(key)
        out.append(x)
    wcut = window_cutoff()
    if wcut:
        kept = [x for x in out if within_window(x, wcut)]
        if len(kept) != len(out):
            print("   时间窗过滤：剔除 {} 条早于 {} 的稿件".format(len(out) - len(kept), wcut))
        out = kept
    return out


def all_items(data, apply_focus=True):
    """生成看板 / CSV 的条目：时间窗 + 可选「只要社会民生事件报道」过滤。

    main() 去重时传 apply_focus=False，保证被聚焦过滤排除的稿仍留在数据文件里，
    关闭 --focus 或调整词表后可随时恢复显示。
    """
    cutoff = window_cutoff()
    curated = []
    for x in data.get("curated", []):
        y = dict(x)
        y["bucket"] = "精选样本"
        y["source"] = x.get("media", "")
        if not y.get("tags_raw"):
            y["tags_raw"] = list(x.get("topics", []))
        y["topics"] = normalize_topics(y)
        curated.append(y)
    crawled = [dict(x, bucket="实时抓取") for x in data.get("crawled", [])]
    for y in crawled:
        y["topics"] = normalize_topics(y)
    merged = [x for x in curated + crawled if within_window(x, cutoff)]
    if apply_focus:
        merged = [x for x in merged if is_social_focus(x)]
    return merged


def safe_write(path, text, encoding="utf-8"):
    """先写临时文件再原子替换；目标被编辑器/预览占用时退化为固定副本，避免文件堆积。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding=encoding, newline="") as f:
            f.write(text)
        os.replace(tmp, path)
        return path
    except PermissionError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        stem, ext = os.path.splitext(path)
        alt = stem + "_最新" + ext
        try:
            with open(alt, "w", encoding=encoding, newline="") as f:
                f.write(text)
        except PermissionError:
            # 同一天多次运行写同一个文件，避免时间戳副本无限堆积
            alt = "{}_{}{}".format(stem, datetime.now().strftime("%Y%m%d"), ext)
            try:
                with open(alt, "w", encoding=encoding, newline="") as f:
                    f.write(text)
            except PermissionError:
                print("! {} 及所有候选副本均被占用，跳过写入".format(os.path.basename(path)))
                return ""
        print("! {} 被占用（可能在面板中打开），已写入副本 {}".format(
            os.path.basename(path), os.path.basename(alt)))
        return alt


def build_csv(data):
    import io
    header = ["数据批次", "日期", "媒体", "类型", "栏目", "报道形态", "标题", "主题标签", "链接", "备注", "同题报道数"]
    buf = io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(header)
    for x in all_items(data):
        w.writerow([
            x.get("bucket", ""), x.get("date", ""), x.get("media", ""),
            x.get("type", ""), x.get("column", ""), x.get("form", ""),
            x.get("title", ""), " / ".join(x.get("topics", [])),
                x.get("url", ""), x.get("note", ""), x.get("dup_count", 0),
            ])
    out = safe_write(CSV_FILE, buf.getvalue(), "utf-8-sig")
    if out:
        print("✓ CSV  →", out)
    else:
        print("! CSV  未写入（文件被占用）；看板内可导出最新 CSV")


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>官媒 & 融媒体社会民生新闻抓取看板</title>
<style>
:root{--bg:#f5f7fa;--card:#fff;--line:#e6e9f0;--text:#1f2430;--muted:#6b7280;--blue:#2f5fe0;--orange:#d9761a;--purple:#7a4fd0;--red:#c0392b;--green:#1f8a5b}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei","Segoe UI",sans-serif}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 60px}
header{margin-bottom:22px}
h1{font-size:26px;font-weight:700;margin:0 0 6px;letter-spacing:-.4px}
.sub{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.stat b{display:block;font-size:26px;line-height:1.2;color:var(--blue)}
.stat span{color:var(--muted);font-size:12px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-bottom:16px}
.toolbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center}
input[type=text],select{border:1px solid var(--line);border-radius:8px;padding:8px 11px;font-size:13px;color:var(--text);background:#fff;outline:none}
input[type=text]{flex:1;min-width:220px}
input[type=text]:focus,select:focus{border-color:var(--blue)}
button{border:1px solid var(--line);background:#fff;color:var(--text);border-radius:8px;padding:8px 13px;font-size:13px;cursor:pointer}
button:hover{border-color:var(--blue);color:var(--blue)}
button.primary{background:var(--blue);border-color:var(--blue);color:#fff}
button.primary:hover{opacity:.9;color:#fff}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:12px}
.chip{border:1px solid var(--line);background:#fafbfe;color:var(--muted);border-radius:999px;padding:4px 11px;font-size:12px;cursor:pointer}
.chip:hover,.chip.on{background:#eaf0ff;border-color:#b9ccff;color:var(--blue)}
.count{color:var(--muted);font-size:12px;margin:14px 2px 8px}
.list{display:grid;gap:10px}
.item{background:var(--card);border:1px solid var(--line);border-radius:11px;padding:14px 16px;display:grid;grid-template-columns:96px 1fr auto;gap:14px;align-items:start}
.item:hover{border-color:#c9d6f5;box-shadow:0 3px 14px rgba(47,95,224,.06)}
.meta{font-size:12px;color:var(--muted);line-height:1.8}
.badge{display:inline-block;border-radius:5px;padding:1px 7px;font-size:11px;font-weight:600;margin-bottom:4px}
.b-官媒{background:#e8f0ff;color:var(--blue)}.b-融媒{background:#fff2e6;color:var(--orange)}.b-榜单{background:#f1ebff;color:var(--purple)}
.t{font-size:15px;font-weight:600;color:var(--text);text-decoration:none;display:block;margin-bottom:5px}
.t:hover{color:var(--blue);text-decoration:underline}
.desc{font-size:12px;color:var(--muted);margin-top:4px}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.tag{background:#f2f4f8;color:#5b6472;border-radius:4px;padding:1px 7px;font-size:11px}
.form{border-radius:6px;padding:3px 9px;font-size:11px;white-space:nowrap;font-weight:600}
.f-深度调查,.f-舆论监督{background:#fdecec;color:var(--red)}
.f-政策服务{background:#e9f6ef;color:var(--green)}
.f-个案维权,.f-通报回应{background:#fff4e3;color:var(--orange)}
.f-评论{background:#eeeafd;color:var(--purple)}
.f-栏目更新{background:#eef2fb;color:var(--blue)}
.sec-title{font-size:17px;font-weight:700;margin:26px 0 12px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.channel{margin-bottom:12px}
.channel h4{font-size:13px;margin:0 0 7px;color:var(--blue)}
.links{display:flex;flex-wrap:wrap;gap:7px}
.links a{font-size:12px;color:var(--text);text-decoration:none;background:#fafbfe;border:1px solid var(--line);border-radius:7px;padding:5px 10px}
.links a:hover{color:var(--blue);border-color:#b9ccff}
.kwgroup{margin-bottom:10px}
.kwgroup b{font-size:12px;color:var(--muted);display:block;margin-bottom:5px;font-weight:600}
.kw{font-size:12px;background:#fafbfe;border:1px solid var(--line);border-radius:6px;padding:3px 8px;margin:0 5px 5px 0;display:inline-block;cursor:pointer}
.kw:hover{color:var(--blue);border-color:#b9ccff}
.note{font-size:12px;color:var(--muted);line-height:1.8}
.empty{text-align:center;color:var(--muted);padding:40px}
footer{margin-top:30px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:16px}
@media(max-width:820px){.stats{grid-template-columns:repeat(2,1fr)}.grid2{grid-template-columns:1fr}.item{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>官媒 &amp; 融媒体 · 社会民生新闻抓取看板</h1>
  <div class="sub">采集范围：人民日报 / 新华社 / 央视网 / 工人日报等官媒，澎湃 / 封面 / 上游 / 九派 / 极目 / 红星 / 新京报 / 中国新闻周刊等融媒体｜重点：民生痛点、深度调查、舆论监督｜更新于 <span id="updated"></span>｜数据窗口 <span id="windowLabel"></span></div>
</header>

<div class="stats" id="stats"></div>

<div class="panel">
  <div class="toolbar">
    <input type="text" id="q" placeholder="搜索标题、媒体、栏目、备注…">
    <select id="fType"><option value="">全部类型</option><option>官媒</option><option>融媒</option><option>榜单</option></select>
    <select id="fForm"><option value="">全部形态</option></select>
    <select id="fBucket"><option value="">全部批次</option><option>精选样本</option><option>实时抓取</option></select>
    <select id="fSort" autocomplete="off">
      <option value="time_desc">时间 ↓ 最新优先（全部条目）</option>
      <option value="time_asc">时间 ↑ 最早优先（全部条目）</option>
    </select>
    <button id="reset">重置</button>
    <button class="primary" id="export">导出 CSV</button>
  </div>
  <div class="chips" id="topicChips"></div>
</div>

<div class="count" id="count"></div>
<div class="list" id="list"></div>

<div class="sec-title">持续抓取入口</div>
<div class="panel"><div class="grid2" id="channels"></div></div>

<div class="sec-title">民生痛点关键词表 <span style="font-size:12px;font-weight:400;color:var(--muted)">（点击填入搜索框）</span></div>
<div class="panel" id="keywords"></div>

<div class="sec-title">抓取链路与用法</div>
<div class="panel note" id="howto"></div>

<footer>数据来自各媒体公开页面与公开检索，链接均指向原文。仅供选题浏览与合规低频采集使用；转载请遵循原媒体版权声明。<br>配套文件：news_data.json（单一数据源）、scripts/collect_news.py（采集与重建）、社会民生新闻样本.csv（表格数据）。</footer>
</div>

<script id="payload" type="application/json">__DATA_JSON__</script>
<script>
const D = JSON.parse(document.getElementById('payload').textContent);
const items = [];
(D.curated||[]).forEach(x=>items.push(Object.assign({},x,{bucket:'精选样本'})));
(D.crawled||[]).forEach(x=>items.push(Object.assign({},x,{bucket:'实时抓取'})));
items.forEach((x,i)=>x._idx=i);
document.getElementById('updated').textContent = D.updated || '';
document.getElementById('windowLabel').textContent = (D.window && D.window.label) ? D.window.label : '不限时间';
// 浏览器可能记住旧的排序值；非法值一律回退到「最新优先」
const sortEl=document.getElementById('fSort');
if(sortEl.value!=='time_desc'&&sortEl.value!=='time_asc') sortEl.value='time_desc';

const forms = [...new Set(items.map(x=>x.form).filter(Boolean))];
const fForm = document.getElementById('fForm');
forms.forEach(f=>{const o=document.createElement('option');o.textContent=f;fForm.appendChild(o);});

const groups = {};
Object.entries(D.keywords||{}).forEach(([g,ws])=>groups[g]=ws);
const allTopics = [...new Set(items.flatMap(x=>x.topics||[]))];
const chipBox = document.getElementById('topicChips');
allTopics.forEach(t=>{const b=document.createElement('span');b.className='chip';b.textContent=t;b.dataset.t=t;b.onclick=()=>{b.classList.toggle('on');render();};chipBox.appendChild(b);});

const kwBox = document.getElementById('keywords');
Object.entries(groups).forEach(([g,ws])=>{
  const d=document.createElement('div');d.className='kwgroup';
  d.innerHTML='<b>'+g+'</b>';
  ws.forEach(w=>{const s=document.createElement('span');s.className='kw';s.textContent=w;s.onclick=()=>{document.getElementById('q').value=w;render();window.scrollTo({top:0,behavior:'smooth'});};d.appendChild(s);});
  kwBox.appendChild(d);
});

const chBox=document.getElementById('channels');
(D.channels||[]).forEach(c=>{
  const d=document.createElement('div');d.className='channel';
  d.innerHTML='<h4>'+c.group+'</h4>';
  const box=document.createElement('div');box.className='links';
  (c.items||[]).forEach(i=>{const a=document.createElement('a');a.href=i.url;a.target='_blank';a.rel='noopener';a.textContent=i.name;box.appendChild(a);});
  d.appendChild(box);chBox.appendChild(d);
});

document.getElementById('howto').innerHTML = [
  '<b>① 内容链路</b>：官媒原发深度稿（央视网《救命血，为何一血难求？》）→ 平台号/融媒体同步（澎湃号·媒体）→ 多家融媒跟进通报与个案。找同类稿要同时盯「原发」和「转载」两端。',
  '<b>② 最高性价比入口</b>：中国记协「我的代表作」每周一更新，集中收录央媒与地方融媒的深度报道、舆论监督稿；澎湃站内搜索 <code>searchResult?id=关键词</code> 的每条结果都标注所属栏目，可反查栏目。',
  '<b>③ 官媒栏目</b>：人民日报「民生一线 / 枝叶关情 / 有所思 / 多棱镜」、央视「焦点访谈 / 每周质量报告 / 央视网评论」、工人日报权益版；多为「个体故事 + 专家释法 + 制度建议」结构。',
  '<b>④ 融媒调查</b>：澎湃「澎湃人物 / 绿政公署 / 一号专案 / 直击现场」、封面新闻「深镜调查组」、界面调查、大象新闻调查组、海报新闻「独立调查」，偏暗访与灰色产业链。',
  '<b>⑤ 四家融媒体解析规则</b>：封面新闻（首页 + 封面深镜专题页，<code>/news/&lt;加密ID&gt;</code>）、上游新闻（<code>/channel/29</code> 调查、<code>/14</code> 法治，URL 自带日期）、极目新闻（<code>/c1716</code> 世相、<code>/c1738</code> 消费、<code>/c1659</code> 健康）、九派新闻（官网为 React 内容后台无公开列表页，改抓其官方微博在新浪新闻的聚合页）。标题统一剥离「来源+时间+阅读量」噪音，跨源同题按标题前 18 字模糊去重。',
  '<b>⑥ 自动化</b>：运行 <code>python scripts/collect_news.py --out &lt;输出目录&gt;</code> 抓取并重建看板与 CSV；<code>--no-fetch</code> 只重建，<code>--days N</code> 控制单源抓取天数，<code>--window N</code> 控制全局保留窗口（默认 30 天，0 为不限，超期稿件会从数据文件物理剔除），<code>--init</code> 重置为模板数据。看板默认按时间倒序，可切换为时间正序；精选样本与实时抓取统一按日期排序，不再按批次分组，避免上周精选稿压住今天的新稿。',
  '<b>⑦ 同题去重</b>：跨媒体、跨批次按事件聚类，标题完全一致无条件合并；其余用字符 bigram 的 Jaccard / 重叠系数判断，相似度达阈值且日期相差不超过 1 天才算同一事件。每簇保留一条主稿（精选样本与官媒优先，同级取更新、信息更全的），其余媒体以「同题 N 家」标签和可点链接挂在主稿下，信息不丢失。<code>--dedup 0.48</code> 可调阈值，<code>--dedup 0</code> 关闭。',
  '<b>⑧ 报道形态</b>：「全部形态」下拉的所有选项由当前数据动态生成，每次重建看板都会同步刷新。精选样本保留人工标注；实时抓取稿按标题关键词自动归入评论 / 舆论监督 / 深度调查 / 通报回应 / 个案维权 / 政策服务，无命中的落到「栏目更新」兜底（多为暖闻、校园活动、健康科普等）。规则表在 <code>scripts/collect_news.py</code> 的 <code>FORM_RULES</code>，可直接增删关键词。',
  '<b>⑨ 社会民生聚焦</b>：默认只显示强相关的<b>事件报道</b>，过滤政务通知、政策发布、会议部署、机构宣传与表彰、时政要闻，以及暖闻、校园趣闻、健康科普等软内容。判定分三级：<code>FOCUS_HARD</code> 命中即排除；<code>FOCUS_SOFT</code> 命中但标题里没有任何具体人物（<code>FOCUS_PEOPLE</code>）才排除，避免误删人物报道；<code>FOCUS_FLUFF</code> 只对「栏目更新」兜底稿生效，剔除暖闻 / 趣闻 / 科普。被过滤稿仍完整保存在 news_data.json，用 <code>--focus 0</code> 可查看全量；四个词表可直接增删关键词。',
  '<b>⑩ 社会问题标签</b>：标签按<b>报道折射出的社会问题</b>命名，共 7 类（公共资源与供需 / 制度落地困境 / 灰色产业链 / 权益侵害与维权 / 公共安全与问责 / 弱势群体与保障 / 基层治理与公平），<b>不以题材命名</b>——「血」只是识别线索，不是标签。《救命血，为何一血难求？》因此同时归入「公共资源与供需」「制度落地困境」「灰色产业链」，与所有折射同类问题的报道一起被筛出。精选样本与实时抓取稿按同一套规则打标，原始细标签保留在 <code>tags_raw</code>。'
].join('<br><br>');

function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function filtered(){
  const q=document.getElementById('q').value.trim().toLowerCase();
  const ty=document.getElementById('fType').value;
  const fo=fForm.value, bu=document.getElementById('fBucket').value;
  const on=[...document.querySelectorAll('.chip.on')].map(x=>x.dataset.t);
  const arr = items.filter(x=>{
    if(ty&&x.type!==ty)return false;
    if(fo&&x.form!==fo)return false;
    if(bu&&x.bucket!==bu)return false;
    if(on.length&&!on.every(t=>(x.topics||[]).includes(t)))return false;
    if(q){const s=[x.title,x.media,x.column,x.note,(x.topics||[]).join(' ')].join(' ').toLowerCase();if(!s.includes(q))return false;}
    return true;
  });
  // 精选样本与实时抓取统一按日期排序，不再按批次分组（否则上周精选会压住今天的新稿）
  const sort=document.getElementById('fSort').value;
  if(sort==='time_asc') arr.sort((a,b)=>(a.date||'').localeCompare(b.date||'')||a._idx-b._idx);
  else arr.sort((a,b)=>(b.date||'').localeCompare(a.date||'')||a._idx-b._idx);
  return arr;
}
function render(){
  const arr=filtered();
  document.getElementById('count').textContent='共 '+arr.length+' 条（全部 '+items.length+' 条）';
  const box=document.getElementById('list');
  if(!arr.length){box.innerHTML='<div class="empty">没有匹配的稿件，换个关键词试试</div>';return;}
  box.innerHTML=arr.map(x=>{
    const tags=(x.topics||[]).map(t=>'<span class="tag">'+esc(t)+'</span>').join('');
    const dups=x.dup_urls||[];
    const dupTag=dups.length?'<span class="tag" style="background:#eaf0ff;color:var(--blue);border:1px solid #c9d8ff">同题 '+dups.length+' 家</span>':'';
    const dupLine=dups.length?'<div class="desc">同题报道：'+dups.map(d=>'<a href="'+esc(d.url)+'" target="_blank" rel="noopener">'+esc(d.media)+'</a>').join(' · ')+'</div>':'';
    return '<div class="item">'
      +'<div class="meta"><span class="badge b-'+esc(x.type)+'">'+esc(x.type)+'</span><br>'+esc(x.date)+'<br>'+esc(x.media)+'</div>'
      +'<div><a class="t" href="'+esc(x.url)+'" target="_blank" rel="noopener">'+esc(x.title)+'</a>'
      +'<div class="meta">'+esc(x.column||'')+(x.bucket==='实时抓取'?' · <span style="color:var(--blue)">实时抓取</span>':'')+'</div>'
      +(x.note?'<div class="desc">'+esc(x.note)+'</div>':'')
      +dupLine
      +'<div class="tags">'+tags+dupTag+'</div></div>'
      +'<div><span class="form f-'+esc(x.form)+'">'+esc(x.form)+'</span></div>'
      +'</div>';
  }).join('');
}
function stats(){
  const n=items.length, gm=items.filter(x=>x.type==='官媒').length, rm=items.filter(x=>x.type==='融媒').length;
  const deep=items.filter(x=>['深度调查','舆论监督'].includes(x.form)).length;
  const media=new Set(items.map(x=>x.media)).size;
  const d=[['稿件总数',n],['官媒 / 央媒',gm],['融媒体 / 市场媒体',rm],['深度调查 · 舆论监督',deep]];
  const ds=items.map(x=>x.date).filter(Boolean).sort();
  const span=ds.length?(ds[0]+' 至 '+ds[ds.length-1]):'—';
  const win=(D.window&&D.window.label)?D.window.label:'不限时间';
  const ded=(D.dedup&&D.dedup.merged)?' · 同题去重合并 '+D.dedup.merged+' 条 / '+D.dedup.clusters+' 个事件簇（阈值 '+D.dedup.threshold+'）':'';
  const fm=(D.forms)?' · 实时稿形态归类 '+D.forms.classified+' 条（'+D.forms.fallback+' 条兜底）':'';
  const fc=(D.focus&&D.focus.enabled)?' · 社会民生聚焦过滤 '+D.focus.excluded+' 条政务/宣传稿':'';
  document.getElementById('stats').innerHTML=d.map(x=>'<div class="stat"><b>'+x[1]+'</b><span>'+x[0]+'</span></div>').join('')
    +'<div class="stat" style="grid-column:1/-1"><span>覆盖 '+media+' 家媒体 / 机构 · 时间跨度 '+esc(span)+' · 数据窗口 '+esc(win)+' · 数据批次：精选样本 '+((D.curated||[]).length)+' 条 + 实时抓取 '+((D.crawled||[]).length)+' 条'+esc(ded)+esc(fm)+esc(fc)+'</span></div>';
}
document.getElementById('export').onclick=()=>{
  const arr=filtered();
  const cols=[['bucket','数据批次'],['date','日期'],['media','媒体'],['type','类型'],['column','栏目'],['form','报道形态'],['title','标题'],['topics','主题标签'],['url','链接'],['note','备注']];
  const lines=[cols.map(c=>'"'+c[1]+'"').join(',')];
  arr.forEach(x=>lines.push(cols.map(c=>{let v=x[c[0]];if(Array.isArray(v))v=v.join(' / ');return '"'+String(v==null?'':v).replace(/"/g,'""')+'"';}).join(',')));
  const blob=new Blob(['\ufeff'+lines.join('\r\n')],{type:'text/csv;charset=utf-8'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='社会民生新闻筛选结果.csv';a.click();
};
document.getElementById('reset').onclick=()=>{document.getElementById('q').value='';document.getElementById('fType').value='';fForm.value='';document.getElementById('fBucket').value='';document.getElementById('fSort').value='time_desc';document.querySelectorAll('.chip.on').forEach(x=>x.classList.remove('on'));render();};
['q','fType','fForm','fBucket','fSort'].forEach(id=>{const el=document.getElementById(id);el.addEventListener(id==='q'?'input':'change',render);});
stats();render();
</script>
</body>
</html>
"""


def build_html(data):
    # 看板 payload 只放聚焦过滤后的条目；数据文件本身仍保留全量，--focus 0 可随时恢复
    focused = all_items(data)
    view = dict(data)
    view["curated"] = [x for x in focused if x.get("bucket") == "精选样本"]
    view["crawled"] = [x for x in focused if x.get("bucket") == "实时抓取"]
    payload = json.dumps(view, ensure_ascii=False).replace("<", "\\u003c")
    out = HTML_TEMPLATE.replace("__DATA_JSON__", payload)
    path = safe_write(HTML_FILE, out, "utf-8")
    if path:
        print("✓ HTML →", path)
    else:
        print("! HTML 未写入（文件被占用）")


KEYWORDS = {}


def main():
    global KEYWORDS, WINDOW_DAYS, DEDUP_THRESHOLD, FOCUS_ENABLED
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="跳过采集，只重建看板与 CSV")
    ap.add_argument("--days", type=int, default=7, help="单源抓取时保留最近 N 天")
    ap.add_argument("--window", type=int, default=30, help="全局只保留近 N 天稿件，0 表示不限（默认 30）")
    ap.add_argument("--dedup", type=float, default=0.48,
                    help="同题去重相似度阈值，0 表示关闭（默认 0.48）")
    ap.add_argument("--focus", type=int, default=None,
                    help="1 只保留社会民生强相关事件报道，0 显示全部（默认 1）")
    ap.add_argument("--max-per-source", type=int, default=20, help="每个源最多保留条数")
    ap.add_argument("--out", default=os.getcwd(), help="输出目录，默认当前工作目录")
    ap.add_argument("--init", action="store_true", help="用模板重新初始化数据文件（覆盖已有数据）")
    args = ap.parse_args()
    WINDOW_DAYS = args.window
    DEDUP_THRESHOLD = args.dedup
    FOCUS_ENABLED = True if args.focus is None else bool(args.focus)

    configure_output(args.out)
    if args.init or not os.path.exists(DATA_FILE):
        if not os.path.exists(TEMPLATE_FILE):
            raise SystemExit("找不到模板文件：" + TEMPLATE_FILE)
        shutil.copyfile(TEMPLATE_FILE, DATA_FILE)
        print("✓ 初始化数据文件 →", DATA_FILE)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    KEYWORDS = data.get("keywords", {})

    # 全局时间窗：精选样本与实时抓取一视同仁，物理剔除超期稿件并写回数据文件
    cutoff = window_cutoff()
    before = len(data.get("curated", [])) + len(data.get("crawled", []))
    if cutoff:
        data["curated"] = [x for x in data.get("curated", []) if within_window(x, cutoff)]
        data["crawled"] = [x for x in data.get("crawled", []) if within_window(x, cutoff)]
    dropped = before - len(data["curated"]) - len(data["crawled"])
    data["window"] = {
        "days": args.window,
        "cutoff": cutoff,
        "label": "近 {} 天（{} 起）".format(args.window, cutoff) if cutoff else "不限时间",
    }

    if not args.no_fetch:
        data["crawled"] = crawl(data, args.days, args.max_per_source)
        data["updated"] = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    # 形态归类：每次运行都重算实时抓取稿，保证筛选分类随数据同步刷新；
    # 精选样本保留人工标注，不被规则覆盖
    for x in data["crawled"]:
        x["form"] = classify_form(x["title"])

    # 同题去重：用不过滤聚焦的全量数据聚类，保证被排除稿仍留在数据文件里可随时恢复
    removed_dup = 0
    if DEDUP_THRESHOLD > 0:
        merged, removed_dup = dedup_items(all_items(data, apply_focus=False), DEDUP_THRESHOLD)
        buckets = [(x.pop("bucket", ""), dict(x)) for x in merged]
        data["curated"] = [x for b, x in buckets if b == "精选样本"]
        data["crawled"] = [x for b, x in buckets if b == "实时抓取"]
    all_kept = data["curated"] + data["crawled"]
    classified = sum(1 for x in data["crawled"] if x.get("form") != "栏目更新")
    data["forms"] = {"classified": classified, "fallback": len(data["crawled"]) - classified}
    excluded = sum(1 for x in all_items(data, apply_focus=False) if not is_social_focus(x))
    data["focus"] = {"enabled": bool(FOCUS_ENABLED), "excluded": excluded}
    data["dedup"] = {
        "threshold": DEDUP_THRESHOLD,
        "removed": removed_dup,
        "merged": sum(x.get("dup_count", 0) for x in all_kept),
        "clusters": sum(1 for x in all_kept if x.get("dup_count")),
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    if dropped > 0:
        print("✓ 时间窗清理：剔除 {} 条早于 {} 的稿件".format(dropped, cutoff))
    if removed_dup > 0:
        print("✓ 同题去重：合并 {} 条重复报道（阈值 {}）".format(removed_dup, DEDUP_THRESHOLD))
    print("✓ 形态归类：{} 条实时稿已细分，{} 条保留「栏目更新」兜底".format(
        data["forms"]["classified"], data["forms"]["fallback"]))
    if FOCUS_ENABLED:
        print("✓ 社会民生聚焦：过滤 {} 条政务通知 / 政策发布 / 机构宣传稿（--focus 0 可查看全量）".format(
            data["focus"]["excluded"]))
    print("✓ 数据   →", DATA_FILE, "（精选 {} + 实时 {} 条，窗口 {}）".format(
        len(data["curated"]), len(data["crawled"]), data["window"]["label"]))
    build_csv(data)
    build_html(data)


if __name__ == "__main__":
    main()
