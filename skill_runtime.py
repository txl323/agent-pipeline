"""Agent Skills 规范运行时。

- 加载：启动时扫描 skills/ 目录，解析全部 SKILL.md 的 YAML frontmatter（name/description）
- 渐进式披露：第一层只暴露 name+description 给 LLM；第二层在 LLM 决定使用技能时才加载正文与脚本
- 记忆：技能使用记录持久化 MySQL（skill_memory 表），跨会话注入上下文
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
from typing import Callable

import pymysql
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(BASE_DIR, "skills")

# ---------------- frontmatter 解析 ----------------

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（轻量实现，避免引入 yaml 依赖歧义）。"""
    m = _FM_RE.match(text)
    if not m:
        return {}
    out: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        out[k.strip()] = v.strip()
    return out


def _parse_body(text: str) -> str:
    """剥离 frontmatter 后的正文。"""
    m = _FM_RE.match(text)
    return text[m.end():].strip() if m else text.strip()


# ---------------- 技能注册表 ----------------

class Skill:
    def __init__(self, name: str, description: str, body: str, path: str):
        self.name = name
        self.description = description
        self.body = body            # 第二层：按需加载
        self.path = path            # 技能目录
        self._funcs: dict[str, Callable] | None = None

    def load_scripts(self) -> dict[str, Callable]:
        """第二层披露：加载技能脚本，返回函数表。懒加载且只加载一次。"""
        if self._funcs is not None:
            return self._funcs
        funcs: dict[str, Callable] = {}
        scripts_dir = os.path.join(self.path, "scripts")
        if os.path.isdir(scripts_dir):
            for fn in sorted(os.listdir(scripts_dir)):
                if not fn.endswith(".py"):
                    continue
                mod_name = f"skill_{self.name}_{fn[:-3]}"
                spec = importlib.util.spec_from_file_location(mod_name, os.path.join(scripts_dir, fn))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                for attr in dir(mod):
                    obj = getattr(mod, attr)
                    if callable(obj) and not attr.startswith("_") and getattr(obj, "__module__", "") == mod_name:
                        funcs[attr] = obj
        self._funcs = funcs
        return funcs


def load_skills() -> dict[str, Skill]:
    """第一层披露：扫描 skills/ 目录，返回 name -> Skill。只解析 frontmatter。"""
    skills: dict[str, Skill] = {}
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for entry in sorted(os.listdir(SKILLS_DIR)):
        md = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(md):
            continue
        text = open(md, encoding="utf-8").read()
        fm = _parse_frontmatter(text)
        name = fm.get("name") or entry
        desc = fm.get("description", "")
        skills[name] = Skill(name, desc, _parse_body(text), os.path.join(SKILLS_DIR, entry))
    return skills


# ---------------- 技能记忆（MySQL 持久化） ----------------

def _db():
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database="agent_pipeline",
        charset="utf8mb4",
        autocommit=True,
    )


def init_memory() -> None:
    """初始化技能记忆表。"""
    with _db() as c, c.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS skill_memory ("
            "id INT AUTO_INCREMENT PRIMARY KEY,"
            "skill VARCHAR(64) NOT NULL, func VARCHAR(64) NOT NULL,"
            "sid VARCHAR(64), ok TINYINT(1), summary VARCHAR(500),"
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "INDEX idx_skill (skill), INDEX idx_sid (sid))"
        )


def remember_skill(skill: str, func: str, sid: str, ok: bool, summary: str) -> None:
    """记录一次技能使用（记忆写入）。"""
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_memory (skill, func, sid, ok, summary) VALUES (%s,%s,%s,%s,%s)",
                (skill, func, sid, 1 if ok else 0, summary[:500]),
            )
    except Exception:  # noqa: BLE001
        pass


def recall_skills(limit: int = 8) -> list[dict]:
    """跨会话回忆：最近使用的技能记录（含成功/失败与摘要）。"""
    try:
        with _db() as c, c.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT skill, func, ok, summary, created_at FROM skill_memory "
                "ORDER BY id DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def memory_context() -> str:
    """把技能记忆格式化为系统提示词片段（注入 LLM 上下文）。"""
    rows = recall_skills()
    if not rows:
        return "（暂无技能使用记忆）"
    lines = []
    for r in rows:
        status = "成功" if r.get("ok") else "失败"
        lines.append(f"- {r['skill']}.{r['func']}（{status}）：{r.get('summary') or ''}")
    return "最近技能使用记录（跨会话记忆，供参考避免重复失败尝试）：\n" + "\n".join(lines)
