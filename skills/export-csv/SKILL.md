---
name: export-csv
description: 数据导出技能。把查询结果导出为 CSV 文件（utf-8-sig 编码，Excel 友好）。仅当用户明确要求导出/下载/拿明细数据时使用。
---
# 数据导出技能

## 适用场景
- 用户明确要求导出/下载/保存明细数据为文件时使用
- 不适用：普通数据查询（用 data-query 技能）、外部信息检索（用 web-search 技能）
- 注意：用户未明确要求导出时不要加载本技能

## 前置条件
- 数据查询服务 datasvc 已在 127.0.0.1:8005 运行（脚本内部处理）
- 服务器 exports/ 目录可写

## 工作流程
1. 确认查询口径正确（必要时先用 data-query 技能查询验证再导出）
2. 组织 SELECT SQL（表带库名前缀，如 sap_mock.sap_sales_orders）
3. export_csv 执行导出，文件写入服务器 exports/ 目录
4. 回复中告知用户文件路径

## 参数契约
- **export_csv(sql, filename)**：sql 为 SELECT 语句（守门规则与 free_query 相同：单条 SELECT/白名单/限行/EXPLAIN）；filename 可选，不含路径，可不含 .csv 后缀，留空自动按表名+时间戳命名。返回 {ok, file, row_count, columns}，file 为相对项目根的路径（如 exports/region_sales_20261125_161500.csv）

## 决策规则
1. 导出前先确认查询口径正确，避免导出错误数据
2. 文件名建议用有业务含义的名称（如『各客户销售额汇总』）
3. 用户只要看数据不要文件时，用 data-query 查询即可，不导出

## 失败处理
- SQL 被守门拦截：按提示调整 SQL（加过滤条件/加 LIMIT/换白名单内的表）
- 导出失败：检查磁盘空间与目录权限，如实报告错误

## 输出约束
- 回复中必须告知用户文件路径（服务器上的绝对位置）
- 说明导出行数与列名，便于用户核验

## 资源引用
- 脚本：`scripts/export_csv.py`（调用数据服务查询 + 本地写 CSV）
