---
name: export-csv
description: 数据导出技能。把查询结果导出为 CSV 文件（utf-8-sig 编码，Excel 友好）。当用户要求导出/下载/拿明细数据时使用。SQL 经数据服务守门后执行，文件写入服务器 exports/ 目录。
---

# 数据导出技能

## export_csv 用法

参数 sql（SELECT 语句，表带库名前缀如 sap_mock.sap_sales_orders）、filename（可选，不含路径，可不含 .csv 后缀，留空自动按表名+时间戳命名）。

返回 {ok, file, row_count, columns}。file 为相对项目根的路径（如 exports/region_sales_20261125_161500.csv），回复中需告知用户文件路径。

## 使用规范

- SQL 守门规则与 free_query 相同（单条 SELECT/白名单/限行/EXPLAIN）
- 导出前先确认查询口径正确（必要时先查询验证再导出）
- 文件名建议用有业务含义的名称（如『各客户销售额汇总』）

## 实现位置

脚本：scripts/export_csv.py（调用数据服务查询 + 本地写 CSV）
