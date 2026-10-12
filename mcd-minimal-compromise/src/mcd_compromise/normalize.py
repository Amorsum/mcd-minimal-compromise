"""标准化层：把官方 MCP 返回转换为内部结构。

边界（实现路径文档 4.1）：
- 不把内部字段名当作官方字段名；
- 未知选项不视作合法替换；
- 品类与食材标签是内部标准化结果，必须记录映射依据与置信度；
- 未提供的属性是 unknown，不能当成 false 或零。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models import (
    ComboChoice,
    ComboDetail,
    ComboRound,
    Coupon,
    MenuItem,
    ModGroup,
    ModValue,
    NormalizedMenu,
    yuan_to_fen,
)

# ---------------------------------------------------------------------------
# 品类标注规则
# ---------------------------------------------------------------------------
# 每条规则：(品类, 关键词列表, 置信度, 依据模板)。按顺序首个命中生效。
# 品类取值：
#   staple_beef / staple_chicken / staple_fish / staple_other —— 主食
#   drink —— 饮料；fries —— 薯条；fries_like —— 薯条近似（ confidence < high ）
#   snack —— 小食；dessert —— 甜品；unknown —— 无法判定

_TAG_RULES: list[tuple[str, tuple[str, ...], str, str]] = [
    # 先识别薯条（避免被「小食」类规则抢先）
    ("fries", ("薯条",), "high", "名称含「薯条」"),
    ("fries_like", ("扭扭薯", "薯饼", "土豆"), "medium", "名称含薯类近似词，未含「薯条」，按近似品类处理"),
    # 主食
    ("staple_beef", ("牛堡", "牛肉", "巨无霸", "吉士汉堡", "安格斯", "不素之霸"), "high", "名称含牛肉堡关键词"),
    ("staple_chicken", ("鸡腿堡", "麦香鸡", "板烧", "炸鸡", "麦辣鸡", "鸡肉堡"), "high", "名称含鸡肉堡关键词"),
    ("staple_fish", ("鳕鱼", "麦香鱼", "鱼排"), "high", "名称含鱼排关键词"),
    ("staple_other", ("卷",), "medium", "名称含「卷」，归为其他主食"),
    # 饮料
    ("drink", ("可乐", "雪碧", "红茶", "绿茶", "美式", "奶铁", "咖啡", "牛奶", "果汁", "苹果汁", "橙", "柠", "椰", "奶茶", "雪冰", "麦炫酷", "纯悦", "怡泉", "燕麦奶"), "high", "名称含饮料关键词"),
    # 小食与甜品
    ("snack", ("麦乐鸡", "玉米杯", "苹果片", "鸡排", "鸡翅", "V翅", "小食", "拼盘", "翅", "饼干", "沙拉", "鸡球", "香肠", "油条"), "high", "名称含小食关键词"),
    ("dessert", ("新地", "麦旋风", "派", "冰淇淋", "圆筒", "奶冻", "明顿", "布丁"), "high", "名称含甜品关键词"),
]

_STAPLE_TAGS = {"staple_beef", "staple_chicken", "staple_fish", "staple_other"}


def tag_item(name: str) -> tuple[str, str, str]:
    """返回 (category, tag_basis, tag_confidence)。"""
    for category, keywords, confidence, basis in _TAG_RULES:
        for kw in keywords:
            if kw in name:
                return category, f"{basis}：命中「{kw}」", confidence
    return "unknown", "无规则命中", "unknown"


def tag_choice(choice_name: str) -> tuple[str, str, str]:
    """套餐选项同样走品类标注；「大/中/小」规格词不影响品类。"""
    return tag_item(choice_name)


# ---------------------------------------------------------------------------
# 官方返回 → 内部结构
# ---------------------------------------------------------------------------

def _first_str(d: dict, *keys: str, default: str = "") -> str:
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)):
            return str(v)
    return default


def normalize_menu(store_id: str, response: dict, fetched_at: str = "") -> NormalizedMenu:
    """query-meals 返回 → NormalizedMenu（仅单品；套餐由 query-meal-detail 补齐）。

    官方在 categories[].meals[].tags 中标注「套餐」等结构信息，优先于名称推断：
    带「套餐」标签的编码标记为 combo 品类，不进入单品池（组成未知，无法做条件检查），
    只有获取了 query-meal-detail 的套餐才参与候选生成。
    """
    data = (response or {}).get("data") or {}
    menu = NormalizedMenu(store_id=store_id, fetched_at=fetched_at)
    # 先收集官方分类标签
    tag_map: dict[str, tuple[str, ...]] = {}
    for cat in data.get("categories") or []:
        if not isinstance(cat, dict):
            continue
        for m in cat.get("meals") or []:
            if isinstance(m, dict) and m.get("code"):
                tag_map[str(m["code"])] = tuple(t for t in (m.get("tags") or []) if isinstance(t, str))
    meals = data.get("meals") or {}
    for code, m in meals.items():
        if not isinstance(m, dict):
            continue
        name = _first_str(m, "name", default=code)
        tags = tag_map.get(code, ())
        if "套餐" in tags:
            category, basis, confidence = "combo", "官方菜单标签含「套餐」，按套餐处理（组成以 query-meal-detail 为准）", "high"
        else:
            category, basis, confidence = tag_item(name)
        try:
            current = yuan_to_fen(m.get("currentPrice", "0"))
        except ValueError:
            current = 0
        try:
            original = yuan_to_fen(m.get("originalPrice", "0"))
        except ValueError:
            original = current
        menu.items[code] = MenuItem(
            code=code,
            name=name,
            current_price_fen=current,
            original_price_fen=original,
            category=category,
            tag_basis=basis,
            tag_confidence=confidence,
            tags=tags,
            is_combo=category == "combo",
        )
    return menu


def _parse_mod_groups(modification: Optional[dict]) -> tuple[ModGroup, ...]:
    if not isinstance(modification, dict):
        return ()
    groups: list[ModGroup] = []
    for item in modification.get("items") or []:
        if not isinstance(item, dict):
            continue
        values: list[ModValue] = []
        for v in item.get("values") or []:
            if not isinstance(v, dict):
                continue
            try:
                price = yuan_to_fen(v.get("price", 0))
            except ValueError:
                price = 0
            values.append(
                ModValue(
                    code=str(v.get("code", "")),
                    name=str(v.get("name", "")),
                    price_fen=price,
                    selected_key=str(v.get("selectedKey", "")),
                    unselected_key=str(v.get("unselectedKey", "") or ""),
                    selected_quantity=int(v.get("selectedQuantity", 0) or 0),
                )
            )
        if values:
            groups.append(
                ModGroup(
                    max_values=int(item.get("maxValues", 1) or 1),
                    min_values=int(item.get("minValues", 0) or 0),
                    values=tuple(values),
                )
            )
    return tuple(groups)


def normalize_meal_detail(store_id: str, response: dict) -> Optional[ComboDetail]:
    """query-meal-detail 返回 → ComboDetail。单品详情（无 rounds）返回 None。"""
    data = (response or {}).get("data") or {}
    if not isinstance(data, dict) or not data.get("code"):
        return None
    rounds_data = data.get("rounds") or []
    if not rounds_data:
        return None  # 单品，不是套餐
    code = str(data["code"])
    name = str(data.get("name") or code)
    rounds: list[ComboRound] = []
    for r in rounds_data:
        if not isinstance(r, dict):
            continue
        choices: list[ComboChoice] = []
        for c in r.get("choices") or []:
            if not isinstance(c, dict):
                continue
            choice_name = str(c.get("name") or c.get("code") or "")
            category, basis, confidence = tag_choice(choice_name)
            diff_raw = c.get("diffPrice", "+ ¥0")
            try:
                diff = yuan_to_fen(diff_raw)
            except ValueError:
                diff = 0
            choices.append(
                ComboChoice(
                    code=str(c.get("code", "")),
                    name=choice_name,
                    diff_price_fen=diff,
                    is_default=bool(int(c.get("isDefault", 0) or 0)),
                    support_modify=bool(c.get("supportModify", False)),
                    category=category,
                    tag_basis=basis,
                    tag_confidence=confidence,
                    modifications=_parse_mod_groups(c.get("modification")),
                )
            )
        rounds.append(
            ComboRound(
                id=str(r.get("id", "")),
                name=str(r.get("name") or ""),
                choices=tuple(choices),
                min_quantity=int(r.get("minQuantity", 1) or 1),
                max_quantity=int(r.get("maxQuantity", 1) or 1),
            )
        )
    if not rounds:
        return None

    # 套餐主食品类由第一轮默认选项推导
    staple_tag, staple_basis = "unknown", "第一轮无默认选项"
    first_round = rounds[0]
    default_choice = next((c for c in first_round.choices if c.is_default), None)
    if default_choice is not None:
        staple_tag = default_choice.category
        staple_basis = f"由第一轮「{first_round.name}」默认选项「{default_choice.name}」推导：{default_choice.tag_basis}"

    return ComboDetail(
        code=code,
        name=name,
        base_price_fen=0,  # 套餐基础价来自菜单 query-meals 的 currentPrice
        rounds=tuple(rounds),
        staple_tag=staple_tag,
        staple_tag_basis=staple_basis,
    )


def normalize_coupons(store_id: str, response: dict) -> list[Coupon]:
    """query-store-coupons 返回 → Coupon 列表（按券 ID 分组）。"""
    data = (response or {}).get("data") or []
    grouped: dict[str, Coupon] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("couponId", ""))
        if not cid:
            continue
        products = entry.get("products") or []
        codes = tuple(str(p.get("productCode", "")) for p in products if isinstance(p, dict))
        trade = str(entry.get("tradeDateTime", ""))
        start, _, end = trade.partition("-")
        if cid not in grouped:
            grouped[cid] = Coupon(
                coupon_id=cid,
                title=str(entry.get("title", "")),
                product_codes=codes,
                trade_start=start.strip(),
                trade_end=end.strip(),
                coupon_code=str(entry.get("couponCode", "")),
            )
    return list(grouped.values())


def attach_combo_base_prices(menu: NormalizedMenu) -> None:
    """把 query-meals 中的套餐价格补到 ComboDetail.base_price_fen。"""
    for code, combo in menu.combos.items():
        item = menu.items.get(code)
        if item is not None:
            menu.combos[code] = ComboDetail(
                code=combo.code,
                name=combo.name,
                base_price_fen=item.current_price_fen,
                rounds=combo.rounds,
                staple_tag=combo.staple_tag,
                staple_tag_basis=combo.staple_tag_basis,
            )


def build_normalized_menu(
    store_id: str,
    menu_response: dict,
    detail_responses: Iterable[dict],
    coupons_response: Optional[dict] = None,
    fetched_at: str = "",
) -> NormalizedMenu:
    """组合入口：菜单 + 若干套餐详情 + 门店优惠 → NormalizedMenu。"""
    menu = normalize_menu(store_id, menu_response, fetched_at=fetched_at)
    for resp in detail_responses:
        combo = normalize_meal_detail(store_id, resp)
        if combo is None:
            continue
        menu.combos[combo.code] = combo
        # 套餐本身也作为 item 存在（来自菜单）；确保编码一致
    if coupons_response is not None:
        menu.coupons = normalize_coupons(store_id, coupons_response)
        # 券商品是独立编码，补成 items（价格以试算为准，这里先记 0 并标注）
        for coupon in menu.coupons:
            for pcode in coupon.product_codes:
                if pcode and pcode not in menu.items:
                    category, basis, confidence = tag_item(coupon.title)
                    menu.items[pcode] = MenuItem(
                        code=pcode,
                        name=coupon.title,
                        current_price_fen=0,
                        original_price_fen=0,
                        category=category,
                        tag_basis=f"来自门店优惠「{coupon.title}」适用商品：{basis}",
                        tag_confidence=confidence,
                        tags=("券商品",),
                        is_combo=False,
                    )
    attach_combo_base_prices(menu)
    return menu
