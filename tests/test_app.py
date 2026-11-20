"""关键功能自测：导入、追溯、遮盖合并、撤回、局部解除、并发、重启持久化。"""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

# 版 A：R1[1000,5000)->成片[0,4000)；R2[0,3000)->成片[4000,7000)
CSV_A = """recording_id,src_start_ms,src_end_ms,timeline_start_ms
R1,1000,5000,0
R2,0,3000,4000
"""
# 版 B：同一段原声的不同用法，用于验证跨版追溯
CSV_B = """recording_id,src_start_ms,src_end_ms,timeline_start_ms
R2,500,2500,0
R1,3000,6000,2000
R3,100,900,5000
"""


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_two_versions(client):
    va = client.post("/api/versions", json={"name": "A版", "csv_text": CSV_A}).json()
    vb = client.post("/api/versions", json={"name": "B版", "csv_text": CSV_B}).json()
    return va["id"], vb["id"]


def muted(client, vid):
    return client.get(f"/api/versions/{vid}/muted").json()["muted"]


# ---------------------------------------------------------------- 导入

def test_import_version(client):
    va, _ = make_two_versions(client)
    v = client.get(f"/api/versions/{va}").json()
    assert v["name"] == "A版"
    assert len(v["segments"]) == 2
    s1, s2 = v["segments"]
    assert (s1["recording_id"], s1["tl_start_ms"], s1["tl_end_ms"]) == ("R1", 0, 4000)
    assert (s2["recording_id"], s2["tl_start_ms"], s2["tl_end_ms"]) == ("R2", 4000, 7000)
    listed = client.get("/api/versions").json()
    assert [x["id"] for x in listed] == [va, va + 1]


def test_import_rejects_overlap(client):
    bad = "R1,0,1000,0\nR2,0,1000,500\n"  # 成片 [0,1000) 与 [500,1500) 重叠
    r = client.post("/api/versions", json={"name": "bad", "csv_text": bad})
    assert r.status_code == 422
    assert "重叠" in r.json()["detail"]


def test_import_rejects_bad_rows(client):
    assert client.post("/api/versions", json={"name": "x", "csv_text": ""}).status_code == 422
    assert client.post(
        "/api/versions", json={"name": "x", "csv_text": "R1,abc,100,0"}
    ).status_code == 422
    assert client.post(
        "/api/versions", json={"name": "x", "csv_text": "R1,100,100,0"}
    ).status_code == 422
    assert client.post(
        "/api/versions", json={"name": "x", "csv_text": "R1,0,100"}
    ).status_code == 422


def test_version_immutable_no_mutation_endpoints(client):
    va, _ = make_two_versions(client)
    assert client.put(f"/api/versions/{va}", json={}).status_code == 405
    assert client.delete(f"/api/versions/{va}").status_code == 405


# ---------------------------------------------------------------- 标记与追溯

def test_marker_traces_source_across_cut(client):
    va, vb = make_two_versions(client)
    # 跨剪接点：成片 [2000,5000) 跨 R1 段与 R2 段
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 2000, "end_ms": 5000,
                          "note": "敏感内容"}).json()
    assert m["status"] == "active"
    got = {(r["recording_id"], r["src_start_ms"], r["src_end_ms"])
           for r in m["source_ranges"]}
    assert got == {("R1", 3000, 5000), ("R2", 0, 1000)}

    # 任一版列出每次使用所需遮盖的时段（相邻同标记段落合并显示）
    assert muted(client, va) == [
        {"start_ms": 2000, "end_ms": 5000, "marker_ids": [m["id"]]},
    ]
    assert muted(client, vb) == [
        {"start_ms": 0, "end_ms": 500, "marker_ids": [m["id"]]},
        {"start_ms": 2000, "end_ms": 4000, "marker_ids": [m["id"]]},
    ]


def test_marker_half_open_boundary(client):
    va, _ = make_two_versions(client)
    # [0,4000) 恰好到剪接点为止：只含 R1 段
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 0, "end_ms": 4000}).json()
    assert {(r["recording_id"], r["src_start_ms"], r["src_end_ms"])
            for r in m["source_ranges"]} == {("R1", 1000, 5000)}
    # [4000,7000) 从剪接点开始：只含 R2 段
    m2 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 4000, "end_ms": 7000}).json()
    assert {(r["recording_id"], r["src_start_ms"], r["src_end_ms"])
            for r in m2["source_ranges"]} == {("R2", 0, 3000)}


def test_marker_outside_segments_rejected(client):
    va, _ = make_two_versions(client)
    r = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 9000, "end_ms": 10000})
    assert r.status_code == 422
    # 整笔提交：失败不留任何标记
    assert client.get("/api/markers").json() == []


