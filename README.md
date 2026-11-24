# 港池等深线分片测图服务

测绘组按**固定方片网格**分片绘制港池等深线，制图员通过本服务取回接好的整图。
FastAPI + SQLite（WAL）实现，支持整批换片、验缝、出图拼接与并发快照。

## 1. 领域规则与实现对照

| 业务要求 | 实现位置 |
| --- | --- |
| 矩形图幅按固定方片网格划分，整数坐标 | `app/geometry.py` `Grid`；`POST /api/grid` 一次性定义，有数据后不可改 |
| 每片提交带水深的整数坐标折线 | `app/schemas.py` `PolylineIn`（非整数坐标在 Pydantic 层拒绝） |
| 折线两端落在片边、避开角点，其余点严格在片内 | `validate_tile_polylines`：`endpoint_not_on_edge` / `interior_point_on_edge` |
| 内缝同一水深、同一位置端点两侧各一个；外边可留端点 | `validate_batch`：只枚举本批两侧都换的内缝，按 `(缝, 位置, 水深)` 配对，错误码 `seam_count_mismatch` |
| 按整批换完的图验缝，**不读未换片折线明细** | 验缝只入参本批 `tiles` + 静态网格；批界缝单侧端点放行，留到出图核对 |
| 接不上指出接缝，换片失败继续用旧图 | 失败返回 HTTP 422 + 接缝坐标/片号/水深，事务回滚；`batches` 表留 `rejected` 审计记录 |
| 出图接成两端位于图幅外边的长线或闭环，每条原折线恰用一次 | `app/assembly.py` 半边图行走；`used == edges` 自检断言保证不重不漏 |
| 换片不等旧图出完，正在出图的请求继续用原版 | SQLite WAL + 单读事务快照，出图期间提交的换片不影响本次结果（见 `tests/test_concurrency.py`） |
| 登记不沿片边的水平/竖直断面测线，起终点为图幅内整数坐标 | `POST /api/sections`；`app/section.py` `build_section_line`（`line_on_tile_edge` / `point_outside_grid` / `not_axis_aligned`） |
| 按测线号 + 距起点整数里程范围取数，返回交点里程、重合区间、未交片区间 | `GET /api/sections/{line_no}/extract`；`extract_section` |
| 每段含起点，只有到测线末端的一段含终点 | `_clip_interval` + `range.start_inclusive` / `range.end_inclusive` |
| 同里程同水深的交点只报一次；同水深重合区间相接或相叠就合并 | 交点按 `(里程, 水深)` 去重；`_merge_intervals` 后按本段截取 |
| 登记时按同一版图幅固定所涉各片版本与缺片状态 | `sections` / `section_tiles` 表；`BEGIN IMMEDIATE` 内读取当前批次并写入 |
| 换片或重启后原测线号仍按登记时数据取数 | 取数只读 `section_tiles.batch_id` 指向的版本，与 `current_tiles` 无关 |
| 折线明细只读与测线相交的片，取数期间照常换片 | `section_snapshot` 只 JOIN 登记片；单读事务快照，不阻塞换片 |

坐标约定：`x` 向右、`y` 向上；片号 `(row, col)`，`row` 自下而上、`col` 自左而右，均从 0 开始。

## 2. 目录结构

```
app/
  main.py       FastAPI 路由与事务边界
  geometry.py   网格、单片几何校验、整批验缝
  assembly.py   整图接图（长线/闭环）与批界缝核对
  section.py    断面测线登记校验、与折线求交、按里程分段取数
  db.py         SQLite 持久层（WAL、版本化、快照读、测线版本固定）
  schemas.py    Pydantic 模型
  seed.py       Swagger 演示数据（2x2：2 条长线 + 1 个菱形闭环 + 2 条演示测线）
tests/          54 个 pytest 用例
Dockerfile, docker-compose.yml, requirements*.txt
```

## 3. Docker Compose 启动

```bash
docker compose up --build
# 服务：http://localhost:8000
# Swagger：http://localhost:8000/docs   （可直接 Try it out）
# 数据库：命名卷 soundings-data 内的 /data/soundings.db
```

## 4. Swagger 操作样例（也可用 curl 复现）

1. `POST /admin/seed` —— 一键载入 2x2 演示图幅（清空重建，仅演示用），
   同时登记两条演示测线 `S-H-25`（水平，y=25）与 `S-V-45`（竖直，x=45）。
2. `GET /api/render` —— 出图：水深 3、5 各一条横贯图幅的长线，水深 10 一个跨四片闭环。
3. `POST /api/batches` —— 请求体下拉选择内置示例：
   * **合格样例-上排修测**：只换上排两片，批内缝匹配、批界缝沿用旧端点，返回 200；
   * **失败样例-接缝对不上**：竖缝上水深 3 端点两侧位置不一致，返回 422 并指出接缝，旧图不变。
4. `GET /api/tiles` —— 各片当前版本（只给条数与批次号，**不返回折线明细**）。
5. `GET /api/batches` —— 全部换批记录，含被拒绝批次的接缝错误。
6. `POST /api/sections` —— 登记断面测线（请求体下拉选择“水平/竖直”两条演示线）。
7. `GET /api/sections/{line_no}/extract` —— 分段取数，见第 5 节。
8. `GET /api/sections` —— 已登记测线及其固定住的片版本/缺片状态。

curl 速览：

