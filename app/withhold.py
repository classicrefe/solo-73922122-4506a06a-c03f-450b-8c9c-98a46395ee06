"""误报暂扣求解：在提交的快照上求数量最少的暂扣读数行集合。

问题设定：
  - 查询固定为 stations s LEFT JOIN readings r ON s.id = r.station_id，
    投影 / WHERE 沿用受支持 SQL 子集（sql_parser 产出的 QueryIR）。
  - 只能暂扣 readings 行（输入行号从 1 起，且必须在 withholdable 集合内）。
  - 目标：结果中给定的误报行（按列位置、NULL=NULL 比较）的所有副本消失，
    其余结果及其重数保持不变；在可行解中取暂扣行数最少者。

实现分三步：
  1. 确定性求值（本模块）：直接在 Python 内模拟 LEFT JOIN / WHERE / 投影，
     得到暂扣前结果多重集，并给每个结果行标注来源（读数行号或左连接补 NULL 行）。
  2. 最小集合求解（z3，迭代加深）：对每个可暂扣读数行设 0/1 决策变量，
     编码"误报元组计数为 0、非误报元组计数与基线一致"，从 k=0 起求最小可行集。
  3. PostgreSQL 复算（verifier.py）：在隔离临时 schema 中实际执行暂扣前后
     两次查询，确认模拟结果与真实库一致、目标条件成立，否则按未知处理。
"""
from __future__ import annotations

from dataclasses import dataclass

from z3 import (
    Bool,
    BoolVal,
    If,
    Not as ZNot,
    Or as ZOr,
    Solver,
    is_true,
    sat,
    unknown as z3_unknown,
)

from .ir import (
    INT_MAX,
    INT_MIN,
    SCHEMA,
    And,
    BoolNode,
    Compare,
    IsNull,
    Not,
    Or,
    QueryIR,
)

# 快照行数上限（沿用平台既有有界约束）
MAX_STATIONS = 3
MAX_READINGS = 24

CellValue = int | None
# 结果行来源：('reading', readings 行号 1-based) 或 ('null_ext', stations 行号 1-based)
RowSource = tuple[str, int]


class SnapshotError(ValueError):
    """快照或输入不满足约束。"""


@dataclass(frozen=True)
class Snapshot:
    stations: list[tuple[int, int | None]]          # (id, threshold)，行号从 1 起
    readings: list[tuple[int | None, int | None]]   # (station_id, value)，行号从 1 起


@dataclass
class EvaluatedResult:
    """一次求值的结果多重集（含每个副本的来源标注）。"""

    rows: list[tuple[tuple[CellValue, ...], RowSource]]

    def multiset(self) -> dict[tuple[CellValue, ...], int]:
        counts: dict[tuple[CellValue, ...], int] = {}
        for tup, _ in self.rows:
            counts[tup] = counts.get(tup, 0) + 1
        return counts


# ---------- 快照校验 ----------

