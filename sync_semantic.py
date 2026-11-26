# -*- coding: utf-8 -*-
"""语义层系统记忆·第一步：感知数据库结构变化 + AI 提议（只读，不碰 semantic_v1.yaml）。

用法:
  python sync_semantic.py            # 全流程：扫描 → diff → LLM 提议 → proposals/<时间戳>.yaml
  python sync_semantic.py --no-llm   # 只扫描 + diff（datasvc 启动钩子用，只打日志）
  python sync_semantic.py --db sap_mock  # 只扫一个库

首次运行只建快照 semantic/schema_snapshot.json 后退出。
绝不修改 semantic/semantic_v1.yaml。
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
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
    ap = argparse.ArgumentParser(description="语义层系统记忆·第一步（感知+提议，不改 yaml）")
    ap.add_argument("--no-llm", action="store_true", help="只扫描+diff，不调 LLM")
    ap.add_argument("--db", default="", help="只扫一个库")
    args = ap.parse_args()

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


if __name__ == "__main__":
    sys.exit(main())
