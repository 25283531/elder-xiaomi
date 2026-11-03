"""口语时间解析回归（parse_when）：老人怎么说时间，服务端就得怎么认。

为什么单独一条冒烟
------------------
「我说了时间，它一直说没听懂」是本项目最典型的投诉形态，而根因往往在
``parse_when()`` 的一个正则上 —— 它失败时只返回一句人话（"没听出具体时间"），
对老人是"没听懂"，对排查是零信息。这类缺陷用真机验太贵（要说 40 句话），
用生产日志又查不到（失败原因以前不落日志），只能靠穷举断言守住。

本脚本 2026-11-03 首次跑就抓到一个真 bug：
``_WEEKDAY_WORDS`` 里有「日」→7，但三处正则的字符类写的是
``[一二三四五六天末]`` —— **漏了「日」**。于是老人说「周日早上九点」时
星期分支匹配不到，落到「没给日期」兜底，排成**明天**早上九点。
老人白等到周日才发现提醒早就响过了。
（「周天」能过是因为「天」在类里 —— 这种"两个同义词一个能过一个不能"
 最难靠人工发现。）

判据口径
--------
· now 固定成 2026-11-03 14:30（周二），不取当前时刻 —— 否则同一个断言
  在上午跑和下午跑结果不同（memory 教训：按钟点变红的用例比没有用例更坏）。
· 正例同时断言**时刻**和**日期偏移**：只断言时刻会漏掉"周日排成明天"
  这类日期错，而日期错对老人来说就是"提醒没响"。
· 反例（歧义说法）必须 ok=False：解析器宁可反问，也不能猜一个时间建进去
  —— 假成功比失败贵得多。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

from app.voice.tools import parse_when  # noqa: E402

NOW = datetime(2026, 11, 3, 14, 30)   # 周二 14:30
WEEKDAY_CN = "一二三四五六日"

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((ok, name, detail))


# (原话, 期望 HH:MM, 期望日期偏移天数；None = 不检查)
CASES: list[tuple[str, str | None, int | None]] = [
    # —— 最标准的说法 ——
    ("明天早上八点", "08:00", 1),
    ("明天早上8点", "08:00", 1),
    ("明天上午十点", "10:00", 1),
    ("明天下午三点", "15:00", 1),
    ("明天晚上八点", "20:00", 1),
    ("明天中午十二点", "12:00", 1),
    ("今天下午五点", "17:00", 0),
    ("后天早上七点半", "07:30", 2),
    ("大后天下午两点", "14:00", 3),
    # —— 半小时 / 一刻 ——
    ("明天早上八点半", "08:30", 1),
    ("明天早上八点一刻", "08:15", 1),
    ("明天下午三点三刻", "15:45", 1),
    ("明天早上八点十五", "08:15", 1),
    ("明天早上八点三十分", "08:30", 1),
    # —— 两位数钟点 ——
    ("明天早上十一点", "11:00", 1),
    ("明天下午一点", "13:00", 1),
    ("明天晚上十点", "22:00", 1),
    ("明天晚上十一点半", "23:30", 1),
    ("明天中午十二点半", "12:30", 1),
    # —— 不带日期（今天过了就排明天）——
    ("八点", "08:00", 1),          # 14:30 已过 8 点 ⇒ 明天
    ("下午四点", "16:00", 0),      # 还没到 ⇒ 今天
    ("晚上九点", "21:00", 0),
    ("早上六点", "06:00", 1),      # 已过 ⇒ 明天
    # —— 星期（★「日」这一档就是 2026-11-03 修掉的那个 bug）——
    ("周日早上九点", "09:00", 5),
    ("礼拜日早上九点", "09:00", 5),
    ("周天早上九点", "09:00", 5),
    ("周三早上八点", "08:00", 1),   # 今天周二，周三已过点 ⇒ 下周三
    ("星期五下午两点", "14:00", 3),
    # —— 每天 / 每周 ——
    ("每天八点", "08:00", None),
    ("每天早上七点半", "07:30", None),
    ("每天中午十二点", "12:00", None),
    ("每周一三五早上八点", "08:00", None),
    ("每周日早上八点", "08:00", None),
    # —— 相对时间 ——
    ("两分钟后", None, None),
    ("半小时后", None, None),
    ("一个小时后", None, None),
    ("一刻钟后", None, None),
    # —— 饭后（约定值见 _MEAL_AFTER_MIN）——
    ("早饭后", "07:30", 1),
    ("晚饭后", "18:30", 0),
    # —— 口语 / 口音 ——
    ("明儿早上八点", "08:00", 1),
    ("今儿下午三点", "15:00", 0),
    ("明天早起八点", "08:00", 1),   # "早起"不是时段词，靠 6-11 兜底
]

# 这些**必须**反问（ok=False）。建成任何一条都是"猜了个时间"，
# 对老人来说比没建更糟 —— 到点响一条不是他要的提醒。
MUST_FAIL = [
    "明天",          # 没钟点
    "待会儿",        # 相对但没量
    "一会儿",
    "现在",
    "明天早上",      # 有时段无钟点
    "饭后",          # 没说是哪顿饭
    "三点",          # 1-5 点歧义（下午三点 vs 凌晨三点）
    "明天三点",
    "昨天下午三点",  # 过去时间
    "每周",          # 没说星期几
    "每月",          # 没说几号
]

for text, want_hm, want_delta in CASES:
    r = parse_when(text, now=NOW)
    if not r.ok:
        check(False, f"「{text}」应能解析", f"却被反问：{r.reason}")
        continue
    got_hm = r.times_of_day[0] if r.times_of_day else None
    if want_hm is not None and got_hm != want_hm:
        check(False, f"「{text}」时刻", f"{got_hm} != {want_hm}（{r.speak}）")
        continue
    if want_delta is not None and r.once_date:
        delta = (datetime.fromisoformat(r.once_date).date() - NOW.date()).days
        if delta != want_delta:
            check(False, f"「{text}」日期", f"+{delta} 天 != +{want_delta} 天（{r.speak}）")
            continue
    check(True, f"「{text}」→ {got_hm or '相对'} {r.speak}")

for text in MUST_FAIL:
    r = parse_when(text, now=NOW)
    if r.ok:
        check(False, f"「{text}」应反问", f"却建成了 {r.times_of_day} {r.once_date}（{r.speak}）")
    else:
        check(True, f"「{text}」正确反问")

passed = sum(1 for ok, _, _ in results if ok)
failed = len(results) - passed
for ok, name, detail in results:
    if not ok:
        print(f"  [FAIL] {name}  {detail}")
print("=" * 64)
print(f"通过 {passed} 项，失败 {failed} 项")
if failed:
    print("结论：**红**")
    raise SystemExit(1)
print("结论：**绿**")