def validate_snapshot(
    stations: list[list[CellValue]],
    readings: list[list[CellValue]],
    withholdable: list[int],
    false_positive_rows: list[list[CellValue]],
    projection_arity: int,
) -> Snapshot:
    """校验请求快照与行号输入，返回归一化 Snapshot。"""
    if not isinstance(stations, list) or not isinstance(readings, list):
        raise SnapshotError("stations / readings 必须为数组")
    if len(stations) > MAX_STATIONS:
        raise SnapshotError(f"stations 快照最多 {MAX_STATIONS} 行")
    if len(readings) > MAX_READINGS:
        raise SnapshotError(f"readings 快照最多 {MAX_READINGS} 行")

    norm_stations: list[tuple[int, int | None]] = []
    seen_ids: set[int] = set()
    for idx, row in enumerate(stations, start=1):
        if not isinstance(row, list) or len(row) != len(SCHEMA["stations"]):
            raise SnapshotError(f"stations 第 {idx} 行必须是 [id, threshold] 两列")
        sid, threshold = row
        if not _is_int(sid):
            raise SnapshotError(f"stations 第 {idx} 行 id 必须为非空 integer")
        if not _is_int(threshold, nullable=True):
            raise SnapshotError(f"stations 第 {idx} 行 threshold 必须为 integer 或 NULL")
        if sid in seen_ids:
            raise SnapshotError(f"stations 主键 id 重复: {sid}")
        seen_ids.add(sid)
        norm_stations.append((sid, threshold))

    norm_readings: list[tuple[int | None, int | None]] = []
    station_ids = {sid for sid, _ in norm_stations}
    for idx, row in enumerate(readings, start=1):
        if not isinstance(row, list) or len(row) != len(SCHEMA["readings"]):
            raise SnapshotError(
                f"readings 第 {idx} 行必须是 [station_id, value] 两列"
            )
        station_id, value = row
        if not _is_int(station_id, nullable=True):
            raise SnapshotError(
                f"readings 第 {idx} 行 station_id 必须为 integer 或 NULL"
            )
        if not _is_int(value, nullable=True):
            raise SnapshotError(f"readings 第 {idx} 行 value 必须为 integer 或 NULL")
        if station_id is not None and station_id not in station_ids:
            raise SnapshotError(
                f"readings 第 {idx} 行外键 station_id={station_id} 在 stations 中不存在"
            )
        norm_readings.append((station_id, value))

    if not isinstance(withholdable, list) or not withholdable:
        raise SnapshotError("withholdable_reading_rows 必须为非空行号数组")
    if any(not isinstance(n, int) or isinstance(n, bool) for n in withholdable):
        raise SnapshotError("可暂扣行号必须为整数（从 1 起）")
    if len(set(withholdable)) != len(withholdable):
        raise SnapshotError("可暂扣行号存在重复")
    for n in withholdable:
        if not 1 <= n <= len(norm_readings):
            raise SnapshotError(
                f"可暂扣行号 {n} 超出 readings 快照范围（1..{len(norm_readings)}）"
            )

    if not isinstance(false_positive_rows, list) or not false_positive_rows:
        raise SnapshotError("false_positive_rows 必须为非空数组")
    fp_tuples: set[tuple[CellValue, ...]] = set()
    for idx, row in enumerate(false_positive_rows, start=1):
        if not isinstance(row, list):
            raise SnapshotError(f"误报行第 {idx} 项必须为数组")
        if len(row) != projection_arity:
            raise SnapshotError(
                f"误报行第 {idx} 行列数 {len(row)} 与查询投影列数 "
                f"{projection_arity} 不一致（按列位置比较）"
            )
        if any(not _is_int(v, nullable=True) for v in row):
            raise SnapshotError(f"误报行第 {idx} 行的值必须为 integer 或 NULL")
        fp_tuples.add(tuple(row))
    return Snapshot(stations=norm_stations, readings=norm_readings)


def _is_int(v: object, nullable: bool = False) -> bool:
    if v is None:
        return nullable
    # bool 是 int 子类，但 SQL integer 不接受
    return isinstance(v, int) and not isinstance(v, bool) and INT_MIN <= v <= INT_MAX


# ---------- 确定性查询求值（SQL 三值逻辑） ----------

def _eval_bool(node: BoolNode, row: dict[tuple[str, str], CellValue]) -> bool | None:
    """三值逻辑求值：返回 True / False / None(UNKNOWN)，WHERE 仅保留 True。"""
    if isinstance(node, Compare):
        v = row[(node.col.table, node.col.column)]
        if v is None:
            return None
        if node.op == "=":
            return v == node.value
        if node.op == "!=":
            return v != node.value
        if node.op == "<":
            return v < node.value
        if node.op == "<=":
            return v <= node.value
        if node.op == ">":
            return v > node.value
        if node.op == ">=":
            return v >= node.value
        raise ValueError(f"未知比较符: {node.op}")
    if isinstance(node, IsNull):
        v = row[(node.col.table, node.col.column)]
        is_null = v is None
        return (not is_null) if node.negated else is_null
    if isinstance(node, And):
        a = _eval_bool(node.left, row)
        b = _eval_bool(node.right, row)
        if a is False or b is False:
            return False
        if a is None or b is None:
            return None
        return True
    if isinstance(node, Or):
        a = _eval_bool(node.left, row)
        b = _eval_bool(node.right, row)
        if a is True or b is True:
            return True
        if a is None or b is None:
            return None
        return False
    if isinstance(node, Not):
        a = _eval_bool(node.child, row)
        return None if a is None else (not a)
    raise TypeError(f"未知布尔节点: {node!r}")


