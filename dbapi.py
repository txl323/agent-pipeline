"""演示数据查询 API：BOM 系统 / 主数据平台 / SAP 模拟。

- FastAPI，监听 127.0.0.1:8004
- 只读查询接口，供 Agent 工具调用与图表数据使用
- 日期参数格式 YYYY-MM-DD，默认最近一个月
"""

from __future__ import annotations

import os
from typing import Optional

import pymysql
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query

load_dotenv()

app = FastAPI(title="Demo Data API")

DB_CONFIGS = {
    "bom_system": "bom_system",
    "mdm_platform": "mdm_platform",
    "sap_mock": "sap_mock",
}


def db(database: str) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=database,
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def query(database: str, sql: str, args: tuple = (), limit: int = 100) -> list[dict]:
    with db(database) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchmany(limit)


def _rows_to_jsonable(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        out.append({k: (str(v) if hasattr(v, "isoformat") else float(v) if hasattr(v, "as_tuple") else v)
                    for k, v in r.items()})
    return out


# ---------------- BOM 系统 ----------------

@app.get("/api/bom/boms")
def list_boms(product_code: str = "", status: str = ""):
    """BOM 清单：可按产品编码/状态过滤。"""
    sql = "SELECT bom_code, product_code, product_name, version, status, created_at FROM bom_headers WHERE 1=1"
    args: list = []
    if product_code:
        sql += " AND product_code=%s"
        args.append(product_code)
    if status:
        sql += " AND status=%s"
        args.append(status)
    sql += " ORDER BY bom_code"
    return {"boms": _rows_to_jsonable(query("bom_system", sql, tuple(args)))}


@app.get("/api/bom/boms/{bom_code}")
def get_bom(bom_code: str):
    """BOM 明细：指定 BOM 编码的全部子件（物料、用量、损耗率）。"""
    head = query("bom_system",
                 "SELECT * FROM bom_headers WHERE bom_code=%s", (bom_code,))
    if not head:
        raise HTTPException(404, "BOM not found")
    items = query("bom_system",
                  "SELECT item_no, material_code, material_name, quantity, unit, scrap_rate "
                  "FROM bom_items WHERE bom_code=%s ORDER BY item_no", (bom_code,), limit=200)
    return {"bom": _rows_to_jsonable(head)[0], "items": _rows_to_jsonable(items)}


@app.get("/api/bom/where-used")
def where_used(material_code: str):
    """反查物料用途：该物料被哪些 BOM 使用、用量多少。"""
    rows = query("bom_system",
                 "SELECT b.bom_code, b.product_name, i.quantity, i.unit, i.scrap_rate "
                 "FROM bom_items i JOIN bom_headers b ON i.bom_code=b.bom_code "
                 "WHERE i.material_code=%s", (material_code,))
    return {"material_code": material_code, "used_in": _rows_to_jsonable(rows)}


# ---------------- 主数据平台 ----------------

@app.get("/api/mdm/materials")
def list_materials(keyword: str = "", category: str = ""):
    """物料主数据：可按名称关键词/分类过滤。"""
    sql = "SELECT material_code, material_name, category, spec, unit, status FROM md_materials WHERE 1=1"
    args: list = []
    if keyword:
        sql += " AND (material_name LIKE %s OR material_code LIKE %s)"
        args += [f"%{keyword}%", f"%{keyword}%"]
    if category:
        sql += " AND category=%s"
        args.append(category)
    return {"materials": _rows_to_jsonable(query("mdm_platform", sql, tuple(args)))}


@app.get("/api/mdm/suppliers")
def list_suppliers(keyword: str = "", region: str = ""):
    """供应商主数据：可按名称关键词/区域过滤。"""
    sql = "SELECT supplier_code, supplier_name, contact, phone, region FROM md_suppliers WHERE 1=1"
    args: list = []
    if keyword:
        sql += " AND supplier_name LIKE %s"
        args.append(f"%{keyword}%")
    if region:
        sql += " AND region=%s"
        args.append(region)
    return {"suppliers": _rows_to_jsonable(query("mdm_platform", sql, tuple(args)))}


@app.get("/api/mdm/customers")
def list_customers(keyword: str = "", region: str = "", level: str = ""):
    """客户主数据：可按名称关键词/区域/等级过滤。"""
    sql = "SELECT customer_code, customer_name, region, level FROM md_customers WHERE 1=1"
    args: list = []
    if keyword:
        sql += " AND customer_name LIKE %s"
        args.append(f"%{keyword}%")
    if region:
        sql += " AND region=%s"
        args.append(region)
    if level:
        sql += " AND level=%s"
        args.append(level)
    return {"customers": _rows_to_jsonable(query("mdm_platform", sql, tuple(args)))}


# ---------------- SAP 模拟 ----------------

@app.get("/api/sap/purchase-orders")
def purchase_orders(
    material_code: str = "",
    supplier_code: str = "",
    start_date: str = Query("2026-10-24"),
    end_date: str = Query("2026-11-23"),
    limit: int = Query(100, le=500),
):
    """采购订单：可按物料/供应商/日期范围过滤。"""
    sql = ("SELECT po_number, supplier_code, supplier_name, material_code, material_name, "
           "quantity, unit, unit_price, amount, order_date, status "
           "FROM sap_purchase_orders WHERE order_date BETWEEN %s AND %s")
    args: list = [start_date, end_date]
    if material_code:
        sql += " AND material_code=%s"
        args.append(material_code)
    if supplier_code:
        sql += " AND supplier_code=%s"
        args.append(supplier_code)
    sql += " ORDER BY order_date DESC"
    return {"purchase_orders": _rows_to_jsonable(query("sap_mock", sql, tuple(args), limit))}


@app.get("/api/sap/sales-orders")
def sales_orders(
    material_code: str = "",
    customer_code: str = "",
    start_date: str = Query("2026-10-24"),
    end_date: str = Query("2026-11-23"),
    limit: int = Query(100, le=500),
):
    """销售订单：可按物料/客户/日期范围过滤。"""
    sql = ("SELECT so_number, customer_code, customer_name, material_code, material_name, "
           "quantity, unit, unit_price, amount, order_date, status "
           "FROM sap_sales_orders WHERE order_date BETWEEN %s AND %s")
    args: list = [start_date, end_date]
    if material_code:
        sql += " AND material_code=%s"
        args.append(material_code)
    if customer_code:
        sql += " AND customer_code=%s"
        args.append(customer_code)
    sql += " ORDER BY order_date DESC"
    return {"sales_orders": _rows_to_jsonable(query("sap_mock", sql, tuple(args), limit))}


@app.get("/api/sap/inventory")
def inventory(material_code: str = "", warehouse: str = ""):
    """库存：可按物料/仓库过滤。"""
    sql = "SELECT material_code, material_name, warehouse, quantity, updated_at FROM sap_inventory WHERE 1=1"
    args: list = []
    if material_code:
        sql += " AND material_code=%s"
        args.append(material_code)
    if warehouse:
        sql += " AND warehouse=%s"
        args.append(warehouse)
    return {"inventory": _rows_to_jsonable(query("sap_mock", sql, tuple(args)))}


# ---------------- 汇总统计（图表数据源） ----------------

@app.get("/api/sap/sales/daily")
def sales_daily(start_date: str = Query("2026-10-24"), end_date: str = Query("2026-11-23")):
    """按日销售汇总：order_date -> 订单数/数量/金额，适合折线图。"""
    rows = query("sap_mock",
                 "SELECT order_date, COUNT(*) AS orders, SUM(quantity) AS qty, SUM(amount) AS amount "
                 "FROM sap_sales_orders WHERE order_date BETWEEN %s AND %s "
                 "GROUP BY order_date ORDER BY order_date", (start_date, end_date))
    return {"daily": _rows_to_jsonable(rows)}


@app.get("/api/sap/sales/by-material")
def sales_by_material(start_date: str = Query("2026-10-24"), end_date: str = Query("2026-11-23")):
    """按物料销售汇总：material -> 数量/金额，适合柱状图/饼图。"""
    rows = query("sap_mock",
                 "SELECT material_code, material_name, SUM(quantity) AS qty, SUM(amount) AS amount "
                 "FROM sap_sales_orders WHERE order_date BETWEEN %s AND %s "
                 "GROUP BY material_code, material_name ORDER BY amount DESC", (start_date, end_date))
    return {"by_material": _rows_to_jsonable(rows)}


@app.get("/api/sap/purchase/by-supplier")
def purchase_by_supplier(start_date: str = Query("2026-10-24"), end_date: str = Query("2026-11-23")):
    """按供应商采购汇总：supplier -> 订单数/金额，适合柱状图。"""
    rows = query("sap_mock",
                 "SELECT supplier_code, supplier_name, COUNT(*) AS orders, SUM(amount) AS amount "
                 "FROM sap_purchase_orders WHERE order_date BETWEEN %s AND %s "
                 "GROUP BY supplier_code, supplier_name ORDER BY amount DESC", (start_date, end_date))
    return {"by_supplier": _rows_to_jsonable(rows)}


from fastapi.responses import FileResponse


@app.get("/")
def index():
    """数据库浏览页面（禁用缓存，确保总是最新版）。"""
    return FileResponse(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "dbviewer.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8004)
