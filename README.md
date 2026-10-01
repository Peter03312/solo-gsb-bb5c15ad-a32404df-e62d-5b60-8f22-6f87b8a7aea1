# 集装箱船配载方案核算服务

基于 Python + FastAPI + SQLAlchemy(SQLite) + PuLP(CBC 整数规划求解器) 实现的集装箱船配载方案核算 HTTP 服务。

录入按**堆栈（stack）与层（tier）**编号的槽位、各槽承重、纵向力臂、相邻槽位关系与全船允许力矩区间，以及箱号、重量、危险类别、卸港先后与不相容危险类别对；服务输出**每箱槽位、总力矩与方案修订号**。方案确认后，还可按岸桥数量与每箱吊装时长/时间窗生成**每箱开工、完工分钟与岸桥编号的装船时间表**，或按每个卸港的允许剩余总力矩闭区间生成**逐港卸箱顺序（各步港序、箱号与卸后剩余力矩）**。

## 一、配载约束（全部为硬性约束）

1. **承重**：任一槽位上箱的重量不得超过该槽承重；
2. **堆叠支撑**：某层有箱时，其下层（同堆栈层编号相邻的槽）必须有箱，即不允许悬空；
3. **重量次序**：下层箱不得轻于上层箱（`重量(下层) >= 重量(上层)`）；
4. **卸港先后**：早卸港箱不得压在晚卸港箱下方（`discharge_order` 小者先卸，先卸港在上、后卸港在下）；
5. **危险类别相邻**：不相容危险类别的两个箱不得占据互为相邻的槽位；
6. **总力矩**：全船总力矩 `Σ(箱重量 × 所在槽纵向力臂)` 必须落在允许区间 `[min_moment, max_moment]` 内；
7. 每个箱恰好占用一个槽，每个槽至多一箱，方案必须覆盖清单内**全部箱**。

## 二、优选规则（完整可行方案之间）

1. **最大占用层数最小**（占用到的最高 `tier` 最小）；
2. 最大占用层数相同时，**按箱号排序后取槽位编号序列字典序最小**者。

不存在完整可行方案时返回 `422` 并说明“无解”及原因，**不保存任何方案**。

## 三、修订与确认

- 槽位资料与箱清单分别维护，每次录入生成递增的**资料修订号**；
- 方案带**方案修订号**：对已确认方案再次核算得到新修订（`revision_no + 1`），同一方案链以 `root_id` 关联；
- 确认方案时必须提交该方案参与的**槽位资料修订号与箱清单修订号**，服务再与当前最新修订核对：
  - 提交修订与方案修订不一致，或资料已产生更新修订（过期）→ 返回 `409` 拒绝，**原方案保持不变**；
- 并发确认通过 SQLite `BEGIN IMMEDIATE` 写锁串行化 + `confirmed_slots(slot_code UNIQUE)` 唯一索引兜底，**不会产生冲突的已确认槽位占用**（一个槽位同一时刻至多属于一个已确认方案）；
- 新修订确认后，同一方案链的旧已确认修订变为 `superseded` 并释放槽位占用。

## 四、装船时间表（岸桥调度）

方案**确认后**，按方案 ID 提交岸桥数量与每箱吊装参数，服务输出绑定该**方案修订**的可执行装船时间表。

### 提交字段（`POST /api/plans/{plan_id}/schedules`）

| 字段 | 含义 |
| --- | --- |
| `crane_count` | 岸桥数量，**正整数** |
| `entries[]` | 每箱一条，箱项必须与方案修订**完全一致**（缺失或多余均拒绝） |
| `entries[].no` | 箱号 |
| `entries[].duration` | 该箱吊装时长（分钟），**正整数** |
| `entries[].release_minute` | 最早开工分钟，非负整数 |
| `entries[].deadline_minute` | 最迟完工分钟，非负整数；`release <= deadline`，允许恰在此时刻结束 |

### 排程硬约束

1. 每箱恰好由一台岸桥作业一次；
2. 同一岸桥作业区间 `[开工, 完工)` 不得重叠（允许首尾相接）；
3. 同堆栈**下层箱完工后上层箱才可开工**（按配载方案已占用层自底向上）；
4. 每箱满足自身时间窗：`release_minute <= 开工` 且 `完工 <= deadline_minute`。

### 优选规则

