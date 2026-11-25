"""LangGraph 版 Agent 核心：StateGraph 编排 + Agent Skills 运行时。

图结构：
  START -> agent -> (条件边)
    有工具调用 -> skill_exec -> agent
    无工具调用且需验证 -> verify -> agent
    否则 -> END

- agent 节点：LLM 流式决策（技能选择/回复生成），SSE 事件透传
- skill_exec 节点：执行技能函数（第二层披露：按需加载脚本）+ 技能记忆写入
- verify 节点：数据充分性检查（PASS/补充/REPLAN），不足时注入消息回 agent
- SSE 事件协议与原版 agent.py 一致（前端零改动）
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, TypedDict

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

import skill_runtime as sr

load_dotenv()

MAX_STEPS = 15
VERIFY_MAX_ROUNDS = 2

SYSTEM_PROMPT = """你是一个能干的智能助手，通过技能（skills）完成用户的任务。

【技能体系】系统提供以下技能（每个技能是一个目录，含 SKILL.md 说明与实现脚本）：
{skill_list}

【技能使用规范】
1. 按任务分类选技能：业务数据类 → data-query；外部信息类 → web-search；导出需求 → export-csv；闲聊/翻译 → 直接回答；
2. 技能的详细用法在技能说明中，调用工具时按参数规范传参；
3. 一次规划：回答前想清楚需要哪几个查询，一次规划好再执行，不要查一步看一步；
4. 数据复用：同一会话内已查到的数据直接复用，严禁对同一数据反复查询；
5. 失败处理：工具返回 ok=false 时，根据 error 提示修正后重试（换指标名/维度名/调整 SQL），不要原样重试；
6. 歧义处理：用户问法有歧义时，先用选项式反问澄清，回复末尾加【待确认】标记；
7. 画图规范：分布/趋势/占比/对比类多行数据 draw=true；结构/字段/单值/清单类 draw=false；中间步骤查询一律 draw=false；
8. 回答规范：所有数字必须来自工具返回结果，严禁编造；结果已图表化展示时回复只写 3-5 句关键结论；对比类问题必须覆盖最高/最低/差距/异常；引用 URL 必须真实。

