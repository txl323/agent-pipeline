"""Agent 核心：真流式工具调用循环。

- stream=True 逐 chunk 输出：思考(reasoning_content)、回复(content)实时推送
- 工具调用参数增量累积，finish_reason=tool_calls 时统一执行
- 三档思考模式：off（不推送过程）/ standard（deepseek-chat）/
  deep（deepseek-reasoner，含 reasoning_content 思考流）
- on_event(event, data) 回调：thinking / tool_start / tool_end / reply
"""

from __future__ import annotations

import json
import os
import re
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MAX_STEPS = 15

SYSTEM_PROMPT = """你是一个能干的智能助手，可以使用工具完成用户的任务。

【第一步：任务分类】收到用户消息后，先判断属于哪一类：
A. 结构类（有什么数据/什么表/什么字段/能查什么）
B. 业务数据类（销售额/订单/采购/库存/区域/物料/客户/供应商等指标问题）
C. 外部信息类（新闻/资料/事实，需要搜索互联网）
D. 闲聊/翻译/改写等无需工具的问题

【第二步：按分类选工具】
A 类 → explore_schema 查看库表结构，基于真实结构回答，严禁用业务指标硬答；
B 类 → 按决策树选：
   1) 语义层能表达（指标+维度+过滤都在 query_data 说明中）→ query_data 传 Plan JSON（口径有保障，优先）；
   2) 语义层表达不了（主数据统计/单价走势/占比计算/跨表计算）→ free_query 写 SELECT SQL（表必须带库名前缀，系统守门校验）；
   3) 不确定数据结构 → 先 explore_schema 再回来查询；
C 类 → web_search 搜索，需要深入阅读时 fetch_url 抓取页面；
D 类 → 直接回答，不调用工具。

【第三步：查询规范】
1. 一次规划：回答前想清楚需要哪几个查询，一次规划好再执行，不要查一步看一步；
2. 数据复用：同一会话内已查到的数据直接复用，严禁对同一数据反复查询；
3. 单次拿全：优先用一次查询拿到全部所需数据（如带占比的分组查询），避免先查总量再逐个查明细；
4. 失败处理：query_data 返回 ok=false 时，根据 error 提示修正后重试（如换指标名/维度名/操作符大写）；free_query 被守门拦截时，按提示调整 SQL（加过滤条件/加 LIMIT/换白名单内的表）；
5. 歧义处理：用户问法有歧义（没说清指标/时间范围/对象）时，先用选项式反问澄清，不要猜测。

【第四步：画图规范】
1. 用户显式要求可视化（画图/图表/饼图/柱状图/折线图/趋势/可视化）→ draw=true；
2. 用户显式不要图（不用画/只要文字）→ draw=false；
3. 用户没提 → 分布/趋势/占比/对比类多行数据 draw=true；结构/字段/单值/清单类或辅助统计查询 draw=false；
4. 中间步骤/辅助性查询一律 draw=false，只有最终呈现给用户的结果才画图；
5. 结果只有一行时系统不生成图表，你必须向用户解释原因并建议可画图的替代问法（各区域对比/按物料分布/按日期趋势）；
6. 用户显式指定图型时传对应 chart_type（柱状图→bar、饼图→pie、折线图→line），未指定传 auto；注意：图型最终由系统在看到数据后按数据形态决定，显式指定仅为偏好，与数据明显冲突时（如饼图配多行时间序列）系统会自动降级为合适图型；
7. 重绘/美化/调整图表类请求：只执行一次最终查询并画图（draw=true），中间尝试性查询一律 draw=false，严禁一轮回答产生多个图表。

【第五步：回答规范】
1. 所有数字必须来自工具返回结果，严禁编造；
2. 查询结果已自动图表化展示给用户，回复默认不重复输出数据明细，只写 3-5 句关键结论与解读（最高/最低/趋势/异常点/建议），可提及个别关键数字；仅当用户明确要求列出数据明细时才输出；
3. 引用事实必须使用工具结果中出现的真实 URL，严禁凭记忆编造链接；
4. 信息不足时明确说明，不猜测；
5. 信息足够后直接给出最终回复，不再调用工具；工具失败时换关键词/换来源/换写法再试，不要原样重试。"""


