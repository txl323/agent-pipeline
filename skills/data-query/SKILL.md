---
name: data-query
description: 业务数据查询技能。查询销售订单/采购订单/库存数据（指标汇总、分组对比、结构探索、表统计）。当用户询问销售额/订单数/采购额/库存量等业务指标，或问『有什么数据/什么表/数据范围』时使用。数据范围 2026-10-24 ~ 2026-11-23。
---
# 数据查询技能

通过数据查询服务（datasvc，HTTP API http://127.0.0.1:8005）访问业务库。SQL 编译与守门是服务内部实现细节，本技能只发 HTTP 请求。

## 适用场景
- 业务指标问题：销售额/订单数/销售数量/采购额/采购订单数/库存量（单值、分组、对比、趋势）
- 结构探索：『有什么数据/什么表/什么字段/数据范围多少条』
- 表统计速查：行数/时间范围/维度基数
- 不适用：业务库之外的外部信息（用 web-search 技能）、导出文件（用 export-csv 技能）

## 前置条件
- 数据查询服务 datasvc 已在 127.0.0.1:8005 运行（脚本内部处理，无需检查）
- 业务三库只读：bom_system / mdm_platform / sap_mock
- 数据时间范围 2026-10-24 ~ 2026-11-23

## 工作流程
1. 判断问题类型：结构类 → explore_schema/table_stats；指标类 → query_data（优先）或 free_query
2. 一次规划好需要的全部查询再执行，不要查一步看一步
3. 同一会话已查到的数据直接复用，严禁对同一数据反复查询
4. 优先用一次查询拿到全部所需数据（如带占比的分组查询），避免先查总量再逐个查明细

## 参数契约
完整契约（合法指标/维度/过滤字段/时间宏/物理列名映射）见 `references/plan_schema.md`（由语义层生成或手工同步）。要点：

- **query_data(plan_json, draw, chart_type)**：Plan JSON 语义层查询，口径有保障，优先使用
  - metric：sales_amount/sales_orders/sales_qty/purchase_amount/purchase_orders/inventory_qty
  - dimensions：customer_region/material/material_code/customer/order_date/order_month/supplier/supplier_region/warehouse（order_month 为月粒度，按月趋势优先用它）
  - filters：[{field, op, value}]，可用字段见 plan_schema.md
  - time_macro：last_month/this_month/last_7_days/last_30_days/all
  - draw：分布/趋势/占比/对比类多行数据 true；结构/单值/清单 false
  - chart_type：bar/pie/line/auto
- **free_query(sql)**：受控 SELECT，表必须带库名前缀（如 sap_mock.sap_sales_orders）。注意：material 是语义层合法维度，物理列名是 material_name
- **explore_schema(database, table)**：库表结构探索，database 限 bom_system/mdm_platform/sap_mock
- **table_stats(database, table)**：行数/时间范围（自动识别日期列）/低基数字段去重数

## 决策规则
1. 语义层能表达（指标+维度+过滤都在 plan_schema.md 内）→ query_data（口径有保障，优先）
2. 同环比问题（环比/同比/比上月/比上月增长多少）：拆成两次 query_data（time_macro 分别取 this_month 与 last_month，其余参数完全一致），由你计算增幅 =（本期-上期）/上期×100% 并在回复中给出两期数值与增幅；注意数据实际范围（2026-10-24 起），不足整月时须说明口径（如『10 月仅含 24-31 日 8 天』），不得隐瞒口径差异
3. 语义层表达不了（主数据统计/单价走势/占比计算/跨表计算）→ free_query 写 SELECT
4. 不确定数据结构 → 先 explore_schema 再回来查询
5. 用户问法有歧义（没说清指标/时间范围/对象）→ 先用选项式反问澄清，不要猜测

## 失败处理
- query_data 返回 ok=false：根据 error 提示修正后重试（换指标名/维度名/操作符大写）
- free_query 被守门拦截：按提示调整 SQL（加过滤条件/加 LIMIT/换白名单内的表）
- 工具失败时换写法再试，不要原样重试

## 输出约束
- 所有数字必须来自工具返回结果，严禁编造
- 查询结果已自动图表化展示给用户，回复默认只写 3-5 句关键结论与解读，不重复输出数据明细
- 对比类问题主动覆盖隐含关注点（谁最高谁最低/差距多大/是否异常）
- 结果只有一行时系统不生成图表，须向用户解释原因并建议可画图的替代问法

## 资源引用
- `references/plan_schema.md`：合法指标/维度/过滤字段/时间宏/物理列名映射（参数契约唯一真相）
- 脚本：`scripts/data_query.py`（HTTP 客户端，调用 http://127.0.0.1:8005）
