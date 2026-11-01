"""新闻播报的服务层：带进程内 TTL 缓存，失败不抛给语音链路。

三条产品约束（决定了这里的实现形状）
------------------------------------
1. **只念标题，不念正文、不做摘要、不加评论。**
   老人要的是"今天有什么大事"，不是一篇评论。而一旦我们开始改写，
   播出去的就是我们的转述而不是媒体的原话 —— 那既不可追溯，也没有必要。
2. **失败必须优雅**：抓不到就说"今天没取到新闻"，绝不能让整轮对话失败。
   语音链路的兜底原则是"记忆/工具都是增益"，新闻也一样。
3. **同一个 feed 在 10 分钟内只抓一次**：老人连着问三遍"再说说新闻"，
   不该打三次外网 —— 那既慢又容易被对方限流。缓存放在进程内存里，
   不建 DB 表：新闻是**过期即无用**的数据，落盘只会多一份要清理的东西。

为什么不用 ``weather_cache`` 那张表
----------------------------------
那张表的键是"城市 + 日期"，语义是"某地某天的天气"，复用它会让两类数据
共用一张表却各有各的过期规则，查问题时必须先分清是哪一类。
进程内缓存 20 行代码就够，且进程重启即失效——对"今天头条"完全合理。
"""
from __future__ import annotations

import logging
import threading
import time

from .client import Headline, NewsError, fetch
from .config import load as load_config

log = logging.getLogger("elder.news")

#: 缓存有效期。10 分钟是"新闻不会比这更新得更快"与"老人连着问不会重复抓"
#: 之间的折中。改小只会增加外网请求次数，不会让内容更新。
CACHE_TTL_SEC = 600

_lock = threading.Lock()
_cache: dict[str, tuple[float, list[Headline]]] = {}


def _cache_key(feed_url: str, limit: int) -> str:
    return f"{feed_url}|{limit}"


def get_headlines(limit: int = 0) -> tuple[list[Headline], str]:
    """取当天头条。返回 ``(标题列表, 失败原因)``；成功时原因为空串。

    **不抛异常**：语音链路的调用方只需要"有没有内容"，不需要区分是超时还是
    解析失败 —— 那两种情况下老人听到的都是同一句"今天没取到新闻"。
    具体原因进日志。
    """
    cfg = load_config()
    want = limit or cfg.items
    key = _cache_key(cfg.feed_url, want)

    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < CACHE_TTL_SEC:
            return hit[1], ""

    try:
        items = fetch(cfg, limit=want)
    except NewsError as exc:
        log.warning("新闻抓取失败（%s）：%s", cfg.feed_url, exc.reason)
        return [], exc.reason
    except Exception as exc:  # noqa: BLE001  新闻永远不该让对话失败
        log.warning("新闻抓取异常：%s: %s", type(exc).__name__, exc)
        return [], f"{type(exc).__name__}"

    with _lock:
        _cache[key] = (now, items)
    return items, ""


def speak_lines(items: list[Headline]) -> list[str]:
    """把标题渲染成"念出来不别扭"的几行。

    为什么不在每条前面加序号：TTS 念"第一条…第二条…"会把三条新闻切得很碎，
    而老人只需要知道"今天大概有这几件事"。序号留给模型自己决定要不要加。
    """
    return [h.title for h in items if h.title]


def reset_cache() -> None:
    """清缓存。只给测试用 —— 生产没有任何调用点，别把它当运维接口。"""
    with _lock:
        _cache.clear()
