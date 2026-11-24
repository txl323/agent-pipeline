"""初始化演示数据库：BOM 系统 / 主数据平台 / SAP 模拟。

- 3 个库 8 张表，物料编码跨库一致，便于关联查询
- 订单数据覆盖最近一个月（2026-10-24 ~ 2026-11-23）
- 幂等：重复执行会先删库重建
"""

from __future__ import annotations

import os
import random
from datetime import date, timedelta

import pymysql
from dotenv import load_dotenv

load_dotenv()

random.seed(42)

START = date(2026, 10, 24)
END = date(2026, 11, 23)
DAYS = (END - START).days + 1  # 31 天

# ---------------- 主数据 ----------------
MATERIALS = [
    ("M10001", "锂电池电芯", "电芯", "3.7V 2600mAh", "颗"),
    ("M10002", "电池保护板", "电路板", "PCB-3S", "块"),
    ("M10003", "铝合金外壳", "结构件", "160x80x22mm", "个"),
    ("M10004", "Type-C 接口", "连接器", "16Pin", "个"),
    ("M10005", "充电适配器", "电源", "20W PD", "个"),
    ("M10006", "包装彩盒", "包材", "180x90x40mm", "个"),
    ("M10007", "说明书", "包材", "A5 双面", "张"),
    ("M10008", "螺丝刀头", "耗材", "PH2", "支"),
    ("M10009", "导热硅脂", "辅料", "1.5W/mK", "克"),
    ("M10010", "绝缘麦拉片", "辅料", "0.1mm", "片"),
    ("M10011", "充电线", "线材", "USB-C to C 1m", "条"),
    ("M10012", "标签贴纸", "包材", "50x30mm", "张"),
]

SUPPLIERS = [
    ("S001", "深圳华芯电子", "张伟", "0755-88210001", "华南"),
    ("S002", "东莞精工五金", "李强", "0769-22330002", "华南"),
    ("S003", "苏州联达连接器", "王芳", "0512-66770003", "华东"),
    ("S004", "宁波恒塑包装", "赵敏", "0574-87650004", "华东"),
    ("S005", "佛山德昌电源", "陈军", "0757-83320005", "华南"),
]

CUSTOMERS = [
    ("C001", "北京京东世纪贸易", "华北", "A"),
    ("C002", "上海天猫供应链", "华东", "A"),
    ("C003", "广州华多网络科技", "华南", "B"),
    ("C004", "杭州拼多多网络", "华东", "A"),
    ("C005", "成都红旗连锁", "西南", "C"),
    ("C006", "武汉中商平价", "华中", "C"),
]

# BOM：2 个产品，各 4-6 个子件
BOMS = [
    ("BOM-PB-001", "P9001", "便携充电宝 10000mAh", "V2.1", "released"),
    ("BOM-PB-002", "P9002", "便携充电宝 20000mAh", "V1.0", "released"),
    ("BOM-CH-001", "P9003", "快充充电器 20W", "V1.2", "released"),
]

BOM_ITEMS = {
    "BOM-PB-001": [("M10001", 4, 0.02), ("M10002", 1, 0.01), ("M10003", 1, 0.00),
                    ("M10004", 1, 0.005), ("M10006", 1, 0.00), ("M10007", 1, 0.00)],
    "BOM-PB-002": [("M10001", 8, 0.02), ("M10002", 1, 0.01), ("M10003", 1, 0.00),
                    ("M10004", 2, 0.005), ("M10006", 1, 0.00), ("M10007", 1, 0.00)],
    "BOM-CH-001": [("M10005", 1, 0.01), ("M10004", 1, 0.005), ("M10006", 1, 0.00), ("M10012", 2, 0.00)],
}


def conn(database: str | None = None) -> pymysql.connections.Connection:
    return pymysql.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ["DB_PORT"]),
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


def run(sql: str, args: tuple = ()) -> None:
    """在 agent_pipeline 连接上执行任意 SQL（建库/删库）。"""
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, args)