1. **全部箱最晚完工时间（makespan）最早**；
2. makespan 相同的方案间，**按箱号升序使 `(开工分钟, 岸桥编号)` 序列字典序最小**
   （同一箱先比开工、再取最小编号岸桥）。

### 保存与生命周期

- 仅 `confirmed` 方案可生成排程；`draft`/`superseded` 返回 `409`；
- 箱项缺失/多余、时间窗倒置等入参问题返回 `422`；时间窗/岸桥能力无解返回 `422` 且**不保存**；
- **保存前在写锁内再次核对方案仍为已确认**：若求解期间方案被新修订取代，返回 `409` 且不保存；
- 同一方案修订重复提交以新排程**原子替换**旧排程；
- 方案修订被新修订确认取代（`superseded`）后，其排程状态转为 **`historical`**：
  仍可通过 `GET /api/plans/{plan_id}/schedules` 或 `GET /api/schedules?historical=true` 查询，
  但不再列入默认的当前排程；
- **空箱方案**：提交 `entries: []`，返回空排程、`makespan = 0`。

### 响应示例

```json
{
  "plan_id": "f256fc9c...",
  "revision_no": 1,
  "status": "current",
  "crane_count": 2,
  "makespan": 20,
  "entries": [
    {"container_no": "C001", "start_minute": 12, "finish_minute": 20, "crane_no": 1, "stack": "A", "tier": 2},
    {"container_no": "C002", "start_minute": 0,  "finish_minute": 12, "crane_no": 1, "stack": "A", "tier": 1},
    {"container_no": "C003", "start_minute": 6,  "finish_minute": 16, "crane_no": 2, "stack": "B", "tier": 2},
    {"container_no": "C004", "start_minute": 0,  "finish_minute": 6,  "crane_no": 2, "stack": "B", "tier": 1}
  ],
  "created_at": "..."
}
```

curl 示例（请求体亦见 `examples/schedule.json`）：

```bash
# 方案确认后提交（422=箱项不一致/时间窗无解且不保存；409=方案未确认或保存前已被取代）
curl -s -X POST $BASE/plans/<plan_id>/schedules \
  -H 'Content-Type: application/json' -d @examples/schedule.json

# 查询当前排程；加 ?historical=true 可同时查历史排程
curl -s "$BASE/schedules"
curl -s "$BASE/schedules?historical=true"

# 按方案修订查询其绑定排程（历史排程同样可查，status=historical）
curl -s $BASE/plans/<plan_id>/schedules
```

## 五、逐港卸箱顺序

方案**确认后**，按方案 ID 提交**每个卸港序号**的允许剩余总力矩闭区间，服务按该方案所用**箱重与槽位力臂**逐箱重算剩余总力矩，输出绑定该**方案修订**的逐港卸箱顺序。

### 提交字段（`POST /api/plans/{plan_id}/unloading`）

| 字段 | 含义 |
| --- | --- |
| `ports[]` | 每个卸港一条；`discharge_order` 必须与该方案箱清单中的卸港序号**完全一致**（不得缺失、多余或重复），顺序可任意提交（服务内部按港序号升序处理） |
| `ports[].discharge_order` | 卸港序号（正整数，小者先卸） |
| `ports[].min_remaining_moment` | 该港允许剩余总力矩闭区间下限 |
| `ports[].max_remaining_moment` | 该港允许剩余总力矩闭区间上限；下限不得高于上限 |

### 卸箱硬约束

1. **逐港**：按卸港序号从小到大，每个卸箱位置只允许卸下属于该港的箱（每港只卸该港箱）；
2. **堆栈先后**：同堆栈上层箱未卸时不得取下下层箱（按配载方案已占用层自底向上的连续链）；
3. **逐步剩余总力矩**：每卸一箱后，以该方案箱重与槽位力臂重算
   `剩余总力矩 = 初始总力矩 − Σ已卸(箱重 × 纵向力臂)`，结果必须落入**该箱所属卸港**的允许闭区间；全部卸完后剩余力矩恒为 0（故末港区间必须包含 0）。

### 优选规则

在所有港口均可完成的卸箱顺序中，取**全程箱号序列字典序最小**者（港口顺序固定，仅在港内/各可行先后间择优）。求解采用整数规划（PuLP/CBC），分块基数编码与完整字典序最小严格等价；求解结果在服务内再以 Python 对全部硬约束逐步复核，不盲信求解器数值容差。

### 保存与生命周期

