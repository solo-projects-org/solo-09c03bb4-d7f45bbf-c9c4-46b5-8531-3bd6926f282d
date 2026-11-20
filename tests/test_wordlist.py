"""逐词稿与成片词表自测：换稿保留旧稿、整词收录、原声复用、消音、缺稿、
放行核对、生成期间并发换稿与新增禁播。"""

import csv
import io
import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# 版 A：R1[1000,5000)->成片[0,4000)；R2[0,3000)->成片[4000,7000)
CSV_A = """recording_id,src_start_ms,src_end_ms,timeline_start_ms
R1,1000,5000,0
R2,0,3000,4000
"""
# 复用版：同一段 R1 原声用了两次（[1000,2000)->成片[0,1000)、[1000,2000)->成片[5000,6000)）
CSV_REUSE = """recording_id,src_start_ms,src_end_ms,timeline_start_ms
R1,1000,2000,0
R2,0,1000,1000
R1,1000,2000,5000
"""

T_R1 = """start_ms,end_ms,text
1000,1500,甲
1500,2500,乙
2500,3000,丙
3000,4000,丁
4000,5000,戊
"""
T_R2 = """start_ms,end_ms,text
0,1000,己
1000,2000,庚
2000,3000,辛
"""


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def add_version(client, name, csv_text):
    return client.post("/api/versions", json={"name": name, "csv_text": csv_text}).json()["id"]


def put_transcript(client, rid, text):
    return client.put(f"/api/transcripts/{rid}", json={"csv_text": text})


def parse_wordlist(text):
    assert text.startswith("﻿")
    return list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))


def gen(client, vid):
    r = client.post(f"/api/versions/{vid}/wordlists")
    assert r.status_code == 201, r.text
    return r.json()


def download(client, gid):
    return client.get(f"/api/generations/{gid}/download")


# ---------------------------------------------------------------- 换稿

def test_replace_transcript_and_keep_old_on_failure(client):
    put_transcript(client, "R1", T_R1)
    t = client.get("/api/transcripts/R1").json()
    assert t["revision"] == 1 and t["word_count"] == 5

    # 换稿失败（毫秒字段非法）：旧稿原样保留
    bad = "start_ms,end_ms,text\n0,1000,新\n500,坏,坏\n"
    r = put_transcript(client, "R1", bad)
    assert r.status_code == 422
    t = client.get("/api/transcripts/R1").json()
    assert t["revision"] == 1
    assert [w["text"] for w in t["words"]] == ["甲", "乙", "丙", "丁", "戊"]

    # 换稿成功：修订号递增，内容替换
    ok = "start_ms,end_ms,text\n1000,2000,新甲\n2000,5000,新乙\n"
    r = put_transcript(client, "R1", ok)
    assert r.status_code == 200 and r.json()["revision"] == 2
    assert [w["text"] for w in client.get("/api/transcripts/R1").json()["words"]] == ["新甲", "新乙"]


def test_transcript_validation(client):
    assert put_transcript(client, "R1", "").status_code == 422
    assert put_transcript(client, "R1", "0,0,零").status_code == 422
    assert put_transcript(client, "R1", "0,100,").status_code == 422
    assert put_transcript(client, "R1", "0,abc,字").status_code == 422
    assert put_transcript(client, "R1", "0,100,一,多列").status_code == 422
    # 首尾相接的词照常接受
    assert put_transcript(client, "R1", "0,100,一\n100,200,二").status_code == 200


# ---------------------------------------------------------------- 基本词表

