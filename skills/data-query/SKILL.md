---
name: data-query
description: 业务数据查询技能。查询销售订单/采购订单/库存数据（指标汇总、分组对比、结构探索、表统计）。当用户询问销售额/订单数/采购额/库存量等业务指标，或问『有什么数据/什么表/数据范围』时使用。数据范围 2026-10-24 ~ 2026-11-23。
---

# 数据查询技能

通过数据查询服务（datasvc，HTTP API）访问业务库。SQL 编译与守门是服务内部实现细节，本技能只发 HTTP 请求。

## 能力清单

| 能力 | 说明 | 适用场景 |
|---|---|---|
| query_data | Plan JSON 语义层查询（口径有保障） | 指标+维度+过滤在语义层内，优先使用 |
| free_query | 受控 SELECT SQL（五重守门） | 语义层表达不了的长尾问题 |
| explore_schema | 库表结构探索 | 『有什么数据/什么表/什么字段』 |
| table_stats | 表统计速查（行数/时间范围/维度基数） | 『数据范围/多少条/有哪些维度值』 |

## query_data 用法

参数 plan_json（Plan JSON 字符串）：
- metric：sales_amount(销售额)/sales_orders(订单数)/sales_qty(销售数量)/purchase_amount(采购额)/purchase_orders(采购订单数)/inventory_qty(库存量)
- dimensions：customer_region(客户区域)/material(物料)/material_code(物料编码)/customer(客户)/order_date(日期)/supplier(供应商)/supplier_region(供应商区域)/warehouse(仓库)
- filters：[{field, op, value}]，可用字段：sales 库 customer_region/material/material_code/customer/status；purchase 库 supplier/supplier_region/material/status；inventory 库 warehouse/material
- time_macro：last_month/this_month/last_7_days/last_30_days/all
- draw：是否生成图表（分布/趋势/占比/对比 true；结构/单值/清单 false）
- chart_type：bar/pie/line/auto

示例：`{"metric": "sales_amount", "dimensions": ["customer_region"], "time_macro": "all"}`

## free_query 用法

参数 sql：SELECT 语句，表必须带库名前缀（如 sap_mock.sap_sales_orders）。守门规则：单条 SELECT/禁多语句/禁危险关键字/表白名单/强制 LIMIT+EXPLAIN。被拦截时按提示调整（加过滤/加 LIMIT/换白名单表）。

## explore_schema 用法

参数 database（bom_system/mdm_platform/sap_mock，空返回全部表清单）、table（空返回该库全部表字段）。

## table_stats 用法

参数 database、table。返回行数、时间范围（自动识别日期列）、低基数字段去重数。

## 实现位置

脚本：scripts/data_query.py（HTTP 客户端，调用 http://127.0.0.1:8005）