- 仅 `confirmed` 方案可规划卸箱顺序；`draft`/`superseded` 返回 `409`；
- 卸港序号不一致、区间倒置、重复港序等入参问题返回 `422`；**无可行顺序或求解无法确定同样返回 `422` 且不保存**（中文说明原因，如末港区间不含 0、堆栈先后与某港区间不相交）；
- **保存前在写锁内再次核对方案仍为已确认**：若求解期间方案被新修订确认取代，返回 `409` 且不保存；
- 同一方案修订重复提交以新顺序**原子替换**旧顺序；
- 方案修订被新修订确认取代（`superseded`）后，其卸箱顺序转为 **`historical`**：
  仍可通过 `GET /api/plans/{plan_id}/unloading` 或 `GET /api/unloading?historical=true` 查询，
  但不再列入默认的当前顺序；
- 卸箱顺序与装船排程相互独立：历史顺序/排程仍可查询，**不影响已有配载与装船排程**；
- **空箱方案**：提交 `ports: []`，返回空顺序、初始/最终力矩均为 0。

### 响应示例

```json
{
  "plan_id": "5c643ef7...",
  "revision_no": 1,
  "status": "current",
  "initial_moment": -30.0,
  "final_moment": 0.0,
  "ports": [
    {"discharge_order": 1, "min_remaining_moment": -100.0, "max_remaining_moment": -30.0},
    {"discharge_order": 2, "min_remaining_moment": -100.0, "max_remaining_moment": 100.0}
  ],
  "steps": [
    {"step_no": 1, "discharge_order": 1, "container_no": "C003",
     "slot_code": "B01", "stack": "B", "tier": 1, "weight": 10.0, "arm": 5.0,
     "remaining_moment": -80.0},
    {"step_no": 2, "discharge_order": 1, "container_no": "C001",
     "slot_code": "A02", "stack": "A", "tier": 2, "weight": 10.0, "arm": -2.0,
     "remaining_moment": -60.0},
    {"step_no": 3, "discharge_order": 2, "container_no": "C002",
     "slot_code": "A01", "stack": "A", "tier": 1, "weight": 30.0, "arm": -2.0,
     "remaining_moment": 0.0}
  ],
  "created_at": "..."
}
```

curl 示例（请求体亦见 `examples/unloading.json`）：

```bash
# 方案确认后提交（422=序号不一致/区间倒置/无可行顺序且不保存；409=未确认或保存前已被取代）
curl -s -X POST $BASE/plans/<plan_id>/unloading \
  -H 'Content-Type: application/json' -d @examples/unloading.json

# 查询当前卸箱顺序；加 ?historical=true 可同时查历史顺序
curl -s "$BASE/unloading"
curl -s "$BASE/unloading?historical=true"

# 按方案修订查询其绑定卸箱顺序（历史顺序同样可查，status=historical）
curl -s $BASE/plans/<plan_id>/unloading
```

## 六、启动

### 方式 A：Docker Compose（推荐，一键启动）

```bash
docker compose up --build -d
docker compose logs -f stowage     # 查看日志
docker compose down                # 停止（数据保留在卷 stowage-data 中）
```

镜像构建时完成依赖安装；容器入口 `scripts/entrypoint.sh` 会先幂等初始化数据库表结构，再启动 API。

### 方式 B：本地直接运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 初始化（幂等）
python -c "from app.database import init_db; init_db()"

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 方式 C：Docker

```bash
docker build -t stowage-service .
docker run -d -p 8000:8000 -v stowage-data:/app/data stowage-service
```

## 七、配置

全部配置通过环境变量覆盖（前缀 `STOWAGE_`，也可用 `.env`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STOWAGE_PORT` | `8000` | Compose 映射到宿主机的端口（仅 compose 用） |
| `STOWAGE_DATABASE_URL` | `sqlite:///./data/stowage.db` | SQLAlchemy 数据库地址；容器内固定 `sqlite:////app/data/stowage.db` |
| `STOWAGE_API_PREFIX` | `/api` | API 路由前缀 |

## 八、地址与端口

- 服务地址（本地）：`http://127.0.0.1:8000`
- 健康检查：`GET http://127.0.0.1:8000/health`
- API 前缀：`http://127.0.0.1:8000/api`
- 交互式文档（Swagger UI）：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

