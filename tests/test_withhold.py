"""误报暂扣求解的单元测试：快照校验、确定性求值、最小集合、无解、NULL 语义。

这些测试不依赖 PostgreSQL（复算环节在 test_api / test_e2e 中打桩或真机验证）。
"""
import pytest

from app.sql_parser import parse_query
from app.withhold import (
    SnapshotError,
    evaluate,
    solve_minimum_withhold,
    validate_snapshot,
)

Q = (
    "SELECT s.id, r.value FROM stations s "
    "LEFT JOIN readings r ON s.id = r.station_id "
    "WHERE r.value IS NULL OR r.value >= 10"
)


def _snap(stations, readings, withholdable, fp_rows):
    ir = parse_query(Q)
    snap = validate_snapshot(
        stations, readings, withholdable, fp_rows, len(ir.projections)
    )
    return ir, snap, {tuple(r) for r in fp_rows}


def test_validate_rejects_bad_snapshot():
    with pytest.raises(SnapshotError):
        # 主键重复
        validate_snapshot([[1, 1], [1, 2]], [], [1], [[1]], 1)
    with pytest.raises(SnapshotError):
        # 外键悬空
        validate_snapshot([[1, 1]], [[9, 1]], [1], [[1]], 1)
    with pytest.raises(SnapshotError):
        # 行数超上限
        validate_snapshot([[i, 1] for i in range(4)], [], [1], [[1]], 1)
    with pytest.raises(SnapshotError):
        validate_snapshot([[1, 1]], [[1, 1]] * 25, list(range(1, 26)), [[1]], 1)
    with pytest.raises(SnapshotError):
        # 行号越界
        validate_snapshot([[1, 1]], [[1, 1]], [2], [[1]], 1)
    with pytest.raises(SnapshotError):
        # 误报行列数不符
        validate_snapshot([[1, 1]], [[1, 1]], [1], [[1, None, 2]], 1)
    with pytest.raises(SnapshotError):
        # bool 不接受为 integer
        validate_snapshot([[True, 1]], [], [1], [[1]], 1)
    with pytest.raises(SnapshotError):
        validate_snapshot([[1, "x"]], [], [1], [[1]], 1)
    with pytest.raises(SnapshotError):
        # 可暂扣行号数组为空
        validate_snapshot([[1, 1]], [[1, 1]], [], [[1]], 1)
    with pytest.raises(SnapshotError):
        # 误报数组为空
        validate_snapshot([[1, 1]], [[1, 1]], [1], [], 1)


def test_basic_minimum_withhold_single_reading():
    ir, snap, fp = _snap(
        stations=[[1, 5], [2, None]],
        readings=[[1, 99], [1, 10], [2, 7], [None, 3]],
        withholdable=[1, 2, 3],
        fp_rows=[[1, 99]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1, 2, 3])
    assert res.status == "solution"
    assert res.withhold_rows == [1]
    after = evaluate(ir, snap, {1}).multiset()
    assert after.get((1, 99), 0) == 0
    # 其他结果及重数保持
    assert after[(1, 10)] == 1
    # 站点 2 的读数 value=7 被 WHERE 过滤（但连接仍匹配，不补 NULL 行），无输出
    assert (2, None) not in after


def test_multiplicity_all_copies_must_go():
    """同一误报元组有多个副本时，产生它们的读数都要暂扣。"""
    ir, snap, fp = _snap(
        stations=[[1, 5]],
        readings=[[1, 99], [1, 99], [1, 10]],
        withholdable=[1, 2, 3],
        fp_rows=[[1, 99]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1, 2, 3])
    assert res.status == "solution"
    assert res.withhold_rows == [1, 2]
    after = evaluate(ir, snap, {1, 2}).multiset()
    assert after == {(1, 10): 1}