def _project(
    q: QueryIR, row: dict[tuple[str, str], CellValue]
) -> tuple[CellValue, ...]:
    return tuple(row[(c.table, c.column)] for c in q.projections)


def evaluate(q: QueryIR, snap: Snapshot, withheld: set[int]) -> EvaluatedResult:
    """在快照上执行 stations LEFT JOIN readings（ON id = station_id）查询。

    withheld 为被暂扣的 readings 行号集合（1-based）；被暂扣行视同不存在。
    结果每个副本附带来源，便于求解与解释。
    """
    out: list[tuple[tuple[CellValue, ...], RowSource]] = []
    for s_idx, (sid, threshold) in enumerate(snap.stations, start=1):
        station_cells = {
            ("stations", "id"): sid,
            ("stations", "threshold"): threshold,
        }
        matched = False
        for r_idx, (station_id, value) in enumerate(snap.readings, start=1):
            if r_idx in withheld:
                continue
            # SQL 等值连接：任一侧 NULL 不匹配（主键 id 非空，此处只需判外键）
            if station_id is None or station_id != sid:
                continue
            matched = True
            cells = {
                **station_cells,
                ("readings", "station_id"): station_id,
                ("readings", "value"): value,
            }
            if q.where is None or _eval_bool(q.where, cells) is True:
                out.append((_project(q, cells), ("reading", r_idx)))
        if not matched:
            # LEFT JOIN 补 NULL 行：readings 列全为 NULL
            cells = {
                **station_cells,
                ("readings", "station_id"): None,
                ("readings", "value"): None,
            }
            if q.where is None or _eval_bool(q.where, cells) is True:
                out.append((_project(q, cells), ("null_ext", s_idx)))
    return EvaluatedResult(out)


# ---------- 最小暂扣集合求解（z3 迭代加深） ----------

@dataclass
class SolveResult:
    status: str                  # 'solution' | 'no_solution' | 'unknown'
    withhold_rows: list[int] | None = None
    detail: str = ""