## 九、HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/slot-revisions` | 录入槽位资料，生成新槽位资料修订号 |
| POST | `/api/manifest-revisions` | 录入箱清单，生成新箱清单修订号 |
| GET | `/api/slot-revisions/{rev}` | 查询槽位资料修订 |
| GET | `/api/manifest-revisions/{rev}` | 查询箱清单修订 |
| POST | `/api/plans/compute` | 核算新方案（无解返回 422，不保存） |
| POST | `/api/plans/{plan_id}/amend` | 基于现有方案重新核算，生成方案修订号 |
| POST | `/api/plans/{plan_id}/confirm` | 确认方案（核对资料修订，409=过期/冲突） |
| POST | `/api/plans/{plan_id}/schedules` | 为**已确认方案修订**生成装船时间表（无解返回 422，不保存） |
| GET | `/api/plans/{plan_id}/schedules` | 查询该方案修订绑定的装船时间表（历史排程可查） |
| GET | `/api/schedules` | 列出装船时间表（默认仅当前；`?historical=true` 含历史） |
| POST | `/api/plans/{plan_id}/unloading` | 为**已确认方案修订**规划逐港卸箱顺序（无可行顺序/无法确定返回 422，不保存） |
| GET | `/api/plans/{plan_id}/unloading` | 查询该方案修订绑定的逐港卸箱顺序（历史顺序可查） |
| GET | `/api/unloading` | 列出逐港卸箱顺序（默认仅当前；`?historical=true` 含历史） |
| GET | `/api/plans` | 列出全部方案 |
| GET | `/api/plans/{plan_id}` | 查询单个方案 |

### 录入字段说明

槽位 `slots[]`：

| 字段 | 含义 |
| --- | --- |
| `code` | 槽位编号（建议定长如 `A01`，字典序即层位次序） |
| `stack` | 堆栈编号；同堆栈按 `tier` 自底向上构成堆叠关系 |
| `tier` | 层编号，自底向上从 1 开始的正整数 |
| `capacity` | 槽位承重，非负 |
| `arm` | 纵向力臂 |

`adjacency`：相邻槽位编号对（无向），如 `[["A01","B01"]]`。
`incompatible_classes`：不相容危险类别对，如 `[["1","5"]]`。

箱 `containers[]`：

| 字段 | 含义 |
| --- | --- |
| `no` | 箱号（同一清单内唯一） |
| `weight` | 重量，必须大于 0（否则“非法重量”报错） |
| `hazmat` | 危险类别字符串；普通箱传 `null` |
| `discharge_order` | 卸港先后正整数，小者先卸 |

### 错误处理

- `400/404`：业务错误（修订/方案/排程/卸箱顺序不存在等），中文 `detail`；
- `409 Conflict`：资料修订过期/不一致、重复确认、并发槽位占用冲突、对未确认方案排程或规划卸箱顺序、排程/卸箱顺序保存前方案已被新修订取代，原数据不变；
- `422`：① 录入校验失败（非法重量、重复箱号、无效槽位、重复槽位编号、力矩区间倒置、排程箱项缺失/多余、岸桥数/时长非正、时间窗倒置、卸港序号缺失/多余/重复、卸港区间倒置等）；② 无完整可行方案、装船时间窗无解或**无可行逐港卸箱顺序/求解无法确定**（均**不保存**）。

## 十、请求示例（curl）

