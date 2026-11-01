"""新闻播报（可选子系统）。

对外只有两个入口：

    get_headlines(limit)     -> (标题列表, 失败原因)
    speak_lines(items)       -> 可直接念的几行

刻意**不做**的事（写在这里是为了防止后来者"顺手加上"）：

* 不抓正文、不做摘要、不排序 —— 见 service.py 的文件头；
* **不做内容过滤** —— 2026-11-01 用户明确决定「不过滤新闻内容」。
  人民网社会频道会带出「接受纪律审查和监察调查」这类纪检通报，念给老人
  不合适，这是**已知并接受**的取舍：谁来定过滤标准、漏掉的那条算谁的，
  比不过滤更麻烦。要加先拿到产品决定，不要自己拍板；
* 不建数据库表、不落盘 —— 新闻过期即无用；
* 不提供 HTTP 接口 —— 目前唯一的消费者是语音工具 ``get_headlines``。
  真要做家属端"新闻偏好设置"，那是另一个决定，需要先有产品需求。
"""
from __future__ import annotations

from .client import Headline, NewsError, fetch, parse
from .config import DEFAULT_FEED_URL, NewsConfig, load
from .service import get_headlines, reset_cache, speak_lines

__all__ = [
    "Headline",
    "NewsError",
    "fetch",
    "parse",
    "NewsConfig",
    "DEFAULT_FEED_URL",
    "load",
    "get_headlines",
    "speak_lines",
    "reset_cache",
]
