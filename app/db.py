"""SQLite 持久层。

设计要点
--------
* WAL 模式 + 每次请求独立连接：读不阻塞写、写不阻塞读；
* 折线按“版本”保存（``tile_revisions`` 只增不删），``current_tiles`` 指向当前版本，
  换片失败时事务整体回滚，旧图原封不动；
* 出图在单个读事务内取齐全部当前版本，得到一致快照；
  换片写入不会等待已经开始的出图，正在出图的请求继续读到原版。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    rows INTEGER NOT NULL,
    cols INTEGER NOT NULL,
    tile_size INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL CHECK (status IN ('committed', 'rejected')),
    reason TEXT,
    tile_count INTEGER NOT NULL,
    errors TEXT
);
CREATE TABLE IF NOT EXISTS tile_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    points TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id)
);
CREATE INDEX IF NOT EXISTS idx_rev_tile ON tile_revisions(tile_row, tile_col, id);
CREATE TABLE IF NOT EXISTS current_tiles (
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (tile_row, tile_col)
);
CREATE TABLE IF NOT EXISTS sections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_no TEXT NOT NULL UNIQUE,
    orientation TEXT NOT NULL CHECK (orientation IN ('horizontal', 'vertical')),
    start_x INTEGER NOT NULL,
    start_y INTEGER NOT NULL,
    end_x INTEGER NOT NULL,
    end_y INTEGER NOT NULL,
    total_length INTEGER NOT NULL,
    grid_rows INTEGER NOT NULL,
    grid_cols INTEGER NOT NULL,
    tile_size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS section_tiles (
    section_id INTEGER NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    tile_row INTEGER NOT NULL,
    tile_col INTEGER NOT NULL,
    batch_id INTEGER REFERENCES batches(id),
    missing INTEGER NOT NULL DEFAULT 0 CHECK (missing IN (0, 1)),
    PRIMARY KEY (section_id, tile_row, tile_col)
);
"""


