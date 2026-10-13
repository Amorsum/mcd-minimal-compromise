"""营养增强（第二阶段）：餐品 → 营养条目映射与组合汇总。

规则（实现路径文档 8）：
- 优先使用稳定标识；没有共同标识时，采用人工审核的名称与规格映射表；
- 仅名称模糊相似的结果不能直接进入硬约束计算；
- 套餐营养按实际组成汇总，避免套餐条目与单品重复累加；
- 缺失任何关键组成营养时显示「营养数据不完整」，不把缺失值当零；
- 修改酱料等特制缺少独立营养数据时，不推算精确减量。
"""

from __future__ import annotations

from typing import Optional

from .models import ExpandedContent


class NutritionMap:
    """名称 → 营养条目。仅精确名称匹配进入硬约束计算；模糊相似不进入。

    匹配来源（按优先级，均为官方数据的精确匹配，不做任何推断）：
    1. 套餐选项名 / 菜单单品名（query-meal-detail / query-meals）；
    2. 官方报价商品名（calculate-price 的 productName，带规格后缀，
       如「雪碧小杯」）——真实联调发现菜单单品名常缺规格，
       而报价名与营养表精确一致（见 MCP_INTEGRATION.md 4.13）。
    """

    def __init__(self) -> None:
        self._by_name: dict[str, dict] = {}
        self._by_code: dict[str, dict] = {}

    def add_entry(self, name: str, entry: dict, code: str = "", confidence: str = "medium") -> None:
        rec = dict(entry)
        rec["name"] = name
        rec["confidence"] = confidence
        self._by_name[name] = rec
        if code:
            self._by_code[code] = rec

    @classmethod
    def from_mcp_response(cls, response: dict) -> "NutritionMap":
        """list-nutrition-foods 返回 → NutritionMap。

        官方返回为一段 CSV 风格文本（data 字段），列为：
        productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium
        按名称精确匹配建立映射，confidence=medium（名称匹配，无共同编码）。
        """
        nm = cls()
        data = (response or {}).get("data")
        if not isinstance(data, str):
            return nm
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        for ln in lines:
            if ln.startswith("[") or "{" in ln:
                continue  # 头部声明行
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 9:
                continue
            name = parts[0]

            def _num(x: str) -> Optional[float]:
                try:
                    return float(x)
                except ValueError:
                    return None

            entry = {
                "energy_kj": _num(parts[2]),
                "energy_kcal": _num(parts[3]),
                "protein": _num(parts[4]),
                "fat": _num(parts[5]),
                "carbohydrate": _num(parts[6]),
                "sodium": _num(parts[7]),
                "calcium": _num(parts[8]),
            }
            nm.add_entry(name, entry, confidence="medium")
        return nm

    def lookup(self, name: str, code: str = "") -> Optional[dict]:
        if code and code in self._by_code:
            return self._by_code[code]
        return self._by_name.get(name)

    def totals_for(self, expanded: ExpandedContent, name_overrides: Optional[dict[str, str]] = None) -> Optional[dict]:
        """按展开组成汇总。任一组成缺失营养 → 返回 None（不把缺失当零）。

        name_overrides: 商品编码 → 官方报价商品名。条目按自身名称匹配失败时，
        才用该编码的官方报价名再做一次精确匹配（见类文档字符串的来源 2）。
        """
        totals = {
            "energy_kj": 0.0,
            "energy_kcal": 0.0,
            "protein": 0.0,
            "fat": 0.0,
            "carbohydrate": 0.0,
            "sodium": 0.0,
            "calcium": 0.0,
        }
        for e in expanded.entries:
            rec = self.lookup(e["name"], e["code"])
            if rec is None and name_overrides:
                official = name_overrides.get(e["code"], "")
                if official:
                    rec = self.lookup(official)
            if rec is None:
                return None
            for k in totals:
                v = rec.get(k)
                if v is None:
                    return None
                totals[k] += v * int(e.get("quantity", 1))
        return totals
