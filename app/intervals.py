"""半开区间 [start, end) 运算：含起点、不含终点。全系统统一使用该语义。"""

from __future__ import annotations


def union_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并重叠或相接的半开区间，返回排序后的不重叠列表。"""
    ordered = sorted((s, e) for s, e in ranges if s < e)
    merged: list[list[int]] = []
    for s, e in ordered:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def subtract_ranges(
    base: list[tuple[int, int]], cuts: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """base 减去 cuts（均为半开区间），返回排序后的不重叠区间列表。"""
    remaining: list[tuple[int, int]] = []
    for bs, be in base:
        pieces = [(bs, be)]
        for cs, ce in cuts:
            nxt: list[tuple[int, int]] = []
            for s, e in pieces:
                if ce <= s or cs >= e:  # 半开区间不相交
                    nxt.append((s, e))
                    continue
                if s < cs:
                    nxt.append((s, cs))
                if ce < e:
                    nxt.append((ce, e))
            pieces = nxt
            if not pieces:
                break
        remaining.extend(pieces)
    return union_ranges(remaining)


def merge_with_ids(ranges: list[tuple[int, int, int]]) -> list[tuple[int, int, list[int]]]:
    """扫描线合并带标记号的区间。

    输入: [(start, end, marker_id), ...]
    输出: [(start, end, [marker_id, ...]), ...]，重叠处列出全部标记号。
    半开语义：某区间终点与另一区间起点相同不算重叠（终点事件先处理）。
    """
    events: list[tuple[int, int, int]] = []
    for s, e, mid in ranges:
        if s >= e:
            continue
        events.append((s, 1, mid))  # 1 = 开始
        events.append((e, 0, mid))  # 0 = 结束，排序时先于同坐标的开始
    events.sort(key=lambda ev: (ev[0], ev[1]))

    out: list[tuple[int, int, list[int]]] = []
    active: set[int] = set()
    prev: int | None = None
    for pos, kind, mid in events:
        if prev is not None and pos > prev and active:
            out.append((prev, pos, sorted(active)))
        if kind == 1:
            active.add(mid)
        else:
            active.discard(mid)
        prev = pos
    # 相邻且标记号集合相同的段合并为一段，便于阅读
    coalesced: list[list] = []
    for s, e, ids in out:
        if coalesced and coalesced[-1][1] == s and coalesced[-1][2] == ids:
            coalesced[-1][1] = e
        else:
            coalesced.append([s, e, ids])
    return [(s, e, ids) for s, e, ids in coalesced]