class DuplicateSectionError(ValueError):
    """测线号已登记。"""

    def __init__(self, line_no: str) -> None:
        super().__init__(f"测线号 {line_no} 已登记")
        self.line_no = line_no


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    def get_grid(self, conn: sqlite3.Connection) -> dict[str, int] | None:
        row = conn.execute(
            "SELECT rows, cols, tile_size FROM grid_meta WHERE id = 1"
        ).fetchone()
        return dict(row) if row else None

    def set_grid(self, conn: sqlite3.Connection, rows: int, cols: int,
                 tile_size: int) -> dict[str, int]:
        existing = self.get_grid(conn)
        if existing is not None:
            if (existing["rows"], existing["cols"], existing["tile_size"]) != (
                rows,
                cols,
                tile_size,
            ):
                raise ValueError("网格已存在且参数不同；不允许在有数据时改网格")
            return existing
        conn.execute(
            "INSERT INTO grid_meta (id, rows, cols, tile_size) VALUES (1, ?, ?, ?)",
            (rows, cols, tile_size),
        )
        return {"rows": rows, "cols": cols, "tile_size": tile_size}

    def has_tiles(self, conn: sqlite3.Connection) -> bool:
        return conn.execute(
            "SELECT 1 FROM current_tiles LIMIT 1"
        ).fetchone() is not None

    # ------------------------------------------------------------------
    def commit_batch(
        self,
        conn: sqlite3.Connection,
        tiles: list[dict],
        reason: str | None,
    ) -> int:
        """在调用方事务内写入一整批新片，返回批次号。"""
        cur = conn.execute(
            "INSERT INTO batches (status, reason, tile_count, errors) "
            "VALUES ('committed', ?, ?, NULL)",
            (reason, len(tiles)),
        )
        batch_id = cur.lastrowid
        for t in tiles:
            row, col = t["row"], t["col"]
            for seq, pl in enumerate(t["polylines"]):
                conn.execute(
                    "INSERT INTO tile_revisions "
                    "(tile_row, tile_col, depth, seq, points, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row,
                        col,
                        pl["depth"],
                        seq,
                        json.dumps(pl["points"], separators=(",", ":")),
                        batch_id,
                    ),
                )
            conn.execute(
                "INSERT INTO current_tiles (tile_row, tile_col, batch_id) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(tile_row, tile_col) DO UPDATE SET "
                "batch_id=excluded.batch_id, updated_at=datetime('now')",
                (row, col, batch_id),
            )
        return batch_id

    def record_rejected_batch(
        self, conn: sqlite3.Connection, tile_count: int, reason: str | None,
        errors: list[dict],
    ) -> int:
        cur = conn.execute(
            "INSERT INTO batches (status, reason, tile_count, errors) "
            "VALUES ('rejected', ?, ?, ?)",
            (reason, tile_count, json.dumps(errors, ensure_ascii=False)),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------
    # 断面测线登记与取数
    # ------------------------------------------------------------------
    def current_tile_batch_ids(
        self, conn: sqlite3.Connection, tile_ids: list[tuple[int, int]]
    ) -> dict[tuple[int, int], int]:
        """只查询指定片号当前的批次版本（不读折线明细，缺片不在结果中）。"""
        if not tile_ids:
            return {}
        where = " OR ".join("(tile_row = ? AND tile_col = ?)" for _ in tile_ids)
        params = [v for tid in tile_ids for v in tid]
        rows = conn.execute(
            "SELECT tile_row AS row, tile_col AS col, batch_id "
            f"FROM current_tiles WHERE {where}",
            params,
        ).fetchall()
        return {(r["row"], r["col"]): r["batch_id"] for r in rows}

    def register_section(
        self,
        conn: sqlite3.Connection,
        line_no: str,
        line: dict,
        tiles: list[dict],
        grid: dict,
    ) -> int:
        """登记测线，并固定其所涉各片当前的版本与本版缺片状态。

        同一测线号重复登记抛 :class:`DuplicateSectionError`。
        ``tiles`` 元素：``{"row", "col", "batch_id": int | None}``，
        ``batch_id`` 为 ``None`` 表示登记时该片缺失。
        """
        if self.get_section(conn, line_no) is not None:
            raise DuplicateSectionError(line_no)
        try:
            cur = conn.execute(
                """
                INSERT INTO sections (
                    line_no, orientation, start_x, start_y, end_x, end_y,
                    total_length, grid_rows, grid_cols, tile_size
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    line_no,
                    line["orientation"],
                    line["start"][0],
                    line["start"][1],
                    line["end"][0],
                    line["end"][1],
                    line["total_length"],
                    grid["rows"],
                    grid["cols"],
                    grid["tile_size"],
                ),
            )
        except sqlite3.IntegrityError:
            # 并发登记同一测线号：UNIQUE 约束兜底
            raise DuplicateSectionError(line_no) from None
        section_id = cur.lastrowid
        for t in tiles:
            conn.execute(
                "INSERT INTO section_tiles "
                "(section_id, tile_row, tile_col, batch_id, missing) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    section_id,
                    t["row"],
                    t["col"],
                    t.get("batch_id"),
                    1 if t.get("batch_id") is None else 0,
                ),
            )
        return section_id

    def get_section(self, conn: sqlite3.Connection, line_no: str) -> dict | None:
        row = conn.execute(
            "SELECT id, line_no, orientation, start_x, start_y, end_x, end_y, "
            "total_length, grid_rows, grid_cols, tile_size, created_at "
            "FROM sections WHERE line_no = ?",
            (line_no,),
        ).fetchone()
        return dict(row) if row else None

    def list_sections(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, line_no, orientation, start_x, start_y, end_x, end_y, "
            "total_length, grid_rows, grid_cols, tile_size, created_at "
            "FROM sections ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["pinned_tiles"] = [
                {
                    "row": t["row"],
                    "col": t["col"],
                    "batch_id": t["batch_id"],
                    "missing": bool(t["missing"]),
                }
                for t in conn.execute(
                    "SELECT tile_row AS row, tile_col AS col, batch_id, missing "
                    "FROM section_tiles WHERE section_id = ? "
                    "ORDER BY tile_row, tile_col",
                    (r["id"],),
                ).fetchall()
            ]
            out.append(item)
        return out

    def section_snapshot(
        self, conn: sqlite3.Connection, line_no: str
    ) -> dict | None:
        """按登记时固定的版本读取该测线所涉片的折线明细。

        只查询 ``section_tiles`` 中登记的片，且只取登记时固定的 ``batch_id``；
        其它片（包括后来换片、缺片）完全不读。缺片片不读折线明细。
        """
        section = self.get_section(conn, line_no)
        if section is None:
            return None
        rows = conn.execute(
            """
            SELECT st.tile_row AS row, st.tile_col AS col,
                   st.batch_id, st.missing,
                   r.depth, r.seq, r.points
            FROM section_tiles st
            LEFT JOIN tile_revisions r
              ON r.tile_row = st.tile_row
             AND r.tile_col = st.tile_col
             AND r.batch_id = st.batch_id
            WHERE st.section_id = ?
            ORDER BY st.tile_row, st.tile_col, r.seq
            """,
            (section["id"],),
        ).fetchall()
        tiles: dict[tuple[int, int], dict] = {}
        for r in rows:
            tile = tiles.setdefault(
                (r["row"], r["col"]),
                {
                    "row": r["row"],
                    "col": r["col"],
                    "batch_id": r["batch_id"],
                    "missing": bool(r["missing"]),
                    "polylines": [],
                },
            )
            if not r["missing"] and r["points"] is not None:
                tile["polylines"].append(
                    {"depth": r["depth"], "points": json.loads(r["points"])}
                )
        return {"section": section, "tiles": list(tiles.values())}

    # ------------------------------------------------------------------
    def current_snapshot(self, conn: sqlite3.Connection) -> dict[str, Any]:
        """单事务内读取整图当前版本的一致快照。"""
        grid = self.get_grid(conn)
        rows = conn.execute(
            """
            SELECT c.tile_row AS row, c.tile_col AS col, c.batch_id,
                   r.depth, r.seq, r.points
            FROM current_tiles c
            JOIN tile_revisions r
              ON r.tile_row = c.tile_row
             AND r.tile_col = c.tile_col
             AND r.batch_id = c.batch_id
            ORDER BY c.tile_row, c.tile_col, r.seq
            """
        ).fetchall()
        tiles: dict[tuple[int, int], dict] = {}
        for r in rows:
            tile = tiles.setdefault(
                (r["row"], r["col"]),
                {"row": r["row"], "col": r["col"], "polylines": []},
            )
            tile["polylines"].append(
                {"depth": r["depth"], "points": json.loads(r["points"])}
            )
        return {"grid": grid, "tiles": list(tiles.values())}

    def list_batches(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            "SELECT id, created_at, status, reason, tile_count, errors "
            "FROM batches ORDER BY id"
        ).fetchall()
        out = []
        for r in rows:
            item = dict(r)
            item["errors"] = json.loads(item["errors"]) if item["errors"] else []
            out.append(item)
        return out

    def list_current_tiles(self, conn: sqlite3.Connection) -> list[dict]:
        rows = conn.execute(
            """
            SELECT c.tile_row AS row, c.tile_col AS col,
                   c.batch_id, c.updated_at,
                   (SELECT COUNT(*) FROM tile_revisions r
                     WHERE r.tile_row = c.tile_row
                       AND r.tile_col = c.tile_col
                       AND r.batch_id = c.batch_id) AS polyline_count
            FROM current_tiles c
            ORDER BY c.tile_row, c.tile_col
            """
        ).fetchall()
        return [dict(r) for r in rows]

    def reset(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            "DELETE FROM section_tiles; DELETE FROM sections; "
            "DELETE FROM current_tiles; DELETE FROM tile_revisions; "
            "DELETE FROM batches; DELETE FROM grid_meta;"
        )