```bash
curl -X POST localhost:8000/admin/seed
curl localhost:8000/api/render | jq
curl -X POST localhost:8000/api/batches -H 'Content-Type: application/json' \
  -d '{"tiles":[{"row":0,"col":0,"polylines":[{"depth":3,"points":[[0,20],[50,30],[100,20]]}]},
               {"row":0,"col":1,"polylines":[{"depth":3,"points":[[100,25],[150,20],[200,20]]}]}]}'
# -> 422，errors 中给出 seam=["v",0,0]、point、depth、两侧片号

# 登记一条水平测线（不沿片边），再从 0 到 100 里程取一段
curl -X POST localhost:8000/api/sections -H 'Content-Type: application/json' \
  -d '{"line_no":"S-01","orientation":"horizontal","start":[0,25],"end":[200,25]}'
curl 'localhost:8000/api/sections/S-01/extract?start_mileage=0&end_mileage=100' | jq
```

## 5. 断面取数

### 5.1 登记：`POST /api/sections`

```json
{"line_no": "S-01", "orientation": "horizontal", "start": [0, 25], "end": [200, 25]}
```

* `orientation` 为 `horizontal`（起终点 y 相同）或 `vertical`（x 相同），
  起终点须为图幅内整数坐标，长度大于 0；
* 测线**不得压在片边上**（`y` 或 `x` 恰为片边长整数倍时返回 422 `line_on_tile_edge`）；
* 测线号在图幅内唯一，重复登记返回 409；
* 登记时在 `BEGIN IMMEDIATE` 事务内读取当前版本，把测线所涉各片的
  `batch_id` 与缺片状态写入 `section_tiles`（`GET /api/sections` 可查）。
  所涉片 = 测线与该片闭集相交的片；端点恰在内缝上时缝两侧片都会固定。
  从未换过的片记为缺片（`missing=true`、`batch_id=null`）。

### 5.2 取数：`GET /api/sections/{line_no}/extract?start_mileage=&end_mileage=`

里程自起点 0 起算，沿起点指向终点增加；终点里程 = 线段长度。
返回：

| 字段 | 含义 |
| --- | --- |
| `intersections[]` | `{mileage, depth}`：折线（非共线段）与测线的横截交点；同里程同水深只报一次 |
| `overlaps[]` | `{depth, start_mileage, end_mileage, start_inclusive, end_inclusive}`：折线与测线共线的重合区间；同水深相接或相叠先合并，再按本段截取 |
| `gaps[]` | `{start_mileage, end_mileage, ..., reason:"tile_missing"}`：登记时缺片、无法交片的测线区间 |
| `range` | 本段范围及 `start_inclusive` / `end_inclusive` |
| `pinned_tiles[]` | 取数实际使用的片版本（登记时固定，不随换片变化） |

分段约定：每段含起点（`start_mileage` 属于本段）；相邻两段共享的边界点
只由后一段报告；只有取到测线末端的一段 `end_inclusive=true`。

示例（seed 数据，`S-H-25` 为 y=25 横穿两列，`S-V-45` 为 x=45 纵穿两行）：

```bash
curl 'localhost:8000/api/sections/S-H-25/extract?start_mileage=0&end_mileage=100'
# -> intersections [{mileage:40,depth:3}]，range.end_inclusive=false
curl 'localhost:8000/api/sections/S-H-25/extract?start_mileage=100&end_mileage=200'
# -> range.end_inclusive=true
```

版本固定与并发：

* 取数只 JOIN `section_tiles` 中登记的片与 `batch_id`，不读 `current_tiles`，
  因此换片、缺片、重启都不改变原测线号的取数结果；
* 各片折线明细只按登记片号读取，与测线不相交的片完全不会被读；
* 取数在单个只读事务内完成快照，期间可照常 `POST /api/batches`，互不阻塞。

## 6. 出图结果说明

`GET /api/render` 返回：

* `status`：`ok` 或 `seam_errors`（新片与未换旧片在批界缝接不上时）；
* `features[]`：`depth`、`closed`、`points`、`source_tiles`；
  * 长线 `closed=false`，首尾点必在图幅外边；
  * 闭环 `closed=true`，点序列首尾重复表示闭合；
* `seam_errors[]`：接不上的节点，含坐标、水深、涉及片号（`seam_dangling` / `seam_collision` / `outer_endpoint_conflict`）。
  即便有错也会返回可拼接部分，且不丢任何原折线。

## 7. 本地开发与自测

```bash
python3 -m venv .venv && source .venv/bin/activate   # 或自行准备解释器
pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=. pytest -q
uvicorn app.main:app --reload
```

## 8. 数据模型（SQLite）

* `grid_meta`：唯一网格定义；
* `batches`：换批记录，`status` 为 `committed` / `rejected`，拒绝时存错误 JSON；
* `tile_revisions`：折线版本表，**只增不删**；
* `current_tiles`：每片当前指向的批次版本（UPSERT）；
* `sections`：登记的断面测线（测线号唯一，含起终点、方向、登记时网格参数）；
* `section_tiles`：登记时固定下来的测线所涉片及其批次版本；`batch_id` 为空
  表示登记时缺片，取数时该片覆盖的里程进入 `gaps`。

换片在单事务内完成：任一片写入失败或进程中断，整批回滚，当前版本指针不变。
测线取数只依据 `sections` / `section_tiles` 固定的版本，不随 `current_tiles` 变化。