def test_overlap_shows_all_marker_ids(client):
    va, _ = make_two_versions(client)
    m1 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    m2 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 3000, "end_ms": 4500}).json()
    assert muted(client, va) == [
        {"start_ms": 2000, "end_ms": 3000, "marker_ids": [m1["id"]]},
        {"start_ms": 3000, "end_ms": 4500, "marker_ids": [m1["id"], m2["id"]]},
        {"start_ms": 4500, "end_ms": 5000, "marker_ids": [m1["id"]]},
    ]


# ---------------------------------------------------------------- 撤回

def test_withdraw_marker(client):
    va, vb = make_two_versions(client)
    m1 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    m2 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 3000, "end_ms": 4500}).json()
    r = client.post(f"/api/markers/{m1['id']}/withdraw")
    assert r.status_code == 200 and r.json()["status"] == "withdrawn"
    # 各版只剩 m2 的遮盖
    assert muted(client, va) == [
        {"start_ms": 3000, "end_ms": 4500, "marker_ids": [m2["id"]]},
    ]
    assert muted(client, vb) == [
        {"start_ms": 3000, "end_ms": 4000, "marker_ids": [m2["id"]]},
    ]
    # 重复撤回 -> 409；已撤回标记不能再解除 -> 409
    assert client.post(f"/api/markers/{m1['id']}/withdraw").status_code == 409
    assert client.post(f"/api/markers/{m1['id']}/lifts",
                       json={"version_id": va, "start_ms": 0, "end_ms": 100}
                       ).status_code == 409


# ---------------------------------------------------------------- 局部解除

def test_lift_traces_source_and_applies_to_all_versions(client):
    va, vb = make_two_versions(client)
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    # 在 B 版按成片时段 [2500,3000) 解除 -> 追到 R1 源 [3500,4000)
    r = client.post(f"/api/markers/{m['id']}/lifts",
                    json={"version_id": vb, "start_ms": 2500, "end_ms": 3000})
    assert r.status_code == 201
    assert r.json()["lifted_source_ranges"] == [
        {"recording_id": "R1", "src_start_ms": 3500, "src_end_ms": 4000}
    ]
    # 剩余原声时段可按标记号核对
    detail = client.get(f"/api/markers/{m['id']}").json()
    assert detail["remaining_source_ranges"] == [
        {"recording_id": "R1", "src_start_ms": 3000, "src_end_ms": 3500},
        {"recording_id": "R1", "src_start_ms": 4000, "src_end_ms": 5000},
        {"recording_id": "R2", "src_start_ms": 0, "src_end_ms": 1000},
    ]
    # 解除作用于各版
    assert muted(client, va) == [
        {"start_ms": 2000, "end_ms": 2500, "marker_ids": [m["id"]]},
        {"start_ms": 3000, "end_ms": 5000, "marker_ids": [m["id"]]},
    ]
    assert muted(client, vb) == [
        {"start_ms": 0, "end_ms": 500, "marker_ids": [m["id"]]},
        {"start_ms": 2000, "end_ms": 2500, "marker_ids": [m["id"]]},
        {"start_ms": 3000, "end_ms": 4000, "marker_ids": [m["id"]]},
    ]


def test_lift_does_not_affect_other_markers(client):
    va, _ = make_two_versions(client)
    m1 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    m2 = client.post("/api/markers",
                     json={"version_id": va, "start_ms": 3000, "end_ms": 4500}).json()
    client.post(f"/api/markers/{m1['id']}/lifts",
                json={"version_id": va, "start_ms": 3000, "end_ms": 4000})
    # m2 照常生效：重叠区仍列出两个标记号
    assert muted(client, va) == [
        {"start_ms": 2000, "end_ms": 3000, "marker_ids": [m1["id"]]},
        {"start_ms": 3000, "end_ms": 4000, "marker_ids": [m2["id"]]},
        {"start_ms": 4000, "end_ms": 4500, "marker_ids": [m1["id"], m2["id"]]},
        {"start_ms": 4500, "end_ms": 5000, "marker_ids": [m1["id"]]},
    ]


def test_lift_spanning_cut_is_atomic(client):
    va, _ = make_two_versions(client)
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    # 跨剪接点的解除：一笔覆盖 R1 尾段与 R2 首段
    r = client.post(f"/api/markers/{m['id']}/lifts",
                    json={"version_id": va, "start_ms": 3500, "end_ms": 4500})
    assert r.status_code == 201
    got = {(x["recording_id"], x["src_start_ms"], x["src_end_ms"])
           for x in r.json()["lifted_source_ranges"]}
    assert got == {("R1", 4500, 5000), ("R2", 0, 500)}
    detail = client.get(f"/api/markers/{m['id']}").json()
    assert detail["remaining_source_ranges"] == [
        {"recording_id": "R1", "src_start_ms": 3000, "src_end_ms": 4500},
        {"recording_id": "R2", "src_start_ms": 500, "src_end_ms": 1000},
    ]


