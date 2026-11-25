"""export-csv 技能实现：查询结果导出为 CSV 文件。"""

from __future__ import annotations

import os
import re

_EXPORT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))), "exports")


def export_csv(sql: str, filename: str = "") -> dict:
    """导出查询结果为 CSV 文件：SELECT SQL 经数据服务守门后执行，结果写入 exports/ 目录。"""
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "skills", "data-query", "scripts"))
    from data_query import free_query

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
