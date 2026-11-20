"""SQLite 连接与建表。所有写事务由 logic 层用 BEGIN IMMEDIATE 显式控制。"""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 剪辑片段：导入后固定，不提供任何修改/删除途径
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    recording_id TEXT NOT NULL,          -- 原录音号
    src_start_ms INTEGER NOT NULL,       -- 源起点（含）
    src_end_ms INTEGER NOT NULL,         -- 源终点（不含）
    tl_start_ms INTEGER NOT NULL,        -- 成片起点（含）
    tl_end_ms INTEGER NOT NULL,          -- 成片终点（不含）= tl_start + (src_end - src_start)，原速
    CHECK (src_start_ms >= 0),
    CHECK (src_end_ms > src_start_ms),
    CHECK (tl_start_ms >= 0)
);
CREATE INDEX IF NOT EXISTS idx_segments_version ON segments(version_id);

-- 禁播标记：登记在某版成片时间线上，可跨剪接点
CREATE TABLE IF NOT EXISTS markers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','withdrawn')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (start_ms >= 0),
    CHECK (end_ms > start_ms)
);

-- 标记追溯到的原声时段（按登记版换算，跨剪接点时一笔多行，同事务提交）
CREATE TABLE IF NOT EXISTS marker_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id INTEGER NOT NULL REFERENCES markers(id),
    recording_id TEXT NOT NULL,
    src_start_ms INTEGER NOT NULL,
    src_end_ms INTEGER NOT NULL,
    CHECK (src_end_ms > src_start_ms)
);
CREATE INDEX IF NOT EXISTS idx_marker_ranges_marker ON marker_ranges(marker_id);

-- 局部解除：在某版按成片时段登记
CREATE TABLE IF NOT EXISTS lifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    marker_id INTEGER NOT NULL REFERENCES markers(id),
    version_id INTEGER NOT NULL REFERENCES versions(id),
    tl_start_ms INTEGER NOT NULL,
    tl_end_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (tl_start_ms >= 0),
    CHECK (tl_end_ms > tl_start_ms)
);
CREATE INDEX IF NOT EXISTS idx_lifts_marker ON lifts(marker_id);

-- 解除换算到原声坐标后的时段（与标记原声时段求交；作用于所有版本）
CREATE TABLE IF NOT EXISTS lift_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lift_id INTEGER NOT NULL REFERENCES lifts(id),
    recording_id TEXT NOT NULL,
    src_start_ms INTEGER NOT NULL,
    src_end_ms INTEGER NOT NULL,
    CHECK (src_end_ms > src_start_ms)
);
CREATE INDEX IF NOT EXISTS idx_lift_ranges_lift ON lift_ranges(lift_id);

-- 逐词转写稿：按原录音号保存，可整份换稿（换稿成功才替换旧稿）
CREATE TABLE IF NOT EXISTS transcripts (
    recording_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL DEFAULT 1,
    digest TEXT NOT NULL,                 -- 规范化内容的 SHA-256，用于生成/放行时比对
    word_count INTEGER NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (revision >= 1)
);

CREATE TABLE IF NOT EXISTS transcript_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id TEXT NOT NULL REFERENCES transcripts(recording_id),
    seq INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,            -- 原声起点（含）
    end_ms INTEGER NOT NULL,              -- 原声终点（不含）
    text TEXT NOT NULL,
    CHECK (start_ms >= 0),
    CHECK (end_ms > start_ms)
);
CREATE INDEX IF NOT EXISTS idx_transcript_words_rec
    ON transcript_words(recording_id, start_ms);

-- 一次性生成的成片词表：保存生成时的转写稿/遮盖时段快照与整份文件，
-- 放行下载前重新比对，任何一侧变动都整份不发。
CREATE TABLE IF NOT EXISTS wordlist_generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES versions(id),
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ready','missing_transcripts','stale','failed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at TEXT,
    transcript_snapshot TEXT NOT NULL,    -- JSON [{recording_id, revision, digest}]
    missing_snapshot TEXT NOT NULL DEFAULT '[]',  -- JSON [recording_id, ...]
    muted_snapshot TEXT NOT NULL,         -- JSON [[start, end], ...] 合并后的遮盖时段
    stale_reasons TEXT NOT NULL DEFAULT '[]',
    row_count INTEGER,
    csv_text TEXT
);
CREATE INDEX IF NOT EXISTS idx_wordlist_generations_version
    ON wordlist_generations(version_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path: str) -> None:
    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    conn = connect(db_path)
    try:
        # WAL：生成词表时的读快照不阻塞换稿、改标记等写入
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()
