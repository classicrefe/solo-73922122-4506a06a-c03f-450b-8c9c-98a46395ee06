"""随机差分测试：Python 确定性求值 vs 真实 PostgreSQL。

对随机快照、随机 LEFT JOIN 查询、随机暂扣组合，比较两侧结果多重集
（NULL=NULL、忽略行序、保留重复）。需要 DATABASE_URL。
"""
import os
import random

import psycopg
import pytest

from app.sql_parser import parse_query
from app.withhold import evaluate, validate_snapshot

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="无 DATABASE_URL")

QUERIES = [
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id",
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value IS NULL OR r.value >= 10",
    "SELECT s.id, s.threshold, r.station_id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE s.threshold > 0 AND (r.value != 5 OR r.value IS NULL)",
    "SELECT r.value, s.id FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE NOT (r.value < 10)",
]

DSN = os.environ.get("DATABASE_URL")


def _canon(rows):
    return sorted(tuple((0, 0) if v is None else (1, v) for v in row) for row in rows)


def _pg_run(cur, stations, readings, query):
    cur.execute("DROP TABLE IF EXISTS readings; DROP TABLE IF EXISTS stations;")
    cur.execute("CREATE TABLE stations (id INTEGER PRIMARY KEY, threshold INTEGER)")
    cur.execute(
        "CREATE TABLE readings (station_id INTEGER REFERENCES stations(id), value INTEGER)"
    )
    if stations:
        cur.executemany(
            "INSERT INTO stations (id, threshold) VALUES (%s,%s)", stations
        )
    if readings:
        cur.executemany(
            "INSERT INTO readings (station_id, value) VALUES (%s,%s)", readings
        )
    cur.execute(query)
    return [list(r) for r in cur.fetchall()]


def test_randomized_simulation_matches_postgres():
    rng = random.Random(20261120)
    with psycopg.connect(DSN) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS wh_diff_test")
            cur.execute("SET search_path TO wh_diff_test")
            for trial in range(120):
                n_st = rng.randint(0, 3)
                ids = rng.sample(range(-5, 6), n_st)
                stations = [[sid, rng.choice([None, rng.randint(-3, 12)])] for sid in ids]
                n_rd = rng.randint(1, 8)
                readings = []
                for _ in range(n_rd):
                    if rng.random() < 0.15:
                        station_id = None
                    elif ids:
                        station_id = rng.choice(ids)
                    else:
                        station_id = None
                    readings.append([station_id, rng.choice([None, rng.randint(-3, 15)])])
                sql = rng.choice(QUERIES)
                ir = parse_query(sql)
                arity = len(ir.projections)
                snap = validate_snapshot(
                    stations,
                    readings,
                    list(range(1, n_rd + 1)),
                    false_positive_rows=[[None] * arity],
                    projection_arity=arity,
                )
                # 随机暂扣组合（含空集与全量）
                k = rng.randint(0, n_rd)
                withheld = set(rng.sample(range(1, n_rd + 1), k)) if k else set()
                sim_rows = [list(t) for t, _ in evaluate(ir, snap, withheld).rows]
                kept = [r for i, r in enumerate(readings, 1) if i not in withheld]
                pg_before = _pg_run(cur, stations, readings, sql)
                pg_after = _pg_run(cur, stations, kept, sql)
                assert _canon(pg_before) == _canon(
                    [list(t) for t, _ in evaluate(ir, snap, set()).rows]
                ), f"trial {trial} baseline mismatch"
                assert _canon(pg_after) == _canon(sim_rows), (
                    f"trial {trial} withhold mismatch: {readings} {withheld}"
                )
            cur.execute("DROP SCHEMA wh_diff_test CASCADE")
        conn.rollback()
