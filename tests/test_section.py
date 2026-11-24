"""断面取数：登记校验、求交/重合/未交片区间、分段边界与版本固定。

坐标与里程约定见 ``app/section.py``：
* 测线不沿片边，水平线 y 不整除片边长、竖直线 x 不整除片边长；
* 里程自起点 0 起算，终点为线段长度；
* 分段 ``[start, end)`` 每段含起点，仅取到测线末端的一段含终点。
"""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.db import Database
from app.geometry import Grid
from app.main import app
from app.section import SectionError, build_section_line, extract_section
from app.seed import GOOD_BATCH


def _register(client, line_no="H", orientation="horizontal", start=(0, 25), end=(200, 25)):
    return client.post(
        "/api/sections",
        json={
            "line_no": line_no,
            "orientation": orientation,
            "start": list(start),
            "end": list(end),
        },
    )


def _extract(client, line_no="H", start_mileage=0, end_mileage=None):
    params = {"start_mileage": start_mileage}
    if end_mileage is not None:
        params["end_mileage"] = end_mileage
    return client.get(f"/api/sections/{line_no}/extract", params=params)


# ----------------------------------------------------------------------
# 登记校验
# ----------------------------------------------------------------------
def test_register_section_pins_tiles(client):
    client.post("/api/grid", json={"rows": 2, "cols": 2, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
                {"row": 0, "col": 1,
                 "polylines": [{"depth": 5, "points": [[100, 25], [200, 25]]}]},
            ]
        },
    )
    r = _register(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_length"] == 200
    assert [(t["row"], t["col"]) for t in body["pinned_tiles"]] == [(0, 0), (0, 1)]
    assert all(t["batch_id"] is not None and not t["missing"] for t in body["pinned_tiles"])
    assert body["missing_tiles"] == []
    # 登记响应不泄露折线明细
    assert "polylines" not in r.text


def test_register_section_line_on_tile_edge_rejected(seeded_client):
    r = _register(seeded_client, line_no="EDGE", start=(0, 100), end=(200, 100))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "line_on_tile_edge"
    r = _register(seeded_client, line_no="EDGE2", orientation="vertical",
                  start=(100, 0), end=(100, 200))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "line_on_tile_edge"


def test_register_section_outside_or_misoriented_rejected(seeded_client):
    r = _register(seeded_client, line_no="OUT", start=(0, 25), end=(201, 25))
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "point_outside_grid"
    # 水平线起终点 y 不同 -> pydantic 422
    assert _register(seeded_client, line_no="MIS", start=(0, 25), end=(100, 26)).status_code == 422
    # 零长度
    assert _register(seeded_client, line_no="ZERO", start=(5, 25), end=(5, 25)).status_code == 422


def test_register_section_duplicate_line_no_rejected(seeded_client):
    assert _register(seeded_client).status_code == 200
    r = _register(seeded_client)
    assert r.status_code == 409


def test_register_section_requires_grid(client):
    r = _register(client)
    assert r.status_code == 409


def test_register_section_marks_missing_tiles(client):
    # 只提交左片，右片从未换过 => 登记时缺片
    client.post("/api/grid", json={"rows": 1, "cols": 2, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
            ]
        },
    )
    r = _register(client, start=(0, 25), end=(200, 25))
    assert r.status_code == 200
    body = r.json()
    assert body["missing_tiles"] == [[0, 1]]
    tiles = {(t["row"], t["col"]): t for t in body["pinned_tiles"]}
    assert tiles[(0, 0)]["missing"] is False
    assert tiles[(0, 1)]["missing"] is True and tiles[(0, 1)]["batch_id"] is None


# ----------------------------------------------------------------------
# 取数：交点、重合区间、未交片区间
# ----------------------------------------------------------------------
def test_extract_intersections_dedup_and_sorted(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [
                     # 同一里程同一水深：两条折线都过 (50,25) -> 只报一次
                     {"depth": 5, "points": [[0, 25], [50, 25], [100, 10]]},
                     {"depth": 5, "points": [[0, 10], [50, 25], [100, 25]]},
                     {"depth": 7, "points": [[0, 60], [60, 25], [100, 60]]},
                 ]},
            ]
        },
    )
    assert _register(client, start=(0, 25), end=(100, 25)).status_code == 200
    body = _extract(client).json()
    assert body["intersections"] == [
        {"mileage": 50, "depth": 5},
        {"mileage": 60, "depth": 7},
    ]


