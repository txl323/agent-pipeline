# -*- coding: utf-8 -*-
"""语义层系统记忆·第一步：感知数据库结构变化 + AI 提议（只读，不碰 semantic_v1.yaml）。

用法:
  python sync_semantic.py            # 第一步全流程：扫描 → diff → LLM 提议 → proposals/<时间戳>.yaml
  python sync_semantic.py --no-llm   # 只扫描 + diff（datasvc 启动钩子用，只打日志）
  python sync_semantic.py --db sap_mock  # 只扫一个库
  python sync_semantic.py --apply proposals/<ts>.yaml [--dry-run]  # 第二步：合入人工确认的提议
  python sync_semantic.py --rollback [ts|latest]  # 回滚到 semantic/backup/ 指定备份（默认最近一份）

首次运行只建快照 semantic/schema_snapshot.json 后退出。
第一步绝不修改 semantic/semantic_v1.yaml；只有 --apply 才会写 yaml（写前备份、逐条校验、version 末位+1、重生成 plan_schema.md）。
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

import nlsql  # noqa: E402  白名单/连接复用

SNAPSHOT_FILE = ROOT / "semantic" / "schema_snapshot.json"
PROPOSAL_DIR = ROOT / "semantic" / "proposals"
SEMANTIC_FILE = ROOT / "semantic" / "semantic_v1.yaml"


def scan_database(db: str) -> dict:
    """扫描一个库：{table: {columns: {col: {type, comment}}, approx_rows}}。"""
    out: dict = {}
    conn = nlsql._db("information_schema")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME, TABLE_ROWS FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA=%s AND TABLE_TYPE='BASE TABLE'", (db,))
            tables = {r[0]: int(r[1] or 0) for r in cur.fetchall()}
            cur.execute(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s", (db,))
            for tname, cname, ctype, comment in cur.fetchall():
                out.setdefault(tname, {"columns": {}, "approx_rows": tables.get(tname, 0)})
                out[tname]["columns"][cname] = {"type": ctype, "comment": (comment or "").strip()}
    finally:
        conn.close()
    return out


def scan_all(dbs: list) -> dict:
    """扫描全部：{'db.table': {...}}。"""
    result: dict = {}
    for db in dbs:
        for tname, info in scan_database(db).items():
            result[db + "." + tname] = info
    return result


def yaml_covered_columns(sem: dict) -> set:
    """yaml 中已引用的全部列名（column/sql/filter_fields 任意出现）。"""
    covered: set = set()
    for section in ("metrics", "dimensions"):
        for item in (sem.get(section) or {}).values():
            col = item.get("column")
            if isinstance(col, str):
                covered.add(col.split(".")[-1].strip("`"))
            elif isinstance(col, dict):
                for v in col.values():
                    covered.add(str(v).split(".")[-1].strip("`"))
            sql = item.get("sql") or ""
            for token in str(sql).replace("(", " ").replace(")", " ").replace(",", " ").split():
                covered.add(token.split(".")[-1].strip("`"))
    for ds_fields in (sem.get("filter_fields") or {}).values():
        for item in ds_fields.values():
            covered.add(str(item.get("column", "")).split(".")[-1].strip("`"))
    return covered


def diff_scan(current: dict, snapshot: dict) -> dict:
    """四类 diff：新增表/新增列/删除列/类型变更。"""
    d = {"new_tables": [], "new_columns": [], "dropped_columns": [], "type_changes": []}
    for key in sorted(set(current) - set(snapshot)):
        d["new_tables"].append({"table": key, "columns": sorted(current[key]["columns"])})
    for key in sorted(set(current) & set(snapshot)):
        cur_cols = current[key]["columns"]
        old_cols = snapshot[key]["columns"]
        for c in sorted(set(cur_cols) - set(old_cols)):
            entry = {"table": key, "column": c}
            entry.update(cur_cols[c])
            d["new_columns"].append(entry)
        for c in sorted(set(old_cols) - set(cur_cols)):
            d["dropped_columns"].append({"table": key, "column": c})
        for c in sorted(set(cur_cols) & set(old_cols)):
            if cur_cols[c]["type"] != old_cols[c]["type"]:
                d["type_changes"].append({"table": key, "column": c,
                                           "old": old_cols[c]["type"], "new": cur_cols[c]["type"]})
    return d


def sample_values(db: str, table: str, column: str, ctype: str) -> str:
    """低基数字段取 ≤10 distinct 值；数值/日期只给类型不给值。"""
    low = ctype.upper().startswith(("CHAR", "VARCHAR", "TEXT", "ENUM"))
    if not low:
        return "（数值/时间类型 %s，不采样）" % ctype
    try:
        conn = nlsql._db(db)
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT `%s` FROM `%s` WHERE `%s` IS NOT NULL LIMIT 10"
                        % (column, table, column))
            vals = [str(r[0]) for r in cur.fetchall()]
        conn.close()
        return ("采样值: " + " / ".join(vals)) if vals else "（无数据）"
    except Exception as e:  # noqa: BLE001
        return "（采样失败: %s）" % e


def llm_propose(candidates: list, sem: dict) -> list:
    """一次 LLM 调用：候选列 → 分类 + yaml 字段建议。失败时抛异常由调用方处理。"""
    by_table: dict = {}
    for c in candidates:
        by_table.setdefault(c["table"], {"columns": [], "candidates": []})
    dbs = sorted({c["table"].split(".")[0] for c in candidates})
    for key, info in scan_all(dbs).items():
        if key in by_table:
            by_table[key]["columns"] = [
                {"name": cn, "type": ci["type"], "comment": ci["comment"]}
                for cn, ci in info["columns"].items()]
    for c in candidates:
        db, table = c["table"].split(".", 1)
        by_table[c["table"]]["candidates"].append({
            "column": c["column"], "type": c["type"], "comment": c["comment"],
            "samples": sample_values(db, table, c["column"], c["type"]),
        })
    example = {
        "dimension": {"name": "客户区域", "aliases": ["区域"], "column": "c.region",
                      "join": "JOIN ...", "applies_to": ["sales"]},
        "metric": {"name": "销售额", "aliases": [], "sql": "SUM(so.amount)",
                   "table": "sap_sales_orders so", "datasource": "sales"},
        "filter_field": {"column": "so.status", "ops": ["=", "IN"]},
    }
    prompt = (
        "你是语义层设计助手。为下列候选列提议语义层条目。\n"
        "表结构: " + json.dumps(by_table, ensure_ascii=False) + "\n"
        "yaml 格式示例: " + json.dumps(example, ensure_ascii=False) + "\n"
        "输出 JSON 数组，每项: {table, column, kind: dimension|metric|filter_field|skip, "
        "entry: {符合示例结构的字段}, reason: 一句话}。column 必须带表别名（如 so.channel）。"
        "只输出 JSON，不要其他文字。"
    )
    resp = requests.post(
        os.environ["DEEPSEEK_BASE_URL"].rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"]},
        json={"model": "deepseek-chat", "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=120,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    return json.loads(text)


def main() -> int:
    ap = argparse.ArgumentParser(description="语义层系统记忆：第一步感知+提议（默认），第二步 --apply 合入 / --rollback 回滚")
    ap.add_argument("--no-llm", action="store_true", help="只扫描+diff，不调 LLM")
    ap.add_argument("--db", default="", help="只扫一个库")
    ap.add_argument("--apply", default="", metavar="PROPOSAL", help="第二步：合入指定 proposals/<时间戳>.yaml（人工确认后）")
    ap.add_argument("--rollback", nargs="?", const="latest", default="", metavar="TS|latest", help="回滚：恢复到 semantic/backup/ 指定时间戳备份（默认 latest 最近一份）")
    ap.add_argument("--dry-run", action="store_true", help="配合 --apply：仅校验与预览，不写 yaml")
    args = ap.parse_args()

    if args.rollback:
        return do_rollback(args.rollback)
    if args.apply:
        return do_apply(args.apply, dry_run=args.dry_run)

    dbs = [args.db] if args.db else list(nlsql.ALLOWED_DATABASES)
    for db in dbs:
        if db not in nlsql.ALLOWED_DATABASES:
            print("库不在白名单: " + db)
            return 1

    current = scan_all(dbs)
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SNAPSHOT_FILE.exists():
        SNAPSHOT_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        print("[sync_semantic] 首次运行：已建快照 %s（%d 表），退出" % (SNAPSHOT_FILE, len(current)))
        return 0

    snapshot = json.loads(SNAPSHOT_FILE.read_text(encoding="utf-8"))
    d = diff_scan(current, snapshot)
    n_changes = sum(len(v) for v in d.values())
    if n_changes == 0:
        print("[sync_semantic] 无变更")
        return 0

    print("[sync_semantic] 检出变更: 新增表 %d，新增列 %d，删除列 %d，类型变更 %d" % (
        len(d["new_tables"]), len(d["new_columns"]), len(d["dropped_columns"]), len(d["type_changes"])))

    sem = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8"))
    covered = yaml_covered_columns(sem)
    candidates = [c for c in d["new_columns"] if c["column"] not in covered]
    skipped_covered = [c for c in d["new_columns"] if c["column"] in covered]
    print("[sync_semantic] 新增列中已覆盖 %d 个（仅记录），候选 %d 个" % (
        len(skipped_covered), len(candidates)))

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    header = {
        "generated_at": datetime.datetime.now().isoformat(),
        "summary": {k: len(v) for k, v in d.items()},
        "candidates": [c["table"] + "." + c["column"] for c in candidates],
        "covered_new_columns": [c["table"] + "." + c["column"] for c in skipped_covered],
        "note": "AI 提议草稿，人工编辑确认后由第二步 --apply 合入 semantic_v1.yaml",
    }
    entries: list = []
    if candidates and not args.no_llm:
        try:
            entries = llm_propose(candidates, sem)
            print("[sync_semantic] LLM 提议完成: %d 条" % len(entries))
        except Exception as e:  # noqa: BLE001
            print("[sync_semantic] LLM 提议失败（候选保留待重试）: %s" % e)
            entries = []
    elif candidates:
        print("[sync_semantic] --no-llm：跳过 LLM 提议（候选 %d 个仅记录）" % len(candidates))

    PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
    out = PROPOSAL_DIR / (ts + ".yaml")
    body = {"diff_summary": header,
            "proposals": entries or ["（无 LLM 提议：--no-llm 模式或调用失败；候选列见头部 candidates）"]}
    out.write_text(yaml.dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print("[sync_semantic] 提议文件已写: %s" % out)
    print("[sync_semantic] semantic_v1.yaml 未被改动")
    return 0


BACKUP_DIR = ROOT / "semantic" / "backup"
PLAN_SCHEMA_FILE = ROOT / "skills" / "data-query" / "references" / "plan_schema.md"
AUDIT_FILE = ROOT / "semantic" / "audit.log"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _yaml_rt():
    from ruamel.yaml import YAML
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.width = 4096
    return y


def _bump_version(version: str) -> str:
    parts = str(version or "1.0.0").strip().strip('"').split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        parts[-1] = str(int(parts[-1]) + 1)
    except ValueError:
        parts[-1] = "1"
    return ".".join(parts[:3])


def _resolve_proposal(arg: str) -> Path:
    p = Path(arg)
    if p.is_absolute() and p.exists():
        return p
    cand = PROPOSAL_DIR / arg
    if cand.exists():
        return cand
    if not arg.endswith(".yaml"):
        cand2 = PROPOSAL_DIR / (arg + ".yaml")
        if cand2.exists():
            return cand2
    raise FileNotFoundError("proposal 不存在: %s" % arg)


def _derive_key(item: dict) -> str:
    key = str(item.get("key") or (item.get("entry") or {}).get("key") or "").strip()
    if key:
        return key
    col = str(item.get("column") or (item.get("entry") or {}).get("column") or "").strip().strip("`")
    suffix = col.split(".")[-1].strip("`") if col else ""
    suffix = re.sub(r"[^A-Za-z0-9_]", "_", suffix).lower()
    if not suffix or not _IDENT_RE.match(suffix):
        raise ValueError("无法从 column 推导合法 key，请在 proposal 里显式给 key: %r" % col)
    return suffix


def _table_exists(db: str, table: str) -> bool:
    with nlsql._db("information_schema") as c, c.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM TABLES WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s LIMIT 1",
            (db, table),
        )
        return cur.fetchone() is not None


def _column_exists(db: str, table: str, column: str) -> bool:
    if not (_IDENT_RE.match(table or "") and _IDENT_RE.match(column or "")):
        return False
    with nlsql._db("information_schema") as c, c.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s LIMIT 1",
            (db, table, column),
        )
        return cur.fetchone() is not None


def _explain_column(db: str, table: str, column: str) -> None:
    if not (_IDENT_RE.match(table or "") and _IDENT_RE.match(column or "")):
        raise ValueError("非法标识符: %s.%s" % (table, column))
    with nlsql._db(db) as c, c.cursor() as cur:
        cur.execute("EXPLAIN SELECT `%s` FROM `%s` LIMIT 1" % (column, table))
        cur.fetchall()


def _validate_join_tables(join_sql: str) -> None:
    for full in re.findall(r"(?:JOIN|FROM)\s+([A-Za-z0-9_]+\.[A-Za-z0-9_]+)", join_sql or "", flags=re.I):
        db, table = full.split(".", 1)
        if not _table_exists(db, table):
            raise ValueError("JOIN 引用的表不存在: %s" % full)


def _validate_proposal_item(item: dict, sem_plain: dict) -> tuple[str, str]:
    kind = str(item.get("kind") or "").strip()
    if kind in ("", "skip"):
        raise ValueError("skip/空 kind 不可合入")
    if kind not in ("dimension", "metric", "filter_field"):
        raise ValueError("未知 kind: %s" % kind)
    entry = item.get("entry")
    if not isinstance(entry, dict):
        raise ValueError("entry 缺失或不是对象")
    key = _derive_key(item)
    table_full = str(item.get("table") or "").strip()
    if "." not in table_full:
        raise ValueError("table 必须是 db.table: %r" % table_full)
    db, table = table_full.split(".", 1)
    if db not in nlsql.ALLOWED_DATABASES:
        raise ValueError("库不在白名单: %s" % db)
    if not _table_exists(db, table):
        raise ValueError("表不存在: %s" % table_full)

    covered = yaml_covered_columns(sem_plain)
    if kind in ("dimension", "filter_field"):
        col = str(entry.get("column") or item.get("column") or "").strip().strip("`")
        if not col:
            raise ValueError("dimension/filter_field 缺少 column")
        if col in covered:
            raise ValueError("列已被语义层覆盖，拒绝重复合入: %s" % col)
        suffix = col.split(".")[-1].strip("`")
        if not ("(" in col or " " in col):
            if not _column_exists(db, table, suffix):
                raise ValueError("列不存在: %s.%s.%s" % (db, table, suffix))
            _explain_column(db, table, suffix)
        _validate_join_tables(str(entry.get("join") or ""))
    elif kind == "metric":
        sql = str(entry.get("sql") or "")
        src_table = str(entry.get("table") or "")
        if not sql or not src_table:
            raise ValueError("metric 缺少 sql/table")
        base_table = src_table.split()[0]
        if not _table_exists(db, base_table):
            raise ValueError("metric 引用的表不存在: %s.%s" % (db, base_table))
    return key, kind


def _merge_entry(sem, key: str, kind: str, entry: dict, table_full: str) -> None:
    if kind == "dimension":
        dims = sem.setdefault("dimensions", {})
        if key in dims:
            raise ValueError("dimension 已存在: %s" % key)
        e = dict(entry)
        e.setdefault("join", "")
        e.setdefault("applies_to", [])
        e.setdefault("aliases", [])
        dims[key] = e
    elif kind == "metric":
        metrics = sem.setdefault("metrics", {})
        if key in metrics:
            raise ValueError("metric 已存在: %s" % key)
        metrics[key] = dict(entry)
    elif kind == "filter_field":
        ds = None
        for name, database in (sem.get("datasources") or {}).items():
            if str(database) == table_full.split(".", 1)[0]:
                ds = str(name)
                break
        if not ds:
            raise ValueError("无法映射 datasource: %s" % table_full)
        ff = sem.setdefault("filter_fields", {}).setdefault(ds, {})
        if key in ff:
            raise ValueError("filter_field 已存在: %s/%s" % (ds, key))
        e = dict(entry)
        e.setdefault("ops", ["=", "IN"])
        ff[key] = e


def _fmt_aliases(aliases) -> str:
    if not aliases:
        return ""
    return "/".join(str(a) for a in list(aliases)[:4])


def _dim_col_text(col) -> str:
    if isinstance(col, dict):
        return " / ".join(str(v) for v in col.values())
    return str(col)


def _data_range_text() -> str:
    """动态统计事实表日期范围（销售/采购），失败时返回说明而不是阻断生成。"""
    try:
        with nlsql._db("sap_mock") as c, c.cursor() as cur:
            cur.execute(
                "SELECT MIN(d), MAX(d) FROM ("
                "SELECT order_date AS d FROM sap_sales_orders "
                "UNION ALL SELECT order_date FROM sap_purchase_orders) t"
            )
            row = cur.fetchone()
        if not row or row[0] is None or row[1] is None:
            return "未知（无数据）"
        return "%s ~ %s" % (row[0], row[1])
    except Exception as e:  # noqa: BLE001
        return "未知（统计失败: %s）" % e


def generate_plan_schema(sem: dict) -> str:
    lines = []
    lines.append("# Plan JSON 参数契约（唯一真相）")
    lines.append("")
    lines.append("> 由语义层 `semantic/semantic_v1.yaml`（version %s）自动生成。不要手工改本文件；改语义层后执行 sync_semantic --apply/--rollback 会重生成。" % sem.get("version", "unknown"))
    lines.append("")
    lines.append("## 指标（metric）")
    lines.append("")
    lines.append("| 指标 | 名称 | 别名 | 数据源 | SQL 表达式 |")
    lines.append("|---|---|---|---|---|")
    for key, m in (sem.get("metrics") or {}).items():
        lines.append("| %s | %s | %s | %s | %s |" % (key, m.get("name", ""), _fmt_aliases(m.get("aliases")), m.get("datasource", ""), m.get("sql", "")))
    lines.append("")
    lines.append("## 维度（dimensions）")
    lines.append("")
    lines.append("| 维度 | 名称 | 物理列名 | 适用数据源 | JOIN |")
    lines.append("|---|---|---|---|---|")
    for key, d in (sem.get("dimensions") or {}).items():
        applies = "/".join(d.get("applies_to") or [])
        join = str(d.get("join") or "").strip() or "（无%s）" % ("，时间维度" if d.get("is_time") else "")
        lines.append("| %s | %s | %s | %s | %s |" % (key, d.get("name", ""), _dim_col_text(d.get("column", "")), applies, join))
    lines.append("")
    lines.append("**注意**：`material` 是语义层合法维度，物理列名是 `material_name`（free_query 写 SQL 时必须用物理列名）。")
    lines.append("")
    lines.append("## 过滤字段（filters）白名单")
    lines.append("")
    for ds, fields in (sem.get("filter_fields") or {}).items():
        lines.append("### %s 库" % ds)
        lines.append("| 字段 | 物理列 | 允许操作符 |")
        lines.append("|---|---|---|")
        for name, f in (fields or {}).items():
            lines.append("| %s | %s | %s |" % (name, f.get("column", ""), " / ".join(f.get("ops") or [])))
        lines.append("")
    lines.append("## 时间宏（time_macro）")
    lines.append("")
    lines.append("| 宏 | 含义 |")
    lines.append("|---|---|")
    macro_names = {"last_month": "上一个自然月", "this_month": "本自然月", "last_7_days": "最近 7 天", "last_30_days": "最近 30 天", "all": "全部数据（默认）"}
    for key in (sem.get("time_macros") or {}).keys():
        lines.append("| %s | %s |" % (key, macro_names.get(key, key)))
    lines.append("")
    lines.append("## 数据范围")
    lines.append("")
    lines.append("- 业务三库只读：bom_system / mdm_platform / sap_mock")
    lines.append("- 数据时间范围：" + _data_range_text())
    lines.append("- 事实表：sap_mock.sap_sales_orders / sap_mock.sap_purchase_orders / sap_mock.sap_inventory")
    lines.append("")
    return "\n".join(lines)


def _regen_plan_schema(sem_plain: dict) -> None:
    PLAN_SCHEMA_FILE.parent.mkdir(parents=True, exist_ok=True)
    PLAN_SCHEMA_FILE.write_text(generate_plan_schema(sem_plain), encoding="utf-8")


def _refresh_snapshot() -> None:
    current = scan_all(list(nlsql.ALLOWED_DATABASES))
    SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_FILE.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")


def _audit(line: str) -> None:
    AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(datetime.datetime.now().isoformat() + " " + line + "\n")


def _list_backups() -> list[Path]:
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("semantic_v1_*.yaml"))


def _backup_current(ts: str) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / ("semantic_v1_%s.yaml" % ts)
    shutil.copy2(SEMANTIC_FILE, dst)
    (BACKUP_DIR / "latest").write_text(dst.name, encoding="utf-8")
    backups = _list_backups()
    for old in backups[:-20]:
        try:
            old.unlink()
        except OSError:
            pass
    return dst


def do_apply(proposal_arg: str, dry_run: bool = False) -> int:
    try:
        prop_file = _resolve_proposal(proposal_arg)
        data = yaml.safe_load(prop_file.read_text(encoding="utf-8")) or {}
        raw = data.get("proposals") or []
        items = [x for x in raw if isinstance(x, dict) and str(x.get("kind") or "") not in ("", "skip")]
        if not items:
            print("[sync_semantic] 无可合入条目（skip/占位/空）: %s" % prop_file)
            return 1
        y = _yaml_rt()
        sem = y.load(SEMANTIC_FILE.read_text(encoding="utf-8"))
        sem_plain = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8")) or {}
        validated = []
        for item in items:
            key, kind = _validate_proposal_item(item, sem_plain)
            validated.append((key, kind, dict(item.get("entry") or {}), str(item.get("table") or "")))
            print("[sync_semantic] 校验通过: %s %s (%s)" % (kind, key, item.get("column") or item.get("entry", {}).get("column") or item.get("entry", {}).get("sql")))
        old_version = str(sem.get("version", "unknown"))
        for key, kind, entry, table_full in validated:
            _merge_entry(sem, key, kind, entry, table_full)
        sem["version"] = _bump_version(sem.get("version"))
        print("[sync_semantic] version: %s -> %s" % (old_version, sem["version"]))
        if dry_run:
            print("[sync_semantic] dry-run：仅校验与预览，不写 yaml")
            return 0
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = _backup_current(ts)
        tmp = SEMANTIC_FILE.with_suffix(".yaml.tmp")
        import io
        buf = io.StringIO()
        y.dump(sem, buf)
        tmp.write_text(buf.getvalue(), encoding="utf-8")
        tmp.replace(SEMANTIC_FILE)
        new_plain = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8")) or {}
        _regen_plan_schema(new_plain)
        _refresh_snapshot()
        _audit("apply %s version=%s backup=%s items=%d" % (prop_file.name, sem["version"], backup.name, len(validated)))
        print("[sync_semantic] 合入完成: %d 条；备份 %s；已重生成 plan_schema.md 并刷新快照" % (len(validated), backup))
        return 0
    except Exception as e:  # noqa: BLE001
        print("[sync_semantic] apply 失败: %s" % e)
        return 1


def do_rollback(which: str = "latest") -> int:
    try:
        backups = _list_backups()
        if not backups:
            print("[sync_semantic] 无备份可回滚")
            return 1
        target = None
        if which in ("", "latest"):
            pointer = BACKUP_DIR / "latest"
            if pointer.exists():
                name = pointer.read_text(encoding="utf-8").strip()
                p = BACKUP_DIR / name
                if p.exists():
                    target = p
            target = target or backups[-1]
        else:
            for p in backups:
                if which in p.name:
                    target = p
                    break
        if not target or not target.exists():
            print("[sync_semantic] 找不到备份: %s" % which)
            return 1
        before = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8")) or {}
        shutil.copy2(target, SEMANTIC_FILE)
        restored = yaml.safe_load(SEMANTIC_FILE.read_text(encoding="utf-8")) or {}
        _regen_plan_schema(restored)
        _refresh_snapshot()
        _audit("rollback %s version %s -> %s" % (target.name, before.get("version"), restored.get("version")))
        print("[sync_semantic] 已回滚到 %s（version %s -> %s），并重生成 plan_schema.md/快照" % (target.name, before.get("version"), restored.get("version")))
        return 0
    except Exception as e:  # noqa: BLE001
        print("[sync_semantic] rollback 失败: %s" % e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
