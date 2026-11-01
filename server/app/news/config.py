"""新闻播报（可选子系统）的配置。

为什么单独一个包、自己的环境变量
--------------------------------
与 ``app/weather/config.py`` 同一个理由：``app/config.py`` 是「服务端能不能
起来」级别的配置（数据库、时区、限速），而新闻是**可选功能** —— 没配、抓不到、
源挂了，服务都该正常启动，只是回答「今天没取到新闻」。把可选能力混进主配置，
之后任何人都可能误以为缺配置是启动故障。

三条环境变量
------------
``ELDER_NEWS_FEED_URL``
    单个 RSS/Atom 地址。默认值指向人民网「要闻」RSS —— 国内可直连、长期稳定、
    无需密钥。**只取标题，不做任何加工**（见 service.py 的说明）。

``ELDER_NEWS_ITEMS``
    一次最多播几条，默认 3。老人听不了 10 条，念到第 4 条就开始走神。

``ELDER_NEWS_TIMEOUT_MS``
    抓取硬超时，默认 4000ms。必须比语音链路的等待预算小：老人问"今天有啥新闻"，
    多等 4 秒已经很勉强，再久他会以为设备坏了。

**刻意不提供"多个源合并"**：多源意味着要处理重复、要排序、要决定谁优先，
而"今天头条"对老人来说不是聚合产品需求。一个源、三条标题，够了。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

#: 默认源：人民网「社会」频道 RSS。选它的四条理由 ——
#:   1. 国内可直连，不像境外源那样经常超时；
#:   2. 无需密钥，部署方什么都不用配就能用；
#:   3. RSS 结构十几年没变过，解析器不会因为改版而失效；
#:   4. **内容以民生为主**（救灾、健康提示、考试天气），适合念给老人听。
#:      「要闻」频道（politics.xml）实测头条全是「学习卡」「镜观·足迹」这类
#:      栏目名，念出来老人听不懂；换成社会频道后是「两部门预拨4500万元支持
#:      应急救灾」这种能直接听懂的句子。
#: 想换任何 RSS/Atom 源（包括自建），设 ``ELDER_NEWS_FEED_URL`` 即可。
DEFAULT_FEED_URL = "http://www.people.com.cn/rss/society.xml"

#: 一次播几条。3 条 ≈ 20 秒语音，是老人能听完的长度。
DEFAULT_ITEMS = 3

#: 抓取硬超时（毫秒）。
DEFAULT_TIMEOUT_MS = 4000


@dataclass(frozen=True)
class NewsConfig:
    feed_url: str
    items: int
    timeout_ms: int

    @property
    def configured(self) -> bool:
        """有 feed 就算配置好了。空字符串 = 家属/运维明确关掉了新闻。"""
        return bool(self.feed_url.strip())


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        # 配错了不报错、不阻止启动：回落到默认值，让功能继续可用。
        # 这类"配置写错"在容器里很常见，把服务卡住是最差的选择。
        return default


def load() -> NewsConfig:
    return NewsConfig(
        feed_url=(os.environ.get("ELDER_NEWS_FEED_URL") or DEFAULT_FEED_URL).strip(),
        items=max(1, min(10, _env_int("ELDER_NEWS_ITEMS", DEFAULT_ITEMS))),
        timeout_ms=max(500, min(20000,
                                _env_int("ELDER_NEWS_TIMEOUT_MS",
                                         DEFAULT_TIMEOUT_MS))),
    )
