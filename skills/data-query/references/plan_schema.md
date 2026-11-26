# Plan JSON 参数契约（唯一真相）

> 由语义层 `semantic/semantic_v1.yaml`（version 1.0.0）同步生成。修改语义层后须重新同步本文件。

## 指标（metric）

| 指标 | 名称 | 别名 | 数据源 | SQL 表达式 |
|---|---|---|---|---|
| sales_amount | 销售额 | 销售金额/营收/金额/GMV | sales | SUM(so.amount) |
| sales_orders | 订单数 | 销售订单数/单量/订单量 | sales | COUNT(*) |
| sales_qty | 销售数量 | 销量/数量 | sales | SUM(so.quantity) |
| purchase_amount | 采购额 | 采购金额/采购总额 | purchase | SUM(po.amount) |
| purchase_orders | 采购订单数 | 采购单量 | purchase | COUNT(*) |
| inventory_qty | 库存量 | 库存/现有库存 | inventory | SUM(inv.quantity) |

## 维度（dimensions）

| 维度 | 名称 | 物理列名 | 适用数据源 | JOIN |
|---|---|---|---|---|
| customer_region | 客户区域 | c.region | sales | JOIN mdm_platform.md_customers c ON so.customer_code = c.customer_code |
| material | 物料 | so.material_name / po.material_name / inv.material_name | sales/purchase/inventory | （无） |
| material_code | 物料编码 | so.material_code / po.material_code / inv.material_code | sales/purchase/inventory | （无） |
| customer | 客户 | so.customer_name | sales | （无） |
| order_date | 日期 | so.order_date | sales | （无，时间维度） |
| order_month | 月份 | CONCAT(YEAR(so.order_date), '-', LPAD(MONTH(so.order_date), 2, '0')) | sales | （无，时间维度，月粒度趋势） |
| supplier | 供应商 | po.supplier_name | purchase | （无） |
| supplier_region | 供应商区域 | s.region | purchase | JOIN mdm_platform.md_suppliers s ON po.supplier_code = s.supplier_code |
| warehouse | 仓库 | inv.warehouse | inventory | （无） |

**注意**：`material` 是语义层合法维度，物理列名是 `material_name`（free_query 写 SQL 时必须用物理列名）。

## 过滤字段（filters）白名单

### sales 库
| 字段 | 物理列 | 允许操作符 |
|---|---|---|
| customer_region | c.region | = / IN |
| material | so.material_name | = / IN / LIKE |
| material_code | so.material_code | = / IN |
| customer | so.customer_name | = / IN / LIKE |
| status | so.status | = / IN |

### purchase 库
| 字段 | 物理列 | 允许操作符 |
|---|---|---|
| supplier | po.supplier_name | = / IN / LIKE |
| supplier_region | s.region | = / IN |
| material | po.material_name | = / IN / LIKE |
| status | po.status | = / IN |

### inventory 库
| 字段 | 物理列 | 允许操作符 |
|---|---|---|
| warehouse | inv.warehouse | = / IN |
| material | inv.material_name | = / IN / LIKE |

## 时间宏（time_macro）

| 宏 | 含义 |
|---|---|
| last_month | 上一个自然月 |
| this_month | 本自然月 |
| last_7_days | 最近 7 天 |
| last_30_days | 最近 30 天 |
| all | 全部数据（默认） |

## Plan JSON 示例

```json
{"metric": "sales_amount", "dimensions": ["customer_region"], "filters": [{"field": "customer_region", "op": "=", "value": "华南"}], "time_macro": "all"}
```

## 数据范围

- 业务三库只读：bom_system / mdm_platform / sap_mock
- 数据时间范围：2026-10-24 ~ 2026-11-23
- 事实表：sap_mock.sap_sales_orders / sap_mock.sap_purchase_orders / sap_mock.sap_inventory