# ---------------- 工具层 ----------------

def web_search(query: str, max_results: int = 5) -> list[dict]:
    """网页搜索（cn.bing.com），返回 [{title, url, snippet}]。"""
    import html as html_mod
    try:
        import httpx
        resp = httpx.get(
            "https://cn.bing.com/search",
            params={"q": query, "count": max_results},
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                                 "Chrome/124.0 Safari/537.36",
                     "Accept-Language": "zh-CN,zh;q=0.9"},
            follow_redirects=True,
        )
        resp.raise_for_status()
        page = resp.text
        results: list[dict] = []
        for block in re.findall(r'<li class="b_algo"[\s\S]*?</li>', page)[:max_results]:
            m = re.search(r'<h2><a[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a></h2>', block)
            if not m:
                continue
            url, title = m.group(1), re.sub(r"<[^>]+>", "", m.group(2)).strip()
            sm = re.search(r"<p[\s\S]*?>([\s\S]*?)</p>", block)
            snippet = re.sub(r"<[^>]+>", "", sm.group(1)).strip() if sm else ""
            results.append({"title": html_mod.unescape(title),
                            "url": url,
                            "snippet": html_mod.unescape(snippet)[:300]})
        return results or {"error": "搜索无结果，建议换关键词"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"搜索失败: {e}"}


def fetch_url(url: str) -> str:
    """抓取网页并提取正文文本（截断）。"""
    try:
        import httpx
        resp = httpx.get(url, timeout=20, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (compatible; agent-pipeline/0.1)"})
        resp.raise_for_status()
        text = resp.text
        text = re.sub(r"<script[\s\S]*?</script>", "", text)
        text = re.sub(r"<style[\s\S]*?</style>", "", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:8000]
    except Exception as e:  # noqa: BLE001
        return f"抓取失败: {e}"


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


TOOLS: dict[str, Callable] = {"web_search": web_search, "fetch_url": fetch_url, "query_data": query_data,
                              "explore_schema": explore_schema, "free_query": free_query}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网公开信息，返回标题、链接和摘要。用于获取新闻、资料、事实等外部信息。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取指定 URL 的网页正文文本。用于深入阅读搜索结果中的某个页面。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的网页地址"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_data",
            "description": "查询业务数据库（销售订单/采购订单/库存，数据范围 2026-10-24 ~ 2026-11-23）。输入结构化 Plan JSON（不是 SQL），由系统校验后安全执行。可用指标：sales_amount(销售额)/sales_orders(订单数)/sales_qty(销售数量)/purchase_amount(采购额)/purchase_orders(采购订单数)/inventory_qty(库存量)。可用维度：customer_region(客户区域)/material(物料)/material_code(物料编码)/customer(客户)/order_date(日期)/supplier(供应商)/supplier_region(供应商区域)/warehouse(仓库)。可用过滤字段：sales 库 customer_region/material/material_code/customer/status；purchase 库 supplier/supplier_region/material/status；inventory 库 warehouse/material。可用时间宏：last_month/this_month/last_7_days/last_30_days/all。",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_json": {
                        "type": "string",
                        "description": "Plan JSON 字符串，格式：{\"metric\": \"sales_amount\", \"dimensions\": [\"customer_region\"], \"filters\": [{\"field\": \"customer_region\", \"op\": \"=\", \"value\": \"华南\"}], \"time_macro\": \"last_month\"}"
                    },
                    "draw": {
                        "type": "boolean",
                        "description": "是否生成图表。用户要看数据分布/趋势/占比/对比时为 true（默认）；查结构/字段/单值/清单类问题传 false"
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "pie", "line", "auto"],
                        "description": "图表类型。用户显式要求柱状图传 bar、饼图传 pie、折线图传 line；用户未指定图型时传 auto（系统自动判断）"
                    }
                },
                "required": ["plan_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_schema",
            "description": "探索数据结构：查看业务库有哪些表、字段、类型、行数。当用户问『有什么数据/什么表/什么字段』或需要了解数据结构时使用。database 可选：bom_system/mdm_platform/sap_mock；不传返回全部表清单，传 database 返回该库各表字段，再传 table 返回单表字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "库名：bom_system/mdm_platform/sap_mock，空则返回全部表清单"},
                    "table": {"type": "string", "description": "表名，空则返回该库全部表的字段"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "free_query",
            "description": "受控自由查询（长尾兜底）：当问题涉及的指标/维度不在 query_data 语义层中时使用。直接写 SELECT SQL（表必须带库名前缀，如 sap_mock.sap_sales_orders），系统守门校验（只读/白名单/限行/EXPLAIN）后执行。优先用 query_data，仅当语义层无法表达时才用本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SELECT 查询语句，表用 库名.表名 全限定，如 SELECT category, COUNT(*) AS cnt FROM mdm_platform.md_materials GROUP BY category"},
                    "params_json": {"type": "string", "description": "参数化查询的参数 JSON 数组，与 SQL 中 %s 占位符对应，无参数传 []"},
                    "draw": {"type": "boolean", "description": "是否生成图表。用户要看数据分布/趋势/占比/对比时为 true（默认）；查结构/字段/单值/清单类问题传 false"},
                    "chart_type": {"type": "string", "enum": ["bar", "pie", "line", "auto"], "description": "图表类型。用户显式要求柱状图传 bar、饼图传 pie、折线图传 line；用户未指定图型时传 auto（系统自动判断）"}
                },
                "required": ["sql"],
            },
        },
    },
]


