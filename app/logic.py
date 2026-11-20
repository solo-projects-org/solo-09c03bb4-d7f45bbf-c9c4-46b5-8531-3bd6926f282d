"""领域逻辑：剪辑版导入、禁播标记登记/撤回、局部解除、各版遮盖计算。

区间一律为半开区间 [start, end)：含起点、不含终点。
所有跨行写入都在单个 BEGIN IMMEDIATE 事务中完成（整笔提交）；
解除（lift）只追加新行、不改写旧行，并发解除不会互相覆盖。
"""

from __future__ import annotations

import csv
import io
import sqlite3

from .errors import DomainError
from .intervals import merge_with_ids, subtract_ranges, union_ranges

EXPECTED_HEADER = ("recording_id", "src_start_ms", "src_end_ms", "timeline_start_ms")


# ---------------------------------------------------------------- 剪辑版

def parse_csv(text: str) -> list[tuple[str, int, int, int]]:
    """解析剪辑 CSV，返回按成片起点排序的 (原录音号, 源起, 源止, 成片起) 列表。"""
    rows: list[tuple[str, int, int, int]] = []
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    for lineno, raw in enumerate(reader, start=1):
        if not raw or all(not c.strip() for c in raw):
            continue  # 跳过空行
        cells = [c.strip() for c in raw]
        if lineno == 1 and tuple(cells[:4]) == EXPECTED_HEADER:
            continue  # 表头
        if len(cells) != 4:
            raise DomainError(422, f"CSV 第 {lineno} 行应有 4 列，实际 {len(cells)} 列")
        recording_id = cells[0]
        if not recording_id:
            raise DomainError(422, f"CSV 第 {lineno} 行原录音号为空")
        try:
            src_start = int(cells[1])
            src_end = int(cells[2])
            tl_start = int(cells[3])
        except ValueError:
            raise DomainError(422, f"CSV 第 {lineno} 行毫秒字段必须是整数")
        if src_start < 0 or tl_start < 0:
            raise DomainError(422, f"CSV 第 {lineno} 行毫秒字段不能为负")
        if src_end <= src_start:
            raise DomainError(422, f"CSV 第 {lineno} 行源终点必须大于源起点")
        rows.append((recording_id, src_start, src_end, tl_start))
    if not rows:
        raise DomainError(422, "CSV 没有有效数据行")
    # 原速 => 成片时长 = 源时长；校验成片时间线上互不重叠
    ordered = sorted(rows, key=lambda r: r[3])
    prev_end: int | None = None
    for rec, ss, se, tl in ordered:
        tl_end = tl + (se - ss)
        if prev_end is not None and tl < prev_end:
            raise DomainError(
                422,
                f"片段在成片时间线上重叠：{rec} 成片起点 {tl}ms 早于前一段终点 {prev_end}ms",
            )
        prev_end = tl_end
    return ordered


