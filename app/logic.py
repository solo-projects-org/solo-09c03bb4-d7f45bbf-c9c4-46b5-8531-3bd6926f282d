"""领域逻辑：剪辑版导入、禁播标记登记/撤回、局部解除、各版遮盖计算。

区间一律为半开区间 [start, end)：含起点、不含终点。
所有跨行写入都在单个 BEGIN IMMEDIATE 事务中完成（整笔提交）；
解除（lift）只追加新行、不改写旧行，并发解除不会互相覆盖。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3

from .errors import DomainError
from .intervals import merge_with_ids, subtract_ranges, union_ranges

EXPECTED_HEADER = ("recording_id", "src_start_ms", "src_end_ms", "timeline_start_ms")
TRANSCRIPT_HEADER = ("start_ms", "end_ms", "text")
MUTE_PLACEHOLDER = "〔消音〕"
WORDLIST_HEADER = ("start_ms", "end_ms", "text")


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


def muted_intervals(conn: sqlite3.Connection, version_id: int) -> list[tuple[int, int]]:
    """某版当前需遮盖的成片时段，只保留覆盖范围（合并相邻/重叠，不带标记号）。

    生成词表时用它判断词是否与遮盖时段有交集；放行时也用它比对
    “该版需遮盖的时段”是否仍与生成时一致。
    """
    _version_row(conn, version_id)
    segments = _segments_of(conn, version_id)
    active = conn.execute(
        "SELECT id FROM markers WHERE status = 'active' ORDER BY id"
    ).fetchall()
    pieces: list[tuple[int, int]] = []
    for m in active:
        remaining = _remaining_source_ranges(conn, m["id"])
        pieces.extend((s, e) for s, e, _ in _usages_in_version(segments, remaining, m["id"]))
    return union_ranges(pieces)


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


# ---------------------------------------------------------------- 逐词转写稿

def parse_transcript_csv(text: str) -> list[tuple[int, int, str]]:
    """解析逐词稿 CSV，返回按起点排序的 (原声起点, 原声终点, 词)。

    每行 3 列：`start_ms,end_ms,text`（表头可带可不带）。
    每行是一个独立的整词单位；行与行之间是否重叠不影响整词判定。
    """
    rows: list[tuple[int, int, str]] = []
    reader = csv.reader(io.StringIO(text.lstrip("\ufeff")))
    for lineno, raw in enumerate(reader, start=1):
        if not raw or all(not c.strip() for c in raw):
            continue
        cells = [c.strip() for c in raw]
        if lineno == 1 and tuple(cells[:3]) == TRANSCRIPT_HEADER:
            continue
        if len(cells) != 3:
            raise DomainError(
                422, f"逐词稿第 {lineno} 行应有 3 列（起点, 终点, 词），实际 {len(cells)} 列"
            )
        if not cells[2]:
            raise DomainError(422, f"逐词稿第 {lineno} 行词为空")
        try:
            start_ms = int(cells[0])
            end_ms = int(cells[1])
        except ValueError:
            raise DomainError(422, f"逐词稿第 {lineno} 行毫秒字段必须是整数")
        if start_ms < 0:
            raise DomainError(422, f"逐词稿第 {lineno} 行起点不能为负")
        if end_ms <= start_ms:
            raise DomainError(
                422, f"逐词稿第 {lineno} 行终点必须大于起点（区间含起点、不含终点）"
            )
        rows.append((start_ms, end_ms, cells[2]))
    if not rows:
        raise DomainError(422, "逐词稿没有有效数据行")
    return sorted(rows, key=lambda r: (r[0], r[1], r[2]))


def _transcript_digest(rows: list[tuple[int, int, str]]) -> str:
    canonical = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def replace_transcript(
    conn: sqlite3.Connection, recording_id: str, csv_text: str
) -> dict:
    """整份换稿：先完整校验新稿，校验通过才在同一事务中替换旧稿。

    校验失败时抛出异常并回滚，旧稿原样保留。
    """
    recording_id = recording_id.strip()
    if not recording_id:
        raise DomainError(422, "原录音号不能为空")
    rows = parse_transcript_csv(csv_text)  # 校验在写事务之前完成
    digest = _transcript_digest(rows)
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT revision FROM transcripts WHERE recording_id = ?", (recording_id,)
        ).fetchone()
        revision = 1 if row is None else row["revision"] + 1
        conn.execute(
            "INSERT INTO transcripts (recording_id, revision, digest, word_count, updated_at)"
            " VALUES (?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))"
            " ON CONFLICT(recording_id) DO UPDATE SET"
            "   revision = excluded.revision, digest = excluded.digest,"
            "   word_count = excluded.word_count, updated_at = excluded.updated_at",
            (recording_id, revision, digest, len(rows)),
        )
        conn.execute("DELETE FROM transcript_words WHERE recording_id = ?", (recording_id,))
        conn.executemany(
            "INSERT INTO transcript_words (recording_id, seq, start_ms, end_ms, text)"
            " VALUES (?,?,?,?,?)",
            [
                (recording_id, i, s, e, t)
                for i, (s, e, t) in enumerate(rows)
            ],
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_transcript(conn, recording_id)


def get_transcript(conn: sqlite3.Connection, recording_id: str) -> dict:
    row = conn.execute(
        "SELECT recording_id, revision, digest, word_count, updated_at"
        " FROM transcripts WHERE recording_id = ?",
        (recording_id,),
    ).fetchone()
    if row is None:
        raise DomainError(404, f"原录音号 {recording_id} 尚无逐词稿")
    t = dict(row)
    t["words"] = [
        dict(w)
        for w in conn.execute(
            "SELECT seq, start_ms, end_ms, text FROM transcript_words"
            " WHERE recording_id = ? ORDER BY seq",
            (recording_id,),
        ).fetchall()
    ]
    return t


def list_transcripts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT recording_id, revision, digest, word_count, updated_at"
        " FROM transcripts ORDER BY recording_id"
    ).fetchall()
    return [dict(r) for r in rows]


def _transcript_snapshot(
    conn: sqlite3.Connection, recording_ids: list[str]
) -> list[dict]:
    """给定录音号当前稿的 (修订号, 摘要) 快照。"""
    out: list[dict] = []
    for rid in sorted(recording_ids):
        row = conn.execute(
            "SELECT revision, digest FROM transcripts WHERE recording_id = ?", (rid,)
        ).fetchone()
        if row is not None:
            out.append(
                {"recording_id": rid, "revision": row["revision"], "digest": row["digest"]}
            )
    return out


def _snapshot_map(snapshot: list[dict]) -> dict[str, str]:
    # 以内容摘要比对：稿子内容未变（即使重传同一份）就不算变动
    return {e["recording_id"]: e["digest"] for e in snapshot}


def _revision_map(snapshot: list[dict]) -> dict[str, int]:
    return {e["recording_id"]: e["revision"] for e in snapshot}


# ---------------------------------------------------------------- 成片词表

def _words_by_recording(
    conn: sqlite3.Connection, recording_ids: list[str]
) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {rid: [] for rid in recording_ids}
    if not recording_ids:
        return out
    placeholders = ",".join("?" for _ in recording_ids)
    rows = conn.execute(
        "SELECT recording_id, seq, start_ms, end_ms, text FROM transcript_words"
        f" WHERE recording_id IN ({placeholders}) ORDER BY recording_id, seq",
        recording_ids,
    ).fetchall()
    for r in rows:
        out[r["recording_id"]].append(dict(r))
    return out


def _build_wordlist(
    segments: list[dict],
    words_by_rec: dict[str, list[dict]],
    muted: list[tuple[int, int]],
) -> list[dict]:
    """按成片时间列出每个词的起止毫秒与文字。

    - 整词必须落在一个剪辑片段内才收录；跨剪接点的词整词不收。
    - 同一段原声在剪辑表中被用了几次，就各列几次（按片段逐一展开）。
    - 词的成片区间与需遮盖时段有交集（半开区间）时，整词换成〔消音〕。
    """
    rows: list[dict] = []
    for seg in segments:
        offset = seg["tl_start_ms"] - seg["src_start_ms"]
        for w in words_by_rec.get(seg["recording_id"], []):
            if w["start_ms"] < seg["src_start_ms"] or w["end_ms"] > seg["src_end_ms"]:
                continue  # 整词没有完全落在该片段内
            start = w["start_ms"] + offset
            end = w["end_ms"] + offset
            text = w["text"]
            if any(start < me and end > ms for ms, me in muted):
                text = MUTE_PLACEHOLDER
            rows.append({"start_ms": start, "end_ms": end, "text": text})
    rows.sort(key=lambda r: (r["start_ms"], r["end_ms"]))
    return rows


def _wordlist_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    buf.write("\ufeff")  # UTF-8 BOM：便于表格软件直接打开
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(WORDLIST_HEADER)
    for r in rows:
        writer.writerow([r["start_ms"], r["end_ms"], r["text"]])
    return buf.getvalue()


def generate_wordlist(conn: sqlite3.Connection, version_id: int) -> dict:
    """为某一版生成逐词 CSV。

    先在一个读事务中取一致的快照（所用逐词稿 + 该版需遮盖的时段），
    在事务外计算内容；因此生成期间换稿、改禁播标记都不会被阻塞，
    而任何变动都会在下载放行时比对出来。缺稿时不产出文件并暂停下载。
    """
    _version_row(conn, version_id)
    conn.execute("BEGIN")
    try:
        segments = _segments_of(conn, version_id)
        used = sorted({s["recording_id"] for s in segments})
        snapshot = _transcript_snapshot(conn, used)
        muted = muted_intervals(conn, version_id)
        words_by_rec = _words_by_recording(conn, used)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

    present = {e["recording_id"] for e in snapshot}
    missing = [rid for rid in used if rid not in present]
    muted_json = json.dumps([[s, e] for s, e in muted])

    if missing:
        status = "missing_transcripts"
        rows = None
        csv_text = None
        stale_reasons: list[str] = []
    else:
        status = "ready"
        rows = _build_wordlist(segments, words_by_rec, muted)
        csv_text = _wordlist_csv(rows)
        stale_reasons = []

    cur = conn.execute(
        "INSERT INTO wordlist_generations"
        " (version_id, status, transcript_snapshot, missing_snapshot, muted_snapshot,"
        "  stale_reasons, row_count, csv_text, finished_at)"
        " VALUES (?,?,?,?,?,?,?,?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
        (
            version_id,
            status,
            json.dumps(snapshot, ensure_ascii=False),
            json.dumps(missing, ensure_ascii=False),
            muted_json,
            json.dumps(stale_reasons, ensure_ascii=False),
            None if rows is None else len(rows),
            csv_text,
        ),
    )
    return get_generation(conn, cur.lastrowid)


def _generation_row(
    conn: sqlite3.Connection, generation_id: int, include_csv: bool = True
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT g.id, g.version_id, v.name AS version_name, g.status, g.created_at,"
        " g.finished_at, g.transcript_snapshot, g.missing_snapshot, g.muted_snapshot,"
        " g.stale_reasons, g.row_count" + (", g.csv_text" if include_csv else "") +
        " FROM wordlist_generations g JOIN versions v ON v.id = g.version_id"
        " WHERE g.id = ?",
        (generation_id,),
    ).fetchone()
    if row is None:
        raise DomainError(404, f"词表生成记录 {generation_id} 不存在")
    return row


def get_generation(conn: sqlite3.Connection, generation_id: int) -> dict:
    """生成记录详情（不含整份 CSV 正文）。"""
    row = _generation_row(conn, generation_id, include_csv=False)
    return {
        "id": row["id"],
        "version_id": row["version_id"],
        "version_name": row["version_name"],
        "status": row["status"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "transcript_snapshot": json.loads(row["transcript_snapshot"]),
        "missing_recording_ids": json.loads(row["missing_snapshot"]),
        "muted_snapshot": json.loads(row["muted_snapshot"]),
        "stale_reasons": json.loads(row["stale_reasons"]),
        "row_count": row["row_count"],
    }


def list_generations(conn: sqlite3.Connection) -> list[dict]:
    ids = conn.execute(
        "SELECT id FROM wordlist_generations ORDER BY id DESC"
    ).fetchall()
    return [get_generation(conn, r["id"]) for r in ids]


def _current_missing(conn: sqlite3.Connection, version_id: int) -> list[str]:
    used = sorted(
        {s["recording_id"] for s in _segments_of(conn, version_id)}
    )
    present = {e["recording_id"] for e in _transcript_snapshot(conn, used)}
    return [rid for rid in used if rid not in present]


def wordlist_download(conn: sqlite3.Connection, generation_id: int) -> tuple[str, str]:
    """放行下载前核对：所用逐词稿与该版需遮盖的时段必须仍与生成时一致。

    返回 (文件名, CSV 文本)；不一致或缺稿时抛 409，整份不发出，
    并把生成记录标为 stale 以便复核。
    """
    row = _generation_row(conn, generation_id)
    version_id = row["version_id"]

    if row["status"] == "missing_transcripts":
        missing = _current_missing(conn, version_id)
        if missing:
            raise DomainError(
                409,
                f"缺少原录音号 {', '.join(missing)} 的逐词稿，词表下载已暂停；"
                "补稿后请重新生成。",
                {"status": "missing_transcripts", "missing_recording_ids": missing},
            )
        # 稿子已补齐，但这份文件是缺稿时生成的：标为失效，要求重新生成
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE wordlist_generations SET status = 'stale', stale_reasons = ?"
                " WHERE id = ? AND status = 'missing_transcripts'",
                (
                    json.dumps(
                        ["生成时缺稿，现已补稿；请重新生成"], ensure_ascii=False
                    ),
                    generation_id,
                ),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        raise DomainError(
            409,
            "逐词稿已补齐，但这份词表生成时缺稿；请重新生成后再下载。",
            {"status": "stale", "missing_recording_ids": []},
        )
    if row["status"] in ("stale", "failed"):
        reasons = json.loads(row["stale_reasons"])
        raise DomainError(
            409,
            "这份词表已失效，请重新生成。" + ("原因：" + "；".join(reasons) if reasons else ""),
            {"status": row["status"], "stale_reasons": reasons},
        )
    if row["status"] != "ready":
        raise DomainError(409, "词表尚未生成完成，请稍后重试。", {"status": row["status"]})

    # ---- 放行核对（单次读事务，取与生成时同口径的快照）----
    conn.execute("BEGIN")
    try:
        used = sorted({s["recording_id"] for s in _segments_of(conn, version_id)})
        current = _transcript_snapshot(conn, used)
        current_muted = muted_intervals(conn, version_id)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")

    reasons: list[str] = []
    before = _snapshot_map(json.loads(row["transcript_snapshot"]))
    after = _snapshot_map(current)
    before_rev = _revision_map(json.loads(row["transcript_snapshot"]))
    after_rev = _revision_map(current)
    for rid in sorted(set(before) | set(after)):
        if rid not in after:
            reasons.append(f"逐词稿 {rid} 已不存在")
        elif rid not in before:
            reasons.append(f"逐词稿 {rid} 为新增")
        elif before[rid] != after[rid]:
            reasons.append(
                f"逐词稿 {rid} 已换稿（生成时修订 {before_rev[rid]}，"
                f"当前修订 {after_rev[rid]}）"
            )
    if current_muted != [tuple(x) for x in json.loads(row["muted_snapshot"])]:
        reasons.append("该版需遮盖的时段有变动")

    if reasons:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE wordlist_generations SET status = 'stale', stale_reasons = ?"
                " WHERE id = ?",
                (json.dumps(reasons, ensure_ascii=False), generation_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        raise DomainError(
            409,
            "生成后内容有变动，整份词表暂不发出，请重试生成。原因：" + "；".join(reasons),
            {"status": "stale", "stale_reasons": reasons},
        )

    filename = f"wordlist_version{version_id}_gen{generation_id}.csv"
    return filename, row["csv_text"]
