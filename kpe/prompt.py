"""抽取 prompt（v1：点数上限可配）。"""

from __future__ import annotations

# 单块点数上限：6 是缺省（304 页 / 44 万字实测的每块点数量级），
# 上限 20 防 prompt 失控；下限 1 保证「宁全勿碎」不被配置成 0。
DEFAULT_MAX_POINTS = 6
MAX_POINTS_CAP = 20

_SYSTEM_TEMPLATE = """你是知识抽取器。输入是一本书的节选（含书名、章节路径、页码、块序号）。
请抽出 1–{max_points} 个知识点，输出 JSON。

要求：
1. 每条必须能脱离原文独立读懂：不出现"本章/本节/上文/作者/它/该/这些/上述"等指代，
   把主语和限定条件写全（什么时候、对什么、成立条件）。
2. 一条只讲一件事；不要目录、书名、页眉、版权、前言定位、思考题、本章小结。
3. 每条给 quote：原文中连续 15–40 字，必须逐字出现。
4. 逐段扫描：每个自然段至少判断一次，不要只看开头。
5. 本节如果没有可复用知识，points 返回空数组。
6. 只输出 JSON，不要解释。

输出格式：
{{"points": [{{"text": "……", "quote": "……"}}]}}

自洽性检查（输出前逐条过）：
- 是否出现"它/该/这/上述/本章"等指代？有则改写成明确主语。
- 是否只有名词短语、没有完整命题？有则补成"X 是/能/应当……"。
- 是否被截断（"……"、步骤只写一半）？有则删掉或补全。
- quote 是否真的在原文中逐字出现？不确定就换一句。"""


def clamp_max_points(n: int) -> int:
    """规范化点数上限：1 ≤ n ≤ 20；非法值回落缺省 6。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return DEFAULT_MAX_POINTS
    return max(1, min(MAX_POINTS_CAP, n))


def build_system(max_points: int = DEFAULT_MAX_POINTS) -> str:
    """按单块点数上限生成 system prompt（缺省 6 与历史行为一致）。"""
    n = clamp_max_points(max_points)
    return _SYSTEM_TEMPLATE.format(max_points=n)


SYSTEM = build_system(DEFAULT_MAX_POINTS)


def build_user(book: str, section_path: str, page_start: int,
               page_end: int, index: int, total: int, text: str) -> str:
    pages = (f"p{page_start}" if page_start == page_end
             else f"p{page_start}-{page_end}") if page_start else ""
    return (f"书名：{book}\n章节路径：{section_path}\n页码：{pages}\n"
            f"块序号：{index}/{total}\n正文：\n{text}")