def test_extract_overlaps_merged_across_tiles(client):
    # 两个相邻片各有一段同水深重合线，跨片相接 -> 合并成一段
    client.post("/api/grid", json={"rows": 1, "cols": 2, "tile_size": 100})
    r = client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
                {"row": 0, "col": 1,
                 "polylines": [{"depth": 5, "points": [[100, 25], [200, 25]]}]},
            ]
        },
    )
    assert r.status_code == 200, r.text
    assert _register(client, start=(0, 25), end=(200, 25)).status_code == 200
    body = _extract(client).json()
    assert [(o["depth"], o["start_mileage"], o["end_mileage"]) for o in body["overlaps"]] == [
        (5, 0, 200)
    ]
    assert body["intersections"] == []


def test_extract_overlaps_merged_when_touching_or_stacked():
    # 同一片内折线端点不必都在片边：直接验证取数层的合并语义
    grid = Grid(1, 1, 100)
    line = build_section_line(grid, "horizontal", [0, 25], [100, 25])
    tiles = [
        {"row": 0, "col": 0, "missing": False, "polylines": [
            # 同水深 5：相接的两段 + 相叠的一段 -> 0..100
            {"depth": 5, "points": [[0, 25], [40, 25]]},
            {"depth": 5, "points": [[40, 25], [70, 25]]},
            {"depth": 5, "points": [[60, 25], [100, 25]]},
            {"depth": 6, "points": [[10, 25], [20, 25]]},
        ]},
    ]
    result = extract_section(line, tiles, 0, 100)
    overlaps = {(o["depth"], o["start_mileage"], o["end_mileage"]) for o in result["overlaps"]}
    assert overlaps == {(5, 0, 100), (6, 10, 20)}
    # 与测线共线的折线段不是“交点”
    assert result["intersections"] == []

    # 合并后再按本段截取：取 25..75
    clipped = extract_section(line, tiles, 25, 75)
    assert [(o["start_mileage"], o["end_mileage"], o["end_inclusive"])
            for o in clipped["overlaps"] if o["depth"] == 5] == [(25, 75, False)]


def test_extract_missing_tile_reported_as_gap(client):
    client.post("/api/grid", json={"rows": 1, "cols": 3, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
            ]
        },
    )
    # 片 1、2 缺片；测线 0..300
    assert _register(client, start=(0, 25), end=(300, 25)).status_code == 200
    body = _extract(client).json()
    assert body["gaps"] == [
        {
            "start_mileage": 100,
            "end_mileage": 300,
            "start_inclusive": True,
            "end_inclusive": True,
            "reason": "tile_missing",
        }
    ]
    # 只有相关片的交点被读出；缺片段不产生交点
    assert body["intersections"] == []


# ----------------------------------------------------------------------
# 分段边界：每段含起点，只有到末端的一段含终点
# ----------------------------------------------------------------------
def test_extract_segment_boundaries_cover_line_without_overlap(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [
                     {"depth": 5, "points": [[0, 25], [100, 25]]},
                     {"depth": 7, "points": [[0, 60], [50, 25], [100, 60]]},
                 ]},
            ]
        },
    )
    assert _register(client, start=(0, 25), end=(100, 25)).status_code == 200

    first = _extract(client, start_mileage=0, end_mileage=50).json()
    second = _extract(client, start_mileage=50, end_mileage=100).json()
    # 里程 50 的交点只在后一段出现（前段不含终点，后段含起点）
    assert first["intersections"] == []
    assert first["range"] == {
        "start_mileage": 0,
        "end_mileage": 50,
        "start_inclusive": True,
        "end_inclusive": False,
    }
    assert second["intersections"] == [{"mileage": 50, "depth": 7}]
    assert second["range"]["end_inclusive"] is True

    # 重合区间在两段里首尾相接，不重叠
    o1 = first["overlaps"]
    o2 = second["overlaps"]
    assert [(o["start_mileage"], o["end_mileage"]) for o in o1] == [(0, 50)]
    assert o1[0]["end_inclusive"] is False
    assert [(o["start_mileage"], o["end_mileage"]) for o in o2] == [(50, 100)]
    assert o2[0]["start_inclusive"] is True


