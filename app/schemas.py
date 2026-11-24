"""请求/响应模型，含 Swagger UI 可直接使用的样例。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class PolylineIn(BaseModel):
    depth: int = Field(..., description="等深线水深（整数）", examples=[5])
    points: list[list[int]] = Field(
        ...,
        description=(
            "整数坐标折线；首尾端点落在片边且避开角点，"
            "中间点严格位于片内，相邻点不得重合"
        ),
        examples=[[[0, 30], [40, 35], [100, 30]]],
    )

    @model_validator(mode="after")
    def _check_points(self) -> "PolylineIn":
        if len(self.points) < 2:
            raise ValueError("折线至少包含 2 个点")
        for p in self.points:
            if len(p) != 2:
                raise ValueError("每个点必须是 [x, y] 两个整数")
        return self


class TileIn(BaseModel):
    row: int = Field(..., ge=0, description="片行号，自下而上从 0 开始")
    col: int = Field(..., ge=0, description="片列号，自左而右从 0 开始")
    polylines: list[PolylineIn] = Field(..., min_length=1, description="本片提交的带水深折线（至少 1 条）")


class GridIn(BaseModel):
    rows: int = Field(..., gt=0, examples=[2])
    cols: int = Field(..., gt=0, examples=[2])
    tile_size: int = Field(..., gt=0, description="方片边长（整数坐标单位）", examples=[100])
    reset: bool = Field(
        False, description="true 时清空已有数据后重建（仅 /admin 演示接口使用）"
    )


class BatchIn(BaseModel):
    reason: str | None = Field(None, description="换批说明（可选）")
    tiles: list[TileIn] = Field(..., min_length=1, description="本批一起换的片")

    @model_validator(mode="after")
    def _unique_tiles(self) -> "BatchIn":
        ids = [(t.row, t.col) for t in self.tiles]
        if len(ids) != len(set(ids)):
            raise ValueError("同一批中片号不得重复")
        return self


class FeatureOut(BaseModel):
    depth: int
    closed: bool
    points: list[list[int]]
    source_tiles: list[list[int]]


class RenderOut(BaseModel):
    status: Literal["ok", "seam_errors"]
    grid: dict | None
    tile_count: int
    features: list[FeatureOut]
    seam_errors: list[dict]


# ----------------------------------------------------------------------
# 断面取数
# ----------------------------------------------------------------------
class SectionIn(BaseModel):
    line_no: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[\w\-.]+$",
        description="测线号（图幅内唯一；重复登记返回 409）",
        examples=["S-01"],
    )
    orientation: Literal["horizontal", "vertical"] = Field(
        ..., description="horizontal 水平（y 相同）或 vertical 竖直（x 相同）"
    )
    start: list[int] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="起点整数坐标 [x, y]，须在图幅内；里程 0 由此起算",
        examples=[[0, 25]],
    )
    end: list[int] = Field(
        ...,
        min_length=2,
        max_length=2,
        description="终点整数坐标 [x, y]，须在图幅内；测线不得压在片边上",
        examples=[[200, 25]],
    )

    @model_validator(mode="after")
    def _orientation_matches(self) -> "SectionIn":
        (x1, y1), (x2, y2) = self.start, self.end
        if self.orientation == "horizontal" and y1 != y2:
            raise ValueError("水平测线要求起终点 y 相同")
        if self.orientation == "vertical" and x1 != x2:
            raise ValueError("竖直测线要求起终点 x 相同")
        if (x1, y1) == (x2, y2):
            raise ValueError("起终点不得重合（测线长度必须大于 0）")
        return self


class SectionRangeOut(BaseModel):
    start_mileage: int
    end_mileage: int
    start_inclusive: bool = Field(True, description="本段是否含起点里程")
    end_inclusive: bool = Field(
        ..., description="本段是否含终点里程（仅当取到测线末端时为 true）"
    )


class IntersectionOut(BaseModel):
    mileage: int | float = Field(..., description="交点里程（距起点，整数或分数里程）")
    depth: int


class OverlapOut(BaseModel):
    depth: int
    start_mileage: int | float
    end_mileage: int | float
    start_inclusive: bool = True
    end_inclusive: bool


class GapOut(BaseModel):
    start_mileage: int | float
    end_mileage: int | float
    start_inclusive: bool = True
    end_inclusive: bool
    reason: Literal["tile_missing"] = "tile_missing"


class PinnedTileOut(BaseModel):
    row: int
    col: int
    batch_id: int | None
    missing: bool


class SectionRegistrationOut(BaseModel):
    status: Literal["registered"] = "registered"
    section_id: int
    line_no: str
    orientation: Literal["horizontal", "vertical"]
    start: list[int]
    end: list[int]
    total_length: int
    grid: dict
    pinned_tiles: list[PinnedTileOut]
    missing_tiles: list[list[int]]


class SectionExtractOut(BaseModel):
    line_no: str
    orientation: Literal["horizontal", "vertical"]
    start: list[int]
    end: list[int]
    total_length: int
    range: SectionRangeOut
    pinned_tiles: list[PinnedTileOut]
    intersections: list[IntersectionOut]
    overlaps: list[OverlapOut]
    gaps: list[GapOut]
