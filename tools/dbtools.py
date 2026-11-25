"""数据查询类工具：通过数据查询服务（datasvc，HTTP API）访问业务库。

Agent 侧不直连数据库：SQL 编译、守门校验、执行全部是数据服务内部的实现细节。
"""

from __future__ import annotations

import json
import os
import re

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
    """受控数据查询：Plan JSON -> 语义层校验 -> SQL 编译 -> 只读执行。

    Plan JSON 格式：
    {"metric": "指标名", "dimensions": ["维度名"], "filters": [{"field": "字段", "op": "=", "value": "值"}], "time_macro": "时间宏"}
    draw=False 时不生成图表（结构/单值/清单类问题用）。
    chart_type 指定图表类型：bar/pie/line，auto 为系统自动判断。
    """
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
    """受控自由查询：直接写 SELECT SQL，数据服务守门（白名单/只读/限行/EXPLAIN）后执行。

    draw=False 时不生成图表（结构/单值/清单类问题用）。
    chart_type 指定图表类型：bar/pie/line，auto 为系统自动判断。
    """
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
    """表统计速查：行数、时间范围、维度基数，辅助口径判断。"""
    try:
        return _post("/q/stats", {"database": database, "table": table})
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"数据服务调用失败: {e}"}


# ---------------- 查询结果导出 CSV（本地文件操作） ----------------

_EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")


def export_csv(sql: str, filename: str = "") -> dict:
    """导出查询结果为 CSV 文件：SELECT SQL 经数据服务守门后执行，结果写入 exports/ 目录。

    返回 {ok, file, row_count, columns}；file 为相对项目根的路径。
    """
    result = free_query(sql, "[]", draw=False)
    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "error": (result or {}).get("error", "查询失败")}
    columns = result.get("columns", [])
    rows = result.get("rows", [])
    if not filename:
        m = re.search(r"from\s+([a-z_]+\.[a-z_0-9]+)", sql, re.I)
        base = m.group(1).replace(".", "_") if m else "export"
        import datetime as _dt
        filename = f"{base}_{_dt.datetime.now():%Y%m%d_%H%M%S}.csv"
    if not filename.endswith(".csv"):
        filename += ".csv"
    filename = os.path.basename(filename)
    os.makedirs(_EXPORT_DIR, exist_ok=True)
    path = os.path.join(_EXPORT_DIR, filename)
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(columns)
        w.writerows(rows)
    return {"ok": True, "file": f"exports/{filename}", "row_count": len(rows),
            "columns": columns}
