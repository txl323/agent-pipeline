# -*- coding: utf-8 -*-
"""轻量 spec → 完整 ECharts option 翻译器。

数字全部来自 result.rows，spec 只提供语义 encoding。
返回 ChartSpec：{kind: "echarts", chart_type, title, option, data_ref}，
结构预留 Generative UI 扩展（kind 字段不锁死成只有 ECharts）。
"""

from __future__ import annotations

import hashlib
import json


def _col_idx(columns: list, name: str) -> int:
    return columns.index(name)


def _axis_name(name: str) -> str:
    return name


def _series_data(rows: list, columns: list, x: str, y: str) -> list:
    xi, yi = _col_idx(columns, x), _col_idx(columns, y)
    return [[r[xi], r[yi]] for r in rows]


def _build_single(c: dict, result: dict, grid_idx: int | None) -> dict:
    """构建单张图的 option 片段（series + yAxis/xAxis 配置）。

    grid_idx 非 None 时为多子图模式，series/grid/axis 带 gridIndex/axisIndex。
    """
    enc = c["encoding"]
    mark = c["mark"]
    columns: list = result.get("columns", [])
    rows: list = result.get("rows", [])
    x = enc["x"]
    y = enc["y"]
    y_list = y if isinstance(y, list) else [y]
    y2 = enc.get("y2")
    stack = bool(enc.get("stack")) or mark == "stacked"

    x_data = [r[_col_idx(columns, x)] for r in rows]
    gi = grid_idx

    opt: dict = {"series": [], "xAxis": [], "yAxis": [], "grid": []}

    def _mk_axis(kind: str, name: str, idx: int) -> dict:
        a = {"type": kind, "name": name}
        if gi is not None:
            a["gridIndex"] = gi
        else:
            a["data"] = x_data if kind == "category" else None
            if a["data"] is None:
                a.pop("data")
        return a

    if mark == "pie":
        yf = y_list[0]
        data = [{"name": r[_col_idx(columns, x)], "value": r[_col_idx(columns, yf)]} for r in rows]
        opt["series"].append({"type": "pie", "radius": "60%", "data": data,
                               "label": {"formatter": "{b}: {d}%"}})
        return opt

    if mark == "scatter":
        for yf in y_list:
            opt["series"].append({"type": "scatter", "symbolSize": 10,
                                   "data": _series_data(rows, columns, x, yf),
                                   "name": yf})
        opt["xAxis"].append(_mk_axis("value", x, 0))
        opt["yAxis"].append(_mk_axis("value", y_list[0], 0))
        return opt

    if mark == "dual":
        # 双轴：y 挂左轴，y2 挂右轴
        for yf in y_list:
            if yf == y2:
                continue
            opt["series"].append({"type": "bar", "name": yf,
                                   "xAxisIndex": gi if gi is not None else 0,
                                   "yAxisIndex": gi * 2 if gi is not None else 0,
                                   "data": [r[_col_idx(columns, yf)] for r in rows]})
        opt["series"].append({"type": "line", "name": y2, "smooth": True,
                               "xAxisIndex": gi if gi is not None else 0,
                               "yAxisIndex": gi * 2 + 1 if gi is not None else 1,
                               "data": [r[_col_idx(columns, y2)] for r in rows]})
        opt["xAxis"].append(_mk_axis("category", x, 0))
        opt["yAxis"].append(_mk_axis("value", y_list[0] if y_list else y2, 0))
        opt["yAxis"].append(_mk_axis("value", y2, 1))
        return opt

    # bar / line / stacked
    stype = "line" if mark == "line" else "bar"
    for yf in y_list:
        s = {"type": stype, "name": yf, "smooth": True,
             "xAxisIndex": gi if gi is not None else 0,
             "yAxisIndex": gi if gi is not None else 0,
             "data": [r[_col_idx(columns, yf)] for r in rows]}
        if stack:
            s["stack"] = "total"
        opt["series"].append(s)
    opt["xAxis"].append(_mk_axis("category", x, 0))
    opt["yAxis"].append(_mk_axis("value", " / ".join(y_list), 0))
    return opt


def build_option(spec: dict, result: dict) -> dict:
    """把轻量 spec 翻译成完整 ECharts option。

    单图返回普通 option；多图返回 grid 数组的多子图 option。
    返回 ChartSpec：{kind, chart_type, title, option, data_ref}。
    """
    charts = spec.get("charts", [])
    columns: list = result.get("columns", [])
    rows: list = result.get("rows", [])

    if len(charts) == 1:
        c = charts[0]
        frag = _build_single(c, result, None)
        option = {"tooltip": {"trigger": "axis"}, "legend": {},
                  "xAxis": frag["xAxis"], "yAxis": frag["yAxis"], "series": frag["series"]}
        if c["mark"] == "pie":
            option["xAxis"] = []
            option["yAxis"] = []
            option["tooltip"] = {"trigger": "item", "formatter": "{b}: {c} ({d}%)"}
        chart_type = c["mark"]
    else:
        # 多子图：grid 数组，每图独立坐标轴
        n = len(charts)
        option = {"tooltip": {"trigger": "axis"}, "legend": {},
                  "grid": [], "xAxis": [], "yAxis": [], "series": []}
        for i, c in enumerate(charts):
            option["grid"].append({"left": "7%", "right": "7%",
                                   "top": f"{6 + i * (88 / n):.0f}%",
                                   "height": f"{80 / n:.0f}%"})
            frag = _build_single(c, result, i)
            for a in frag["xAxis"]:
                a["gridIndex"] = i
                option["xAxis"].append(a)
            for j, a in enumerate(frag["yAxis"]):
                a["gridIndex"] = i
                option["yAxis"].append(a)
            option["series"].extend(frag["series"])
        chart_type = "multi"

    # data_ref：结果数据指纹，供落库与回放定位
    data_ref = hashlib.md5(json.dumps([columns, rows[:20]], ensure_ascii=False,
                                      default=str).encode()).hexdigest()[:12]
    return {"kind": "echarts", "chart_type": chart_type,
            "title": " / ".join(c["mark"] for c in charts),
            "option": option, "data_ref": data_ref}