def test_extract_full_line_contains_endpoint(client):
    client.post("/api/grid", json={"rows": 1, "cols": 1, "tile_size": 100})
    client.post(
        "/api/batches",
        json={
            "tiles": [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
            ]
        },
    )
    assert _register(client, start=(0, 25), end=(100, 25)).status_code == 200
    body = _extract(client).json()
    assert body["range"]["end_inclusive"] is True
    assert body["overlaps"][0]["end_mileage"] == 100


def test_extract_range_validation(seeded_client):
    assert _register(seeded_client, line_no="A", start=(0, 25), end=(200, 25)).status_code == 200
    assert _extract(seeded_client, line_no="A", start_mileage=60, end_mileage=60).status_code == 422
    assert _extract(seeded_client, line_no="A", start_mileage=0, end_mileage=201).status_code == 422
    assert _extract(seeded_client, line_no="A", start_mileage=-1).status_code == 422
    assert _extract(seeded_client, line_no="NOPE").status_code == 404


def test_extract_reversed_vertical_line_uses_start_as_origin(seeded_client):
    # 竖直测线自 (45,200) 向上... 即起点在上、终点在下；里程沿起点指向终点增加
    r = _register(seeded_client, line_no="V", orientation="vertical",
                  start=(45, 200), end=(45, 0))
    assert r.status_code == 200
    body = _extract(seeded_client, line_no="V").json()
    assert body["total_length"] == 200
    # 种子数据中 y=100 的接缝靠近中心闭环；交点应在合理里程内且按里程递增
    miles = [i["mileage"] for i in body["intersections"]]
    assert miles == sorted(miles)


# ----------------------------------------------------------------------
# 版本固定：换片/重启后原测线号仍按登记时的数据取数
# ----------------------------------------------------------------------
def test_extraction_uses_pinned_versions_after_swap(seeded_client):
    assert _register(seeded_client, line_no="PIN", start=(0, 40), end=(200, 40)).status_code == 200
    before = _extract(seeded_client, line_no="PIN").json()
    assert before["pinned_tiles"][0]["batch_id"] == 1

    r = seeded_client.post("/api/batches", json=GOOD_BATCH)
    assert r.status_code == 200, r.text

    after = _extract(seeded_client, line_no="PIN").json()
    # 版本指针固定为登记时的批次；交换后取数结果不变
    assert after["pinned_tiles"] == before["pinned_tiles"]
    assert after["intersections"] == before["intersections"]
    assert after["overlaps"] == before["overlaps"]
    assert after["gaps"] == before["gaps"]

    # 同一测线号重新登记（不同线号）才使用新版本
    assert _register(seeded_client, line_no="PIN2", start=(0, 40), end=(200, 40)).status_code == 200
    fresh = _extract(seeded_client, line_no="PIN2").json()
    assert fresh["pinned_tiles"][0]["batch_id"] == 2


def test_extraction_survives_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "restart.db"))
    with TestClient(app) as c:
        c.post("/admin/seed")
        assert _register(c, line_no="R", start=(0, 40), end=(200, 40)).status_code == 200
        before = _extract(c, line_no="R").json()
    # 新进程/新连接：只剩 SQLite 文件
    with TestClient(app) as c:
        after = _extract(c, line_no="R").json()
        assert after["pinned_tiles"] == before["pinned_tiles"]
        assert after["intersections"] == before["intersections"]


def test_extraction_clips_to_line_span_when_endpoint_inside_tile():
    """测线端点落在片内部时，不能把同片折线与测线延长线的交点算进来。"""
    grid = Grid(1, 1, 100)
    line = build_section_line(grid, "horizontal", [20, 25], [80, 25])
    tiles = [
        {"row": 0, "col": 0, "missing": False, "polylines": [
            # 与测线延长线（y=25，x<20 与 x>80）相交，不能算作本测线交点
            {"depth": 4, "points": [[0, 40], [10, 25], [15, 40]]},
            {"depth": 8, "points": [[85, 40], [95, 25], [100, 40]]},
            # 真正的横截交点 x=50 -> 里程 30
            {"depth": 6, "points": [[50, 10], [50, 40]]},
            # 共线段 y=25 从 x=0 到 x=100，应被截到 0..60（里程）
            {"depth": 7, "points": [[0, 25], [100, 25]]},
        ]},
    ]
    result = extract_section(line, tiles, 0, 60)
    assert result["intersections"] == [{"mileage": 30, "depth": 6}]
    assert [(o["depth"], o["start_mileage"], o["end_mileage"]) for o in result["overlaps"]] == [
        (7, 0, 60)
    ]


