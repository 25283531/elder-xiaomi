"""新闻 RSS 抓取与解析（标准库 urllib + ElementTree）。

为什么用 RSS 而不是新闻 API
---------------------------
1. **不要密钥**：部署方什么都不配就能用。天气那套（和风）必须配私钥，
   是因为它按调用量计费；新闻没有这个必要，多一个密钥就多一个会过期的东西。
2. **结构稳定**：RSS 2.0 的 ``<item><title>`` 二十年没变过。而各家新闻站的
   HTML 改版一次，抓取脚本就废一次 —— 那种"静默失效"最难发现。

三个必须显式处理的点
--------------------
1. **gzip**：新闻站普遍返回 ``Content-Encoding: gzip``，而 urllib 不会自动解压。
   忘了这步的现象是"HTTP 200 + 解析出 0 条"，日志里一切正常。
2. **编码**：国内 RSS 大量是 GB2312/GBK，且 ``Content-Type`` 里经常写错。
   所以先按响应头给的字符集解，失败就按 XML 声明解，再失败按 UTF-8 兜底 ——
   顺序反过来会把正常的中文解成乱码。
3. **硬超时**：老人问一句"今天有啥新闻"，多等 4 秒已经很勉强。DNS 卡住、
   对端不响应都必须在这个预算内返回，绝不无限阻塞语音链路。

⚠️ 只取标题，不取正文、不取链接、不做摘要。理由见 service.py 的文件头。
"""
from __future__ import annotations

import gzip
import io
import logging
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .config import NewsConfig, load as load_config

log = logging.getLogger("elder.news.client")

#: 标题里的 HTML 实体/标签/多余空白。RSS 的 title 里真的会带 ``<b>`` 和 ``&nbsp;``。
_TAG_RE = re.compile(r"<[^>]{1,200}>")
_WS_RE = re.compile(r"\s+")
#: 常见命名实体（不引 html.unescape 的完整表，只处理新闻标题里真正出现的几个）
_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&quot;": '"', "&apos;": "'", "&#39;": "'", "&ldquo;": "“",
    "&rdquo;": "”", "&mdash;": "—", "&hellip;": "…",
}

#: 单条标题的长度上限。新闻标题正常 15~30 字，超过这个数多半是把摘要塞进 title 了，
#: 而一条 80 字的"标题"念出来老人根本抓不住重点。
MAX_TITLE_CHARS = 60


class NewsError(Exception):
    """抓取/解析失败。``reason`` 是给日志看的一句人话。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Headline:
    title: str
    source: str = ""


def _clean_title(raw: str) -> str:
    """把 RSS 的 title 弄成一句能直接念出来的中文。

    只做**去噪声**（标签、实体、空白、截断），不改写、不摘要、不排序 ——
    一旦这里开始"加工"，播报出来的就不再是媒体原话，而是我们的转述。
    """
    text = _TAG_RE.sub("", raw or "")
    for k, v in _ENTITIES.items():
        text = text.replace(k, v)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip() + "…"
    return text


def _decode(body: bytes, charset: str | None) -> str:
    """按「响应头 → XML 声明 → UTF-8」的顺序解码。

    顺序不能反：国内 RSS 大量是 GB2312，而响应头里经常写着 UTF-8。
    先信响应头会把中文解成乱码，而乱码在解析阶段**不报错**，
    只是标题变成一串问号 —— 那是最难查的一类故障。
    """
    candidates = [c for c in (charset, "utf-8", "gb18030") if c]
    for cs in candidates:
        try:
            return body.decode(cs, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    # 全都严格解码失败：用 gb18030 宽松解（它对 GB2312/GBK/GB18030 全兼容）
    return body.decode("gb18030", errors="replace")


def fetch(cfg: NewsConfig | None = None, limit: int = 0) -> list[Headline]:
    """抓一次 feed 并解析出标题。失败抛 :class:`NewsError`。

    ``limit`` 为 0 时用配置里的条数。
    """
    cfg = cfg or load_config()
    if not cfg.configured:
        raise NewsError("新闻源未配置（ELDER_NEWS_FEED_URL 为空）")

    req = urllib.request.Request(cfg.feed_url, headers={
        # 有些站点对无 UA 的请求直接 403。UA 写成普通客户端，不伪装浏览器 ——
        # 伪装成浏览器反而更容易被当成爬虫。
        "User-Agent": "voice-avatar-box/1.0 (+news)",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
        "Accept-Encoding": "gzip",
    })
    timeout = cfg.timeout_ms / 1000.0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            charset = resp.headers.get_content_charset()
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
    except urllib.error.HTTPError as exc:
        raise NewsError(f"新闻源返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise NewsError(f"新闻源连不上：{exc.reason}") from exc
    except TimeoutError as exc:
        raise NewsError(f"新闻源超时（{cfg.timeout_ms}ms）") from exc

    if encoding == "gzip":
        try:
            body = gzip.decompress(body)
        except OSError as exc:
            raise NewsError("新闻源声明了 gzip 但解压失败") from exc

    text = _decode(body, charset)
    return parse(text, limit=limit or cfg.items)


def parse(text: str, limit: int = 3) -> list[Headline]:
    """解析 RSS 2.0 或 Atom 文本，返回前 ``limit`` 条标题。

    两种格式都要支持：人民网用 RSS 2.0（``rss/channel/item``），
    而不少站点（含自建博客）用 Atom（``feed/entry``）。只认一种的话，
    用户把 ``ELDER_NEWS_FEED_URL`` 换成 Atom 源就会"0 条"，且不报错。
    """
    # 拒绝带 DTD 的文档：RSS 不需要 DTD，而 DTD 是 XXE / billion-laughs 的入口。
    # ElementTree 本身不解析外部实体，但 entity expansion 仍可能吃满内存。
    if "<!DOCTYPE" in text[:2048].upper():
        raise NewsError("新闻源含 DTD，拒绝解析（RSS 不需要 DTD）")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise NewsError(f"新闻源不是合法 XML：{exc}") from exc

    # 命名空间无关地取标签名：Atom 是 {http://www.w3.org/2005/Atom}entry
    def _tag(el: ET.Element) -> str:
        return el.tag.rsplit("}", 1)[-1].lower()

    items: list[ET.Element] = []
    for el in root.iter():
        if _tag(el) in ("item", "entry"):
            items.append(el)
        if len(items) >= max(limit * 3, limit):   # 多取一些，清洗后可能不够
            break

    out: list[Headline] = []
    for it in items:
        title = ""
        for child in it:
            if _tag(child) == "title":
                title = "".join(child.itertext())
                break
        title = _clean_title(title)
        if not title:
            continue
        out.append(Headline(title=title))
        if len(out) >= limit:
            break
    if not out:
        raise NewsError("新闻源解析成功但一条标题都没有（可能改版或返回空 feed）")
    return out