def _client() -> OpenAI:
    return OpenAI(
        api_key=os.environ["DEEPSEEK_API_KEY"],
        base_url=os.environ["DEEPSEEK_BASE_URL"],
    )


_PLAN_SYSTEM = """你是任务规划师。判断用户任务是否需要多步骤计划，并生成计划。

判断标准：
- 简单问题（单点事实、闲聊、单次查询、翻译、改写）-> 输出 {"complex": false}
- 复杂任务（需要对比多个对象、多轮搜索汇总、分阶段研究、需要综合分析后给结论）-> 生成计划

计划格式：
{"complex": true, "steps": [
  {"goal": "这一步要完成什么", "done_when": "达到什么状态算完成"}
]}

要求：
- 步骤数 2-6 个，按执行顺序排列；
- 每步目标具体可执行（明确搜索什么/分析什么）；
- 最后一步通常是汇总分析或给出结论；
- 只输出 JSON，不要其他文字。"""


def generate_plan(task: str) -> dict | None:
    """判断任务复杂度，复杂任务返回计划 dict，简单任务返回 None。"""
    try:
        client = _client()
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": _PLAN_SYSTEM},
                      {"role": "user", "content": task}],
            temperature=0.1, max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
        fence = chr(96) * 3
        if text.startswith(fence):
            text = text[3:]
        if text.endswith(fence):
            text = text[:-3]
        text = text.strip()
        if text.startswith("json"):
            text = text[4:].strip()
        data = json.loads(text)
        if not data.get("complex"):
            return None
        steps = data.get("steps") or []
        if not isinstance(steps, list) or not steps:
            return None
        return {"complex": True, "steps": steps[:6]}
    except Exception:
        return None


def _collect_evidence(tool: str, args: dict, result, evidences: list) -> None:
    """从工具结果中提取证据（URL/标题/摘要），供回复引用与防幻觉校验。"""
    if tool == "web_search" and isinstance(result, list):
        for r in result[:6]:
            url = str(r.get("url", "")).strip()
            if url and url.startswith("http"):
                evidences.append({
                    "url": url[:768],
                    "title": str(r.get("title", ""))[:256],
                    "excerpt": str(r.get("snippet", ""))[:500],
                })
    elif tool == "fetch_url" and isinstance(result, str) and not result.startswith("抓取失败"):
        url = str(args.get("url", "")).strip()
        if url.startswith("http"):
            evidences.append({
                "url": url[:768],
                "title": "",
                "excerpt": result[:500],
            })


