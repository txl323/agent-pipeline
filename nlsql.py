"""受控取数模块：语义层 + Plan 编译 + 代码守门执行。

设计原则（对应规划文档）：
- LLM 只输出结构化 Plan JSON，不裸写 SQL
- 白名单校验、权限下推、EXPLAIN 护栏、只读执行全部在代码侧
- 每次执行落 trace_artifact，数字可反查 SQL/参数/语义层版本
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pymysql
import yaml
from dotenv import load_dotenv

load_dotenv()

SEMANTIC_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "semantic", "semantic_v1.yaml")

MAX_ROWS = 10_000
QUERY_TIMEOUT_S = 30
EXPLAIN_MAX_ROWS = 1_000_000

_semantic_cache: dict | None = None


def load_semantic() -> dict:
    """加载语义层（进程内缓存）。"""
    global _semantic_cache
    if _semantic_cache is None:
        with open(SEMANTIC_FILE, encoding="utf-8") as f:
            _semantic_cache = yaml.safe_load(f)
    return _semantic_cache


def semantic_version() -> str:
    return str(load_semantic().get("version", "unknown"))


def _db(database: str) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.Cursor,
        read_timeout=QUERY_TIMEOUT_S,
        write_timeout=QUERY_TIMEOUT_S,
    )


# ---------------- 时间宏展开（归编译器） ----------------

def _expand_time_macro(macro: str) -> tuple[str, str] | None:
    """宏名 -> (start, end) 日期字符串。"""
    today = date.today()
    if macro == "all":
        return None
    if macro == "last_month":
        first = today.replace(day=1) - timedelta(days=1)
        last = first.replace(day=1)
        return last.replace(day=1).isoformat(), first.isoformat()
    if macro == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if macro == "last_7_days":
        return (today - timedelta(days=7)).isoformat(), today.isoformat()
    if macro == "last_30_days":
        return (today - timedelta(days=30)).isoformat(), today.isoformat()
    return None


# ---------------- Plan 校验（白名单） ----------------

def validate_plan(plan: dict) -> tuple[bool, str]:
    """校验 LLM 输出的 Plan JSON 是否在语义层白名单内。"""
    sem = load_semantic()
    if not isinstance(plan, dict):
        return False, "plan 必须是 JSON 对象"
    metric = plan.get("metric")
    if not metric or metric not in sem["metrics"]:
        return False, f"未知指标: {metric}，可选: {list(sem['metrics'])}"
    m = sem["metrics"][metric]
    ds = m.get("datasource", "")
    dims = plan.get("dimensions") or []
    for d in dims:
        if d not in sem["dimensions"]:
            return False, f"未知维度: {d}"
        if ds not in sem["dimensions"][d].get("applies_to", []):
            return False, f"维度 {d} 不适用于指标 {metric}"
    for f in plan.get("filters") or []:
        field = f.get("field")
        if field not in sem["filter_fields"].get(ds, {}):
            return False, f"过滤字段不在白名单: {field}"
        op = str(f.get("op", "")).upper()
        f["op"] = op
        if op not in sem["filter_fields"][ds][field]["ops"]:
            return False, f"操作符不允许: {f.get('op')}"
    tm = plan.get("time_macro")
    if tm and tm not in sem.get("time_macros", {}):
        return False, f"未知时间宏: {tm}"
    return True, ""


# ---------------- SQL 编译（权限下推 + 宏展开） ----------------

def compile_sql(plan: dict, allowed_regions: list[str] | None = None) -> tuple[str, list, str]:
    """Plan -> (sql, params, datasource)。权限 region IN 由编译器注入，非 LLM 提供。"""
    sem = load_semantic()
    metric = plan["metric"]
    m = sem["metrics"][metric]
    ds = m["datasource"]

    def _dim_col(d: str) -> str:
        """维度物理列：支持字符串或按数据源映射的字典。"""
        col = sem["dimensions"][d]["column"]
        if isinstance(col, dict):
            return col[ds]
        return col

    dims = plan.get("dimensions") or []
    select_parts = [m["sql"] + " AS value"]
    group_cols: list[str] = []
    for d in dims:
        col = _dim_col(d)
        select_parts.append(f"{col} AS `{d}`")
        group_cols.append(col)

    joins: list[str] = []
    for d in dims:
        j = sem["dimensions"][d].get("join") or ""
        if j and j not in joins:
            joins.append(j)

    where: list[str] = []
    params: list = []

    # 时间宏展开（归编译器）
    tm = plan.get("time_macro")
    if tm:
        rng = _expand_time_macro(tm)
        if rng:
            date_col = "so.order_date" if ds == "sales" else "po.order_date"
            where.append(f"{date_col} BETWEEN %s AND %s")
            params.extend(rng)

    # 过滤条件（白名单列）
    for f in plan.get("filters") or []:
        spec = sem["filter_fields"][ds][f["field"]]
        col = spec["column"]
        # 过滤列引用 JOIN 别名（如 c.region）时，按列前缀补齐对应 JOIN
        if "." in col:
            prefix = col.split(".")[0] + "."
            for dspec in sem["dimensions"].values():
                dcol = dspec["column"]
                if isinstance(dcol, dict):
                    dcol = dcol.get(ds, "")
                if dcol.startswith(prefix) and dspec.get("join") and dspec["join"] not in joins:
                    joins.append(dspec["join"])
        op = f["op"]
        val = f.get("value")
        if op == "IN":
            if not isinstance(val, list) or not val:
                continue
            where.append(f"{col} IN ({', '.join(['%s'] * len(val))})")
            params.extend(val)
        elif op == "BETWEEN":
            if not isinstance(val, list) or len(val) != 2:
                continue
            where.append(f"{col} BETWEEN %s AND %s")
            params.extend(val)
        elif op == "LIKE":
            where.append(f"{col} LIKE %s")
            params.append(f"%{val}%")
        else:
            where.append(f"{col} {op} %s")
            params.append(val)

    # 权限下推：区域过滤注入 SQL 层（不靠事后过滤）
    if allowed_regions is not None:
        region_col = "c.region" if ds == "sales" else "s.region"
        if ds == "sales" and "JOIN mdm_platform.md_customers c ON so.customer_code = c.customer_code" not in joins:
            joins.append("JOIN mdm_platform.md_customers c ON so.customer_code = c.customer_code")
        if ds == "purchase" and "JOIN mdm_platform.md_suppliers s ON po.supplier_code = s.supplier_code" not in joins:
            joins.append("JOIN mdm_platform.md_suppliers s ON po.supplier_code = s.supplier_code")
        if ds in ("sales", "purchase"):
            if not allowed_regions:
                raise PermissionError("无任何区域权限")
            where.append(f"{region_col} IN ({', '.join(['%s'] * len(allowed_regions))})")
            params.extend(allowed_regions)

    sql = f"SELECT {', '.join(select_parts)} FROM {m['table']}"
    if joins:
        sql += " " + " ".join(joins)
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_cols:
        # 分组查询：LIMIT 收紧（图表场景足够）
        sql += " GROUP BY " + ", ".join(group_cols)
        # 时间维度强制升序（趋势图正确时间轴），其余按指标降序
        if "order_date" in (plan.get("dimensions") or []):
            sql += " ORDER BY order_date ASC LIMIT 1000"
        elif "order_month" in (plan.get("dimensions") or []):
            sql += " ORDER BY order_month ASC LIMIT 1000"
        else:
            sql += " ORDER BY value DESC LIMIT 1000"
    # 无分组：单值查询，不需要 ORDER BY/LIMIT
    return sql, params, ds


# ---------------- EXPLAIN 护栏 ----------------

def explain_check(sql: str, params: list, ds: str) -> tuple[bool, str]:
    """EXPLAIN 成本护栏：全表扫描/扫描行数超阈值 -> 拦截。"""
    try:
        with _db("sap_mock") as c, c.cursor() as cur:
            cur.execute("EXPLAIN " + sql, params)
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001
        return False, f"EXPLAIN 失败: {e}"
    total_rows = 0
    for r in rows:
        # EXPLAIN 列: id,select_type,table,partitions,type,possible_keys,key,key_len,ref,rows,Extra
        rtype = str(r[4] or "").upper()
        est = int(r[9] or 0)
        total_rows += est
        if rtype == "ALL" and est > 100_000:
            return False, f"全表扫描且预估行数过大: {r[1]} est={est}"
    if total_rows > EXPLAIN_MAX_ROWS:
        return False, f"预估扫描行数超阈值: {total_rows} > {EXPLAIN_MAX_ROWS}"
    return True, ""


# ---------------- 只读执行 ----------------

def execute_readonly(sql: str, params: list, ds: str) -> dict:
    """只读执行（SELECT 强校验），返回 {columns, rows, row_count, elapsed_ms}。"""
    if not re.match(r"^\s*SELECT\b", sql, re.I):
        raise PermissionError("仅允许 SELECT 查询")
    t0 = time.time()
    # 空参数传 None：避免 pymysql 把 SQL 中的 %（如 LIKE '%xx%'）当格式化占位符
    exec_args = params if params else None
    with _db("sap_mock") as c, c.cursor() as cur:
        cur.execute(sql, exec_args)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(MAX_ROWS)

    def _conv(v):
        if isinstance(v, Decimal):
            return float(v)
        if isinstance(v, (date, datetime)):
            return v.isoformat()
        return v

    return {
        "columns": cols,
        "rows": [[_conv(v) for v in r] for r in rows],
        "row_count": len(rows),
        "elapsed_ms": int((time.time() - t0) * 1000),
    }


# ---------------- 主入口：Plan JSON -> 结果 ----------------

def run_plan(plan_json: str, allowed_regions: list[str] | None = None, sid: str = "") -> dict:
    """LLM 工具入口：Plan JSON 字符串 -> 受控执行结果。

    返回 {ok, error, sql, params, sem_ver, columns, rows, row_count, elapsed_ms, artifact_id}
    """
    try:
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"Plan JSON 解析失败: {e}"}
    ok, err = validate_plan(plan)
    if not ok:
        return {"ok": False, "error": err}
    try:
        sql, params, ds = compile_sql(plan, allowed_regions)
    except PermissionError as e:
        return {"ok": False, "error": f"权限不足: {e}"}
    ok, err = explain_check(sql, params, ds)
    if not ok:
        return {"ok": False, "error": f"EXPLAIN 护栏拦截: {err}"}
    try:
        result = execute_readonly(sql, params, ds)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"执行失败: {e}"}
    result.update({"ok": True, "sql": sql, "params": params, "sem_ver": semantic_version()})
    try:
        result["stats"] = _stats_summary(result.get("columns", []), result.get("rows", []))
        result["artifact_ref"] = save_result(result, sid)
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------- 可追溯：artifact 落库 ----------------

# ---------------- 统计摘要 + 完整结果 artifact ----------------
def _stats_summary(columns: list, rows: list) -> dict:
    """代码计算统计摘要：最高/最低/总量/行数/是否截断。数字来自 SQL 结果，非 LLM 生成。"""
    if not rows or not columns:
        return {"row_count": len(rows), "truncated": False}
    num_idx = [i for i, r in enumerate(rows[0]) if isinstance(r, (int, float)) and not isinstance(r, bool)]
    if not num_idx:
        return {"row_count": len(rows), "truncated": len(rows) >= 1000}
    mi = num_idx[-1] if len(num_idx) > 1 else num_idx[0]
    vals = [(r[mi], r) for r in rows if isinstance(r[mi], (int, float))]
    if not vals:
        return {"row_count": len(rows), "truncated": len(rows) >= 1000}
    hi = max(vals, key=lambda x: x[0])
    lo = min(vals, key=lambda x: x[0])
    return {
        "metric_col": columns[mi],
        "max": {"value": hi[0], "row": list(hi[1])},
        "min": {"value": lo[0], "row": list(lo[1])},
        "total": round(sum(v for v, _ in vals), 2),
        "row_count": len(rows),
        "truncated": len(rows) >= 1000,
    }


def save_result(result: dict, sid: str = "") -> str:
    """完整查询结果落 result_store 表，返回 ref（供追问复用，不重查库）。"""
    ref = str(uuid.uuid4())
    with _db("agent_pipeline") as c, c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS result_store ("
            "id INT AUTO_INCREMENT PRIMARY KEY, ref VARCHAR(64) NOT NULL UNIQUE, "
            "sid VARCHAR(64), result_json MEDIUMTEXT, row_count INT, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, INDEX idx_ref (ref))"
        )
        cur.execute(
            "INSERT INTO result_store (ref, sid, result_json, row_count) VALUES (%s,%s,%s,%s)",
            (ref, sid, json.dumps(result, ensure_ascii=False), result.get("row_count", 0)),
        )
    return ref


def load_result(ref: str) -> dict:
    """按 ref 取回完整结果（追问复用，不重查库）。"""
    with _db("agent_pipeline") as c, c.cursor() as cur:
        cur.execute("SELECT result_json FROM result_store WHERE ref=%s", (ref,))
        row = cur.fetchone()
    if not row:
        return {"ok": False, "error": f"artifact 不存在: {ref}"}
    try:
        return json.loads(row[0])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"artifact 解析失败: {e}"}


def save_artifact(plan: dict, sql: str, params: list, result: dict, sid: str) -> str:
    """执行快照落 trace_artifact 表，返回 ref（UUID）。"""
    ref = str(uuid.uuid4())
    with _db("agent_pipeline") as c, c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS trace_artifact ("
            "id INT AUTO_INCREMENT PRIMARY KEY, ref VARCHAR(64) NOT NULL UNIQUE, "
            "sid VARCHAR(64), plan_json TEXT, sql_text TEXT, params_json TEXT, "
            "result_hash VARCHAR(64), row_count INT, sem_ver VARCHAR(32), "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)",
        )
        import hashlib
        rh = hashlib.sha256(json.dumps(result.get("rows", []), ensure_ascii=False).encode()).hexdigest()[:32]
        cur.execute(
            "INSERT INTO trace_artifact (ref, sid, plan_json, sql_text, params_json, result_hash, row_count, sem_ver) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
            (ref, sid, json.dumps(plan, ensure_ascii=False), sql,
             json.dumps(params, ensure_ascii=False), rh, result.get("row_count", 0), semantic_version()),
        )
    return ref


def query_lineage(ref: str) -> dict:
    """按 ref 反查血缘：SQL 全文/参数/结果 hash/语义层版本/执行时刻。"""
    with _db("agent_pipeline") as c, c.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM trace_artifact WHERE ref=%s", (ref,))
        row = cur.fetchone()
    return dict(row) if row else {}


# ---------------- 探索：查表结构（information_schema） ----------------

ALLOWED_DATABASES = ("bom_system", "mdm_platform", "sap_mock")


def explore_schema(database: str = "", table: str = "") -> dict:
    """查看演示库的表结构：表清单/列名/类型/行数。

    database 为空返回三个库的表清单（含行数）；指定库返回该库各表的列定义；
    再指定 table 返回单表列定义。
    """
    if database and database not in ALLOWED_DATABASES:
        return {"ok": False, "error": f"库不在白名单: {database}，可选: {list(ALLOWED_DATABASES)}"}
    try:
        with _db("information_schema") as c:
            if not database:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT table_schema, table_name, table_rows FROM tables "
                        "WHERE table_schema IN (%s,%s,%s) ORDER BY table_schema, table_name",
                        ALLOWED_DATABASES,
                    )
                    tables = [{"db": r[0], "table": r[1], "approx_rows": int(r[2] or 0)} for r in cur.fetchall()]
                return {"ok": True, "databases": list(ALLOWED_DATABASES), "tables": tables}
            if not table:
                with c.cursor() as cur:
                    cur.execute(
                        "SELECT table_name, column_name, column_type FROM columns "
                        "WHERE table_schema=%s ORDER BY table_name, ordinal_position",
                        (database,),
                    )
                    cols = cur.fetchall()
                by_table: dict = {}
                for r in cols:
                    by_table.setdefault(r[0], []).append({"column": r[1], "type": r[2]})
                return {"ok": True, "database": database, "tables": by_table}
            with c.cursor() as cur:
                cur.execute(
                    "SELECT column_name, column_type FROM columns "
                    "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position",
                    (database, table),
                )
                rows = cur.fetchall()
            if not rows:
                return {"ok": False, "error": f"表不存在: {database}.{table}"}
            return {"ok": True, "database": database, "table": table,
                    "columns": [{"column": r[0], "type": r[1]} for r in rows]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"探索失败: {e}"}


# ---------------- 受控自由查询（LLM 写 SQL + 代码守门） ----------------

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|lock|call|load_file|outfile)\b",
    re.I,
)


def free_query(sql: str, params: list | None = None,
              allowed_regions: list[str] | None = None, sid: str = "") -> dict:
    """长尾查询兜底：LLM 直接生成 SQL，代码守门后执行。

    守门规则：
    - 只允许单条 SELECT（禁止任何写操作/多语句）
    - 表必须在三个演示库白名单内（FROM/JOIN 必须带库名前缀）
    - 强制 LIMIT（无 LIMIT 自动加 1000，上限 10000）
    - EXPLAIN 护栏（扫描行数超阈值拦截）
    """
    if allowed_regions is not None:
        return {"ok": False,
                "error": "权限不足：当前用户受区域权限限制，free_query 已禁用。请使用 query_data（语义层查询，区域权限自动下推到 WHERE）"}
    params = params or []
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return {"ok": False, "error": "SQL 为空"}
    if ";" in sql:
        return {"ok": False, "error": "禁止多语句"}
    if not re.match(r"^SELECT\b", sql, re.I):
        return {"ok": False, "error": "仅允许 SELECT 查询"}
    if _FORBIDDEN_SQL.search(sql):
        return {"ok": False, "error": "SQL 含禁止的关键字"}
    refs = re.findall(r"(?:from|join)\s+([a-z_]+\.[a-z_0-9]+)", sql, re.I)
    if not refs:
        return {"ok": False, "error": "表引用必须带库名前缀，如 sap_mock.sap_sales_orders"}
    for ref in refs:
        db = ref.split(".")[0]
        if db not in ALLOWED_DATABASES:
            return {"ok": False, "error": f"库不在白名单: {db}，可选: {list(ALLOWED_DATABASES)}"}
    if not re.search(r"\blimit\s+\d+", sql, re.I):
        sql += " LIMIT 1000"
    else:
        m = re.search(r"\blimit\s+(\d+)", sql, re.I)
        if int(m.group(1)) > MAX_ROWS:
            sql = sql[:m.start()] + f"LIMIT {MAX_ROWS}"
    try:
        with _db("sap_mock") as c, c.cursor() as cur:
            cur.execute("EXPLAIN " + sql, params if params else None)
            plan = cur.fetchall()
        total = sum(int(r[9] or 0) for r in plan)
        if total > EXPLAIN_MAX_ROWS:
            return {"ok": False, "error": f"EXPLAIN 护栏拦截：预估扫描 {total} 行超阈值，请加过滤条件"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"EXPLAIN 失败: {e}"}
    try:
        result = execute_readonly(sql, params, "free")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"执行失败: {e}"}
    result.update({"ok": True, "sql": sql, "params": params, "sem_ver": semantic_version()})
    try:
        result["stats"] = _stats_summary(result.get("columns", []), result.get("rows", []))
        result["artifact_ref"] = save_result(result, sid)
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------- 表统计速查 ----------------

_TABLE_STATS_MAX_COLS = 20


def table_stats(database: str, table: str) -> dict:
    """表统计速查：行数、时间范围、维度基数，辅助口径判断。

    返回 {ok, database, table, row_count, time_range: {min, max}, cardinality: {列名: 去重数}}
    自动识别日期/时间类型列取时间范围；对低基数文本列（去重数<=50）统计基数。
    """
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