def test_lift_without_intersection_rejected(client):
    va, vb = make_two_versions(client)
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    # B 版 [5000,5400) 是 R3，与该标记无关
    r = client.post(f"/api/markers/{m['id']}/lifts",
                    json={"version_id": vb, "start_ms": 5000, "end_ms": 5400})
    assert r.status_code == 422
    # 整笔提交：失败不留解除记录
    assert client.get(f"/api/markers/{m['id']}").json()["lifts"] == []


def test_concurrent_lifts_do_not_overwrite(tmp_path):
    app = create_app(str(tmp_path / "c.db"))
    with TestClient(app) as c:
        va = c.post("/api/versions", json={"name": "A", "csv_text": CSV_A}).json()["id"]
        vb = c.post("/api/versions", json={"name": "B", "csv_text": CSV_B}).json()["id"]
        mid = c.post("/api/markers",
                     json={"version_id": va, "start_ms": 2000, "end_ms": 5000}
                     ).json()["id"]

    def lift(start, end):
        with TestClient(app) as c:
            return c.post(f"/api/markers/{mid}/lifts",
                          json={"version_id": vb, "start_ms": start, "end_ms": end})

    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(lift, 2500, 3000), ex.submit(lift, 3000, 3500)]
        results = [f.result() for f in futs]
    assert all(r.status_code == 201 for r in results)

    with TestClient(app) as c:
        detail = c.get(f"/api/markers/{mid}").json()
    # 两笔并发解除都生效：R1 源 [3500,4000) 与 [4000,4500) 均被解除
    assert detail["lifts"] and len(detail["lifts"]) == 2
    assert detail["remaining_source_ranges"] == [
        {"recording_id": "R1", "src_start_ms": 3000, "src_end_ms": 3500},
        {"recording_id": "R1", "src_start_ms": 4500, "src_end_ms": 5000},
        {"recording_id": "R2", "src_start_ms": 0, "src_end_ms": 1000},
    ]


# ---------------------------------------------------------------- 持久化

def test_persistence_across_restart(tmp_path):
    dbp = str(tmp_path / "restart.db")
    with TestClient(create_app(dbp)) as c:
        va = c.post("/api/versions", json={"name": "A", "csv_text": CSV_A}).json()["id"]
        vb = c.post("/api/versions", json={"name": "B", "csv_text": CSV_B}).json()["id"]
        mid = c.post("/api/markers",
                     json={"version_id": va, "start_ms": 2000, "end_ms": 5000}
                     ).json()["id"]
        c.post(f"/api/markers/{mid}/lifts",
               json={"version_id": vb, "start_ms": 2500, "end_ms": 3000})

    # 模拟重启：同一数据库文件重新建应用
    with TestClient(create_app(dbp)) as c:
        detail = c.get(f"/api/markers/{mid}").json()
        assert detail["remaining_source_ranges"] == [
            {"recording_id": "R1", "src_start_ms": 3000, "src_end_ms": 3500},
            {"recording_id": "R1", "src_start_ms": 4000, "src_end_ms": 5000},
            {"recording_id": "R2", "src_start_ms": 0, "src_end_ms": 1000},
        ]
        assert len(c.get("/api/versions").json()) == 2
        m = c.get(f"/api/versions/{va}/muted").json()["muted"]
        assert m == [
            {"start_ms": 2000, "end_ms": 2500, "marker_ids": [mid]},
            {"start_ms": 3000, "end_ms": 5000, "marker_ids": [mid]},
        ]


def test_impact_lists_each_usage_in_any_version(client):
    va, vb = make_two_versions(client)
    m = client.post("/api/markers",
                    json={"version_id": va, "start_ms": 2000, "end_ms": 5000}).json()
    impact = client.get(f"/api/markers/{m['id']}/impact").json()
    by_version = {v["version_id"]: v["usages"] for v in impact["versions"]}
    assert by_version[va] == [
        {"tl_start_ms": 2000, "tl_end_ms": 4000},
        {"tl_start_ms": 4000, "tl_end_ms": 5000},
    ]
    assert by_version[vb] == [
        {"tl_start_ms": 0, "tl_end_ms": 500},
        {"tl_start_ms": 2000, "tl_end_ms": 4000},
    ]