def create_version(conn: sqlite3.Connection, name: str, csv_text: str) -> dict:
    rows = parse_csv(csv_text)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute("INSERT INTO versions (name) VALUES (?)", (name,))
        vid = cur.lastrowid
        for rec, ss, se, tl in rows:
            conn.execute(
                "INSERT INTO segments"
                " (version_id, recording_id, src_start_ms, src_end_ms, tl_start_ms, tl_end_ms)"
                " VALUES (?,?,?,?,?,?)",
                (vid, rec, ss, se, tl, tl + (se - ss)),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_version(conn, vid)


def list_versions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT v.id, v.name, v.created_at, COUNT(s.id) AS segment_count"
        " FROM versions v LEFT JOIN segments s ON s.version_id = v.id"
        " GROUP BY v.id ORDER BY v.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_version(conn: sqlite3.Connection, version_id: int) -> dict:
    v = _version_row(conn, version_id)
    v["segments"] = _segments_of(conn, version_id)
    return v


def _version_row(conn: sqlite3.Connection, version_id: int) -> dict:
    row = conn.execute(
        "SELECT id, name, created_at FROM versions WHERE id = ?", (version_id,)
    ).fetchone()
    if row is None:
        raise DomainError(404, f"版本 {version_id} 不存在")
    return dict(row)


def _segments_of(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT id, recording_id, src_start_ms, src_end_ms, tl_start_ms, tl_end_ms"
        " FROM segments WHERE version_id = ? ORDER BY tl_start_ms, id",
        (version_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 坐标换算

def _source_ranges_for_window(
    segments: list[dict], t1: int, t2: int
) -> list[dict]:
    """成片时段 [t1, t2) 经该版剪辑表追溯到的原声时段。"""
    out: list[dict] = []
    for s in segments:
        lo = max(t1, s["tl_start_ms"])
        hi = min(t2, s["tl_end_ms"])
        if lo < hi:
            offset = s["src_start_ms"] - s["tl_start_ms"]
            out.append(
                {
                    "recording_id": s["recording_id"],
                    "src_start_ms": lo + offset,
                    "src_end_ms": hi + offset,
                }
            )
    return out


def _remaining_source_ranges(conn: sqlite3.Connection, marker_id: int) -> list[dict]:
    """标记当前仍生效的原声时段 = 登记的原声时段 − 全部解除时段（按原录音分别计算）。"""
    mrows = conn.execute(
        "SELECT recording_id, src_start_ms, src_end_ms FROM marker_ranges WHERE marker_id = ?",
        (marker_id,),
    ).fetchall()
    lrows = conn.execute(
        "SELECT lr.recording_id, lr.src_start_ms, lr.src_end_ms FROM lift_ranges lr"
        " JOIN lifts l ON l.id = lr.lift_id WHERE l.marker_id = ?",
        (marker_id,),
    ).fetchall()
    base: dict[str, list[tuple[int, int]]] = {}
    for r in mrows:
        base.setdefault(r["recording_id"], []).append((r["src_start_ms"], r["src_end_ms"]))
    cuts: dict[str, list[tuple[int, int]]] = {}
    for r in lrows:
        cuts.setdefault(r["recording_id"], []).append((r["src_start_ms"], r["src_end_ms"]))
    out: list[dict] = []
    for rec, ranges in base.items():
        for s, e in subtract_ranges(union_ranges(ranges), union_ranges(cuts.get(rec, []))):
            out.append({"recording_id": rec, "src_start_ms": s, "src_end_ms": e})
    out.sort(key=lambda r: (r["recording_id"], r["src_start_ms"]))
    return out


# ---------------------------------------------------------------- 标记

def create_marker(
    conn: sqlite3.Connection, version_id: int, start_ms: int, end_ms: int, note: str = ""
) -> dict:
    if start_ms < 0:
        raise DomainError(422, "起点不能为负")
    if end_ms <= start_ms:
        raise DomainError(422, "终点必须大于起点（区间含起点、不含终点）")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _version_row(conn, version_id)
        segments = _segments_of(conn, version_id)
        ranges = _source_ranges_for_window(segments, start_ms, end_ms)
        if not ranges:
            raise DomainError(422, "该时段不涉及任何片段的原声，未登记")
        cur = conn.execute(
            "INSERT INTO markers (version_id, start_ms, end_ms, note) VALUES (?,?,?,?)",
            (version_id, start_ms, end_ms, note),
        )
        mid = cur.lastrowid
        # 跨剪接点时一笔多行，与标记同一事务提交
        for r in ranges:
            conn.execute(
                "INSERT INTO marker_ranges (marker_id, recording_id, src_start_ms, src_end_ms)"
                " VALUES (?,?,?,?)",
                (mid, r["recording_id"], r["src_start_ms"], r["src_end_ms"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_marker(conn, mid)


def list_markers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT m.id, m.version_id, v.name AS version_name, m.start_ms, m.end_ms,"
        " m.note, m.status, m.created_at"
        " FROM markers m JOIN versions v ON v.id = m.version_id ORDER BY m.id"
    ).fetchall()
    return [dict(r) for r in rows]


def get_marker(conn: sqlite3.Connection, marker_id: int) -> dict:
    row = conn.execute(
        "SELECT m.id, m.version_id, v.name AS version_name, m.start_ms, m.end_ms,"
        " m.note, m.status, m.created_at"
        " FROM markers m JOIN versions v ON v.id = m.version_id WHERE m.id = ?",
        (marker_id,),
    ).fetchone()
    if row is None:
        raise DomainError(404, f"标记 {marker_id} 不存在")
    m = dict(row)
    m["source_ranges"] = [
        dict(r)
        for r in conn.execute(
            "SELECT recording_id, src_start_ms, src_end_ms FROM marker_ranges"
            " WHERE marker_id = ? ORDER BY recording_id, src_start_ms",
            (marker_id,),
        ).fetchall()
    ]
    return m


def get_marker_detail(conn: sqlite3.Connection, marker_id: int) -> dict:
    """按标记号核对：登记的原声时段、各笔解除、剩余仍生效的原声时段。"""
    m = get_marker(conn, marker_id)
    lifts = [
        dict(r)
        for r in conn.execute(
            "SELECT l.id, l.version_id, v.name AS version_name, l.tl_start_ms, l.tl_end_ms,"
            " l.created_at FROM lifts l JOIN versions v ON v.id = l.version_id"
            " WHERE l.marker_id = ? ORDER BY l.id",
            (marker_id,),
        ).fetchall()
    ]
    for lift in lifts:
        lift["source_ranges"] = [
            dict(r)
            for r in conn.execute(
                "SELECT recording_id, src_start_ms, src_end_ms FROM lift_ranges"
                " WHERE lift_id = ? ORDER BY recording_id, src_start_ms",
                (lift["id"],),
            ).fetchall()
        ]
    m["lifts"] = lifts
    m["remaining_source_ranges"] = (
        [] if m["status"] == "withdrawn" else _remaining_source_ranges(conn, marker_id)
    )
    return m


def withdraw_marker(conn: sqlite3.Connection, marker_id: int) -> dict:
    """整笔撤回：标记在所有版本立即不再产生遮盖。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status FROM markers WHERE id = ?", (marker_id,)
        ).fetchone()
        if row is None:
            raise DomainError(404, f"标记 {marker_id} 不存在")
        if row["status"] == "withdrawn":
            raise DomainError(409, f"标记 {marker_id} 已撤回")
        conn.execute("UPDATE markers SET status = 'withdrawn' WHERE id = ?", (marker_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_marker(conn, marker_id)


# ---------------------------------------------------------------- 局部解除

def create_lift(
    conn: sqlite3.Connection,
    marker_id: int,
    version_id: int,
    start_ms: int,
    end_ms: int,
) -> dict:
    """在任一版按成片时段局部解除。

    解除换算到原声坐标并与该标记的原声时段求交后存储，因此作用于所有版本；
    只追加新行（lifts / lift_ranges），并发解除不会互相覆盖。
    """
    if start_ms < 0:
        raise DomainError(422, "起点不能为负")
    if end_ms <= start_ms:
        raise DomainError(422, "终点必须大于起点（区间含起点、不含终点）")
    conn.execute("BEGIN IMMEDIATE")
    try:
        mrow = conn.execute(
            "SELECT status FROM markers WHERE id = ?", (marker_id,)
        ).fetchone()
        if mrow is None:
            raise DomainError(404, f"标记 {marker_id} 不存在")
        if mrow["status"] != "active":
            raise DomainError(409, f"标记 {marker_id} 已撤回，无法再解除")
        _version_row(conn, version_id)
        segments = _segments_of(conn, version_id)
        window = _source_ranges_for_window(segments, start_ms, end_ms)
        if not window:
            raise DomainError(422, "该成片时段不涉及任何片段的原声")
        # 追到该标记对应的原声：与标记登记的原声时段求交
        mrows = conn.execute(
            "SELECT recording_id, src_start_ms, src_end_ms FROM marker_ranges WHERE marker_id = ?",
            (marker_id,),
        ).fetchall()
        effective: list[dict] = []
        for w in window:
            for mr in mrows:
                if mr["recording_id"] != w["recording_id"]:
                    continue
                lo = max(w["src_start_ms"], mr["src_start_ms"])
                hi = min(w["src_end_ms"], mr["src_end_ms"])
                if lo < hi:
                    effective.append(
                        {
                            "recording_id": w["recording_id"],
                            "src_start_ms": lo,
                            "src_end_ms": hi,
                        }
                    )
        if not effective:
            raise DomainError(422, "该时段与标记的原声时段没有交集，无需解除")
        cur = conn.execute(
            "INSERT INTO lifts (marker_id, version_id, tl_start_ms, tl_end_ms)"
            " VALUES (?,?,?,?)",
            (marker_id, version_id, start_ms, end_ms),
        )
        lid = cur.lastrowid
        # 跨剪接点时一笔多行，与解除同一事务提交
        for r in effective:
            conn.execute(
                "INSERT INTO lift_ranges (lift_id, recording_id, src_start_ms, src_end_ms)"
                " VALUES (?,?,?,?)",
                (lid, r["recording_id"], r["src_start_ms"], r["src_end_ms"]),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {
        "lift_id": lid,
        "marker_id": marker_id,
        "version_id": version_id,
        "tl_start_ms": start_ms,
        "tl_end_ms": end_ms,
        "lifted_source_ranges": effective,
    }


# ---------------------------------------------------------------- 遮盖计算

def _usages_in_version(
    segments: list[dict], remaining: list[dict], marker_id: int
) -> list[tuple[int, int, int]]:
    """标记剩余原声时段在某版成片时间线上的每次使用。"""
    pieces: list[tuple[int, int, int]] = []
    for s in segments:
        offset = s["tl_start_ms"] - s["src_start_ms"]
        for r in remaining:
            if r["recording_id"] != s["recording_id"]:
                continue
            lo = max(r["src_start_ms"], s["src_start_ms"])
            hi = min(r["src_end_ms"], s["src_end_ms"])
            if lo < hi:
                pieces.append((lo + offset, hi + offset, marker_id))
    return pieces


def muted_for_version(conn: sqlite3.Connection, version_id: int) -> dict:
    """某版当前需要遮盖的成片时段；重叠处列出全部标记号。"""
    _version_row(conn, version_id)
    segments = _segments_of(conn, version_id)
    active = conn.execute(
        "SELECT id FROM markers WHERE status = 'active' ORDER BY id"
    ).fetchall()
    pieces: list[tuple[int, int, int]] = []
    for m in active:
        remaining = _remaining_source_ranges(conn, m["id"])
        pieces.extend(_usages_in_version(segments, remaining, m["id"]))
    merged = merge_with_ids(pieces)
    return {
        "version_id": version_id,
        "muted": [
            {"start_ms": s, "end_ms": e, "marker_ids": ids} for s, e, ids in merged
        ],
    }


def marker_impact(conn: sqlite3.Connection, marker_id: int) -> dict:
    """按标记追溯到的原声，列出其在每一版中每次使用所需遮盖的成片时段。"""
    m = get_marker(conn, marker_id)
    if m["status"] == "withdrawn":
        return {"marker_id": marker_id, "status": "withdrawn", "versions": []}
    remaining = _remaining_source_ranges(conn, marker_id)
    versions = conn.execute("SELECT id, name FROM versions ORDER BY id").fetchall()
    out = []
    for v in versions:
        segments = _segments_of(conn, v["id"])
        usages = [
            {"tl_start_ms": s, "tl_end_ms": e}
            for s, e, _ in _usages_in_version(segments, remaining, marker_id)
        ]
        usages.sort(key=lambda u: u["tl_start_ms"])
        out.append(
            {"version_id": v["id"], "version_name": v["name"], "usages": usages}
        )
    return {"marker_id": marker_id, "status": m["status"], "versions": out}
