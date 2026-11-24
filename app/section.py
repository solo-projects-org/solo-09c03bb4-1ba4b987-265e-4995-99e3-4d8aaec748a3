"""断面测线：登记校验、与各片折线求交、按里程分段取数。

里程定义
--------
测线为水平或竖直的直线段，起点里程为 0，终点里程为线段长度
（整数坐标，因此 ``0 <= 里程 <= total_length`` 且两端点为整数）。
方向由起点指向终点：水平线从左到右或从右到左，竖直线从下到上或从上到下。

分段约定
--------
取数请求给定整数里程范围 ``[start_mileage, end_mileage)``：
* **每段含起点**（``start_mileage`` 属于本段）；
* 只有本段一直取到测线末端（``end_mileage == total_length``）时才含终点，
  否则 ``end_mileage`` 归下一段，避免相邻两段重复报同一个边界点；
* ``range.start_inclusive`` / ``range.end_inclusive`` 标明本段的实际范围；
  重合区间的 ``end_inclusive`` 另据其自身末端是否落在本段内给出，
  缺片区间按片的半开覆盖给出（片边界属于相邻片，故末端不含），
  两者都保证相邻两段合起来恰好覆盖一次交点/区间端点。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable

from .geometry import Grid

ORIENTATIONS = ("horizontal", "vertical")


class SectionError(ValueError):
    """测线登记参数不合法（带稳定错误码，便于 API 返回 422）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SectionLine:
    """一条已通过几何校验的水平/竖直测线。"""

    orientation: str
    start: tuple[int, int]
    end: tuple[int, int]
    grid: Grid

    # ------------------------------------------------------------------
    @property
    def total_length(self) -> int:
        (x1, y1), (x2, y2) = self.start, self.end
        return abs(x2 - x1) if self.orientation == "horizontal" else abs(y2 - y1)

    @property
    def direction(self) -> int:
        """沿测线里程增大的坐标方向：+1 或 -1。"""
        if self.orientation == "horizontal":
            return 1 if self.end[0] > self.start[0] else -1
        return 1 if self.end[1] > self.start[1] else -1

    @property
    def constant(self) -> int:
        """水平线的 y（或竖直线的 x）。"""
        return self.start[1] if self.orientation == "horizontal" else self.start[0]

    def mileage_of(self, x: int | Fraction, y: int | Fraction) -> Fraction:
        """坐标点到起点的有向里程（水平线只看 x，竖直线只看 y）。"""
        if self.orientation == "horizontal":
            return (Fraction(x) - self.start[0]) * self.direction
        return (Fraction(y) - self.start[1]) * self.direction

    # ------------------------------------------------------------------
    def involved_tiles(self) -> list[tuple[int, int]]:
        """测线与片（闭集）相交的全部片号，按坐标顺序排列。

        测线不沿片边，因此水平线只落在同一行片内、竖直线只落在同一列片内。
        端点恰好落在内缝上时，缝两侧的片都会列入（后者只被端点触及，
        其里程区间长度为 0，只为采集该点的交点）。
        """
        s = self.grid.tile_size
        width = self.grid.cols * s
        height = self.grid.rows * s
        if self.orientation == "horizontal":
            row = self.start[1] // s  # y 不落在片边上，唯一
            xs = sorted((self.start[0], self.end[0]))
            c0, c1 = xs[0] // s, xs[1] // s
            if xs[0] % s == 0 and xs[0] > 0:
                c0 -= 1
            if xs[1] % s == 0 and xs[1] < width:
                c1 += 1
            return [(row, c) for c in range(max(c0, 0), min(c1, self.grid.cols - 1) + 1)]
        col = self.start[0] // s  # x 不落在片边上，唯一
        ys = sorted((self.start[1], self.end[1]))
        r0, r1 = ys[0] // s, ys[1] // s
        if ys[0] % s == 0 and ys[0] > 0:
            r0 -= 1
        if ys[1] % s == 0 and ys[1] < height:
            r1 += 1
        return [(r, col) for r in range(max(r0, 0), min(r1, self.grid.rows - 1) + 1)]

    def tile_mileage_interval(
        self, row: int, col: int
    ) -> tuple[Fraction, Fraction] | None:
        """测线落在某片内的里程区间（与被测线端点裁剪后）。

        只在片边界接触（区间长度为 0）时返回 ``None``。
        """
        x_min, y_min, x_max, y_max = self.grid.tile_bounds(row, col)
        if self.orientation == "horizontal":
            if not (y_min <= self.constant < y_max):
                return None
            span_lo = min(self.start[0], self.end[0])
            span_hi = max(self.start[0], self.end[0])
            lo = max(x_min, span_lo)
            hi = min(x_max, span_hi)
            if hi <= lo:
                return None
            y = self.constant
            t1 = self.mileage_of(lo, y)
            t2 = self.mileage_of(hi, y)
        else:
            if not (x_min <= self.constant < x_max):
                return None
            span_lo = min(self.start[1], self.end[1])
            span_hi = max(self.start[1], self.end[1])
            lo = max(y_min, span_lo)
            hi = min(y_max, span_hi)
            if hi <= lo:
                return None
            x = self.constant
            t1 = self.mileage_of(x, lo)
            t2 = self.mileage_of(x, hi)
        return (min(t1, t2), max(t1, t2))


