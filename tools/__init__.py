"""工具层：实现分散在 webtools/dbtools，本模块统一注册 TOOLS 与 TOOL_SCHEMAS。"""

from __future__ import annotations

from typing import Callable

from .dbtools import export_csv, explore_schema, free_query, query_data, table_stats
from .webtools import fetch_url, web_search

TOOLS: dict[str, Callable] = {"web_search": web_search, "fetch_url": fetch_url, "query_data": query_data,
                              "explore_schema": explore_schema, "free_query": free_query,
                              "table_stats": table_stats, "export_csv": export_csv}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网公开信息，返回标题、链接和摘要。用于获取新闻、资料、事实等外部信息。",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_url",
            "description": "抓取指定 URL 的网页正文文本。用于深入阅读搜索结果中的某个页面。",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的网页地址"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_data",
            "description": "查询业务数据库（销售订单/采购订单/库存，数据范围 2026-10-24 ~ 2026-11-23）。输入结构化 Plan JSON（不是 SQL），由系统校验后安全执行。可用指标：sales_amount(销售额)/sales_orders(订单数)/sales_qty(销售数量)/purchase_amount(采购额)/purchase_orders(采购订单数)/inventory_qty(库存量)。可用维度：customer_region(客户区域)/material(物料)/material_code(物料编码)/customer(客户)/order_date(日期)/supplier(供应商)/supplier_region(供应商区域)/warehouse(仓库)。可用过滤字段：sales 库 customer_region/material/material_code/customer/status；purchase 库 supplier/supplier_region/material/status；inventory 库 warehouse/material。可用时间宏：last_month/this_month/last_7_days/last_30_days/all。",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_json": {
                        "type": "string",
                        "description": "Plan JSON 字符串，格式：{\"metric\": \"sales_amount\", \"dimensions\": [\"customer_region\"], \"filters\": [{\"field\": \"customer_region\", \"op\": \"=\", \"value\": \"华南\"}], \"time_macro\": \"last_month\"}"
                    },
                    "draw": {
                        "type": "boolean",
                        "description": "是否生成图表。用户要看数据分布/趋势/占比/对比时为 true（默认）；查结构/字段/单值/清单类问题传 false"
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "pie", "line", "auto"],
                        "description": "图表类型。用户显式要求柱状图传 bar、饼图传 pie、折线图传 line；用户未指定图型时传 auto（系统自动判断）"
                    }
                },
                "required": ["plan_json"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "explore_schema",
            "description": "探索数据结构：查看业务库有哪些表、字段、类型、行数。当用户问『有什么数据/什么表/什么字段』或需要了解数据结构时使用。database 可选：bom_system/mdm_platform/sap_mock；不传返回全部表清单，传 database 返回该库各表字段，再传 table 返回单表字段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "库名：bom_system/mdm_platform/sap_mock，空则返回全部表清单"},
                    "table": {"type": "string", "description": "表名，空则返回该库全部表的字段"}
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "free_query",
            "description": "受控自由查询（长尾兜底）：当问题涉及的指标/维度不在 query_data 语义层中时使用。直接写 SELECT SQL（表必须带库名前缀，如 sap_mock.sap_sales_orders），系统守门校验（只读/白名单/限行/EXPLAIN）后执行。优先用 query_data，仅当语义层无法表达时才用本工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SELECT 查询语句，表用 库名.表名 全限定，如 SELECT category, COUNT(*) AS cnt FROM mdm_platform.md_materials GROUP BY category"},
                    "params_json": {"type": "string", "description": "参数化查询的参数 JSON 数组，与 SQL 中 %s 占位符对应，无参数传 []"},
                    "draw": {
                        "type": "boolean",
                        "description": "是否生成图表。用户要看数据分布/趋势/占比/对比时为 true（默认）；查结构/字段/单值/清单类问题传 false"
                    },
                    "chart_type": {
                        "type": "string",
                        "enum": ["bar", "pie", "line", "auto"],
                        "description": "图表类型。用户显式要求柱状图传 bar、饼图传 pie、折线图传 line；用户未指定图型时传 auto（系统自动判断）"
                    }
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "table_stats",
            "description": "表统计速查：一次调用返回指定表的行数、时间范围（自动识别日期列的 min/max）、低基数字段去重数（维度基数）。用于快速判断数据口径（如数据覆盖哪些日期、区域有几个值），比 explore_schema 更适合回答『数据范围/有多少条/有哪些维度值』类问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "database": {"type": "string", "description": "库名：bom_system/mdm_platform/sap_mock"},
                    "table": {"type": "string", "description": "表名"}
                },
                "required": ["database", "table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "export_csv",
            "description": "导出查询结果为 CSV 文件：SELECT SQL 经与 free_query 相同的守门校验（只读/白名单/限行/EXPLAIN）后执行，结果写入服务器 exports/ 目录的 CSV 文件。用户要求导出/下载/拿明细数据时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "SELECT 查询语句，表用 库名.表名 全限定，如 SELECT * FROM sap_mock.sap_sales_orders LIMIT 1000"},
                    "filename": {"type": "string", "description": "导出文件名（不含路径，可不含 .csv 后缀），留空则自动按表名+时间戳命名"}
                },
                "required": ["sql"],
            },
        },
    },
]
