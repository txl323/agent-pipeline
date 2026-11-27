# -*- coding: utf-8 -*-
"""固定回归测试（阶段②）：4 个基线问题端到端跑通 agent_lg.run_agent_stream。

断言：
1. 每个基线问题在 2 次 query_data 调用内成功（reply 非空）；
2. 查询计划无非法维度（维度必须来自 semantic_v1.yaml 的 dimensions 白名单）；
3. 无 free_query 降级（受限场景必须走语义层 query_data）；
4. 统一出图：多行结果问题恰好推送 1 张 chart，单值问题不推送 chart。

运行前提：.env 配置 DEEPSEEK_API_KEY 与数据库连接；datasvc(8005) 已启动。
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from agent_lg import run_agent_stream  # noqa: E402

# semantic_v1.yaml 合法维度白名单
VALID_DIMS = {
    "customer_region", "material", "material_code", "customer",
    "order_date", "order_month", "supplier", "supplier_region", "warehouse",
}
MAX_QUERY_CALLS = 2

# (问题, 期望出现的维度/过滤字段, 是否期望出图)
BASELINES = [
    ("华东区域上个月的销售总额是多少", "customer_region", False),
    ("华东区域各物料的销售额排名", ["customer_region", "material"], True),
    ("各区域销售额对比", "customer_region", True),
    ("按月的销售趋势", "order_date", True),
]


async def _run_async(question: str):
    events = []
    final: dict = {}
    sid = f"regtest-{int(time.time())}"
    async for ev, data in run_agent_stream(
        [{"role": "user", "content": question}],
        sid=sid,
    ):
        if ev == "final":
            final = data
        else:
            events.append((ev, data))
    return final, events


def _run(question: str):
    return asyncio.run(_run_async(question))


def _query_plans(events):
    """从 tool_start 事件提取全部 query_data 的 plan_json（完整参数，不受 trace 截断影响）。"""
    plans = []
    for name, data in events:
        if name == "tool_start" and data.get("tool") == "query_data":
            try:
                plans.append(json.loads(data["args"].get("plan_json", "{}")))
            except (json.JSONDecodeError, AttributeError):
                plans.append({})
    return plans


async def _check_case(question, expect, want_chart):
    result, events = await _run_async(question)
    ctx = f"[{question}]"

    # 1. 成功：最终回复非空
    assert result.get("reply"), f"{ctx} 无最终回复"

    # 2. 2 次调用内成功
    plans = _query_plans(events)
    assert 1 <= len(plans) <= MAX_QUERY_CALLS, \
        f"{ctx} query_data 调用 {len(plans)} 次（上限 {MAX_QUERY_CALLS}）"

    # 3. 无 free_query 降级
    free_calls = [d for n, d in events if n == "tool_start" and d.get("tool") == "free_query"]
    assert not free_calls, f"{ctx} 发生 free_query 降级: {free_calls}"

    # 4. 无非法维度 + 期望维度命中
    expects = expect if isinstance(expect, list) else [expect]
    for plan in plans:
        dims = plan.get("dimensions") or []
        illegal = [d for d in dims if d not in VALID_DIMS]
        assert not illegal, f"{ctx} 非法维度: {illegal}（plan={plan}）"
        hit = set(dims) | {f.get("field") for f in (plan.get("filters") or [])}
        for exp in expects:
            # 月粒度维度 order_month 可等价满足 order_date 期望（按月趋势优先 order_month）
            assert exp in hit or (exp == "order_date" and "order_month" in hit), \
                f"{ctx} 未命中期望字段 {exp}（plan={plan}）"

    # 5. 统一出图：多行问题恰好 1 张 chart，单值问题不出图
    charts = [d for n, d in events if n == "chart"]
    if want_chart:
        assert len(charts) == 1, f"{ctx} 期望恰好 1 张图，实际 {len(charts)} 张"
        assert charts[0].get("rows"), f"{ctx} chart payload 无数据行"
    else:
        assert not charts, f"{ctx} 单值问题不应出图，实际推送 {len(charts)} 张"

    return result, events


async def _run_all():
    for question, expect, want_chart in BASELINES:
        await _check_case(question, expect, want_chart)


def test_regression_baselines():
    # 单事件循环跑全部基线：AsyncSqliteSaver 的 asyncio.Lock 绑定首个循环，
    # 多次 asyncio.run 会触发 bound to a different event loop
    asyncio.run(_run_all())


if __name__ == "__main__":
    async def _main():
        for q, e, c in BASELINES:
            r, ev = await _run_async(q)
            reply = (r.get("reply") or "")[:120].replace("\n", " ")
            plans = _query_plans(ev)
            charts = [d for n, d in ev if n == "chart"]
            print(f"Q: {q}")
            print(f"  plans={len(plans)} charts={len(charts)} reply={reply}")
    asyncio.run(_main())