def _compress_tool_result(tool: str, result) -> str:
    """工具结果即时压缩：回传模型前控制长度，历史不堆积全文。

    - web_search：每条只留 标题+摘要(120字)+URL，最多 6 条
    - fetch_url：正文截断到 3000 字符
    - query_data：只回传摘要（结果已图表化展示给用户）
    - 其他：字符串截断到 3000 字符
    """
    if tool in ("query_data", "free_query") and isinstance(result, dict) and result.get("ok"):
        rows = result.get("rows", [])
        cols = result.get("columns", [])
        top = "；".join(" / ".join(str(v) for v in r) for r in rows[:3])
        single = result.get("row_count", 0) <= 1
        if single:
            chart_note = ("注意：结果只有一行，系统未生成图表（单行数据无法构成有意义的图表）。"
                          "如用户要求画图，必须向用户解释原因，并建议改为对比/分布类问题（如各区域对比、按物料分布、按日期趋势）。")
        elif result.get("draw", True):
            chart_note = "结果已自动以图表报表形式展示给用户（用户可见完整数据）。回复默认不重复输出数据明细，只写3-5句关键结论与解读；仅当用户明确要求在回复中列出数据时才输出。"
        else:
            chart_note = "本次查询未生成图表（draw=false），请用文字清晰回答。"
        return (
            f"查询成功，共 {result.get('row_count', len(rows))} 行\n"
            f"列: {cols}\n前3行摘要: {top}\n"
            f"SQL: {result.get('sql', '')}\n"
            f"{chart_note}"
        )
    if tool == "web_search" and isinstance(result, list):
        lines = []
        for r in result[:6]:
            lines.append(
                f"- {str(r.get('title', ''))[:60]}\n"
                f"  {str(r.get('snippet', ''))[:120]}\n"
                f"  来源: {r.get('url', '')}"
            )
        return "\n".join(lines) if lines else "（无搜索结果）"
    text = str(result)
    return text[:3000] + ("\n…（内容过长已截断）" if len(text) > 3000 else "")


VERIFY_MAX_ROUNDS = 2  # 验证回环上限，防止死循环
_VERIFY_SYSTEM = """你是数据分析验证员。根据对话历史判断已有数据是否足以回答用户最后的问题：
- 数据充分、能直接回答 -> 只回复 PASS
- 数据不足/数值明显异常/缺少关键维度或时间范围 -> 用一句话说明缺什么、需要补充什么查询（不要调用工具，不要展开分析）"""


