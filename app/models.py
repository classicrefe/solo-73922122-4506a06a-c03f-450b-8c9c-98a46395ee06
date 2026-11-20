"""API 请求 / 响应模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .ir import MAX_ROWS


class CheckRequest(BaseModel):
    query_a: str = Field(..., description="原始 SELECT（当前在用的质控 SQL）")
    query_b: str = Field(..., description="改写后的 SELECT")
    solver_timeout_ms: int | None = Field(
        None,
        ge=100,
        le=120000,
        description="z3 求解超时（毫秒）；缺省取环境变量 SOLVER_TIMEOUT_MS 或 10000",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query_a": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value > 0"
                    ),
                    "query_b": (
                        "SELECT s.id, r.value FROM stations s "
                        "INNER JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value >= 1"
                    ),
                }
            ]
        }
    }


class Counterexample(BaseModel):
    setup_sql: str = Field(..., description="可装载的建表与数据 SQL 脚本")
    query_a_result: list[list[Any]] = Field(..., description="反例数据下 query_a 的实际结果")
    query_b_result: list[list[Any]] = Field(..., description="反例数据下 query_b 的实际结果")


class CheckResponse(BaseModel):
    verdict: Literal["EQUIVALENT", "NOT_EQUIVALENT", "UNKNOWN"] = Field(
        ...,
        description=(
            "EQUIVALENT=界内无反例；NOT_EQUIVALENT=差异已在隔离 PostgreSQL 复现；"
            "UNKNOWN=求解或复现失败"
        ),
    )
    bound_rows: int = Field(
        MAX_ROWS, description="等价结论成立的每表行数上界（含 0 行）"
    )
    message: str = Field(..., description="判定说明")
    counterexample: Counterexample | None = Field(
        None, description="verdict=NOT_EQUIVALENT 时给出可复现的反例"
    )


# ---------- 误报暂扣 ----------

class WithholdRequest(BaseModel):
    query: str = Field(
        ...,
        description=(
            "当前在用的质控 SELECT；必须为 stations 按 id = station_id "
            "LEFT JOIN readings，沿用既有投影与 WHERE 语法子集"
        ),
    )
    stations: list[list[Any]] = Field(
        ...,
        description="stations 快照行 [id, threshold]，integer 或 NULL（id 非空），最多 3 行",
    )
    readings: list[list[Any]] = Field(
        ...,
        description=(
            "readings 快照行 [station_id, value]，integer 或 NULL，最多 24 行；"
            "行号即数组下标（从 1 起）"
        ),
    )
    false_positive_rows: list[list[Any]] = Field(
        ...,
        description="已确认误报的结果行（按列位置给出，两个 NULL 视为相等）",
    )
    withholdable_reading_rows: list[int] = Field(
        ...,
        description="允许暂扣的 readings 输入行号（从 1 起）",
    )
    solver_timeout_ms: int | None = Field(
        None, ge=100, le=120000, description="求解超时（毫秒），缺省取环境变量或 10000"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "query": (
                        "SELECT s.id, r.value FROM stations s "
                        "LEFT JOIN readings r ON s.id = r.station_id "
                        "WHERE r.value IS NULL OR r.value >= 10"
                    ),
                    "stations": [[1, 5], [2, None]],
                    "readings": [
                        [1, 99],
                        [1, 10],
                        [2, 7],
                        [None, 3],
                    ],
                    "false_positive_rows": [[1, 99]],
                    "withholdable_reading_rows": [1, 2, 3],
                }
            ]
        }
    }


class WithholdRecheckInfo(BaseModel):
    before_result: list[list[Any]] = Field(
        ..., description="隔离 PostgreSQL 中暂扣前查询的实际结果（多重集，保重复）"
    )
    after_result: list[list[Any]] = Field(
        ..., description="隔离 PostgreSQL 中暂扣后查询的实际结果"
    )


class WithholdResponse(BaseModel):
    verdict: Literal["SOLVED", "NO_SOLUTION", "UNKNOWN"] = Field(
        ...,
        description=(
            "SOLVED=已找到最少暂扣行集合并经隔离 PostgreSQL 复算确认；"
            "NO_SOLUTION=不存在满足条件的暂扣组合；UNKNOWN=求解或复算无法确认"
        ),
    )
    withhold_rows: list[int] | None = Field(
        None, description="verdict=SOLVED 时数量最少的暂扣 readings 行号（从 1 起）"
    )
    message: str = Field(..., description="判定说明")
    recheck: WithholdRecheckInfo | None = Field(
        None, description="verdict=SOLVED 时隔离 PostgreSQL 的暂扣前后复算结果"
    )