def test_wordlist_lists_words_by_timeline(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    g = gen(client, va)
    assert g["status"] == "ready" and g["row_count"] == 8

    r = download(client, g["id"])
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = parse_wordlist(r.text)
    assert [(x["start_ms"], x["end_ms"], x["text"]) for x in rows] == [
        ("0", "500", "甲"), ("500", "1500", "乙"), ("1500", "2000", "丙"),
        ("2000", "3000", "丁"), ("3000", "4000", "戊"),
        ("4000", "5000", "己"), ("5000", "6000", "庚"), ("6000", "7000", "辛"),
    ]


def test_whole_word_must_fit_in_one_segment(client):
    # 片段 R1 只用 [1000,3000)；词 [500,1500) 与 [2500,3500) 都跨出该段，不收
    va = add_version(client, "A", "R1,1000,3000,0\n")
    put_transcript(client, "R1", "start_ms,end_ms,text\n500,1500,跨头\n1000,2000,全内\n2000,3000,全内二\n2500,3500,跨尾\n")
    g = gen(client, va)
    rows = parse_wordlist(download(client, g["id"]).text)
    assert [x["text"] for x in rows] == ["全内", "全内二"]
    assert [(x["start_ms"], x["end_ms"]) for x in rows] == [("0", "1000"), ("1000", "2000")]


# ---------------------------------------------------------------- 原声复用

def test_reused_source_listed_once_per_occurrence(client):
    vid = add_version(client, "复用版", CSV_REUSE)
    put_transcript(client, "R1", "start_ms,end_ms,text\n1000,1200,甲\n1200,2000,乙\n")
    put_transcript(client, "R2", T_R2)
    g = gen(client, vid)
    rows = parse_wordlist(download(client, g["id"]).text)
    # R1[1000,2000) 被用了两次：同一批词各列一次
    # R2 只用了源 [0,1000)，只有“己”整词落在片段内，“庚”“辛”不收
    assert [(x["start_ms"], x["end_ms"], x["text"]) for x in rows] == [
        ("0", "200", "甲"), ("200", "1000", "乙"),
        ("1000", "2000", "己"),
        ("5000", "5200", "甲"), ("5200", "6000", "乙"),
    ]


def test_reused_source_mutes_every_occurrence(client):
    vid = add_version(client, "复用版", CSV_REUSE)
    put_transcript(client, "R1", "start_ms,end_ms,text\n1000,1200,甲\n1200,2000,乙\n")
    put_transcript(client, "R2", T_R2)
    # 在成片 [5020,5180) 登记禁播（R1 段用法的第二次出现处）
    m = client.post("/api/markers", json={"version_id": vid, "start_ms": 5020, "end_ms": 5180}).json()
    assert m["source_ranges"] == [{"recording_id": "R1", "src_start_ms": 1020, "src_end_ms": 1180}]
    g = gen(client, vid)
    rows = parse_wordlist(download(client, g["id"]).text)
    # 禁播按原声时段追溯，作用于该原声的每一次使用：两处“甲”都消音
    assert [(x["start_ms"], x["end_ms"], x["text"]) for x in rows] == [
        ("0", "200", "〔消音〕"), ("200", "1000", "乙"),
        ("1000", "2000", "己"),
        ("5000", "5200", "〔消音〕"), ("5200", "6000", "乙"),
    ]


# ---------------------------------------------------------------- 消音

def test_word_intersecting_muted_range_is_fully_replaced(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    # 成片 [1400,1600) 换算到 R1 源 [2400,2600)；乙 [1500,2500) 的终点 1500 与
    # 遮盖起点 1400 有交集 → 整词消音；丙源 [2500,3000) 与 [2400,2600) 相交
    # 于 [2500,2600) → 也整词消音；甲源 [1000,1500) 不相交，保留
    client.post("/api/markers", json={"version_id": va, "start_ms": 1400, "end_ms": 1600})
    g = gen(client, va)
    rows = parse_wordlist(download(client, g["id"]).text)
    assert [(x["start_ms"], x["end_ms"], x["text"]) for x in rows] == [
        ("0", "500", "甲"), ("500", "1500", "〔消音〕"),
        ("1500", "2000", "〔消音〕"),
        ("2000", "3000", "丁"), ("3000", "4000", "戊"),
        ("4000", "5000", "己"), ("5000", "6000", "庚"), ("6000", "7000", "辛"),
    ]


def test_half_open_boundary_no_overlap(client):
    # 遮盖 [500,1500) 恰好与乙 [500,1500) 相同，与丙 [1500,2000) 不重叠（终点不含）
    va = add_version(client, "A", "R1,0,2000,0\n")
    put_transcript(client, "R1", "start_ms,end_ms,text\n0,500,甲\n500,1500,乙\n1500,2000,丙\n")
    client.post("/api/markers", json={"version_id": va, "start_ms": 500, "end_ms": 1500})
    g = gen(client, va)
    rows = parse_wordlist(download(client, g["id"]).text)
    assert [x["text"] for x in rows] == ["甲", "〔消音〕", "丙"]


# ---------------------------------------------------------------- 缺稿

def test_missing_transcripts_block_download(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    g = gen(client, va)
    assert g["status"] == "missing_transcripts"
    assert g["missing_recording_ids"] == ["R2"]
    assert g["row_count"] is None

    r = download(client, g["id"])
    assert r.status_code == 409
    assert r.json()["missing_recording_ids"] == ["R2"]
    assert "R2" in r.json()["detail"]

    # 补稿后旧生成记录仍不放行，需重新生成
    put_transcript(client, "R2", T_R2)
    assert download(client, g["id"]).status_code == 409
    g2 = gen(client, va)
    assert g2["status"] == "ready"
    assert download(client, g2["id"]).status_code == 200


# ---------------------------------------------------------------- 放行核对

def test_download_rejected_after_transcript_replaced(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    g = gen(client, va)
    assert download(client, g["id"]).status_code == 200

    put_transcript(client, "R1", "start_ms,end_ms,text\n1000,5000,整段\n")
    r = download(client, g["id"])
    assert r.status_code == 409
    assert "换稿" in r.json()["detail"]
    assert r.json()["status"] == "stale"
    # 整份不发：已标 stale，重试生成才可下载
    assert download(client, g["id"]).status_code == 409
    g2 = gen(client, va)
    rows = parse_wordlist(download(client, g2["id"]).text)
    assert ("0", "4000", "整段") in [(x["start_ms"], x["end_ms"], x["text"]) for x in rows]


def test_download_rejected_after_marker_added_or_lifted(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    g = gen(client, va)
    assert download(client, g["id"]).status_code == 200

    m = client.post("/api/markers", json={"version_id": va, "start_ms": 1500, "end_ms": 1600}).json()
    r = download(client, g["id"])
    assert r.status_code == 409
    assert "遮盖的时段有变动" in r.json()["detail"]

    g2 = gen(client, va)
    assert download(client, g2["id"]).status_code == 200

    # 撤回标记同样算变动
    client.post(f"/api/markers/{m['id']}/withdraw")
    assert download(client, g2["id"]).status_code == 409

    g3 = gen(client, va)
    rows = parse_wordlist(download(client, g3["id"]).text)
    assert all(x["text"] != "〔消音〕" for x in rows)


def test_marker_in_other_version_does_not_invalidate(client):
    va = add_version(client, "A", CSV_A)
    vb = add_version(client, "B", "R2,0,1000,0\n")
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    g = gen(client, vb)
    # 标记登记在 A 版，且不涉及 B 版使用的原声：B 版词表不受影响
    client.post("/api/markers", json={"version_id": va, "start_ms": 0, "end_ms": 100})
    assert download(client, g["id"]).status_code == 200


def test_identical_reupload_keeps_generation_valid(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    g = gen(client, va)
    put_transcript(client, "R1", T_R1)  # 内容相同的重传
    assert download(client, g["id"]).status_code == 200


# ---------------------------------------------------------------- 生成期间并发

def test_transcript_can_change_while_generating(tmp_path, monkeypatch):
    """生成期间换稿与新增禁播不被阻塞；放行时发现变动则整份不发。"""
    from app import logic

    dbp = str(tmp_path / "gen.db")
    app = create_app(dbp)
    with TestClient(app) as c:
        va = add_version(c, "A", CSV_A)
        put_transcript(c, "R1", T_R1)
        put_transcript(c, "R2", T_R2)

    started = threading.Event()
    proceed = threading.Event()
    real_build = logic._build_wordlist

    def slow_build(segments, words_by_rec, muted):
        started.set()
        assert proceed.wait(timeout=10)
        return real_build(segments, words_by_rec, muted)

    monkeypatch.setattr(logic, "_build_wordlist", slow_build)

    result = {}

    def do_generate():
        with TestClient(app) as c:
            result["gen"] = c.post(f"/api/versions/{va}/wordlists").json()

    t = threading.Thread(target=do_generate)
    t.start()
    assert started.wait(timeout=10)

    # 生成尚未完成时：换稿、登记禁播都应立即成功
    with TestClient(app) as c:
        assert put_transcript(c, "R1", "start_ms,end_ms,text\n1000,5000,换过\n").status_code == 200
        assert c.post("/api/markers", json={"version_id": va, "start_ms": 0, "end_ms": 200}).status_code == 201

    proceed.set()
    t.join(timeout=10)
    g = result["gen"]
    assert g["status"] == "ready"  # 快照来自生成开始前

    with TestClient(app) as c:
        r = download(c, g["id"])
    assert r.status_code == 409
    assert "换稿" in r.json()["detail"] and "遮盖的时段有变动" in r.json()["detail"]


def test_word_straddling_cut_not_listed(client):
    """同一原录音号的两段在成片时间线上相接，跨剪接点的词整词不收。"""
    vid = add_version(
        client, "同源两段",
        "R1,0,1000,0\nR1,1000,2000,1000\n",
    )
    put_transcript(client, "R1", "start_ms,end_ms,text\n0,800,前\n800,1200,跨\n1200,2000,后\n")
    g = gen(client, vid)
    rows = parse_wordlist(download(client, g["id"]).text)
    assert [(x["start_ms"], x["end_ms"], x["text"]) for x in rows] == [
        ("0", "800", "前"), ("1200", "2000", "后"),
    ]


def test_lift_invalidates_generation(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    put_transcript(client, "R2", T_R2)
    m = client.post("/api/markers", json={"version_id": va, "start_ms": 1400, "end_ms": 1600}).json()
    g = gen(client, va)
    assert download(client, g["id"]).status_code == 200
    client.post(f"/api/markers/{m['id']}/lifts",
                json={"version_id": va, "start_ms": 1400, "end_ms": 1600})
    assert download(client, g["id"]).status_code == 409

    g2 = gen(client, va)
    rows = parse_wordlist(download(client, g2["id"]).text)
    assert all(x["text"] != "〔消音〕" for x in rows)


def test_generation_list_and_detail(client):
    va = add_version(client, "A", CSV_A)
    put_transcript(client, "R1", T_R1)
    g = gen(client, va)  # 缺 R2
    listed = client.get("/api/generations").json()
    assert [x["id"] for x in listed] == [g["id"]]
    assert listed[0]["missing_recording_ids"] == ["R2"]
    detail = client.get(f"/api/generations/{g['id']}").json()
    assert detail["muted_snapshot"] == []
    assert client.get("/api/generations/9999").status_code == 404
    assert client.get("/api/generations/9999/download").status_code == 404


def test_unknown_version_generation_404(client):
    assert client.post("/api/versions/42/wordlists").status_code == 404


def test_transcript_endpoints(client):
    assert client.get("/api/transcripts").json() == []
    assert client.get("/api/transcripts/未知").status_code == 404
    put_transcript(client, "R1", T_R1)
    listed = client.get("/api/transcripts").json()
    assert [t["recording_id"] for t in listed] == ["R1"]
    assert listed[0]["word_count"] == 5


def test_generation_snapshot_uses_pre_change_content(tmp_path, monkeypatch):
    """并发换稿发生在快照之后，生成记录仍保留生成时的稿件版本。"""
    from app import logic

    app = create_app(str(tmp_path / "snap.db"))
    with TestClient(app) as c:
        va = add_version(c, "A", "R1,0,2000,0\n")
        put_transcript(c, "R1", "start_ms,end_ms,text\n0,1000,旧\n1000,2000,旧二\n")

    started = threading.Event()
    proceed = threading.Event()
    real_build = logic._build_wordlist

    def slow_build(segments, words_by_rec, muted):
        started.set()
        assert proceed.wait(timeout=10)
        return real_build(segments, words_by_rec, muted)

    monkeypatch.setattr(logic, "_build_wordlist", slow_build)
    out = {}

    def do_generate():
        with TestClient(app) as c:
            out["gen"] = c.post(f"/api/versions/{va}/wordlists").json()

    t = threading.Thread(target=do_generate)
    t.start()
    assert started.wait(timeout=10)
    with TestClient(app) as c:
        assert put_transcript(c, "R1", "start_ms,end_ms,text\n0,2000,新\n").status_code == 200
    proceed.set()
    t.join(timeout=10)

    g = out["gen"]
    assert g["transcript_snapshot"] == [
        {"recording_id": "R1", "revision": 1, "digest": g["transcript_snapshot"][0]["digest"]}
    ]
    with TestClient(app) as c:
        assert download(c, g["id"]).status_code == 409