def _verify_check(client, model, messages: list[dict]) -> str | None:
    """验证数据充分性。返回 None=通过；返回 str=需补充的查询说明。"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _VERIFY_SYSTEM}] + messages[-12:],
            temperature=0.1,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text or text.upper().startswith("PASS"):
            return None
        return text[:300]
    except Exception:  # noqa: BLE001
        return None


_REPORT_SYSTEM = """你是数据分析报告撰写员。把最终回复重组为结构化报告 JSON，字段：
{"conclusion": "一句话结论", "findings": ["关键发现", ...], "evidence": ["数据证据（含具体数字）", ...], "risks": ["风险或局限", ...], "suggestions": ["建议", ...]}
只输出 JSON，不要任何其他内容。findings/evidence/risks/suggestions 各 2-4 条，内容必须来自回复原文，严禁编造数字。"""


def _generate_report(client, model, reply: str) -> dict | None:
    """报告生成层：把最终回复重组为结构化报告。失败返回 None（不影响主流程）。"""
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _REPORT_SYSTEM},
                      {"role": "user", "content": reply}],
            temperature=0.1,
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
        fence = chr(96) * 3
        if text.startswith(fence):
            text = text.split("\n", 1)[1] if "\n" in text else text
            text = text.rsplit(fence, 1)[0]
        data = json.loads(text)
        if isinstance(data, dict) and data.get("conclusion"):
            return data
        return None
    except Exception:  # noqa: BLE001
        return None


def _decide_chart_type(columns: list, rows: list, row_count: int, preferred: str) -> str:
    """数据返回后判定图型：显式指定仅为偏好，与数据形态冲突时降级。
    规则：
    - 有时间列（date/日期/时间/周）且行数>8 -> line
    - 单维度列且行数<=8 -> pie
    - 其他 -> bar
    冲突降级：pie 遇多行/时间列 -> line 或 bar；line 无时间列 -> bar
    """
    cols = [str(c) for c in columns]
    has_time = any(re.search(r"date|日期|时间|周", c, re.I) for c in cols)
    dim_cols = [c for c in cols if c != "value"]
    n = row_count or len(rows)
    # 系统按数据形态计算的合适图型
    if has_time and n > 8:
        fit = "line"
    elif len(dim_cols) <= 1 and n <= 8:
        fit = "pie"
    else:
        fit = "bar"
    # 显式偏好仅在适配时生效
    if preferred in ("bar", "pie", "line"):
        if preferred == fit:
            return preferred
        # 明显冲突：pie 遇多行或时间列；line 无时间列
        if preferred == "pie" and (n > 8 or has_time):
            return fit
        if preferred == "line" and not has_time:
            return "bar"
        return preferred
    return fit


def run_agent_stream(messages: list[dict], mode: str = "standard",
                     on_event: Callable | None = None) -> dict:
    """流式执行 Agent 循环。

    mode: off / standard / deep
    on_event(event, data) 事件：
      thinking {text}   思考增量（deep 模式）
      tool_start {tool, args}
      tool_end {tool, preview}
      reply {text}      回复增量
    返回：{"reply": 最终回复, "steps": 步数}
    """
    emit = on_event or (lambda e, d: None)
    client = _client()
    model = "deepseek-reasoner" if mode == "deep" else "deepseek-chat"
    evidences: list[dict] = []  # 本次运行收集的证据 [{url, title, excerpt}]
    trace: list[dict] = []  # 执行轨迹 [{round, tool, query, urls}]
    verify_rounds = 0  # 验证回环计数

    for step in range(1, MAX_STEPS + 1):
        stream = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=TOOL_SCHEMAS,
            stream=True,
            temperature=0.3,
        )

        tool_acc: dict[int, dict] = {}
        content_acc = ""
        finish = None

        for chunk in stream:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            if delta is None:
                continue
            rc = getattr(delta, "reasoning_content", None)
            if rc:
                emit("thinking", {"text": rc})
            if delta.content:
                content_acc += delta.content
                emit("reply", {"text": delta.content})
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index if tc.index is not None else 0
                    acc = tool_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["args"] += tc.function.arguments
            if choice.finish_reason:
                finish = choice.finish_reason

        # 无工具调用 -> 完成
        if not tool_acc or finish != "tool_calls":
            # 分析验证层：数据查询类问题检查数据充分性，不足时回环补充查询
            if (mode != "off" and verify_rounds < VERIFY_MAX_ROUNDS
                    and any(t.get("tool") in ("query_data", "free_query") for t in trace)):
                verdict = _verify_check(client, model, messages)
                if verdict:
                    verify_rounds += 1
                    trace.append({"round": step, "tool": "verify",
                                  "query": "数据不足：" + verdict[:120], "urls": []})
                    messages.append({"role": "user",
                                     "content": f"（验证环节：已有数据不足以完整回答，原因：{verdict}。请继续调用工具补充查询，然后给出最终回复。）"})
                    continue
            # 报告生成层：数据查询类问题生成结构化报告
            report = None
            if mode != "off" and any(t.get("tool") in ("query_data", "free_query") for t in trace):
                report = _generate_report(client, model, content_acc)
                if report:
                    emit("report", report)
            return {"reply": content_acc, "steps": step, "evidences": evidences, "trace": trace, "report": report}

        # 记录 assistant 工具调用消息（reasoning 不回传）
        messages.append({
            "role": "assistant",
            "content": content_acc,
            "tool_calls": [
                {"id": acc["id"] or f"call_{i}", "type": "function",
                 "function": {"name": acc["name"], "arguments": acc["args"] or "{}"}}
                for i, acc in sorted(tool_acc.items())
            ],
        })

        # 执行工具并回传
        for i, acc in sorted(tool_acc.items()):
            name = acc["name"]
            try:
                args = json.loads(acc["args"] or "{}")
            except json.JSONDecodeError:
                args = {}
            emit("tool_start", {"tool": name, "args": args})
            if name not in TOOLS:
                result = {"error": f"未知工具: {name}"}
            else:
                try:
                    result = TOOLS[name](**args)
                except Exception as e:  # noqa: BLE001
                    result = {"error": f"执行错误: {e}"}
            # 证据收集：从工具结果中提取 URL 与标题
            _collect_evidence(name, args, result, evidences)
            # 数据查询成功：推送 chart 事件（前端渲染图表）
            # 不画图的情况：LLM 明确传 draw=false；或结果为单值（1行1列，画图无意义）
            if name in ("query_data", "free_query") and isinstance(result, dict) and result.get("ok"):
                single_row = result.get("row_count", 0) <= 1
                if result.get("draw", True) and not single_row:
                    emit("chart", {
                    "columns": result.get("columns", []),
                    "rows": result.get("rows", [])[:100],
                    "row_count": result.get("row_count", 0),
                    "sql": result.get("sql", ""),
                    "params": result.get("params", []),
                    "sem_ver": result.get("sem_ver", ""),
                    "plan_json": str(args.get("plan_json", "")),
                    "chart_type": _decide_chart_type(
                        result.get("columns", []),
                        result.get("rows", []),
                        result.get("row_count", 0),
                        result.get("chart_type", "auto"),
                    ),
                })
            # 执行轨迹：记录本轮调用的工具与查阅的网页
            t_urls: list[str] = []
            if name == "web_search" and isinstance(result, list):
                t_urls = [str(r.get("url", "")).strip() for r in result[:6]
                          if str(r.get("url", "")).strip().startswith("http")]
            elif name == "fetch_url":
                _u = str(args.get("url", "")).strip()
                if _u.startswith("http"):
                    t_urls = [_u]
            trace.append({
                "round": step,
                "tool": name,
                "query": str(args.get("query") or args.get("url") or "")[:120],
                "urls": t_urls,
            })
            result_str = _compress_tool_result(name, result)
            # query_data 成功时，工具卡片预览显示 SQL 便于检查
            if name in ("query_data", "free_query") and isinstance(result, dict) and result.get("ok"):
                preview = "SQL: " + result.get("sql", "") + " | 参数: " + str(result.get("params", []))
            else:
                preview = result_str[:150]
            emit("tool_end", {"tool": name, "preview": preview})
            messages.append({
                "role": "tool",
                "tool_call_id": acc["id"] or f"call_{i}",
                "content": result_str,
            })

    # 步数上限，强制收尾（非流式一次调用）
    messages.append({"role": "user",
                     "content": "（系统提示：已达到最大步数，请基于已有信息立即给出最终回复，不要再调用工具。）"})
    resp = client.chat.completions.create(model=model, messages=messages, temperature=0.3)
    reply = resp.choices[0].message.content or ""
    if reply:
        emit("reply", {"text": reply})
    return {"reply": reply, "steps": MAX_STEPS, "evidences": evidences, "trace": trace}


# ---------------- CLI 入口 ----------------

if __name__ == "__main__":
    import sys
    task = " ".join(sys.argv[1:]) or "搜索一下 DeepSeek 最新发布的模型有哪些，简要总结"
    mode = sys.argv[1] if sys.argv[1:] and sys.argv[1] in ("off", "standard", "deep") else "standard"
    if sys.argv[1:] and sys.argv[1] in ("off", "standard", "deep"):
        task = " ".join(sys.argv[2:]) or task
    print(f"任务: {task} | 模式: {mode}\n" + "=" * 50)
    msgs = [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": task}]

    def show(event: str, data: dict) -> None:
        if event == "thinking":
            print(data["text"], end="", flush=True)
        elif event == "tool_start":
            print(f"\n[调用 {data['tool']}] {data['args']}")
        elif event == "tool_end":
            print(f"  -> {data['preview'][:80]}")
        elif event == "reply":
            print(data["text"], end="", flush=True)

    result = run_agent_stream(msgs, mode=mode, on_event=show)
    print(f"\n\n({result['steps']} 步)")
