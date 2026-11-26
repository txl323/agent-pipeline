"""数据查询服务：把 nlsql 的查询能力包成 HTTP API。
- FastAPI，监听 127.0.0.1:8005
- Agent 不再直连数据库，通过本服务查询；SQL 编译与守门是服务内部实现细节
- 端点：POST /q/plan（Plan JSON 查询）、POST /q/sql（受控自由查询）、
        GET /q/schema（结构探索）、POST /q/stats（表统计）
- 区域权限：调用方（server.py）通过请求头 X-User-Regions 注入（逗号分隔区域列表，
  缺省头表示不受限）。权限由服务端注入，绝不从 LLM 参数传入。
  受限用户：query_data 编译时下推 WHERE region IN (...)；free_query 直接禁用。
"""
from __future__ import annotations
import json
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
    sid: str = ""
    user_regions: list[str] | None = None  # 区域权限（服务端注入，None 不受限）


class SqlReq(BaseModel):
    sql: str
    params_json: str = "[]"
    draw: bool = True
    chart_type: str = "auto"
    sid: str = ""
    user_regions: list[str] | None = None  # 区域权限（服务端注入，None 不受限）


class StatsReq(BaseModel):
    database: str
    table: str


@app.post("/q/plan")
def q_plan(req: PlanReq) -> dict:
    """语义层查询：Plan JSON -> 校验 -> SQL 编译（区域权限下推 WHERE）-> 只读执行。"""
    result = nlsql.run_plan(req.plan_json, allowed_regions=req.user_regions, sid=req.sid)
    if isinstance(result, dict):
        result["draw"] = req.draw
        result["chart_type"] = req.chart_type if req.chart_type in ("bar", "pie", "line") else "auto"
        # 时间维度强制折线图：趋势类查询（order_date/order_month）无论 LLM 传何图型一律 line
        try:
            _pj = json.loads(req.plan_json) if isinstance(req.plan_json, str) else req.plan_json
            if isinstance(_pj, dict) and any(d in (_pj.get("dimensions") or []) for d in ("order_date", "order_month")):
                result["chart_type"] = "line"
        except Exception:  # noqa: BLE001
            pass
    return result


@app.post("/q/sql")
def q_sql(req: SqlReq) -> dict:
    """受控自由查询：SELECT SQL 经五重守门后执行。受限用户（user_regions 非空）直接禁用。"""
    import json as _json
    try:
        params = _json.loads(req.params_json) if req.params_json else []
    except _json.JSONDecodeError:
        params = []
    result = nlsql.free_query(req.sql, params, allowed_regions=req.user_regions, sid=req.sid)
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


def _startup_sync_semantic() -> None:
    """启动时运行 sync_semantic --no-llm：感知库结构变更并生成提议，失败不阻断服务。"""
    import subprocess
    import sys
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(here, "sync_semantic.py"), "--no-llm"],
            cwd=here, capture_output=True, text=True, timeout=120,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        print(f"[datasvc] sync_semantic rc={r.returncode} {out}", flush=True)
        if err:
            print(f"[datasvc] sync_semantic stderr: {err[:500]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[datasvc] sync_semantic skipped: {e}", flush=True)


if __name__ == "__main__":
    import uvicorn
    _startup_sync_semantic()
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("DATASVC_PORT", "8005")))
