"""数据查询服务：把 nlsql 的查询能力包成 HTTP API。

- FastAPI，监听 127.0.0.1:8005
- Agent 不再直连数据库，通过本服务查询；SQL 编译与守门是服务内部实现细节
- 端点：POST /q/plan（Plan JSON 查询）、POST /q/sql（受控自由查询）、
        GET /q/schema（结构探索）、POST /q/stats（表统计）
"""
from __future__ import annotations

import os

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI
from pydantic import BaseModel

import nlsql

load_dotenv()

app = FastAPI(title="Data Query Service")


class PlanReq(BaseModel):
    plan_json: str
    draw: bool = True
    chart_type: str = "auto"


class SqlReq(BaseModel):
    sql: str
    params_json: str = "[]"
    draw: bool = True
    chart_type: str = "auto"


class StatsReq(BaseModel):
    database: str
    table: str


@app.post("/q/plan")
def q_plan(req: PlanReq) -> dict:
    """语义层查询：Plan JSON -> 校验 -> SQL 编译 -> 只读执行。"""
    result = nlsql.run_plan(req.plan_json)
    if isinstance(result, dict):
        result["draw"] = req.draw
        result["chart_type"] = req.chart_type if req.chart_type in ("bar", "pie", "line") else "auto"
    return result


@app.post("/q/sql")
def q_sql(req: SqlReq) -> dict:
    """受控自由查询：SELECT SQL 经五重守门后执行。"""
    import json as _json
    try:
        params = _json.loads(req.params_json) if req.params_json else []
    except _json.JSONDecodeError:
        params = []
    result = nlsql.free_query(req.sql, params)
    if isinstance(result, dict):
        result["draw"] = req.draw
        result["chart_type"] = req.chart_type if req.chart_type in ("bar", "pie", "line") else "auto"
    return result


@app.get("/q/schema")
def q_schema(database: str = "", table: str = "") -> dict:
    """结构探索：三库表清单/列定义。"""
    return nlsql.explore_schema(database, table)


@app.post("/q/stats")
def q_stats(req: StatsReq) -> dict:
    """表统计速查：行数/时间范围/维度基数。"""
    return nlsql.table_stats(req.database, req.table)


@app.get("/healthz")
def healthz() -> dict:
    """健康检查：验证数据库连通。"""
    try:
        with nlsql._db("information_schema") as c, c.cursor() as cur:
            cur.execute("SELECT 1")
        return {"ok": True, "service": "datasvc"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DATASVC_PORT", "8005")))
