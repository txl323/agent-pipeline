"""数据查询类工具：语义层查询、结构探索、受控自由查询、表统计、CSV 导出。"""

from __future__ import annotations

import json
import os
import re


def query_data(plan_json: str, draw: bool = True, chart_type: str = "auto") -> dict:
    """受控数据查询：Plan JSON -> 语义层校验 -> SQL 编译 -> 只读执行。

    Plan JSON 格式：
    {"metric": "指标名", "dimensions": ["维度名"], "filters": [{"field": "字段", "op": "=", "value": "值"}], "time_macro": "时间宏"}
    draw=False 时不生成图表（结构/单值/清单类问题用）。
    chart_type 指定图表类型：bar/pie/line，auto 为系统自动判断。
    """
    from nlsql import run_plan
    result = run_plan(plan_json)
    if isinstance(result, dict):
        result["draw"] = draw
        result["chart_type"] = chart_type if chart_type in ("bar", "pie", "line") else "auto"
    return result


def explore_schema(database: str = "", table: str = "") -> dict:
    """探索数据结构：查看三个业务库有哪些表、字段、行数。"""
    from nlsql import explore_schema as _es
    return _es(database, table)


def free_query(sql: str, params_json: str = "[]", draw: bool = True, chart_type: str = "auto") -> dict:
    """受控自由查询：直接写 SELECT SQL，系统守门（白名单/只读/限行/EXPLAIN）后执行。

    draw=False 时不生成图表（结构/单值/清单类问题用）。
    chart_type 指定图表类型：bar/pie/line，auto 为系统自动判断。
    """
    import json as _json
    from nlsql import free_query as _fq
    try:
        params = _json.loads(params_json) if params_json else []
    except _json.JSONDecodeError:
        params = []
    result = _fq(sql, params)
    if isinstance(result, dict):
        result["draw"] = draw
        result["chart_type"] = chart_type if chart_type in ("bar", "pie", "line") else "auto"
    return result


# ---------------- 新增：表统计速查 ----------------

_TABLE_STATS_MAX_COLS = 20


def table_stats(database: str, table: str) -> dict:
    """表统计速查：行数、时间范围、维度基数，辅助口径判断。

    返回 {ok, database, table, row_count, time_range: {min, max}, cardinality: {列名: 去重数}}
    自动识别日期/时间类型列取时间范围；对低基数文本列（去重数<=50）统计基数。
    """
    from nlsql import ALLOWED_DATABASES, _db
    if database not in ALLOWED_DATABASES:
        return {"ok": False, "error": f"库不在白名单: {database}，可选: {list(ALLOWED_DATABASES)}"}
    try:
        with _db("information_schema") as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT column_name, data_type FROM columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (database, table),
                )
                cols = cur.fetchall()
        if not cols:
            return {"ok": False, "error": f"表不存在: {database}.{table}"}
        col_names = [r[0] for r in cols]
        date_cols = [r[0] for r in cols if r[1] in ("date", "datetime", "timestamp")]
        text_cols = [r[0] for r in cols if r[1] in ("char", "varchar")][:_TABLE_STATS_MAX_COLS]

        with _db(database) as c, c.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM `{database}`.`{table}`")
            row_count = cur.fetchone()[0]

            time_range = None
            if date_cols:
                dc = date_cols[0]
                cur.execute(
                    f"SELECT MIN(`{dc}`), MAX(`{dc}`) FROM `{database}`.`{table}`"
                )
                mn, mx = cur.fetchone()
                if mn is not None:
                    time_range = {"column": dc,
                                  "min": mn.isoformat() if hasattr(mn, "isoformat") else str(mn),
                                  "max": mx.isoformat() if hasattr(mx, "isoformat") else str(mx)}

            cardinality = {}
            for tc in text_cols:
                cur.execute(
                    f"SELECT COUNT(DISTINCT `{tc}`) FROM `{database}`.`{table}`"
                )
                n = cur.fetchone()[0]
                if n <= 50:
                    cardinality[tc] = n

        return {"ok": True, "database": database, "table": table,
                "row_count": row_count, "time_range": time_range,
                "cardinality": cardinality}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"统计失败: {e}"}


# ---------------- 新增：查询结果导出 CSV ----------------

_EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")


def export_csv(sql: str, filename: str = "") -> dict:
    """导出查询结果为 CSV 文件：SELECT SQL 经 free_query 同款守门后执行，结果写入 exports/ 目录。

    返回 {ok, file, row_count, columns}；file 为相对项目根的路径。
    """
    from nlsql import free_query as _fq
    result = _fq(sql, [])
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
