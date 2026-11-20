"""在隔离的 PostgreSQL 模式中实际复现反例 / 复算暂扣方案。

z3 给出的候选反例必须在真实 PostgreSQL 中重放并确认两侧结果多重集确实不同，
才允许上报"不等价"；复现失败或未复现差异时一律按"未知"处理（由调用方决定）。
误报暂扣方案同理：模拟求解出的最少暂扣行集合，必须在真实 PostgreSQL 中分别
执行暂扣前 / 暂扣后两次查询，确认基线一致、误报副本清零、其他结果重数不变。
隔离方式：每次复现使用独立的临时 SCHEMA，整个流程在单事务内完成并回滚，
不在数据库中留下任何对象。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg

from .z3check import CounterexampleData

_CREATE_STATIONS = "CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)"
_CREATE_READINGS = (
    "CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER)"
)


@dataclass
class ReproOutcome:
    ok: bool                     # 复现流程是否成功执行
    differs: bool = False        # 两侧结果多重集是否确实不同
    detail: str = ""
    query_a_result: list[list[Any]] | None = None
    query_b_result: list[list[Any]] | None = None


def setup_sql(witness: CounterexampleData) -> str:
    """生成可装载的 SQL 脚本（重建表结构与反例数据）。"""
    lines = [
        "DROP TABLE IF EXISTS readings;",
        "DROP TABLE IF EXISTS stations;",
        _CREATE_STATIONS + ";",
        _CREATE_READINGS + ";",
    ]
    if witness.stations:
        vals = ", ".join(f"({sid}, {_lit(th)})" for sid, th in witness.stations)
        lines.append(f"INSERT INTO stations (id, threshold) VALUES {vals};")
    if witness.readings:
        vals = ", ".join(f"({_lit(sid)}, {_lit(v)})" for sid, v in witness.readings)
        lines.append(f"INSERT INTO readings (station_id, value) VALUES {vals};")
    return "\n".join(lines) + "\n"


def _lit(v: Any) -> str:
    return "NULL" if v is None else str(int(v))


def _canon(rows: list[list[Any]]) -> list[tuple]:
    """规范化结果多重集：NULL 可比较、忽略行序、保留重复。"""
    return sorted(
        tuple((0, 0) if v is None else (1, v) for v in row) for row in rows
    )


class Verifier:
    def __init__(self, dsn: str, statement_timeout_ms: int = 10000) -> None:
        self._dsn = dsn
        self._timeout_ms = statement_timeout_ms

    def ping(self) -> bool:
        try:
            with psycopg.connect(self._dsn, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    def reproduce(
        self, witness: CounterexampleData, query_a: str, query_b: str
    ) -> ReproOutcome:
        schema = "cc_" + uuid.uuid4().hex
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA "{schema}"')
                    cur.execute(f'SET search_path TO "{schema}"')
                    cur.execute(f"SET LOCAL statement_timeout TO {self._timeout_ms}")
                    cur.execute(_CREATE_STATIONS)
                    cur.execute(_CREATE_READINGS)
                    if witness.stations:
                        cur.executemany(
                            "INSERT INTO stations (id, threshold) VALUES (%s, %s)",
                            witness.stations,
                        )
                    if witness.readings:
                        cur.executemany(
                            "INSERT INTO readings (station_id, value) VALUES (%s, %s)",
                            witness.readings,
                        )
                    cur.execute(query_a)
                    res_a = [list(r) for r in cur.fetchall()]
                    cur.execute(query_b)
                    res_b = [list(r) for r in cur.fetchall()]
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return ReproOutcome(ok=False, detail=f"PostgreSQL 复现失败: {exc}")
        return ReproOutcome(
            ok=True,
            differs=_canon(res_a) != _canon(res_b),
            query_a_result=res_a,
            query_b_result=res_b,
        )

    # ---------- 误报暂扣方案复算 ----------

    def recheck_withhold(
        self,
        stations: list[tuple[int, int | None]],
        readings: list[tuple[int | None, int | None]],
        query: str,
        withheld_rows: set[int],
        fp_tuples: set[tuple],
        expected_before: list[list[Any]],
        expected_after: list[list[Any]],
    ) -> "WithholdRecheck":
        """在隔离临时 schema 中重跑暂扣前 / 暂扣后查询并核对目标。

        核对三件事：
          1. 暂扣前真实结果与 Python 模拟基线一致（NULL=NULL、保多重集）；
          2. 暂扣后真实结果与模拟的预期结果一致；
          3. 误报元组计数全部为 0，非误报元组计数与暂扣前一致。
        """
        schema = "wh_" + uuid.uuid4().hex
        kept_readings = [
            row
            for idx, row in enumerate(readings, start=1)
            if idx not in withheld_rows
        ]
        try:
            with psycopg.connect(self._dsn, connect_timeout=10) as conn:
                with conn.cursor() as cur:
                    cur.execute(f'CREATE SCHEMA "{schema}"')
                    cur.execute(f'SET search_path TO "{schema}"')
                    cur.execute(f"SET LOCAL statement_timeout TO {self._timeout_ms}")
                    cur.execute(_CREATE_STATIONS)
                    cur.execute(_CREATE_READINGS)
                    if stations:
                        cur.executemany(
                            "INSERT INTO stations (id, threshold) VALUES (%s, %s)",
                            stations,
                        )

                    def run_query(cur_readings) -> list[list[Any]]:
                        cur.execute("TRUNCATE readings")
                        if cur_readings:
                            cur.executemany(
                                "INSERT INTO readings "
                                "(station_id, value) VALUES (%s, %s)",
                                cur_readings,
                            )
                        cur.execute(query)
                        return [list(r) for r in cur.fetchall()]

                    before = run_query(readings)
                    after = run_query(kept_readings)
                conn.rollback()  # 隔离：事务内建临时模式，回滚即清理
        except Exception as exc:  # 数据库不可达 / 执行失败 -> 未知
            return WithholdRecheck(ok=False, detail=f"PostgreSQL 复算失败: {exc}")

        problems: list[str] = []
        if _canon(before) != _canon(expected_before):
            problems.append("暂扣前真实查询结果与快照模拟结果不一致")
        if _canon(after) != _canon(expected_after):
            problems.append("暂扣后真实查询结果与模拟预期结果不一致")
        after_counts = _count_tuples(after)
        before_counts = _count_tuples(before)
        fp_keys = {_key(t) for t in fp_tuples}
        for tup in fp_tuples:
            if after_counts.get(_key(tup), 0) != 0:
                problems.append("暂扣后误报行仍存在副本")
        for key, n in before_counts.items():
            if key in fp_keys:
                continue
            if after_counts.get(key, 0) != n:
                problems.append("非误报结果的重数发生变化")
        return WithholdRecheck(
            ok=not problems,
            detail="；".join(problems),
            before_result=before,
            after_result=after,
        )


def _key(row: tuple | list) -> tuple:
    return tuple((0, 0) if v is None else (1, v) for v in row)


def _count_tuples(rows: list[list[Any]]) -> dict[tuple, int]:
    counts: dict[tuple, int] = {}
    for row in rows:
        key = _key(row)
        counts[key] = counts.get(key, 0) + 1
    return counts


@dataclass
class WithholdRecheck:
    ok: bool
    detail: str = ""
    before_result: list[list[Any]] | None = None
    after_result: list[list[Any]] | None = None
