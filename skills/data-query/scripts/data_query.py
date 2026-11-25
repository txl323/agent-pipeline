"""data-query 技能实现：通过数据查询服务（datasvc）访问业务库。

Agent 不直连数据库：SQL 编译、守门校验、执行全部是数据服务内部的实现细节。
"""

from __future__ import annotations

import json
import os

_DATASVC = os.environ.get("DATASVC_URL", "http://127.0.0.1:8005")


def _post(path: str, payload: dict, timeout: float = 30.0) -> dict:
    import httpx
    resp = httpx.post(f"{_DATASVC}{path}", json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _get(path: str, params: dict, timeout: float = 30.0) -> dict:
    import httpx
    resp = httpx.get(f"{_DATASVC}{path}", params=params, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def query_data(plan_json: str, draw: bool = True, chart_type: str = "auto") -> dict:
    """受控数据查询：Plan JSON -> 语义层校验 -> SQL 编译 -> 只读执行。"""
    try:
        return _post("/q/plan", {"plan_json": plan_json, "draw": draw, "chart_type": chart_type})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"数据服务调用失败: {e}"}


def explore_schema(database: str = "", table: str = "") -> dict:
    """探索数据结构：查看三个业务库有哪些表、字段、行数。"""
    try:
        return _get("/q/schema", {"database": database, "table": table})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"数据服务调用失败: {e}"}


def free_query(sql: str, params_json: str = "[]", draw: bool = True, chart_type: str = "auto") -> dict:
    """受控自由查询：SELECT SQL 经五重守门后执行。"""
    try:
        params = json.loads(params_json) if params_json else []
    except json.JSONDecodeError:
        params = []
    try:
        return _post("/q/sql", {"sql": sql, "params_json": json.dumps(params, ensure_ascii=False),
                                 "draw": draw, "chart_type": chart_type})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"数据服务调用失败: {e}"}


def table_stats(database: str, table: str) -> dict:
    """表统计速查：行数、时间范围、维度基数。"""
    try:
        return _post("/q/stats", {"database": database, "table": table})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"数据服务调用失败: {e}"}
