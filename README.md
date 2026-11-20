# SQL 改写有界等价核对服务

面向数仓质控场景提供两类能力：

1. **SQL 改写等价核对**（`POST /check`）：维护员替换站点读数入仓质控 SQL 前，
   提交**原始 SELECT** 与**改写 SELECT**，服务在固定模式下枚举每表 **0~3 行**的
   所有合法取值，核对两侧结果**多重集**是否一致（按列位置比较、忽略行序、保留
   重复），用于排查改写对缺测（NULL）与重复采样的影响。
2. **误报暂扣建议**（`POST /withhold`）：值班员撤下质控报告中已确认的误报时，
   提交在用质控 SELECT、`stations`（≤3 行）/ `readings`（≤24 行）快照、按列
   位置给出的误报结果行与允许暂扣的 readings 输入行号（从 1 起），服务在提交
   的快照上求**数量最少**的暂扣行集合，使误报行的所有副本消失且其他结果及重数
   保持一致，并在隔离 PostgreSQL 中重跑暂扣前后确认。

- 差异必须在**隔离 PostgreSQL** 中实际复现才报 `NOT_EQUIVALENT`，并返回
  可装载 SQL 与两侧实际结果；
- 界内无反例报 `EQUIVALENT`（仅三行界内结论，不构成更大表上的证明）；
- 求解或复现失败报 `UNKNOWN`。

## 固定模式

```sql
CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER);
CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER);
```

全列 `integer`；非主键列可 `NULL`；`readings.station_id` 为外键（NULL 合法）。

## 支持的 SQL 子集（其余一律 400 拒绝）

| 子句 | 支持内容 |
| --- | --- |
| SELECT | 仅列投影（可带表限定、别名；不支持 `*`、表达式、函数、字面量） |
| FROM | 单表，或两表间至多一次 `INNER` / `LEFT [OUTER]` 等值连接（`ON a.col = b.col`） |
| WHERE | 列与整数字面量比较（`=` `!=` `<` `<=` `>` `>=`）、`IS [NOT] NULL`、`AND` / `OR` / `NOT`、括号 |

不支持：聚合 / `GROUP BY` / `HAVING` / `ORDER BY` / `LIMIT` / `DISTINCT` /
子查询 / `IN` / `BETWEEN` / 字符串与浮点字面量 / 列间比较（WHERE 内）/
自连接 / `RIGHT` / `FULL` / `CROSS` 连接 / 写操作等。

## 快速开始（Docker Compose）

```bash
docker compose up --build
# API: http://localhost:8000  交互文档: http://localhost:8000/docs
```

`docker-compose.yml` 启动两个服务：`db`（postgres:16，仅内部网络，专用于
反例复现）与 `api`（FastAPI）。无需初始化数据——复现用的表结构由服务在
临时 schema 中自建，事务回滚即清理。

## API 用法

### `POST /check`

```json
{
  "query_a": "SELECT id FROM stations WHERE threshold = 5",
  "query_b": "SELECT id FROM stations WHERE threshold = 5 OR threshold IS NULL",
  "solver_timeout_ms": 10000
}
```

响应（`NOT_EQUIVALENT` 示例）：

```json
{
  "verdict": "NOT_EQUIVALENT",
  "bound_rows": 3,
  "message": "两侧结果存在差异，已在隔离 PostgreSQL 中实际复现。",
  "counterexample": {
    "setup_sql": "DROP TABLE IF EXISTS readings;\nDROP TABLE IF EXISTS stations;\nCREATE TABLE stations ...\nINSERT INTO stations ...",
    "query_a_result": [],
    "query_b_result": [[-1]]
  }
}
```

- `verdict=EQUIVALENT`：每表 0~3 行所有合法取值下结果多重集相同；
- `verdict=NOT_EQUIVALENT`：`counterexample.setup_sql` 可直接装载到任意
   scratch 库复现差异，`query_a_result` / `query_b_result` 为实际执行结果；
- `verdict=UNKNOWN`：z3 超时/无法判定，或 PostgreSQL 复现失败（`message`
  说明原因）。

curl 示例：

```bash
curl -s -X POST http://localhost:8000/check \
  -H 'Content-Type: application/json' \
  -d '{"query_a":"SELECT s.id FROM stations s LEFT JOIN readings r ON s.id = r.station_id",
       "query_b":"SELECT id FROM stations"}'
```

### `POST /withhold`

值班员撤下已确认误报、其他结果照常供数。请求体：

| 字段 | 说明 |
| --- | --- |
| `query` | 在用质控 SELECT；必须为 `stations` 按 `id = station_id` `LEFT JOIN readings`，投影与 WHERE 沿用上述语法子集 |
| `stations` | 快照行 `[id, threshold]`，integer 或 NULL（id 非空、唯一），最多 3 行 |
| `readings` | 快照行 `[station_id, value]`，integer 或 NULL，外键必须引用快照内站点，最多 24 行；**数组下标即输入行号（从 1 起）** |
| `false_positive_rows` | 已确认误报的结果行，按**列位置**给出；两个 NULL 视为相等 |
| `withholdable_reading_rows` | 允许暂扣的 readings 输入行号（从 1 起），求解只会从中选择 |
| `solver_timeout_ms` | 可选，求解超时（毫秒） |

