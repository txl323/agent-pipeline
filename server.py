"""Web 服务层：多轮对话 + 真流式 SSE + MySQL 会话持久化。

- 会话存储：MySQL agent_pipeline 库（sessions + messages 两表）
- 接口与 JSON 文件版完全一致，前端零改动
- 连接管理：每请求短连接（pymysql），失败自动重连一次
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from agent import SYSTEM_PROMPT, run_agent_stream

load_dotenv()

app = FastAPI(title="Agent Pipeline")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_SESSIONS = 50


class ChatRequest(BaseModel):
    session_id: str = ""
    message: str = Field(..., min_length=1)
    mode: str = "standard"  # off / standard / deep


# ---------------- MySQL 访问层 ----------------

def _db() -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"],
        charset="utf8mb4",
        autocommit=True,
    )


def db_execute(fn):
    """执行数据库操作，断线自动重试一次。"""
    last_err = None
    for _ in range(2):
        conn = None
        try:
            conn = _db()
            return fn(conn)
        except pymysql.err.OperationalError as e:
            last_err = e
            continue
        finally:
            if conn:
                conn.close()
    raise last_err  # type: ignore[misc]


def create_session() -> str:
    sid = uuid.uuid4().hex[:12]
    now = time.time()
    def op(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (sid, title, created_at, updated_at) VALUES (%s, %s, %s, %s)",
                (sid, "", now, now),
            )
    db_execute(op)
    return sid


def load_session(sid: str) -> dict | None:
    def op(conn):
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT sid, title, created_at, updated_at FROM sessions WHERE sid=%s", (sid,))
            row = cur.fetchone()
            if not row:
                return None
            cur.execute(
                "SELECT role, content FROM messages WHERE sid=%s ORDER BY id ASC", (sid,))
            # 只保留有内容的 user/assistant 消息：
            # tool 消息与空 assistant（tool_calls 中间态）缺少 tool_call_id，
            # 回传 API 会 422，且对后续对话无上下文价值
            row["messages"] = [
                {"role": m["role"], "content": m["content"] or ""}
                for m in cur.fetchall()
                if m["role"] in ("user", "assistant") and (m["content"] or "").strip()
            ]
            return row
    return db_execute(op)


def save_messages(sid: str, messages: list[dict]) -> None:
    """全量重写会话消息（简单可靠，消息量级小）。"""
    def op(conn):
        with conn.cursor() as cur:
            cur.execute("DELETE FROM messages WHERE sid=%s", (sid,))
            for m in messages:
                cur.execute(
                    "INSERT INTO messages (sid, role, content, created_at) VALUES (%s, %s, %s, %s)",
                    (sid, m["role"], m.get("content") or "", time.time()),
                )
            cur.execute("UPDATE sessions SET updated_at=%s WHERE sid=%s", (time.time(), sid))
    db_execute(op)


def set_title(sid: str, title: str) -> None:
    def op(conn):
        with conn.cursor() as cur:
            cur.execute("UPDATE sessions SET title=%s WHERE sid=%s", (title[:128], sid))
    db_execute(op)


def save_evidences(sid: str, evidences: list[dict]) -> None:
    """工具证据入库（去重：同会话同 URL 不重复插入）。"""
    if not evidences:
        return
    def op(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM evidence WHERE sid=%s", (sid,))
            existing = {row[0] for row in cur.fetchall()}
            now = time.time()
            for ev in evidences:
                url = ev.get("url", "")
                if not url or url in existing:
                    continue
                cur.execute(
                    "INSERT INTO evidence (sid, url, title, excerpt, created_at) VALUES (%s,%s,%s,%s,%s)",
                    (sid, url, ev.get("title", ""), ev.get("excerpt", ""), now),
                )
                existing.add(url)
    db_execute(op)


def verify_reply_sources(sid: str, reply: str) -> list[dict]:
    """提取回复中的 URL，与会话证据库比对，返回来源列表（含验证状态）。"""
    import re
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)\]<>\"']+", reply)))
    if not urls:
        return []
    def op(conn):
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT url, title FROM evidence WHERE sid=%s", (sid,))
            return {row["url"]: row["title"] for row in cur.fetchall()}
    known = db_execute(op)
    result = []
    for url in urls[:10]:
        url_clean = url.rstrip(".,;、。")
        verified = url_clean in known or any(url_clean.startswith(k) for k in known)
        title = known.get(url_clean, "")
        if not title:
            for k, v in known.items():
                if url_clean.startswith(k):
                    title = v
                    break
        result.append({"url": url_clean, "title": title, "verified": verified})
    return result


def load_running_plan(sid: str) -> tuple[int | None, dict | None]:
    """跨轮延续：取会话中 running 状态的最新计划（id, plan）。"""
    def op(conn):
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT id, plan_json FROM plans WHERE sid=%s AND status='running' ORDER BY id DESC LIMIT 1",
                (sid,),
            )
            row = cur.fetchone()
            if not row:
                return None, None
            try:
                return row["id"], json.loads(row["plan_json"])
            except Exception:  # noqa: BLE001
                return None, None
    return db_execute(op)


def save_plan(sid: str, plan: dict) -> int:
    """计划落库，返回计划 ID。"""
    now = time.time()
    def op(conn):
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO plans (sid, plan_json, status, progress, created_at, updated_at) "
                "VALUES (%s,%s,'running',0,%s,%s)",
                (sid, json.dumps(plan, ensure_ascii=False), now, now),
            )
            return cur.lastrowid
    return db_execute(op)


def update_plan_progress(plan_id: int, progress: int, status: str = "running") -> None:
    def op(conn):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE plans SET progress=%s, status=%s, updated_at=%s WHERE id=%s",
                (progress, status, time.time(), plan_id),
            )
    db_execute(op)


def list_sessions() -> list[dict]:
    def op(conn):
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT s.sid, s.title, s.created_at, s.updated_at,"
                " (SELECT COUNT(*) FROM messages m WHERE m.sid=s.sid AND m.role='user') AS turns "
                "FROM sessions s ORDER BY s.updated_at DESC LIMIT %s", (MAX_SESSIONS,))
            return cur.fetchall()
    return db_execute(op)


# ---------------- 上下文压缩 ----------------

# 触发阈值：约 48K 字符（≈16K token，为 32K 上限留余量）
COMPRESS_THRESHOLD_CHARS = 48000
# 压缩后保留的近期消息字符预算
KEEP_RECENT_CHARS = 16000

_COMPRESS_SYSTEM = """你是对话压缩器。把给定的多轮对话压缩为一段简洁摘要，要求：
1. 保留所有关键事实：用户提到的偏好、数字、名称、决定、结论（如幸运数字、项目名、日期）；
2. 保留已完成的任务及其结论、引用过的重要来源 URL；
3. 丢弃寒暄、重复、工具调用的中间过程细节；
4. 输出不超过 500 字，直接输出摘要正文。"""


def _messages_chars(messages: list[dict]) -> int:
    return sum(len(m.get("content") or "") for m in messages)


def _maybe_compress(messages: list[dict]) -> list[dict]:
    """超过阈值时：旧消息 LLM 压缩为摘要注入 system，近期消息保留。"""
    if _messages_chars(messages) <= COMPRESS_THRESHOLD_CHARS:
        return messages

    system = messages[0]
    rest = messages[1:]

    # 从尾部往前收集近期消息，不超过字符预算
    recent: list[dict] = []
    used = 0
    cut = len(rest)
    for i in range(len(rest) - 1, -1, -1):
        size = len(rest[i].get("content") or "")
        if used + size > KEEP_RECENT_CHARS and recent:
            cut = i + 1
            break
        recent.insert(0, rest[i])
        used += size
    else:
        cut = 0

    old = rest[:cut]
    if not old:
        return messages

    # 旧消息转文本供 LLM 压缩
    lines = []
    for m in old:
        role = "用户" if m.get("role") == "user" else ("助手" if m.get("role") == "assistant" else m.get("role", ""))
        content = (m.get("content") or "")[:600]
        if content:
            lines.append(f"{role}: {content}")
    old_text = "\n".join(lines)[:24000]

    summary = None
    try:
        from agent import _client
        client = _client()
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": _COMPRESS_SYSTEM},
                      {"role": "user", "content": old_text}],
            temperature=0.1, max_tokens=800,
        )
        summary = (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        summary = None

    if summary:
        new_system = {
            "role": "system",
            "content": system["content"] + "\n\n【此前对话摘要】\n" + summary,
        }
        return [new_system] + recent

    # LLM 失败降级：简单截断（保 system + 近期）
    return [system] + recent


# ---------------- 接口 ----------------

@app.get("/")
def index() -> FileResponse:
    path = os.path.join(BASE_DIR, "web", "index.html")
    if not os.path.exists(path):
        raise HTTPException(404, "index.html missing")
    return FileResponse(path, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/conversations")
def list_conversations() -> dict:
    items = []
    for row in list_sessions():
        title = row["title"]
        if not title:
            title = "新对话"
        items.append({
            "session_id": row["sid"],
            "title": title,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "turns": row["turns"],
        })
    return {"conversations": items}


@app.delete("/api/conversations/{sid}")
def delete_conversation(sid: str) -> dict:
    """删除会话：清理 sessions/messages/evidence/plans 全部记录。"""
    def op(conn):
        with conn.cursor() as cur:
            cur.execute("SELECT sid FROM sessions WHERE sid=%s", (sid,))
            if not cur.fetchone():
                raise HTTPException(404, "session not found")
            cur.execute("DELETE FROM messages WHERE sid=%s", (sid,))
            cur.execute("DELETE FROM evidence WHERE sid=%s", (sid,))
            cur.execute("DELETE FROM plans WHERE sid=%s", (sid,))
            cur.execute("DELETE FROM sessions WHERE sid=%s", (sid,))
    db_execute(op)
    return {"ok": True, "deleted": sid}


@app.get("/api/conversations/{sid}/messages")
def get_messages(sid: str) -> dict:
    data = load_session(sid)
    if not data:
        raise HTTPException(404, "session not found")
    visible = [
        {"role": m["role"], "content": m["content"]}
        for m in data["messages"]
        if m["role"] in ("user", "assistant") and m["content"]
    ]
    return {"session_id": sid, "messages": visible}


@app.post("/api/sql")
async def run_sql(req: ChatRequest) -> dict:
    """SQL 直执行：跳过 NL 解析，直接执行 SELECT（复用 free_query 五重守门）。

    message 字段传 SQL 语句；session_id 可选（用于落库留痕）。
    """
    from nlsql import free_query
    sql = (req.message or "").strip()
    if not sql:
        raise HTTPException(400, "SQL 为空")
    result = free_query(sql, [])
    if req.session_id:
        try:
            save_messages(req.session_id, [
                {"role": "user", "content": "[SQL] " + sql},
                {"role": "assistant", "content": json.dumps(
                    {k: result.get(k) for k in ("ok", "error", "sql", "row_count", "elapsed_ms")},
                    ensure_ascii=False)},
            ])
        except Exception:  # noqa: BLE001
            pass
    return result


@app.post("/api/chat")
async def chat(req: ChatRequest) -> StreamingResponse:
    sid = req.session_id
    data = load_session(sid) if sid else None
    if data is None:
        sid = create_session()
        data = load_session(sid)

    mode = req.mode if req.mode in ("off", "standard", "deep") else "standard"
    data["messages"].append({"role": "user", "content": req.message})
    # 送入模型的消息剥离图表/报告标记，避免污染上下文
    messages = [
        {"role": m["role"], "content": (m["content"] or "").split("<!--CHART:")[0].split("<!--REPORT:")[0].rstrip()}
        for m in data["messages"]
    ]

    # 任务规划：复杂任务先生成计划（简单任务 plan 为 None）
    # 跨轮延续：会话存在 running 状态的计划时复用，不重新生成
    plan = None
    plan_id = None
    if mode != "off":
        plan_id, plan = load_running_plan(sid)
    if plan is None:
        try:
            from agent import generate_plan
            plan = generate_plan(req.message)
        except Exception:  # noqa: BLE001
            plan = None
    # 新计划落库（复用的计划不重复落库）
    if plan and plan_id is None:
        plan_id = save_plan(sid, plan)

    # 标题：首条用户消息
    if not data.get("title"):
        set_title(sid, req.message[:18])

    async def event_stream():
        def emit(event: str, payload: dict) -> bytes:
            return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()

        yield emit("session", {"session_id": sid, "mode": mode})

        # 计划推送（落库已在生成/复用时完成）
        if plan:
            yield emit("plan", {"plan": plan})

        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        # 图表持久化：捕获 chart 事件，随回复一起落库
        charts: list[dict] = []
        # 报告持久化：捕获 report 事件，随回复一起落库
        report: dict | None = None

        # 计划进度跟踪：按工具调用轮次估算进度
        plan_step = 0
        plan_total = len(plan["steps"]) if plan else 0

        def on_event(event: str, payload: dict) -> None:
            nonlocal plan_step, report
            if event == "chart":
                charts.append(payload)
            elif event == "report":
                report = payload
            if plan_id and event == "tool_start":
                plan_step += 1
                progress = min(100, int(plan_step / max(plan_total, 1) * 100))
                try:
                    update_plan_progress(plan_id, progress)
                except Exception:  # noqa: BLE001
                    pass
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    ("plan_progress", {"step": min(plan_step, plan_total),
                                       "total": plan_total, "progress": progress}),
                )
                # tool_start 本身也要转发，plan 模式下不能吞掉
                loop.call_soon_threadsafe(queue.put_nowait, (event, payload))
            else:
                loop.call_soon_threadsafe(queue.put_nowait, (event, payload))

        def run_sync() -> dict:
            return run_agent_stream(messages, mode=mode, on_event=on_event, plan=plan)

        task = loop.run_in_executor(None, run_sync)

        while not task.done() or not queue.empty():
            try:
                event, payload = await asyncio.wait_for(queue.get(), timeout=0.15)
            except asyncio.TimeoutError:
                continue
            yield emit(event, payload)

        result = await task

        # 证据入库 + 回复来源校验
        save_evidences(sid, result.get("evidences", []))
        sources = verify_reply_sources(sid, result["reply"])

        reply_content = result["reply"]
        # 落库去重：只保留最后一个图表（重绘/美化迭代的中间图无保留价值）
        for cp in charts[-1:]:
            reply_content += "\n<!--CHART:" + json.dumps(cp, ensure_ascii=False) + "-->"
        if report:
            reply_content += "\n<!--REPORT:" + json.dumps(report, ensure_ascii=False) + "-->"
        data["messages"].append({"role": "assistant", "content": reply_content})
        # 上下文压缩：按字符量触发，LLM 摘要保留关键事实
        data["messages"] = _maybe_compress(data["messages"])
        save_messages(sid, data["messages"])

        if sources:
            yield emit("sources", {"sources": sources})
        # 执行轨迹：按轮次列出调用的工具与查阅的网页
        trace = result.get("trace") or []
        if trace:
            yield emit("trace", {"trace": trace})
        if plan_id:
            try:
                # 真实状态：本轮实际执行了工具，且回复末尾无【待确认】标记，
                # 才标记完成；澄清/确认轮（模型显式标记）保持 running，供下一轮延续
                reply_text = (result["reply"] or "").rstrip()
                awaiting = "【待确认】" in reply_text[-30:]
                if result["steps"] > 1 and not awaiting:
                    update_plan_progress(plan_id, 100, "done")
            except Exception:  # noqa: BLE001
                pass
        yield emit("end", {"steps": result["steps"]})

    return StreamingResponse(event_stream(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8003)