def test_cannot_withhold_non_withholdable_reading():
    """误报读数不在可暂扣范围 -> 无解。"""
    ir, snap, fp = _snap(
        stations=[[1, 5], [2, 1]],
        readings=[[1, 99], [2, 50]],
        withholdable=[2],
        fp_rows=[[1, 99]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [2])
    assert res.status == "no_solution"


def test_null_extended_false_positive_unsolvable():
    """误报来自 LEFT JOIN 补 NULL 行（站点无任何匹配读数），暂扣读数无法消除。"""
    ir, snap, fp = _snap(
        stations=[[1, 5], [2, 9]],
        readings=[[2, 8]],
        withholdable=[1],
        fp_rows=[[1, None]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1])
    assert res.status == "no_solution"


def test_withholding_all_readings_reintroduces_null_row_blocks_solution():
    """站点所有匹配读数都被暂扣会补出 NULL 行；若该 NULL 行是误报则不可行。

    场景：误报 (1, 42) 必须暂扣读数 1，但站点 1 只有这一条读数，
    暂扣后补出的 (1, NULL) 同样在误报集合中 -> 无解。
    """
    ir, snap, fp = _snap(
        stations=[[1, 5]],
        readings=[[1, 42]],
        withholdable=[1],
        fp_rows=[[1, 42], [1, None]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1])
    assert res.status == "no_solution"


def test_withholding_all_readings_null_row_preserves_other_tuple():
    """补出的 NULL 行作为"其他结果"必须按重数保留。"""
    ir = parse_query(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    # 站点 2 本就无读数，基线已有一个 (2, NULL)；站点 1 暂扣唯一读数后补出
    # (1, NULL) —— 与误报 (1,42) 不同元组，属于"其他结果"，必须存在且计数不变。
    snap = validate_snapshot(
        stations=[[1, 5], [2, 8]],
        readings=[[1, 42]],
        withholdable=[1],
        false_positive_rows=[[1, 42]],
        projection_arity=2,
    )
    fp = {(1, 42)}
    res = solve_minimum_withhold(ir, snap, fp, [1])
    assert res.status == "solution"
    assert res.withhold_rows == [1]
    before = evaluate(ir, snap, set()).multiset()
    after = evaluate(ir, snap, {1}).multiset()
    assert after[(1, None)] == 1
    assert after[(2, None)] == before[(2, None)] == 1
    # (1,42) 已消除，其余元组重数与基线一致
    assert (1, 42) not in after


def test_fp_tuple_not_in_baseline_is_no_solution():
    ir, snap, fp = _snap(
        stations=[[1, 5]],
        readings=[[1, 10]],
        withholdable=[1],
        fp_rows=[[1, 123]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1])
    assert res.status == "no_solution"


def test_null_equals_null_comparison():
    """两个 NULL 视为相等：value 为 NULL 的读数行与补 NULL 行同元组计数。"""
    ir = parse_query(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id"
    )
    snap = validate_snapshot(
        stations=[[1, 5], [2, 9]],
        readings=[[1, None]],
        withholdable=[1],
        false_positive_rows=[[1, None]],
        projection_arity=2,
    )
    # 基线：站点1 一条 (1,NULL) 读数 + 站点2 补 NULL (2,NULL)
    base = evaluate(ir, snap, set()).multiset()
    assert base[(1, None)] == 1 and base[(2, None)] == 1
    # 暂扣读数 1 后，站点 1 改由补 NULL 行贡献 (1,NULL) —— 误报副本仍在 -> 无解
    res = solve_minimum_withhold(ir, snap, {(1, None)}, [1])
    assert res.status == "no_solution"


def test_minimality_prefers_smaller_set():
    """两条读数产生同一误报副本，但只暂扣其中一条即可满足时选最少的。

    读数 1 产生误报 (1,99)；读数 2 也产生 (1,99)（重复采样），两条都得扣；
    对照：读数 3 产生非误报 (1,10)，不可被纳入最小解。
    """
    ir, snap, fp = _snap(
        stations=[[1, 5]],
        readings=[[1, 99], [1, 99], [1, 10]],
        withholdable=[1, 2, 3],
        fp_rows=[[1, 99]],
    )
    res = solve_minimum_withhold(ir, snap, fp, [1, 2, 3])
    assert res.withhold_rows == [1, 2]
    assert 3 not in res.withhold_rows


def test_empty_withhold_already_satisfies_when_fp_absent():
    """误报不在基线 -> 直接无解判定（无需任何暂扣）。"""
    ir, snap, fp = _snap(
        stations=[[1, 5]],
        readings=[[1, 10]],
        withholdable=[1],
        fp_rows=[[9, 9]],
    )
    assert solve_minimum_withhold(ir, snap, fp, [1]).status == "no_solution"


def test_where_filter_three_valued_logic():
    """WHERE 含 NULL 比较时按 SQL 三值逻辑过滤，补 NULL 行也参与过滤。"""
    ir = parse_query(
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value > 5"
    )
    snap = validate_snapshot(
        stations=[[1, 1]],
        readings=[[1, None], [1, 6]],
        withholdable=[1, 2],
        false_positive_rows=[[1, 6]],
        projection_arity=2,
    )
    base = evaluate(ir, snap, set()).multiset()
    # NULL > 5 为 UNKNOWN 被过滤；补 NULL 行同样被过滤 -> 基线仅 (1,6)
    assert base == {(1, 6): 1}
    res = solve_minimum_withhold(ir, snap, {(1, 6)}, [1, 2])
    assert res.status == "solution" and res.withhold_rows == [2]
    # 暂扣后站点 1 无匹配存活行，补 NULL 行也被 WHERE 过滤 -> 结果为空
    assert evaluate(ir, snap, {2}).multiset() == {}