def solve_minimum_withhold(
    q: QueryIR,
    snap: Snapshot,
    fp_tuples: set[tuple[CellValue, ...]],
    withholdable: list[int],
    timeout_ms: int = 10000,
) -> SolveResult:
    """求数量最少的可暂扣读数行号集合；无解 / 未知按约定返回。"""
    baseline = evaluate(q, snap, set())
    base_counts = baseline.multiset()

    # 误报行必须确实出现在基线结果中，否则输入本身不成立
    missing = sorted(t for t in fp_tuples if t not in base_counts)
    if missing:
        return SolveResult(
            "no_solution",
            detail=(
                "给定误报行在当前查询结果中不存在（按列位置、NULL=NULL 比较），"
                "无需也无法通过暂扣读数处理"
            ),
        )

    n = len(snap.readings)
    withholdable_set = set(withholdable)
    # w[i]：readings 第 i+1 行是否被暂扣；不可暂扣的行用常量 False 固定
    w = [
        Bool(f"withhold_{i + 1}") if (i + 1) in withholdable_set else BoolVal(False)
        for i in range(n)
    ]

    # 预计算每个 (站点行号, 读数行号) 的匹配关系与投影/过滤结果
    # station_terms[k]：stations 第 k 行（1-based）在给定 w 下的输出元组贡献
    all_tuples = set(base_counts) | fp_tuples
    solver_constraints: list = []

    # 每个站点独立编码其贡献；全局计数为各站点之和
    station_counts: dict[int, dict[tuple[CellValue, ...], object]] = {}
    for s_idx, (sid, threshold) in enumerate(snap.stations, start=1):
        station_cells = {
            ("stations", "id"): sid,
            ("stations", "threshold"): threshold,
        }
        null_cells = {
            **station_cells,
            ("readings", "station_id"): None,
            ("readings", "value"): None,
        }
        null_kept = q.where is None or _eval_bool(q.where, null_cells) is True
        null_tuple = _project(q, null_cells)

        any_live_match = BoolVal(False)
        # tuple -> 计数表达式（本站点输出中该元组的副本数）
        terms: dict[tuple[CellValue, ...], list] = {}
        for r_idx, (station_id, value) in enumerate(snap.readings, start=1):
            if station_id != sid:
                continue
            cells = {
                **station_cells,
                ("readings", "station_id"): station_id,
                ("readings", "value"): value,
            }
            # 该读数行存在（未暂扣）即构成一次连接匹配，无论是否通过 WHERE
            any_live_match = ZOr(any_live_match, ZNot(w[r_idx - 1]))
            kept = q.where is None or _eval_bool(q.where, cells) is True
            tup = _project(q, cells)
            if kept:
                # 通过 WHERE 时才贡献一条输出
                terms.setdefault(tup, []).append(ZNot(w[r_idx - 1]))
        # 补 NULL 行：没有任何存活的匹配读数（被 WHERE 过滤的匹配仍算匹配）
        null_present = ZNot(any_live_match)
        if null_kept:
            terms.setdefault(null_tuple, []).append(null_present)

        counts: dict[tuple[CellValue, ...], object] = {}
        for tup in all_tuples:
            exprs = terms.get(tup, [])
            counts[tup] = _sum([If(e, 1, 0) for e in exprs]) if exprs else 0
        station_counts[s_idx] = counts

    # 全局目标计数：
    #   - 误报元组（必在基线中，前置检查已保证）：计数必须为 0；
    #   - 基线中的非误报元组：计数保持不变；
    #   - 基线外新出现的元组（如补 NULL 行）只要不是误报即不约束。
    def total_count(tup: tuple[CellValue, ...]):
        return _sum([station_counts[k][tup] for k in station_counts])

    target_counts = {
        tup: (0 if tup in fp_tuples else base_counts[tup]) for tup in all_tuples
    }
    for tup, target in target_counts.items():
        solver_constraints.append(total_count(tup) == target)

    # 迭代加深：从 k=0 到可暂扣行数，取第一个可行的最小 k。
    # 每轮独立求解并共享同一超时预算上限；任一轮无法确认即按未知处理。
    decisions = [w[i - 1] for i in withholdable]
    for k in range(0, len(withholdable) + 1):
        solver = Solver()
        solver.set("timeout", timeout_ms)
        solver.add(*solver_constraints)
        solver.add(_sum([If(d, 1, 0) for d in decisions]) == k)
        result = solver.check()
        if result == z3_unknown:
            return SolveResult(
                "unknown",
                detail=f"求解器无法判定（原因: {solver.reason_unknown()}）",
            )
        if result == sat:
            model = solver.model()
            chosen = sorted(
                i for i in withholdable if _is_true(model, w[i - 1])
            )
            # 防御：模型给出的解必须确定性复算通过
            sim = evaluate(q, snap, set(chosen))
            if not _targets_met(sim.multiset(), fp_tuples, base_counts):
                return SolveResult(
                    "unknown", detail="内部不一致：求解结果确定性复算未通过"
                )
            return SolveResult(
                "solution",
                withhold_rows=chosen,
                detail=f"最少暂扣 {k} 行（枚举上界 {len(withholdable)} 行）",
            )
    return SolveResult(
        "no_solution",
        detail="遍历全部可暂扣组合（含空集）均无法同时消除误报并保持其他结果重数",
    )


def _sum(terms: list):
    total = 0
    for t in terms:
        total = total + t
    return total


def _is_true(model, expr) -> bool:
    return is_true(model.eval(expr, model_completion=True))


def _targets_met(
    counts: dict[tuple[CellValue, ...], int],
    fp_tuples: set[tuple[CellValue, ...]],
    base_counts: dict[tuple[CellValue, ...], int],
) -> bool:
    # 误报元组必须全部消失
    for tup in fp_tuples:
        if counts.get(tup, 0) != 0:
            return False
    # 基线中的非误报元组，重数必须保持不变；
    # 暂扣后新出现的元组（典型为 LEFT JOIN 补 NULL 行）允许存在，只要它不是误报
    for tup, base_n in base_counts.items():
        if tup in fp_tuples:
            continue
        if counts.get(tup, 0) != base_n:
            return False
    return True
