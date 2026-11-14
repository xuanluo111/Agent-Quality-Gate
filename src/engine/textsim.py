"""字符 shingle 与 Jaccard —— 同一个口径，两处消费。

中文没有词边界，字符 shingle 比分词稳：不依赖词典，也不会因为分词器升级而漂移。
两处消费方必须共用一套实现，否则下面这两个数字不能并排放在同一段话里：

- `suite_audit` 比两条**题面**有多像（C04 重复率）；
- `stability` 比同一用例多次尝试的**回复**有多像（K08 变序到底分化了多少）。

这也是本模块单独存在的理由：同一段 Jaccard 写两遍，阈值调了却只改一处，
是这类指标最常见也最难在评审里看见的漂移。
"""
from __future__ import annotations

import re
from typing import Iterable, Set

SHINGLE_SIZE = 3

# 去标点与空白再比：题面与回复的差异常常只是标点/换行，留着会让 Jaccard 虚低。
_NON_WORD = re.compile(r"[^\w]+")


def normalize(text: object) -> str:
    """转小写并去掉所有非词字符。"""
    return _NON_WORD.sub("", str(text or "").lower())


def shingles(text: object, size: int = SHINGLE_SIZE) -> Set[str]:
    """归一化后的字符 shingle 集合。

    长度不足 `size` 的文本返回**空集**（沿用 `suite_audit` 的既有口径）：太短的东西
    谈不上相似度，空集会让调用方显式跳过，而不是给出一个 0 或 1 的假结论。
    """
    normalized = normalize(text)
    return {
        normalized[index : index + size]
        for index in range(max(0, len(normalized) - size + 1))
    }


def jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    """两个集合的 Jaccard。两边都空记 1.0 —— “都没内容”确实一致，不是不确定。"""
    first, second = set(left), set(right)
    if not first and not second:
        return 1.0
    return len(first & second) / len(first | second)


__all__ = ["SHINGLE_SIZE", "jaccard", "normalize", "shingles"]
