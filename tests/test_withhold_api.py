"""/withhold API 测试：输入校验 400、SOLVED（打桩复算）、NO_SOLUTION、
UNKNOWN（复算无法确认）、文档示例可用性。"""
from fastapi.testclient import TestClient

from app import main
from app.main import app

client = TestClient(app)

_BASE_BODY = {
    "query": (
        "SELECT s.id, r.value FROM stations s "
        "LEFT JOIN readings r ON s.id = r.station_id "
        "WHERE r.value IS NULL OR r.value >= 10"
    ),
    "stations": [[1, 5], [2, None]],
    "readings": [[1, 99], [1, 10], [2, 7], [None, 3]],
    "false_positive_rows": [[1, 99]],
    "withholdable_reading_rows": [1, 2, 3],
}


class _OkRecheck:
    """模拟隔离 PostgreSQL 复算通过。"""

    def __init__(self, dsn):
        pass

    def recheck_withhold(
        self,
        stations,
        readings,
        query,
        withheld_rows,
        fp_tuples,
        expected_before,
        expected_after,
    ):
        from app.verifier import WithholdRecheck

        return WithholdRecheck(
            ok=True,
            before_result=expected_before,
            after_result=expected_after,
        )


class _FailRecheck:
    def __init__(self, dsn):
        pass

    def recheck_withhold(self, *args, **kwargs):
        from app.verifier import WithholdRecheck

        return WithholdRecheck(ok=False, detail="PostgreSQL 复算失败: 连接被拒")


def test_withhold_solved_with_stubbed_recheck(monkeypatch):
    monkeypatch.setattr(main, "Verifier", _OkRecheck)
    resp = client.post("/withhold", json=_BASE_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "SOLVED"
    assert body["withhold_rows"] == [1]
    assert body["recheck"]["before_result"]
    assert [1, 99] not in body["recheck"]["after_result"]
    # 其他结果保留
    assert [1, 10] in body["recheck"]["after_result"]


def test_withhold_rejects_non_left_join():
    body = {
        **_BASE_BODY,
        "query": "SELECT s.id FROM stations s INNER JOIN readings r "
        "ON s.id = r.station_id",
        "false_positive_rows": [[1]],
    }
    resp = client.post("/withhold", json=body)
    assert resp.status_code == 400
    assert "LEFT JOIN" in resp.json()["detail"]


def test_withhold_rejects_wrong_join_condition():
    body = {
        **_BASE_BODY,
        "query": "SELECT s.id FROM stations s LEFT JOIN readings r "
        "ON s.threshold = r.value",
        "false_positive_rows": [[1]],
    }
    resp = client.post("/withhold", json=body)
    assert resp.status_code == 400
    assert "station_id" in resp.json()["detail"]


def test_withhold_rejects_unsupported_sql():
    resp = client.post("/withhold", json={**_BASE_BODY, "query": "SELECT * FROM stations"})
    assert resp.status_code == 400
    assert "受支持子集" in resp.json()["detail"]


def test_withhold_rejects_snapshot_violations():
    # 外键悬空
    bad = {**_BASE_BODY, "readings": [[9, 1]]}
    r = client.post("/withhold", json=bad)
    assert r.status_code == 400 and "外键" in r.json()["detail"]
    # 行号越界
    bad = {**_BASE_BODY, "withholdable_reading_rows": [99]}
    r = client.post("/withhold", json=bad)
    assert r.status_code == 400 and "超出" in r.json()["detail"]
    # 误报行列数与投影不一致
    bad = {**_BASE_BODY, "false_positive_rows": [[1]]}
    r = client.post("/withhold", json=bad)
    assert r.status_code == 400 and "列数" in r.json()["detail"]
    # 行数上限
    bad = {**_BASE_BODY, "stations": [[i, 1] for i in range(4)]}
    r = client.post("/withhold", json=bad)
    assert r.status_code == 400 and "最多 3 行" in r.json()["detail"]


def test_withhold_no_solution_null_extended(monkeypatch):
    monkeypatch.setattr(main, "Verifier", _OkRecheck)
    body = {
        "query": (
            "SELECT s.id, r.value FROM stations s "
            "LEFT JOIN readings r ON s.id = r.station_id "
            "WHERE r.value IS NULL"
        ),
        "stations": [[1, 5], [2, 9]],
        "readings": [[2, 8]],
        "false_positive_rows": [[1, None]],
        "withholdable_reading_rows": [1],
    }
    resp = client.post("/withhold", json=body)
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "NO_SOLUTION"
    assert resp.json()["withhold_rows"] is None


def test_withhold_unknown_when_recheck_unavailable(monkeypatch):
    monkeypatch.setattr(main, "Verifier", _FailRecheck)
    resp = client.post("/withhold", json=_BASE_BODY)
    assert resp.status_code == 200
    body = resp.json()
    assert body["verdict"] == "UNKNOWN"
    assert "PostgreSQL" in body["message"]


def test_withhold_null_equals_null_multiplicity(monkeypatch):
    """NULL=NULL 比较：误报 (1,NULL) 有读数行与补 NULL 行两个来源时无解。"""
    monkeypatch.setattr(main, "Verifier", _OkRecheck)
    body = {
        "query": (
            "SELECT s.id, r.value FROM stations s "
            "LEFT JOIN readings r ON s.id = r.station_id"
        ),
        "stations": [[1, 5]],
        "readings": [[1, None]],
        "false_positive_rows": [[1, None]],
        "withholdable_reading_rows": [1],
    }
    resp = client.post("/withhold", json=body)
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "NO_SOLUTION"


def test_withhold_docs_include_endpoint_and_examples():
    spec = client.get("/openapi.json").json()
    assert "/withhold" in spec["paths"]
    examples = spec["paths"]["/withhold"]["post"]["requestBody"]["content"][
        "application/json"
    ]["examples"]
    assert "withhold-false-positive-reading" in examples
