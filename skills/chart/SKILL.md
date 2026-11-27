---
name: chart
description: 图表生成技能。输入查询结果引用（artifact_ref）+ 自然语言图表诉求，输出轻量 spec（mark + encoding），由代码校验并生成完整 ECharts option。需出图时使用。
---
# 图表生成技能

输入：`artifact_ref`（查询结果引用）+ 自然语言图表诉求。
输出：轻量 spec（JSON），由代码校验并翻译成完整 ECharts option。

## 核心原则
- **模型只出轻量 spec，绝不出完整 ECharts option、绝不出数字**
- spec 的 encoding 字段名**必须是结果集里真实存在的列**（由校验环节比对 result.columns，不存在则拒绝重出）
- 数字全部来自 result.rows，spec 中不包含任何数据值

## 轻量 spec 格式

```json
{
  "charts": [
    {"mark": "line", "encoding": {"x": "order_date", "y": ["sales_amount", "order_qty"]}},
    {"mark": "bar", "encoding": {"x": "customer", "y": "sales_amount", "stack": true}}
  ]
}
```

### mark 六选一
| mark | 适用场景 | encoding 要求 |
|------|----------|---------------|
| `bar` | 分类对比 | x: 分类列, y: 数值列（或数组） |
| `pie` | 占比/构成 | x: 分类列, y: 数值列（单值） |
| `line` | 趋势/时序 | x: 时间列, y: 数值列（或数组） |
| `scatter` | 相关性/分布 | x: 数值列, y: 数值列 |
| `dual` | 量纲不同的双指标 | x: 分类/时间列, y: 数值列, y2: 数值列（挂右轴） |
| `stacked` | 构成/堆积 | x: 分类列, y: 数值列数组, stack: true |

### encoding 字段
- `x`: x 轴字段名（必须存在于 result.columns）
- `y`: y 轴字段名或字段名数组（必须存在于 result.columns）
- `y2`: 右轴字段名（仅 dual 用，必须存在于 result.columns）
- `stack`: 布尔，是否堆积（仅 stacked 用）

### 多图
`charts` 数组多个元素 = 多张图。一次查询可按指标/维度拆多张，多次查询各一张。

## 选用规则
- 趋势 → line
- 分类对比 → bar
- 占比 → pie
- 相关性 → scatter
- 量纲不同的双指标（如销售额+数量）→ dual
- 构成/堆积 → stacked

## 约束
- 字段名必须来自结果集真实列名，不可编造
- 非法 spec 会被校验拒绝并要求重出，仍非法走兜底（默认 bar 图）
- ChartSpec 结构为 {kind: "echarts", chart_type, title, option, data_ref}，预留 Generative UI 扩展