```bash
BASE=http://127.0.0.1:8000/api

# 1) 录入槽位资料（得到 slots_revision）
curl -s -X POST $BASE/slot-revisions -H 'Content-Type: application/json' -d '{
  "slots": [
    {"code":"A01","stack":"A","tier":1,"capacity":100,"arm":2.0},
    {"code":"A02","stack":"A","tier":2,"capacity":100,"arm":3.0},
    {"code":"B01","stack":"B","tier":1,"capacity":100,"arm":2.0},
    {"code":"B02","stack":"B","tier":2,"capacity":100,"arm":3.0}
  ],
  "adjacency": [["A01","B01"],["A02","B02"]],
  "incompatible_classes": [["1","5"]],
  "min_moment": -100000,
  "max_moment": 100000
}'

# 2) 录入箱清单（得到 manifest_revision）
curl -s -X POST $BASE/manifest-revisions -H 'Content-Type: application/json' -d '{
  "containers": [
    {"no":"C002","weight":20,"hazmat":null,"discharge_order":1},
    {"no":"C001","weight":10,"hazmat":null,"discharge_order":1}
  ]
}'

# 3) 核算方案（可省略修订号，默认取双方最新修订；无解返回 422 且不保存）
curl -s -X POST $BASE/plans/compute -H 'Content-Type: application/json' -d '{}'
# -> assignments: C001->A01, C002->B01；total_moment=60；revision_no=1

# 4) 确认方案（核对资料修订；过期/冲突返回 409，原方案不变）
curl -s -X POST $BASE/plans/<plan_id>/confirm \
  -H 'Content-Type: application/json' \
  -d '{"slots_revision":1,"manifest_revision":1}'

# 5) 资料更新后修订并重新核算（生成 revision_no=2 的草稿，原确认方案暂保持）
curl -s -X POST $BASE/plans/<plan_id>/amend \
  -H 'Content-Type: application/json' -d '{}'

# 6) 方案确认后生成装船时间表（详见第四章；422=箱项不一致/时间窗无解且不保存）
curl -s -X POST $BASE/plans/<plan_id>/schedules \
  -H 'Content-Type: application/json' -d @examples/schedule.json

# 7) 方案确认后规划逐港卸箱顺序（详见第五章；422=卸港序号不一致/区间倒置/无可行顺序且不保存）
curl -s -X POST $BASE/plans/<plan_id>/unloading \
  -H 'Content-Type: application/json' -d @examples/unloading.json

# 8) 查询
curl -s $BASE/plans
curl -s $BASE/plans/<plan_id>
curl -s "$BASE/schedules"                 # 当前排程
curl -s "$BASE/schedules?historical=true" # 含历史排程
curl -s $BASE/plans/<plan_id>/schedules   # 按方案修订查排程
curl -s "$BASE/unloading"                 # 当前卸箱顺序
curl -s "$BASE/unloading?historical=true" # 含历史卸箱顺序
curl -s $BASE/plans/<plan_id>/unloading   # 按方案修订查卸箱顺序
```

方案响应示例：

```json
{
  "plan_id": "2f1e529a...",
  "revision_no": 1,
  "status": "confirmed",
  "slots_revision": 1,
  "manifest_revision": 1,
  "total_moment": 60.0,
  "max_occupied_tier": 1,
  "assignments": [
    {"container_no": "C001", "slot_code": "A01", "stack": "A", "tier": 1},
    {"container_no": "C002", "slot_code": "B01", "stack": "B", "tier": 1}
  ],
  "parent_id": null,
  "root_id": "2f1e529a...",
  "created_at": "...",
  "confirmed_at": "..."
}
```

更多现成请求体见 `examples/` 目录，可直接：

```bash
curl -s -X POST $BASE/slot-revisions    -H 'Content-Type: application/json' -d @examples/slots.json
curl -s -X POST $BASE/manifest-revisions -H 'Content-Type: application/json' -d @examples/containers.json
```

## 十一、测试

```bash
pip install pytest httpx
PYTHONPATH=. pytest -q
```

测试覆盖：配载求解器全部约束与两阶段优选、岸桥调度求解器全部约束与 makespan/字典序优选、逐港卸箱顺序求解器全部约束（逐港/堆栈先后/逐步力矩区间）与全程箱号字典序优选（含 200+ 随机案例穷举交叉验证）、录入校验（非法重量/重复箱号/无效槽位/时间窗倒置/卸港序号不一致与区间倒置）、无解不落库、过期修订拒绝、方案修订链、并发确认互斥、装船排程与卸箱顺序的箱项一致性及历史标记、以及排程/卸箱顺序保存前的方案状态复核（68 个用例）。

## 十二、目录结构

```
app/
  config.py      # 环境变量配置
  database.py    # 引擎/会话/建表，SQLite WAL、外键、busy_timeout
  models.py      # ORM：资料修订、方案、方案项、已确认占用、装船排程、卸箱顺序等
  schemas.py     # Pydantic 模式与录入校验
  solver.py      # 配载 MILP 建模与两阶段优选（核心）
  scheduler.py   # 装船时间表（岸桥调度）MILP 与两阶段优选（核心）
  unloader.py    # 逐港卸箱顺序 MILP、字典序优选与全约束 Python 复核（核心）
  services.py    # 业务逻辑、并发安全确认、排程/卸箱顺序保存前复核
  routers.py     # HTTP 路由
  main.py        # FastAPI 入口
scripts/entrypoint.sh  # 容器初始化入口
tests/           # 单元/接口/并发测试（含调度器/卸箱顺序穷举交叉验证）
examples/        # 请求示例（含 schedule.json、unloading.json）
Dockerfile / docker-compose.yml / requirements.txt
```