```json
{
  "query": "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
           "ON s.id = r.station_id WHERE r.value IS NULL OR r.value >= 10",
  "stations": [[1, 5], [2, null]],
  "readings": [[1, 99], [1, 10], [2, 7], [null, 3]],
  "false_positive_rows": [[1, 99]],
  "withholdable_reading_rows": [1, 2, 3]
}
```

响应（找到最少暂扣集合，并经隔离 PostgreSQL 复算确认）：

```json
{
  "verdict": "SOLVED",
  "withhold_rows": [1],
  "message": "最少暂扣 1 行……已在隔离 PostgreSQL 复算暂扣前后结果……",
  "recheck": {
    "before_result": [[1, 99], [1, 10], [2, null]],
    "after_result": [[1, 10], [2, null]]
  }
}
```

- `SOLVED`：`withhold_rows` 为数量最少的暂扣行号集合；`recheck` 给出隔离
  PostgreSQL 中暂扣前后的真实结果，误报副本全部为 0、非误报元组重数不变；
- `NO_SOLUTION`：任何可暂扣组合都无法同时满足"误报清零 + 其他结果重数不变"
  （典型：误报来自无匹配站点的 LEFT JOIN 补 NULL 行，或产生误报的读数不在
  可暂扣范围内）；误报行本身不在当前结果中也按无解处理；
- `UNKNOWN`：求解器超时/无法判定，或隔离 PostgreSQL 复算未通过（含模拟与
  真实库结果不一致），此时不给出暂扣建议。

> 注意：仅在**提交的快照**上试算，结论不外推到其他数据；暂扣动作本身需由
> 值班员在业务侧执行，服务只给出建议与复算证据。

### 其他端点

- `GET /`：服务信息；
- `GET /health`：健康检查（含数据库连通性）；
- `GET /docs`：Swagger 交互文档，内置等价 / 不等价（NULL 语义、LEFT JOIN
  重复行）等命名示例，可直接 "Try it out"。

## 判定原理

1. **解析与白名单校验**（`app/sql_parser.py`，基于 sqlglot）：越界语法拒绝。
2. **z3 有界判定**（`app/z3check.py`）：两表各 0~3 行符号化，施加主键唯一、
   外键引用、int4 值域约束；WHERE 按 SQL 三值逻辑编码（`UNKNOWN` 被过滤）；
   JOIN/LEFT JOIN 展开为符号行；结果多重集差异编码为"存在某元组值两侧计数
   不同"。`UNSAT` 即界内等价，`SAT` 提取候选反例。
3. **PostgreSQL 复现**（`app/verifier.py`）：候选反例在独立临时 schema 中
   建表、装载、实际执行两条查询并比较多重集；确认差异才报
   `NOT_EQUIVALENT`，否则报 `UNKNOWN`。

误报暂扣（`app/withhold.py` + `Verifier.recheck_withhold`）：

1. 快照按表约束校验（主键唯一、外键引用、int4 值域、3/24 行上限、行号范围）；
2. Python 内按 SQL 三值逻辑确定性模拟 LEFT JOIN / WHERE / 投影，得到暂扣前
   结果多重集与每行来源（读数行或补 NULL 行）；
3. z3 对每个可暂扣读数行设 0/1 变量，编码"误报元组计数为 0、基线非误报元组
   计数不变"，迭代加深（k=0,1,2,…）取第一个可行解即最少暂扣集；遍历无解报
   `NO_SOLUTION`，求解器无法判定报 `UNKNOWN`；
4. 建议在隔离 PostgreSQL 临时 schema 中重跑暂扣前后两次原 SQL，核对模拟与
   真实库结果一致且目标条件成立，否则降级 `UNKNOWN`。

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://postgres:postgres@localhost:5432/postgres` | 复现用 PostgreSQL DSN |
| `SOLVER_TIMEOUT_MS` | `10000` | z3 求解超时（可被请求字段 `solver_timeout_ms` 覆盖） |

## 已知边界

- 等价结论仅限每表 ≤3 行（有界模型检验，非全量证明）；
- 仅支持上述 SQL 子集；两查询投影列数不一致返回 400；
- 复现使用单事务 + 临时 schema，不在库中留存对象；数据库不可达时
  不等价判定降级为 `UNKNOWN`（等价判定不依赖数据库）；
- `/withhold` 仅接受 `stations LEFT JOIN readings ON s.id = r.station_id`
  形态的查询；结论仅对提交的快照有效，不构成对其他数据的保证；
- `/withhold` 的建议必须通过隔离 PostgreSQL 复算才返回 `SOLVED`，数据库
  不可达或复算不一致时返回 `UNKNOWN`。

## 本地开发与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/                       # 单元测试（解析器 / z3 / API 打桩 / 暂扣求解）
DATABASE_URL=postgresql://... pytest tests/test_e2e.py   # 端到端（需真实 PG）
```

测试矩阵中 `tests/test_withhold_brute.py` 用暴力枚举对照最少暂扣解的正确性
与最小性，`tests/test_withhold_diff.py` 用 120 组随机快照对照 Python 模拟与
PostgreSQL 真实执行的结果多重集（均需 `DATABASE_URL`）。