def test_extraction_reads_only_involved_tiles(tmp_path):
    """折线明细只读与测线相交的片：无关片即使登记后被删也不会影响取数。"""
    path = str(tmp_path / "only.db")
    db = Database(path)
    with db.connect() as conn:
        db.set_grid(conn, 2, 2, 100)
        db.commit_batch(
            conn,
            [
                {"row": 0, "col": 0,
                 "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]},
                {"row": 1, "col": 1,
                 "polylines": [{"depth": 9, "points": [[100, 125], [200, 125]]}]},
            ],
            "seed",
        )
        line = build_section_line(Grid(2, 2, 100), "horizontal", [0, 25], [200, 25])
        involved = line.involved_tiles()
        assert involved == [(0, 0), (0, 1)]
        pinned = [{"row": r, "col": c, "batch_id": 1} for r, c in involved]
        db.register_section(conn, "ONLY", {
            "orientation": "horizontal",
            "start": [0, 25],
            "end": [200, 25],
            "total_length": 200,
        }, pinned, {"rows": 2, "cols": 2, "tile_size": 100})
        conn.commit()

        # 删除无关片 (1,1) 的所有折线明细，取数应完全不受影响
        conn.execute(
            "DELETE FROM tile_revisions WHERE tile_row = 1 AND tile_col = 1"
        )
        conn.commit()

        snap = db.section_snapshot(conn, "ONLY")
        assert {(t["row"], t["col"]) for t in snap["tiles"]} == {(0, 0), (0, 1)}
        result = extract_section(line, snap["tiles"], 0, 200)
        assert result["gaps"] == []


def test_extraction_isolated_from_concurrent_swap(tmp_path):
    """取数期间的换片不影响本次取数：快照在同一读事务内完成。"""
    path = str(tmp_path / "conc.db")
    db = Database(path)
    with db.connect() as conn:
        db.set_grid(conn, 1, 1, 100)
        db.commit_batch(conn, [{"row": 0, "col": 0,
                                "polylines": [{"depth": 5, "points": [[0, 25], [100, 25]]}]}], "v1")
        pinned = [{"row": 0, "col": 0, "batch_id": 1}]
        db.register_section(conn, "C", {"orientation": "horizontal", "start": [0, 25],
                                        "end": [100, 25], "total_length": 100},
                            pinned, {"rows": 1, "cols": 1, "tile_size": 100})
        conn.commit()

    with db.connect() as conn:
        conn.execute("BEGIN")
        snap = db.section_snapshot(conn, "C")
        # 在另一连接里换片（删掉旧版本、换新版本）
        with db.connect() as other:
            db.commit_batch(other, [{"row": 0, "col": 0,
                                     "polylines": [{"depth": 9, "points": [[0, 60], [100, 60]]}]}], "v2")
            other.commit()
        # 本事务仍读到登记时的版本
        again = db.section_snapshot(conn, "C")
        assert snap == again
        line = build_section_line(Grid(1, 1, 100), "horizontal", [0, 25], [100, 25])
        result = extract_section(line, again["tiles"], 0, 100)
        assert result["overlaps"][0]["depth"] == 5
        conn.commit()

    # 换片后原登记测线仍固定旧版本：依然读到水深 5
    with db.connect() as conn:
        snap = db.section_snapshot(conn, "C")
        assert snap["tiles"][0]["polylines"][0]["depth"] == 5


# ----------------------------------------------------------------------
# 纯几何单元测试
# ----------------------------------------------------------------------
def test_build_section_line_rejects_diagonal():
    with pytest.raises(SectionError) as exc:
        build_section_line(Grid(2, 2, 100), "horizontal", [0, 25], [100, 30])
    assert exc.value.code == "not_axis_aligned"


def test_tile_mileage_interval_clipped_to_line_ends():
    line = build_section_line(Grid(2, 2, 100), "horizontal", [50, 25], [150, 25])
    assert line.tile_mileage_interval(0, 0) == (0, 50)
    assert line.tile_mileage_interval(0, 1) == (50, 100)
    # 测线位于 row 0（y=25），下排片不相交
    assert line.tile_mileage_interval(1, 0) is None