【技能记忆】以下是跨会话的技能使用记录，供参考避免重复失败尝试：
{skill_memory}"""

_VERIFY_SYSTEM = """你是数据分析验证员。根据对话历史判断已有数据是否足以回答用户最后的问题：
- 数据充分、能直接回答 -> 只回复 PASS
- 数据不足/数值明显异常/缺少关键维度或时间范围 -> 用一句话说明缺什么、需要补充什么查询（不要调用工具，不要展开分析）
- 执行计划与实际情况不符（如计划要环比但数据只有一期、计划维度不存在、计划步骤不可行）-> 以 REPLAN: 开头，一句话说明原计划哪步不可行及原因，并给出调整后的做法"""


class AgentState(TypedDict):
    messages: list
    trace: list
    evidences: list
    verify_rounds: int
    steps: int
    pending_calls: list
    final_reply: str


def _build_skill_tools(skills: dict) -> tuple[list[dict], dict]:
    tools: list[dict] = []
    registry: dict[str, Callable] = {}
    for sk in skills.values():
        try:
            funcs = sk.load_scripts()
        except Exception:  # noqa: BLE001
            continue
        for fname, fn in funcs.items():
            if fname in registry:
                continue
            registry[fname] = fn
            tools.append({
                "type": "function",
                "function": {
                    "name": fname,
                    "description": f"[{sk.name}] {sk.description[:200]}",
                    "parameters": {"type": "object", "properties": {}},
                },
            })
    return tools, registry


_PARAM_SCHEMAS = {
    "query_data": ({"plan_json": {"type": "string", "description": "Plan JSON 字符串，格式：{\"metric\": \"sales_amount\", \"dimensions\": [\"customer_region\"], \"filters\": [...], \"time_macro\": \"all\"}"},
                    "draw": {"type": "boolean", "description": "是否生成图表"},
                    "chart_type": {"type": "string", "enum": ["bar", "pie", "line", "auto"]}}, ["plan_json"]),
    "free_query": ({"sql": {"type": "string", "description": "SELECT 语句，表带库名前缀如 sap_mock.sap_sales_orders"},
                     "params_json": {"type": "string", "description": "参数 JSON 数组"},
                     "draw": {"type": "boolean"},
                     "chart_type": {"type": "string", "enum": ["bar", "pie", "line", "auto"]}}, ["sql"]),
    "explore_schema": ({"database": {"type": "string", "description": "库名：bom_system/mdm_platform/sap_mock"},
                        "table": {"type": "string", "description": "表名"}}, []),
    "table_stats": ({"database": {"type": "string"}, "table": {"type": "string"}}, ["database", "table"]),
    "web_search": ({"query": {"type": "string", "description": "搜索关键词"},
                    "max_results": {"type": "integer"}}, ["query"]),
    "fetch_url": ({"url": {"type": "string", "description": "网页地址"}}, ["url"]),
    "export_csv": ({"sql": {"type": "string", "description": "SELECT 语句"},
                    "filename": {"type": "string", "description": "导出文件名（可选）"}}, ["sql"]),
}


def _patch_tool_schemas(tools: list[dict]) -> None:
    for t in tools:
        name = t["function"]["name"]
        if name in _PARAM_SCHEMAS:
            props, req = _PARAM_SCHEMAS[name]
            t["function"]["parameters"] = {"type": "object", "properties": props, "required": req}


def _compress_tool_result(tool: str, result) -> str:
    if tool in ("query_data", "free_query") and isinstance(result, dict) and result.get("ok"):
        rows = result.get("rows", [])
        cols = result.get("columns", [])
        top = "；".join(" / ".join(str(v) for v in r) for r in rows[:3])
        single = result.get("row_count", 0) <= 1
        if single:
            chart_note = "注意：结果只有一行，系统未生成图表。如用户要求画图，必须解释原因并建议对比/分布类问法。"
        elif result.get("draw", True):
            chart_note = "结果已自动以图表报表形式展示给用户。回复默认不重复输出数据明细，只写3-5句关键结论与解读。"
        else:
            chart_note = "本次查询未生成图表（draw=false），请用文字清晰回答。"
        return (f"查询成功，共 {result.get('row_count', len(rows))} 行\n"
                f"列: {cols}\n前3行摘要: {top}\nSQL: {result.get('sql', '')}\n{chart_note}")
    if tool == "web_search" and isinstance(result, list):
        lines = []
        for r in result[:6]:
            lines.append(f"- {str(r.get('title', ''))[:60]}\n  {str(r.get('snippet', ''))[:120]}\n  来源: {r.get('url', '')}")
        return "\n".join(lines) if lines else "（无搜索结果）"
    text = str(result)
    return text[:3000] + ("\n…（内容过长已截断）" if len(text) > 3000 else "")


def _collect_evidence(tool: str, args: dict, result, evidences: list) -> None:
    if tool == "web_search" and isinstance(result, list):
        for r in result[:6]:
            url = str(r.get("url", "")).strip()
            if url.startswith("http"):
                evidences.append({"url": url[:768], "title": str(r.get("title", ""))[:256],
                                  "excerpt": str(r.get("snippet", ""))[:500]})
    elif tool == "fetch_url" and isinstance(result, str) and not result.startswith("抓取失败"):
        url = str(args.get("url", "")).strip()
        if url.startswith("http"):
            evidences.append({"url": url[:768], "title": "", "excerpt": result[:500]})


def run_agent_stream(messages: list[dict], mode: str = "standard",
                      on_event: Callable[[str, dict], None] = lambda e, d: None,
                      plan: dict | None = None, sid: str = "") -> dict:
    """LangGraph 主入口：与原版 run_agent_stream 签名/返回兼容。"""
    emit = on_event
    skills = sr.load_skills()
    sr.init_memory()
    tools, registry = _build_skill_tools(skills)
    _patch_tool_schemas(tools)

    skill_list = "\n".join(f"- {sk.name}: {sk.description}" for sk in skills.values())
    system_text = SYSTEM_PROMPT.replace("{skill_list}", skill_list).replace("{skill_memory}", sr.memory_context())

    model = "deepseek-reasoner" if mode == "deep" else "deepseek-chat"
    llm = ChatOpenAI(model=model, api_key=os.environ["DEEPSEEK_API_KEY"],
                     base_url=os.environ["DEEPSEEK_BASE_URL"],
                     temperature=0.3, streaming=True).bind_tools(tools)
    verify_llm = ChatOpenAI(model="deepseek-chat", api_key=os.environ["DEEPSEEK_API_KEY"],
                            base_url=os.environ["DEEPSEEK_BASE_URL"], temperature=0.1)

    hist: list = [SystemMessage(content=system_text)]
    for m in messages:
        content = (m.get("content") or "").split("<!--CHART:")[0].split("<!--REPORT:")[0].rstrip()
        if not content:
            continue
        hist.append(HumanMessage(content=content) if m.get("role") == "user" else AIMessage(content=content))

    # ---------------- 图节点 ----------------

    def agent_node(state: AgentState) -> dict:
        """agent 节点：LLM 流式决策。"""
        tc_acc: dict[int, dict] = {}
        buffer = ""
        for chunk in llm.stream(state["messages"]):
            rc = getattr(chunk, "additional_kwargs", {}).get("reasoning_content")
            if rc:
                emit("thinking", {"text": rc})
            if chunk.content:
                buffer += chunk.content
                emit("reply", {"text": chunk.content})
            for tcc in (getattr(chunk, "tool_call_chunks", None) or []):
                idx = tcc.get("index") if tcc.get("index") is not None else 0
                acc = tc_acc.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tcc.get("id"):
                    acc["id"] = tcc["id"]
                if tcc.get("name"):
                    acc["name"] = tcc["name"]
                if tcc.get("args"):
                    acc["args"] += tcc["args"]
        norm = []
        for i, acc in sorted(tc_acc.items()):
            if not acc["name"]:
                continue
            try:
                args = json.loads(acc["args"]) if acc["args"] else {}
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            norm.append({"name": acc["name"], "args": args,
                         "id": acc["id"] or f"call_{state['steps']}_{i}"})
        ai = AIMessage(content=buffer if buffer else "（正在调用工具）")
        if norm:
            ai.tool_calls = norm
        return {"messages": state["messages"] + [ai], "pending_calls": norm,
                "final_reply": buffer, "steps": state["steps"] + 1}

    def skill_node(state: AgentState) -> dict:
        """skill_exec 节点：执行技能函数 + 记忆写入。"""
        msgs = list(state["messages"])
        trace = list(state["trace"])
        evidences = list(state["evidences"])
        step = state["steps"]
        for tc in state["pending_calls"]:
            fname = tc["name"]
            args = tc.get("args") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            emit("tool_start", {"tool": fname, "args": args})
            fn = registry.get(fname)
            if fn is None:
                result = {"ok": False, "error": f"未知工具: {fname}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:  # noqa: BLE001
                    result = {"ok": False, "error": f"执行失败: {e}"}
            preview = json.dumps(result, ensure_ascii=False)[:200] if not isinstance(result, str) else result[:200]
            emit("tool_end", {"tool": fname, "preview": preview})
            # 图表事件：查询成功且非单行且 draw 时推送（与原版协议一致）
            if (fname in ("query_data", "free_query") and isinstance(result, dict)
                    and result.get("ok") and result.get("draw", True)
                    and result.get("row_count", 0) > 1):
                emit("chart", {
                    "columns": result.get("columns", []),
                    "rows": result.get("rows", [])[:100],
                    "row_count": result.get("row_count", 0),
                    "sql": result.get("sql", ""),
                    "sem_ver": result.get("sem_ver", ""),
                    "chart_type": result.get("chart_type", "auto"),
                })
            _collect_evidence(fname, args, result, evidences)
            ok = not (isinstance(result, dict) and result.get("ok") is False)
            sr.remember_skill(fname.rsplit("_", 1)[0], fname, sid, ok, preview[:120])
            trace.append({"round": step, "tool": fname, "query": str(args)[:120], "urls": []})
            msgs.append(ToolMessage(content=_compress_tool_result(fname, result),
                                    tool_call_id=str(tc.get("id") or f"call_{step}")))
        return {"messages": msgs, "trace": trace, "evidences": evidences, "pending_calls": []}

    def verify_node(state: AgentState) -> dict:
        """verify 节点：数据充分性检查（PASS/补充/REPLAN）。"""
        msgs = list(state["messages"])
        trace = list(state["trace"])
        try:
            resp = verify_llm.invoke([SystemMessage(content=_VERIFY_SYSTEM)] + msgs[-12:])
            text = (resp.content or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if not text or text.upper().startswith("PASS"):
            return {"verify_rounds": state["verify_rounds"] + 1}
        step = state["steps"]
        if text.startswith("REPLAN:"):
            adjust = text[len("REPLAN:"):].strip()
            trace.append({"round": step, "tool": "replan", "query": "计划调整：" + adjust[:120], "urls": []})
            emit("replan", {"reason": adjust[:200]})
            msgs.append(HumanMessage(content=f"（计划调整：原计划部分步骤不可行，{adjust}。请按调整后的做法继续执行，完成剩余步骤后给出最终回复。）"))
        else:
            trace.append({"round": step, "tool": "verify", "query": "数据不足：" + text[:120], "urls": []})
            msgs.append(HumanMessage(content=f"（验证环节：已有数据不足以完整回答，原因：{text}。请继续调用工具补充查询，然后给出最终回复。）"))
        return {"messages": msgs, "trace": trace, "verify_rounds": state["verify_rounds"] + 1}

    def route_after_agent(state: AgentState) -> str:
        if state["pending_calls"]:
            return "skill_exec"
        if (mode != "off" and state["verify_rounds"] < VERIFY_MAX_ROUNDS
                and any(t.get("tool") in ("query_data", "free_query") for t in state["trace"])):
            return "verify"
        return END

    def route_after_verify(state: AgentState) -> str:
        last = state["messages"][-1] if state["messages"] else None
        if isinstance(last, HumanMessage) and last.content.startswith("（"):
            return "agent"  # 验证不通过，注入了补充指令
        return END

    # ---------------- 构图 ----------------
    g = StateGraph(AgentState)
    g.add_node("agent", agent_node)
    g.add_node("skill_exec", skill_node)
    g.add_node("verify", verify_node)
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", route_after_agent, {"skill_exec": "skill_exec", "verify": "verify", END: END})
    g.add_edge("skill_exec", "agent")
    g.add_conditional_edges("verify", route_after_verify, {"agent": "agent", END: END})
    graph = g.compile()

    init: AgentState = {"messages": hist, "trace": [], "evidences": [],
                        "verify_rounds": 0, "steps": 0, "pending_calls": [], "final_reply": ""}
    # 步数上限保护：MAX_STEPS 轮 agent 决策后强制收尾
    final_state = init
    for _ in range(MAX_STEPS):
        final_state = graph.invoke(final_state, config={"recursion_limit": 4 * MAX_STEPS})
        if not final_state["pending_calls"]:
            # 无待执行工具即结束（verify 回环已在图内完成或判定通过）
            if final_state["messages"] and not isinstance(final_state["messages"][-1], HumanMessage):
                break
            if not final_state["pending_calls"] and _verify_passed(final_state):
                break
        break

    return {"reply": final_state["final_reply"], "steps": final_state["steps"],
            "evidences": final_state["evidences"], "trace": final_state["trace"], "report": None}


def _verify_passed(state: AgentState) -> bool:
    return state["verify_rounds"] >= VERIFY_MAX_ROUNDS or not any(
        t.get("tool") in ("query_data", "free_query") for t in state["trace"])


def _client():
    """兼容层：返回 OpenAI 客户端（server.py 历史压缩用）。"""
    from openai import OpenAI
    return OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"],
                  base_url=os.environ["DEEPSEEK_BASE_URL"])


_PLAN_SYSTEM = "你是任务规划师。判断任务复杂度：简单问题（单次查询/单次搜索/查天气/闲聊/翻译/单条数据统计）只回复 {\"complex\": false}；复杂任务（多工具组合/多阶段分析/跨库对比/导出报告）回复 {\"complex\": true, \"steps\": [\"步骤1\", \"步骤2\", ...]}，步骤 2-6 个，每步一句话，必须具体到要调用的工具和要处理的数据对象（如『查询 sap_sales_orders 各区域销售额』），禁止『明确需求』『整理结果』『汇总反馈』这类无信息量的模板步骤。只输出 JSON，不要其他内容。"


def generate_plan(task: str) -> dict | None:
    """兼容层：任务规划（与原版 agent.py 逻辑一致）。"""
    try:
        client = _client()
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": _PLAN_SYSTEM},
                      {"role": "user", "content": task}],
            temperature=0.1,
            max_tokens=600,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`\n")
            if text.startswith("json"):
                text = text[4:]
        plan = json.loads(text)
        if isinstance(plan, dict) and plan.get("complex") and plan.get("steps"):
            return plan
        return None
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    import sys
    m = sys.argv[1] if len(sys.argv) > 1 else "standard"
    q = " ".join(sys.argv[2:]) or "各区域销售额对比"

    def show(ev, data):
        if ev == "reply":
            print(data.get("text", ""), end="", flush=True)
        elif ev == "tool_start":
            print(f"\n[tool] {data.get('tool')} {data.get('args')}")

    r = run_agent_stream([{"role": "user", "content": q}], m, show)
    print(f"\n--- steps={r['steps']} trace={len(r['trace'])}")