def build_section_line(
    grid: Grid, orientation: str, start: Iterable[int], end: Iterable[int]
) -> SectionLine:
    """校验登记参数并构造测线；不合法时抛 :class:`SectionError`。"""
    if orientation not in ORIENTATIONS:
        raise SectionError("invalid_orientation", "orientation 必须为 horizontal 或 vertical")
    try:
        x1, y1 = (int(start[0]), int(start[1]))
        x2, y2 = (int(end[0]), int(end[1]))
    except (TypeError, ValueError, IndexError):
        raise SectionError("invalid_point", "起终点必须是两个整数坐标") from None

    if orientation == "horizontal":
        if y1 != y2:
            raise SectionError("not_axis_aligned", "水平测线的起点与终点 y 必须相同")
        if x1 == x2:
            raise SectionError("zero_length", "测线长度必须大于 0")
    else:
        if x1 != x2:
            raise SectionError("not_axis_aligned", "竖直测线的起点与终点 x 必须相同")
        if y1 == y2:
            raise SectionError("zero_length", "测线长度必须大于 0")

    width = grid.cols * grid.tile_size
    height = grid.rows * grid.tile_size
    if not (0 <= x1 <= width and 0 <= y1 <= height and 0 <= x2 <= width and 0 <= y2 <= height):
        raise SectionError(
            "point_outside_grid", f"测线起终点必须位于图幅 [0,{width}]x[0,{height}] 内"
        )

    s = grid.tile_size
    if orientation == "horizontal" and y1 % s == 0:
        raise SectionError(
            "line_on_tile_edge", f"水平测线 y={y1} 压在片边上，请改用不沿片边的 y"
        )
    if orientation == "vertical" and x1 % s == 0:
        raise SectionError(
            "line_on_tile_edge", f"竖直测线 x={x1} 压在片边上，请改用不沿片边的 x"
        )

    return SectionLine(orientation, (x1, y1), (x2, y2), grid)


# ----------------------------------------------------------------------
# 与折线的求交
# ----------------------------------------------------------------------
def _segment_crossings(
    line: SectionLine, a: tuple[int, int], b: tuple[int, int]
) -> list[Fraction]:
    """折线段与测线的横截交点里程（不含与测线共线的段）。"""
    ax, ay = a
    bx, by = b
    if line.orientation == "horizontal":
        y = line.constant
        if ay == by:
            return []  # 水平段：要么共线（重合区间），要么平行无交点
        if not (min(ay, by) <= y <= max(ay, by)):
            return []
        x = ax + Fraction((bx - ax) * (y - ay), (by - ay))
        return [line.mileage_of(x, y)]
    x = line.constant
    if ax == bx:
        return []
    if not (min(ax, bx) <= x <= max(ax, bx)):
        return []
    y = ay + Fraction((by - ay) * (x - ax), (bx - ax))
    return [line.mileage_of(x, y)]


def _segment_overlap(
    line: SectionLine, a: tuple[int, int], b: tuple[int, int]
) -> tuple[Fraction, Fraction] | None:
    """折线段与测线共线时的重合里程区间（正长度）。"""
    ax, ay = a
    bx, by = b
    if line.orientation == "horizontal":
        if ay != by or ay != line.constant or ax == bx:
            return None
        t1 = line.mileage_of(ax, ay)
        t2 = line.mileage_of(bx, by)
    else:
        if ax != bx or ax != line.constant or ay == by:
            return None
        t1 = line.mileage_of(ax, ay)
        t2 = line.mileage_of(bx, by)
    return (min(t1, t2), max(t1, t2))


