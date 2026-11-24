"""港池等深线分片测图服务（FastAPI）。

工作流
------
1. ``POST /api/grid``  定义固定方片网格；
2. ``POST /api/batches``  整批换片：先做单片几何校验与批内缝校验，
   接不上直接 422 并指出接缝，事务回滚、旧图继续有效；
3. ``GET /api/render``  按当前整图快照接图，输出端点位于图幅外边的长线或闭环，
   同时核对新片与未换旧片之间的批界缝；
4. 出图只在单个只读事务内取快照，换片写入不等待出图，正在出图的请求继续用原版。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import seed
from .assembly import assemble
from .db import Database, DuplicateSectionError
from .geometry import Grid, validate_batch
from .schemas import (
    BatchIn,
    GridIn,
    RenderOut,
    SectionExtractOut,
    SectionIn,
    SectionRegistrationOut,
)
from .section import SectionError, build_section_line, extract_section


def get_db(request: Request) -> Database:
    return request.app.state.db


def build_grid(db: Database, conn) -> Grid:
    meta = db.get_grid(conn)
    if meta is None:
        raise HTTPException(status_code=409, detail="尚未定义网格，请先调用 POST /api/grid")
    return Grid(meta["rows"], meta["cols"], meta["tile_size"])


@asynccontextmanager
async def lifespan(app: FastAPI) -> None:
    db_path = os.environ.get("DB_PATH", "/data/soundings.db")
    app.state.db = Database(db_path)
    yield


app = FastAPI(
    title="港池等深线分片测图服务",
    version="1.0.0",
    description=(
        "矩形图幅按固定方片网格划分，测绘组按片提交带水深的整数坐标折线；"
        "整批换片、验缝，出图时接成外边长线或闭环。数据存 SQLite（WAL）。"
    ),
    lifespan=lifespan,
)


@app.get("/health", tags=["运维"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/grid", tags=["网格"], summary="定义（或确认）固定方片网格")
def define_grid(body: GridIn, db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        if body.reset:
            db.reset(conn)
        if not body.reset and db.has_tiles(conn):
            raise HTTPException(status_code=409, detail="图幅已有换片数据；如需重建请传 reset=true")
        meta = db.set_grid(conn, body.rows, body.cols, body.tile_size)
        conn.commit()
    return {"status": "ok", "grid": meta}


@app.post(
    "/api/batches",
    tags=["换片"],
    summary="整批换片（验缝失败则整批拒绝，旧图继续有效）",
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "合格样例-上排修测": {"value": seed.GOOD_BATCH},
                        "失败样例-接缝对不上": {"value": seed.BAD_BATCH},
                    }
                }
            }
        },
    },
)
def submit_batch(body: BatchIn, db: Database = Depends(get_db)) -> dict:
    payload = [t.model_dump() for t in body.tiles]
    with db.connect() as conn:
        grid = build_grid(db, conn)
        # 片号范围与重复校验
        for t in payload:
            if not (0 <= t["row"] < grid.rows and 0 <= t["col"] < grid.cols):
                raise HTTPException(
                    status_code=422,
                    detail={
                        "code": "tile_out_of_grid",
                        "message": f"片 ({t['row']},{t['col']}) 超出 {grid.rows}x{grid.cols} 网格",
                    },
                )
        # 整批验缝：只读本批提交数据，不读未换片折线明细
        _, errors = validate_batch(grid, payload)
        if errors:
            rejected_id = db.record_rejected_batch(
                conn, tile_count=len(payload), reason=body.reason, errors=errors
            )
            conn.commit()
            return JSONResponse(
                status_code=422,
                content={
                    "status": "rejected",
                    "batch_id": rejected_id,
                    "message": "验缝未通过，整批拒绝，当前图幅仍使用旧图",
                    "error_count": len(errors),
                    "errors": errors,
                },
            )
        batch_id = db.commit_batch(conn, payload, body.reason)
        conn.commit()
    return {
        "status": "committed",
        "batch_id": batch_id,
        "tile_count": len(payload),
        "message": "换片已生效，可调用 GET /api/render 出图",
    }


@app.get("/api/render", tags=["出图"], summary="接图：外边长线或闭环", response_model=RenderOut)
def render(db: Database = Depends(get_db)) -> dict[str, Any]:
    # 单个只读事务 = 一致快照；并发换片不会影响本次出图（正在出图继续用原版）
    with db.connect() as conn:
        snapshot = db.current_snapshot(conn)
    meta = snapshot["grid"]
    if meta is None:
        raise HTTPException(status_code=409, detail="尚未定义网格")
    grid = Grid(meta["rows"], meta["cols"], meta["tile_size"])
    tiles = snapshot["tiles"]
    features, seam_errors = assemble(grid, tiles)
    return {
        "status": "ok" if not seam_errors else "seam_errors",
        "grid": meta,
        "tile_count": len(tiles),
        "features": features,
        "seam_errors": seam_errors,
    }


@app.get("/api/tiles", tags=["查询"], summary="当前图幅各片状态（不含未换片折线明细）")
def list_tiles(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        meta = db.get_grid(conn)
        tiles = db.list_current_tiles(conn)
    return {"grid": meta, "tiles": tiles}


@app.get("/api/batches", tags=["查询"], summary="换批记录（含被拒绝批次及其接缝错误）")
def list_batches(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        return {"batches": db.list_batches(conn)}


# ----------------------------------------------------------------------
# 断面取数
# ----------------------------------------------------------------------
@app.post(
    "/api/sections",
    tags=["断面取数"],
    summary="登记断面测线（固定所涉各片版本与缺片状态）",
    response_model=SectionRegistrationOut,
    responses={409: {"description": "测线号已登记"}, 422: {"description": "测线参数不合法"}},
    openapi_extra={
        "requestBody": {
            "content": {
                "application/json": {
                    "examples": {
                        "水平测线-横穿两列": {
                            "value": {
                                "line_no": "S-H-25",
                                "orientation": "horizontal",
                                "start": [0, 25],
                                "end": [200, 25],
                            }
                        },
                        "竖直测线-纵穿两行": {
                            "value": {
                                "line_no": "S-V-45",
                                "orientation": "vertical",
                                "start": [45, 0],
                                "end": [45, 200],
                            }
                        },
                    }
                }
            }
        },
    },
)
def register_section(body: SectionIn, db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        # 先取写锁：读网格、读当前版本、写登记在同一事务内完成，
        # 固定下来的版本必然来自同一版图幅，不会在读取与写入之间被换片改掉。
        conn.execute("BEGIN IMMEDIATE")
        meta = db.get_grid(conn)
        if meta is None:
            raise HTTPException(status_code=409, detail="尚未定义网格，请先调用 POST /api/grid")
        grid = Grid(meta["rows"], meta["cols"], meta["tile_size"])
        try:
            line = build_section_line(grid, body.orientation, body.start, body.end)
        except SectionError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "message": exc.message}
            ) from None

        current = db.current_tile_batch_ids(conn, line.involved_tiles())
        pinned: list[dict] = []
        for row, col in line.involved_tiles():
            pinned.append(
                {"row": row, "col": col, "batch_id": current.get((row, col))}
            )
        line_payload = {
            "orientation": line.orientation,
            "start": list(line.start),
            "end": list(line.end),
            "total_length": line.total_length,
        }
        try:
            section_id = db.register_section(conn, body.line_no, line_payload, pinned, meta)
            conn.commit()
        except DuplicateSectionError as exc:
            conn.rollback()
            raise HTTPException(status_code=409, detail=exc.args[0]) from None

    # 登记响应只暴露片号/版本/缺片状态，不返回折线明细
    pinned_out = [
        {
            "row": t["row"],
            "col": t["col"],
            "batch_id": t["batch_id"],
            "missing": t["batch_id"] is None,
        }
        for t in pinned
    ]
    return {
        "status": "registered",
        "section_id": section_id,
        "line_no": body.line_no,
        "orientation": line.orientation,
        "start": list(line.start),
        "end": list(line.end),
        "total_length": line.total_length,
        "grid": meta,
        "pinned_tiles": pinned_out,
        "missing_tiles": [
            [t["row"], t["col"]] for t in pinned_out if t["missing"]
        ],
    }


@app.get(
    "/api/sections/{line_no}/extract",
    tags=["断面取数"],
    summary="按里程范围取数（交点、重合区间、未交片区间）",
    response_model=SectionExtractOut,
    responses={404: {"description": "测线号未登记"}},
)
def extract_section_by_range(
    line_no: str,
    start_mileage: int = Query(
        0, ge=0, description="起始里程（整数，含起点）"
    ),
    end_mileage: int | None = Query(
        None, ge=0, description="结束里程（整数，不含；缺省为测线末端，此时含终点）"
    ),
    db: Database = Depends(get_db),
) -> dict:
    with db.connect() as conn:
        # 显式只读事务：登记信息与折线明细取自同一快照；
        # 取数期间提交的换片不会影响本次结果，也不阻塞换片写入。
        conn.execute("BEGIN")
        snapshot = db.section_snapshot(conn, line_no)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"测线号 {line_no} 未登记")

    sec = snapshot["section"]
    grid = Grid(sec["grid_rows"], sec["grid_cols"], sec["tile_size"])
    line = build_section_line(
        grid,
        sec["orientation"],
        [sec["start_x"], sec["start_y"]],
        [sec["end_x"], sec["end_y"]],
    )
    total = line.total_length
    end = total if end_mileage is None else end_mileage
    if start_mileage > total or end > total:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "range_out_of_bounds",
                "message": f"里程范围 [{start_mileage},{end}) 超出测线 [0,{total}]",
            },
        )
    if end <= start_mileage:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "empty_range",
                "message": f"结束里程 {end} 必须大于起始里程 {start_mileage}",
            },
        )

    result = extract_section(line, snapshot["tiles"], start_mileage, end)
    pinned_out = [
        {
            "row": t["row"],
            "col": t["col"],
            "batch_id": t["batch_id"],
            "missing": t["missing"],
        }
        for t in snapshot["tiles"]
    ]
    return {
        "line_no": line_no,
        "orientation": line.orientation,
        "start": list(line.start),
        "end": list(line.end),
        "total_length": total,
        "range": result["range"],
        "pinned_tiles": pinned_out,
        "intersections": result["intersections"],
        "overlaps": result["overlaps"],
        "gaps": result["gaps"],
    }


@app.get(
    "/api/sections",
    tags=["断面取数"],
    summary="已登记测线列表（含所固定各片版本与缺片状态）",
)
def list_sections(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        return {"sections": db.list_sections(conn)}


@app.post(
    "/admin/seed",
    tags=["演示"],
    summary="一键载入 Swagger 演示数据（2x2 港池样例，会清空重建）",
)
def admin_seed(db: Database = Depends(get_db)) -> dict:
    with db.connect() as conn:
        db.reset(conn)
        db.set_grid(conn, seed.GRID["rows"], seed.GRID["cols"], seed.GRID["tile_size"])
        batch_id = db.commit_batch(conn, seed.SEED_TILES, "初始演示数据")
        grid = Grid(seed.GRID["rows"], seed.GRID["cols"], seed.GRID["tile_size"])
        current = db.current_tile_batch_ids(
            conn,
            [
                (row, col)
                for line in seed.DEMO_SECTIONS
                for row, col in build_section_line(
                    grid, line["orientation"], line["start"], line["end"]
                ).involved_tiles()
            ],
        )
        for line in seed.DEMO_SECTIONS:
            section_line = build_section_line(
                grid, line["orientation"], line["start"], line["end"]
            )
            pinned = [
                {"row": row, "col": col, "batch_id": current.get((row, col))}
                for row, col in section_line.involved_tiles()
            ]
            db.register_section(
                conn,
                line["line_no"],
                {
                    "orientation": section_line.orientation,
                    "start": list(section_line.start),
                    "end": list(section_line.end),
                    "total_length": section_line.total_length,
                },
                pinned,
                seed.GRID,
            )
        conn.commit()
    return {
        "status": "ok",
        "batch_id": batch_id,
        "demo_sections": [s["line_no"] for s in seed.DEMO_SECTIONS],
        "next_steps": [
            "GET /api/render 查看接好的整图（2 条长线 + 1 个闭环）",
            "POST /api/batches 用 Swagger 示例体验合格/失败换片",
            "GET /api/sections/{line_no}/extract 分段取数"
            f"（演示线号：{', '.join(s['line_no'] for s in seed.DEMO_SECTIONS)}）",
        ],
    }
