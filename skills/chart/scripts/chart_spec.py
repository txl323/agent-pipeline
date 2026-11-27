# -*- coding: utf-8 -*-
"""轻量图表 spec 校验：JSON Schema 结构校验 + 字段存在性校验。

模型产出的 spec 必须同时通过两层校验才可进入渲染：
1. 结构校验：mark 枚举、encoding 必填字段、字段类型；
2. 字段存在性：encoding 中引用的字段名必须存在于结果集 columns。
"""

from __future__ import annotations

# 轻量 spec 的 JSON Schema（不引入第三方依赖，手工校验）
VALID_MARKS = {"bar", "pie", "line", "scatter", "dual", "stacked"}


def _check_encoding(enc: dict, columns: list, idx: int) -> str | None:
    """校验单个 chart 的 encoding，返回错误信息或 None。"""
    if not isinstance(enc, dict):
        return f"charts[{idx}].encoding 必须是对象"
    cols = set(columns)
    x = enc.get("x")
    if not x or not isinstance(x, str):
        return f"charts[{idx}].encoding.x 必填且为字符串"
    if x not in cols:
        return f"charts[{idx}].encoding.x 字段『{x}』不存在于结果列 {sorted(cols)}"
    y = enc.get("y")
    if y is None or y == "" or y == []:
        return f"charts[{idx}].encoding.y 必填"
    y_list = y if isinstance(y, list) else [y]
    if not all(isinstance(v, str) for v in y_list):
        return f"charts[{idx}].encoding.y 必须是字符串或字符串数组"
    for f in y_list:
        if f not in cols:
            return f"charts[{idx}].encoding.y 字段『{f}』不存在于结果列 {sorted(cols)}"
    y2 = enc.get("y2")
    if y2 is not None:
        if not isinstance(y2, str):
            return f"charts[{idx}].encoding.y2 必须是字符串"
        if y2 not in cols:
            return f"charts[{idx}].encoding.y2 字段『{y2}』不存在于结果列 {sorted(cols)}"
    stack = enc.get("stack")
    if stack is not None and not isinstance(stack, bool):
        return f"charts[{idx}].encoding.stack 必须是布尔值"
    return None


def validate_spec(spec: dict, columns: list) -> tuple[bool, str]:
    """校验轻量 spec：结构 + 字段存在性。返回 (是否合法, 错误原因)。"""
    try:
        if not isinstance(spec, dict):
            return False, "spec 必须是 JSON 对象"
        charts = spec.get("charts")
        if not isinstance(charts, list) or not charts:
            return False, "spec.charts 必须是非空数组"
        if len(charts) > 6:
            return False, "单次最多 6 张图"
        for i, c in enumerate(charts):
            if not isinstance(c, dict):
                return False, f"charts[{i}] 必须是对象"
            mark = c.get("mark")
            if mark not in VALID_MARKS:
                return False, f"charts[{i}].mark 必须是 {sorted(VALID_MARKS)} 之一，实际『{mark}』"
            enc = c.get("encoding")
            err = _check_encoding(enc, columns, i)
            if err:
                return False, err
            # mark 专属约束
            if mark == "dual" and not enc.get("y2"):
                return False, f"charts[{i}] mark=dual 时 encoding.y2 必填（右轴字段）"
            if mark == "pie" and isinstance(enc.get("y"), list) and len(enc["y"]) > 1:
                return False, f"charts[{i}] mark=pie 时 encoding.y 只能是单个字段"
        return True, ""
    except Exception as e:  # noqa: BLE001
        return False, f"spec 校验异常: {e}"
