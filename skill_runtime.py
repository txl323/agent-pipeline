"""Agent Skills 规范运行时（v2）。
- 加载：启动时扫描 skills/ 目录，只解析 SKILL.md 的 YAML frontmatter（name/description），不读取正文
- 渐进式披露：第一层只暴露 name+description；第二层 load_skill_body 按需读取正文；
  第三层 load_skill_resource 按需读取 references/ 资源；脚本 load_scripts 懒加载且缓存
- 校验：name 必填、description 必填、name 格式合法、目录名与 name 一致、重复 name 报错、
  frontmatter 非法时明确记录（SKILL_ERRORS，不中断启动）
- 记忆：只保留写入（remember_skill），系统提示注入暂时关闭（memory_context 返回空），
  避免全局跨会话失败记录污染当前请求
"""
from __future__ import annotations
import importlib.util
import os
import re
import pymysql
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SKILLS_DIR = os.path.join(BASE_DIR, "skills")

# name 合法格式：小写字母/数字/连字符，1-64 字符，字母或数字开头
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# description 长度上限（第一层披露注入系统提示词，需控制 token）
_DESC_MAX = 1024

# 启动校验问题记录（供诊断，不中断启动）
SKILL_ERRORS: list[str] = []

# ---------------- frontmatter 解析 ----------------
_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


def _parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML frontmatter（轻量实现，仅支持扁平 key: value）。"""
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


# ---------------- 技能注册表 ----------------
class Skill:
    def __init__(self, name: str, description: str, path: str, version: str = "unknown"):
        self.name = name
        self.description = description
        self.version = version        # 技能版本（frontmatter 可选字段）
        self.path = path                # 技能目录
        self._body: str | None = None    # 第二层：正文，按需读取
        self._funcs: dict | None = None  # 第三层：脚本函数表，懒加载缓存

    # ---- 第二层披露：正文 ----
    def body(self) -> str:
        """按需读取 SKILL.md 正文（剥离 frontmatter），缓存。"""
        if self._body is None:
            md = os.path.join(self.path, "SKILL.md")
            with open(md, encoding="utf-8") as f:
                text = f.read()
            m = _FM_RE.match(text)
            self._body = text[m.end():].strip() if m else text.strip()
        return self._body

    # ---- 第三层披露：资源 ----
    def resource(self, rel_path: str) -> str:
        """按需读取技能目录内资源（如 references/plan_schema.md），防目录穿越。"""
        base = os.path.realpath(self.path)
        target = os.path.realpath(os.path.join(base, rel_path))
        if not (target == base or target.startswith(base + os.sep)):
            raise ValueError(f"非法资源路径（目录穿越）：{rel_path}")
        if not os.path.isfile(target):
            raise FileNotFoundError(f"技能 {self.name} 无资源：{rel_path}")
        with open(target, encoding="utf-8") as f:
            return f.read()

    # ---- 第三层披露：脚本 ----
    def load_scripts(self) -> dict:
        """加载技能脚本，返回函数表。懒加载且只加载一次（缓存）。"""
        if self._funcs is not None:
            return self._funcs
        funcs: dict = {}
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
    """第一层披露：扫描 skills/ 目录，只解析 frontmatter（不读正文），返回 name -> Skill。
    校验失败的技术能跳过并记录到 SKILL_ERRORS，不中断启动。"""
    skills: dict[str, Skill] = {}
    SKILL_ERRORS.clear()
    if not os.path.isdir(SKILLS_DIR):
        return skills
    for entry in sorted(os.listdir(SKILLS_DIR)):
        dpath = os.path.join(SKILLS_DIR, entry)
        md = os.path.join(dpath, "SKILL.md")
        if not os.path.isdir(dpath) or not os.path.isfile(md):
            continue
        try:
            with open(md, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            SKILL_ERRORS.append(f"{entry}: SKILL.md 读取失败 {e}")
            continue
        fm = _parse_frontmatter(text)
        if not fm:
            SKILL_ERRORS.append(f"{entry}: SKILL.md 缺少合法 frontmatter（--- name/description ---）")
            continue
        name = fm.get("name", "").strip()
        desc = fm.get("description", "").strip()
        if not name:
            SKILL_ERRORS.append(f"{entry}: frontmatter 缺少 name")
            continue
        if not _NAME_RE.match(name):
            SKILL_ERRORS.append(f"{entry}: name 非法（{name}），须为小写字母/数字/连字符，1-64 字符")
            continue
        if not desc:
            SKILL_ERRORS.append(f"{entry}: frontmatter 缺少 description")
            continue
        if len(desc) > _DESC_MAX:
            SKILL_ERRORS.append(f"{entry}: description 超长（{len(desc)}>{_DESC_MAX}），已截断")
            desc = desc[:_DESC_MAX]
        if name != entry:
            SKILL_ERRORS.append(f"{entry}: 目录名与 name（{name}）不一致，已跳过")
            continue
        if name in skills:
            SKILL_ERRORS.append(f"{name}: 重复技能名，后出现的目录 {entry} 已跳过")
            continue
        skills[name] = Skill(name, desc, dpath, fm.get("version", "unknown").strip() or "unknown")
    return skills


# ---------------- 按需加载接口（供 load_skill 工具调用） ----------------
def load_skill_body(name: str) -> str:
    """第二层披露：按需读取技能正文。技能不存在时抛 KeyError。"""
    skills = load_skills()
    if name not in skills:
        raise KeyError(f"技能不存在：{name}（可用：{', '.join(sorted(skills))}）")
    return skills[name].body()


def load_skill_resource(name: str, rel_path: str) -> str:
    """第三层披露：按需读取技能资源（防目录穿越）。"""
    skills = load_skills()
    if name not in skills:
        raise KeyError(f"技能不存在：{name}")
    return skills[name].resource(rel_path)


def get_skill(name: str) -> Skill | None:
    """获取技能对象（含脚本函数表）。"""
    skills = load_skills()
    return skills.get(name)


# ---------------- 技能记忆（MySQL 持久化，只写不注入） ----------------
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
    """记录一次技能使用（记忆写入，保留）。"""
    try:
        with _db() as c, c.cursor() as cur:
            cur.execute(
                "INSERT INTO skill_memory (skill, func, sid, ok, summary) VALUES (%s,%s,%s,%s,%s)",
                (skill, func, sid, 1 if ok else 0, summary[:500]),
            )
    except Exception:  # noqa: BLE001
        pass


def recall_skills(limit: int = 8) -> list[dict]:
    """跨会话回忆：最近使用的技能记录（诊断用，不注入系统提示词）。"""
    try:
        with _db() as c, c.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT skill, func, ok, summary, created_at FROM skill_memory "
                "ORDER BY id DESC LIMIT %s", (limit,))
            return [dict(r) for r in cur.fetchall()]
    except Exception:  # noqa: BLE001
        return []


def memory_context() -> str:
    """系统提示注入已暂时关闭：跨会话失败记录会污染当前请求，只保留写入。"""
    return ""