def seed(database: str, ddl: list[str], inserts: list[tuple[str, tuple]]) -> None:
    with conn(database) as c, c.cursor() as cur:
        for stmt in ddl:
            cur.execute(stmt)
        for sql, rows in inserts:
            cur.executemany(sql, rows)


def main() -> None:
    # 幂等：删库重建
    for db in ("bom_system", "mdm_platform", "sap_mock"):
        run(f"DROP DATABASE IF EXISTS {db}")
        run(f"CREATE DATABASE {db} DEFAULT CHARACTER SET utf8mb4")

    # ---------- 主数据平台 ----------
    seed("mdm_platform", [
        """CREATE TABLE md_materials (
            id INT AUTO_INCREMENT PRIMARY KEY,
            material_code VARCHAR(32) NOT NULL UNIQUE,
            material_name VARCHAR(128) NOT NULL,
            category VARCHAR(64),
            spec VARCHAR(128),
            unit VARCHAR(16),
            status VARCHAR(16) DEFAULT 'active',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE md_suppliers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            supplier_code VARCHAR(32) NOT NULL UNIQUE,
            supplier_name VARCHAR(128) NOT NULL,
            contact VARCHAR(32),
            phone VARCHAR(32),
            region VARCHAR(32),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE md_customers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            customer_code VARCHAR(32) NOT NULL UNIQUE,
            customer_name VARCHAR(128) NOT NULL,
            region VARCHAR(32),
            level VARCHAR(8),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
    ], [
        ("INSERT INTO md_materials (material_code, material_name, category, spec, unit) VALUES (%s,%s,%s,%s,%s)",
         MATERIALS),
        ("INSERT INTO md_suppliers (supplier_code, supplier_name, contact, phone, region) VALUES (%s,%s,%s,%s,%s)",
         SUPPLIERS),
        ("INSERT INTO md_customers (customer_code, customer_name, region, level) VALUES (%s,%s,%s,%s)",
         CUSTOMERS),
    ])

    # ---------- BOM 系统 ----------
    items_rows = []
    for bom_code, _, _, _, _ in BOMS:
        for no, (mat, qty, scrap) in enumerate(BOM_ITEMS[bom_code], start=1):
            name = next(m[1] for m in MATERIALS if m[0] == mat)
            unit = next(m[4] for m in MATERIALS if m[0] == mat)
            items_rows.append((bom_code, no, mat, name, qty, unit, scrap))
    seed("bom_system", [
        """CREATE TABLE bom_headers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            bom_code VARCHAR(32) NOT NULL UNIQUE,
            product_code VARCHAR(32) NOT NULL,
            product_name VARCHAR(128) NOT NULL,
            version VARCHAR(16),
            status VARCHAR(16),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE bom_items (
            id INT AUTO_INCREMENT PRIMARY KEY,
            bom_code VARCHAR(32) NOT NULL,
            item_no INT NOT NULL,
            material_code VARCHAR(32) NOT NULL,
            material_name VARCHAR(128),
            quantity DECIMAL(12,4) NOT NULL,
            unit VARCHAR(16),
            scrap_rate DECIMAL(6,4) DEFAULT 0,
            KEY idx_bom (bom_code),
            KEY idx_mat (material_code))""",
    ], [
        ("INSERT INTO bom_headers (bom_code, product_code, product_name, version, status) VALUES (%s,%s,%s,%s,%s)",
         BOMS),
        ("INSERT INTO bom_items (bom_code, item_no, material_code, material_name, quantity, unit, scrap_rate) VALUES (%s,%s,%s,%s,%s,%s,%s)",
         items_rows),
    ])

    # ---------- SAP 模拟（一个月订单 + 库存） ----------
    price = {m[0]: round(random.uniform(1.5, 60), 2) for m in MATERIALS}
    po_rows, so_rows = [], []
    for i in range(60):  # 采购订单 60 条
        d = START + timedelta(days=random.randrange(DAYS))
        mat = random.choice(MATERIALS)
        sup = random.choice(SUPPLIERS)
        qty = random.randrange(100, 2000, 50)
        amount = round(qty * price[mat[0]], 2)
        po_rows.append((f"PO-2026-{i + 1:05d}", sup[0], sup[1], mat[0], mat[1],
                        qty, mat[4], price[mat[0]], amount, d.isoformat(),
                        random.choice(["open", "released", "closed"])))
    for i in range(80):  # 销售订单 80 条
        d = START + timedelta(days=random.randrange(DAYS))
        mat = random.choice(MATERIALS)
        cust = random.choice(CUSTOMERS)
        qty = random.randrange(10, 500, 10)
        unit_price = round(price[mat[0]] * random.uniform(1.3, 2.0), 2)
        amount = round(qty * unit_price, 2)
        so_rows.append((f"SO-2026-{i + 1:05d}", cust[0], cust[1], mat[0], mat[1],
                        qty, mat[4], unit_price, amount, d.isoformat(),
                        random.choice(["open", "delivered", "closed"])))
    inv_rows = []
    for m in MATERIALS:  # 库存：每物料 2 个仓
        for wh in ("WH01", "WH02"):
            inv_rows.append((m[0], m[1], wh, random.randrange(0, 5000, 10),
                             (END + timedelta(days=1)).isoformat()))
    seed("sap_mock", [
        """CREATE TABLE sap_purchase_orders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            po_number VARCHAR(32) NOT NULL UNIQUE,
            supplier_code VARCHAR(32),
            supplier_name VARCHAR(128),
            material_code VARCHAR(32),
            material_name VARCHAR(128),
            quantity INT,
            unit VARCHAR(16),
            unit_price DECIMAL(12,2),
            amount DECIMAL(14,2),
            order_date DATE,
            status VARCHAR(16),
            KEY idx_date (order_date),
            KEY idx_mat (material_code))""",
        """CREATE TABLE sap_sales_orders (
            id INT AUTO_INCREMENT PRIMARY KEY,
            so_number VARCHAR(32) NOT NULL UNIQUE,
            customer_code VARCHAR(32),
            customer_name VARCHAR(128),
            material_code VARCHAR(32),
            material_name VARCHAR(128),
            quantity INT,
            unit VARCHAR(16),
            unit_price DECIMAL(12,2),
            amount DECIMAL(14,2),
            order_date DATE,
            status VARCHAR(16),
            KEY idx_date (order_date),
            KEY idx_mat (material_code))""",
        """CREATE TABLE sap_inventory (
            id INT AUTO_INCREMENT PRIMARY KEY,
            material_code VARCHAR(32) NOT NULL,
            material_name VARCHAR(128),
            warehouse VARCHAR(16),
            quantity INT,
            updated_at DATE,
            KEY idx_mat (material_code))""",
    ], [
        ("INSERT INTO sap_purchase_orders (po_number, supplier_code, supplier_name, material_code, material_name, quantity, unit, unit_price, amount, order_date, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", po_rows),
        ("INSERT INTO sap_sales_orders (so_number, customer_code, customer_name, material_code, material_name, quantity, unit, unit_price, amount, order_date, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", so_rows),
        ("INSERT INTO sap_inventory (material_code, material_name, warehouse, quantity, updated_at) VALUES (%s,%s,%s,%s,%s)", inv_rows),
    ])

    print("OK: bom_system / mdm_platform / sap_mock 已创建")
    print(f"  mdm_platform: 物料 {len(MATERIALS)} 供应商 {len(SUPPLIERS)} 客户 {len(CUSTOMERS)}")
    print(f"  bom_system: BOM {len(BOMS)} 明细 {len(items_rows)}")
    print(f"  sap_mock: 采购订单 {len(po_rows)} 销售订单 {len(so_rows)} 库存 {len(inv_rows)}")
    print(f"  订单日期范围: {START} ~ {END}")


if __name__ == "__main__":
    main()