def _merge_intervals(
    intervals: Iterable[tuple[Fraction, Fraction]]
) -> list[tuple[Fraction, Fraction]]:
    """同水深区间排序合并：相接（端点相等）或相叠都并成一段。"""
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end in sorted(intervals):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _clip_interval(
    interval: tuple[Fraction, Fraction],
    start_mileage: int,
    end_mileage: int,
) -> tuple[Fraction, Fraction, bool] | None:
    """把区间截取到本段，返回 ``(start, end, end_inclusive)``。

    * 区间起点落在段内或段首：``start_inclusive`` 恒为 true（每段含起点）；
    * 区间末端不超过本段右端（自然终止，含恰好等于右端）时含终点；
    * 被本段右端截断的区间，末端归下一段，由下一段作为起点覆盖。
    """
    start, end = interval
    clipped_start = max(start, Fraction(start_mileage))
    clipped_end = min(end, Fraction(end_mileage))
    if clipped_end <= clipped_start:
        return None
    return clipped_start, clipped_end, end <= end_mileage


def extract_section(
    line: SectionLine,
    tiles: list[dict],
    start_mileage: int,
    end_mileage: int,
) -> dict:
    """按里程范围从已固定的片数据中提取交点、重合区间与未交片区间。

    ``tiles`` 元素：``{"row", "col", "missing", "polylines": [{"depth", "points"}]}``。
    只使用传入的片（调用方只查与测线相交的片）；``missing=True`` 表示登记时缺片，
    其覆盖的测线里程会进入 ``gaps``。
    """
    intersection_keys: set[tuple[Fraction, int]] = set()
    overlaps_by_depth: dict[int, list[tuple[Fraction, Fraction]]] = defaultdict(list)
    missing_intervals: list[tuple[Fraction, Fraction]] = []
    total_length = line.total_length

    for tile in tiles:
        if tile.get("missing"):
            interval = line.tile_mileage_interval(tile["row"], tile["col"])
            if interval is not None:
                missing_intervals.append(interval)
            continue
        for pl in tile.get("polylines", []):
            depth = int(pl["depth"])
            pts = [(int(p[0]), int(p[1])) for p in pl["points"]]
            for a, b in zip(pts, pts[1:]):
                for t in _segment_crossings(line, a, b):
                    # 测线端点可能落在片内部：只保留落在测线实际跨度内的交点，
                    # 排除与测线延长线相交的点
                    if 0 <= t <= total_length:
                        intersection_keys.add((t, depth))
                overlap = _segment_overlap(line, a, b)
                if overlap is not None:
                    # 同理把共线段截到测线跨度内
                    start = max(overlap[0], Fraction(0))
                    end = min(overlap[1], Fraction(total_length))
                    if end > start:
                        overlaps_by_depth[depth].append((start, end))

    intersections = []
    for t, depth in sorted(intersection_keys, key=lambda k: (k[0], k[1])):
        if t < start_mileage:
            continue
        # 本段不含终点，除非取到测线末端（此时终点属于本段）
        if t > end_mileage or (t == end_mileage and end_mileage != line.total_length):
            continue
        intersections.append({"mileage": _json_number(t), "depth": depth})

    overlaps: list[dict] = []
    for depth in sorted(overlaps_by_depth):
        for start, end in _merge_intervals(overlaps_by_depth[depth]):
            clipped = _clip_interval((start, end), start_mileage, end_mileage)
            if clipped is None:
                continue
            c_start, c_end, end_inclusive = clipped
            overlaps.append(
                {
                    "depth": depth,
                    "start_mileage": _json_number(c_start),
                    "end_mileage": _json_number(c_end),
                    "start_inclusive": True,
                    "end_inclusive": end_inclusive,
                }
            )
    overlaps.sort(key=lambda x: (x["start_mileage"], x["depth"], x["end_mileage"]))

    gaps: list[dict] = []
    for start, end in _merge_intervals(missing_intervals):
        clipped = _clip_interval((start, end), start_mileage, end_mileage)
        if clipped is None:
            continue
        c_start, c_end, end_inclusive = clipped
        gaps.append(
            {
                "start_mileage": _json_number(c_start),
                "end_mileage": _json_number(c_end),
                "start_inclusive": True,
                "end_inclusive": end_inclusive,
                "reason": "tile_missing",
            }
        )

    return {
        "range": {
            "start_mileage": start_mileage,
            "end_mileage": end_mileage,
            "start_inclusive": True,
            "end_inclusive": end_mileage == line.total_length,
        },
        "intersections": intersections,
        "overlaps": overlaps,
        "gaps": gaps,
    }


def _json_number(value: Fraction) -> int | float:
    """整数里程输出 int，非整数交点里程输出 float（内部始终用分数精确去重）。"""
    if value.denominator == 1:
        return int(value)
    return float(value)
