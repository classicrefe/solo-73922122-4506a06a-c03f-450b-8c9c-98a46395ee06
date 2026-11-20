"""求解器暴力对照：小空间内穷举所有暂扣组合，与 z3 迭代加深结果逐一对照。"""
import itertools
import random

from app.sql_parser import parse_query
from app.withhold import evaluate, solve_minimum_withhold, validate_snapshot

QUERIES = [
    "SELECT s.id, r.value FROM stations s LEFT JOIN readings r "
    "ON s.id = r.station_id",
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value IS NULL OR r.value >= 10",
    "SELECT s.id, s.threshold, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE s.threshold > 0 AND r.value != 3",
]


def _brute_force(ir, snap, fp, withholdable):
    base = evaluate(ir, snap, set()).multiset()
    best = None
    for r in range(0, len(withholdable) + 1):
        for combo in itertools.combinations(withholdable, r):
            counts = evaluate(ir, snap, set(combo)).multiset()
            ok = all(counts.get(t, 0) == 0 for t in fp) and all(
                counts.get(t, 0) == n for t, n in base.items() if t not in fp
            )
            if ok:
                return list(combo)
    return None


def test_solver_matches_brute_force():
    rng = random.Random(42)
    for trial in range(60):
        n_st = rng.randint(1, 3)
        ids = rng.sample(range(1, 8), n_st)
        stations = [[sid, rng.choice([None, rng.randint(-2, 9)])] for sid in ids]
        n_rd = rng.randint(1, 7)
        readings = [
            [rng.choice(ids + [None]), rng.choice([None, rng.randint(-2, 15)])]
            for _ in range(n_rd)
        ]
        sql = QUERIES[trial % len(QUERIES)]
        ir = parse_query(sql)
        withholdable = list(range(1, n_rd + 1))
        base_rows = evaluate(
            ir,
            validate_snapshot(stations, readings, withholdable, [[None] * len(ir.projections)],
                              len(ir.projections)),
            set(),
        ).rows
        if not base_rows:
            continue
        fp_list = [list(rng.choice(base_rows)[0])]
        snap = validate_snapshot(
            stations, readings, withholdable, fp_list, len(ir.projections)
        )
        fp = {tuple(r) for r in fp_list}
        got = solve_minimum_withhold(ir, snap, fp, withholdable)
        expected = _brute_force(ir, snap, fp, withholdable)
        if expected is None:
            assert got.status == "no_solution", trial
        else:
            assert got.status == "solution", trial
            assert got.withhold_rows == expected, (trial, got.withhold_rows, expected)


def test_solver_handles_max_24_readings():
    """24 行读数上限下求解仍快速返回且结果自洽。"""
    stations = [[1, 5], [2, 0], [3, None]]
    readings = [[(i % 3) + 1, (i * 7) % 20 - 5] for i in range(24)]
    ir = parse_query(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value IS NULL OR r.value >= 0"
    )
    pre = validate_snapshot(
        stations, readings, list(range(1, 25)), [[1, 0]], 2
    )
    # 取基线中真实存在的一个结果元组作为误报
    fp_tuple = next(iter(evaluate(ir, pre, set()).multiset()))
    snap = validate_snapshot(
        stations, readings, list(range(1, 25)), [list(fp_tuple)], 2
    )
    import time

    t0 = time.monotonic()
    res = solve_minimum_withhold(ir, snap, {fp_tuple}, list(range(1, 25)))
    elapsed = time.monotonic() - t0
    assert res.status == "solution"
    assert elapsed < 10
    counts = evaluate(ir, snap, set(res.withhold_rows)).multiset()
    assert counts.get(fp_tuple, 0) == 0
