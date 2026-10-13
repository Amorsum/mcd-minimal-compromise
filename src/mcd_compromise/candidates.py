"""候选生成器：在明确声明的搜索边界内枚举候选。

搜索边界（实现路径文档 6.1，首版）：
- 一个选定门店、当前自取场景、一人用餐；
- 一份主食、零或一份饮料、零或一份允许的小食，以及满足此结构的套餐与合法替换；
- 排除：多份重复商品、跨订单拆单、未知替换选项、超出结构的大型分享餐。

候选优先级只用于安排试算顺序与截断，不用于剪除（没有可靠价格下界时，
不能按标价剪除「看起来超预算」的候选 —— 优惠可能改变结果）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from .models import (
    Candidate,
    ComboChoice,
    ComboDetail,
    Constraint,
    ConstraintKind,
    MealRequest,
    MenuItem,
    Negotiability,
    NormalizedMenu,
    OrderItem,
    RoundSelection,
    Strength,
)

# 生成规模上限（项目配置，非官方承诺）
MAX_STAPLES = 8
MAX_DRINKS = 6
MAX_SNACKS = 6
MAX_COMBOS = 8
MAX_VARIANTS_PER_COMBO = 8
MAX_SWAPS_PER_ROUND = 3
DEFAULT_MAX_CANDIDATES = 64

SEARCH_SCOPE_TEMPLATE = {
    "store": "single",
    "diningMode": "pickup",
    "people": 1,
    "maxStaples": 1,
    "maxDrinks": 1,
    "maxSnacks": 1,
    "comboReplacements": "legal-choices-only",
    "couponVariants": "store-coupon-products",
    "excluded": ["duplicate-items", "split-orders", "unknown-replacements", "large-sharing-meals"],
}


@dataclass
class _Filter:
    excluded_categories: set[str] = field(default_factory=set)
    excluded_items: set[str] = field(default_factory=set)
    excluded_mod_codes: set[str] = field(default_factory=set)
    required_staple: Optional[tuple[str, str]] = None  # ("tag"|"code", value)
    required_categories: set[str] = field(default_factory=set)
    max_items: Optional[int] = None
    prefer_coupon: bool = False


def _build_filter(menu: NormalizedMenu, request: MealRequest) -> _Filter:
    f = _Filter()
    known_codes = set(menu.items) | set(menu.combos)
    for c in request.constraints:
        if c.strength == Strength.SOFT:
            if c.kind == ConstraintKind.PREFER_COUPON:
                f.prefer_coupon = True
            continue
        if c.kind == ConstraintKind.EXCLUDE_CATEGORY:
            f.excluded_categories.add(str(c.value))
        elif c.kind == ConstraintKind.EXCLUDE_ITEM:
            v = str(c.value)
            if v in known_codes:
                f.excluded_items.add(v)
            else:
                # 不是已知商品编码 → 按特制/配料编码处理（如「不要酸黄瓜」）
                f.excluded_mod_codes.add(v)
        elif c.kind == ConstraintKind.INCLUDE_STAPLE:
            v = str(c.value)
            if v.startswith("staple_"):
                f.required_staple = ("tag", v)
            else:
                f.required_staple = ("code", v)
        elif c.kind == ConstraintKind.REQUIRE_CATEGORY:
            f.required_categories.add(str(c.value))
        elif c.kind == ConstraintKind.MAX_ITEMS:
            f.max_items = int(c.value)
    return f


def _item_ok(mi: MenuItem, f: _Filter) -> bool:
    if "券商品" in mi.tags:
        return False  # 券商品只能经优惠变体生成（必须携带券引用；不带券官方返回空清单）
    if mi.category in f.excluded_categories:
        return False
    if mi.code in f.excluded_items:
        return False
    return True


def _coupon_item_ok(mi: MenuItem, f: _Filter) -> bool:
    """优惠变体路径的过滤：券商品在此路径合法，只按品类/编码排除。"""
    if mi.category in f.excluded_categories:
        return False
    if mi.code in f.excluded_items:
        return False
    return True


def _filtered_staples(menu: NormalizedMenu, f: _Filter) -> list[MenuItem]:
    out = []
    for mi in menu.all_staples():
        if not _item_ok(mi, f):
            continue
        if f.required_staple:
            kind, value = f.required_staple
            if kind == "tag":
                if mi.category != value:
                    continue  # 含未知品类的主食也无法验证，一并排除
            elif mi.code != value:
                continue
        out.append(mi)
    out.sort(key=lambda m: (m.current_price_fen, m.code))
    return out[:MAX_STAPLES]


def _filtered_combos(menu: NormalizedMenu, f: _Filter) -> list[ComboDetail]:
    out = []
    for combo in menu.combos.values():
        if f.required_staple:
            kind, value = f.required_staple
            if kind == "tag" and combo.staple_tag != value:
                continue  # 主食品类不匹配或未知
            if kind == "code":
                # 指定主食编码：套餐第一轮默认选项需匹配该编码
                first = combo.rounds[0] if combo.rounds else None
                default = next((c for c in first.choices if c.is_default), None) if first else None
                if default is None or default.code != value:
                    continue
        out.append(combo)
    out.sort(key=lambda c: (c.base_price_fen, c.code))
    return out[:MAX_COMBOS]


def _choice_ok(choice: ComboChoice, f: _Filter) -> bool:
    if choice.category in f.excluded_categories:
        return False
    if choice.code in f.excluded_items:
        return False
    return True


def _mod_removal_selections(combo: ComboDetail, mod_codes: set[str]) -> dict[str, list[RoundSelection]]:
    """对每个需要去除的特制编码，生成对应的轮次选择（该组全部项按选中/未选中传 key）。"""
    result: dict[str, list[RoundSelection]] = {}
    if not mod_codes:
        return result
    for r in combo.rounds:
        for choice in r.choices:
            if not choice.is_default:
                continue
            for group in choice.modifications:
                group_codes = {mv.code for mv in group.values}
                hit = group_codes & mod_codes
                if not hit:
                    continue
                for code in sorted(hit):
                    sels: list[tuple[str, str, int]] = []
                    for mv in group.values:
                        if mv.code == code:
                            # 去掉该项：传 unselectedKey（若无则退化为 selectedKey 且数量 0）
                            sels.append((mv.code, mv.unselected_key or mv.selected_key, 0))
                        else:
                            sels.append((mv.code, mv.selected_key, 1 if mv.selected_quantity > 0 else 0))
                    result.setdefault(code, []).append(
                        RoundSelection(
                            round_id=r.id,
                            choice_code=choice.code,
                            quantity=1,
                            modifications=tuple(sels),
                        )
                    )
    return result


def _combo_variants(combo: ComboDetail, f: _Filter) -> list[Candidate]:
    variants: list[Candidate] = []
    seen: set[str] = set()

    def _selections_valid(selections: list[RoundSelection]) -> bool:
        """变体的每一轮选择都必须通过过滤（含未替换轮的默认选项）。"""
        if len(selections) != len(combo.rounds):
            return False
        for sel in selections:
            r = next((x for x in combo.rounds if x.id == sel.round_id), None)
            if r is None:
                return False
            choice = next((c for c in r.choices if c.code == sel.choice_code), None)
            if choice is None or not _choice_ok(choice, f):
                return False
        return True

    def add(selections: list[RoundSelection], note: str) -> None:
        if not _selections_valid(selections):
            return
        cand = _make_candidate(
            [
                OrderItem(
                    product_code=combo.code,
                    quantity=1,
                    rounds=tuple(selections),
                )
            ],
            note=note,
        )
        if cand.key not in seen:
            seen.add(cand.key)
            variants.append(cand)

    def default_selection(round_id: str) -> Optional[RoundSelection]:
        r = next((x for x in combo.rounds if x.id == round_id), None)
        if r is None:
            return None
        default = next((c for c in r.choices if c.is_default), None)
        if default is None or not _choice_ok(default, f):
            return None
        return RoundSelection(round_id=r.id, choice_code=default.code, quantity=1)

    # 1) 默认搭配（每一轮默认选项都需通过品类排除检查）
    default_sel: list[RoundSelection] = []
    ok = True
    for r in combo.rounds:
        sel = default_selection(r.id)
        if sel is None:
            ok = False
            break
        default_sel.append(sel)
    if ok and default_sel:
        add(default_sel, "默认搭配")

    # 2) 特制去除变体（如「不要酸黄瓜」）：在默认搭配基础上替换指定轮次
    for code, selections in _mod_removal_selections(combo, f.excluded_mod_codes).items():
        replaced = {s.round_id: s for s in selections}
        merged = [replaced.get(r.id) or default_selection(r.id) for r in combo.rounds]
        if all(m is not None for m in merged):
            add([m for m in merged if m is not None], f"去除特制项 {code}")

    # 3) 单轮替换变体（覆盖不同结构与妥协方向）
    for r in combo.rounds:
        alts = [c for c in r.choices if not c.is_default and _choice_ok(c, f)]
        alts.sort(key=lambda c: (abs(c.diff_price_fen), c.code))
        for alt in alts[:MAX_SWAPS_PER_ROUND]:
            sel = [
                RoundSelection(round_id=x.id, choice_code=alt.code, quantity=1)
                if x.id == r.id
                else default_selection(x.id)
                for x in combo.rounds
            ]
            if all(s is not None for s in sel):
                add([s for s in sel if s is not None], f"「{r.name}」换为「{alt.name}」")
        if len(variants) >= MAX_VARIANTS_PER_COMBO:
            break

    return variants[:MAX_VARIANTS_PER_COMBO]


def _make_candidate(items: list[OrderItem], note: str = "") -> Candidate:
    norm = sorted(
        (
            {
                "productCode": i.product_code,
                "quantity": i.quantity,
                "rounds": sorted(
                    [
                        {
                            "round": s.round_id,
                            "choice": s.choice_code,
                            "modifications": sorted(s.modifications),
                        }
                        for s in i.rounds
                    ],
                    key=lambda x: x["round"],
                ),
                "couponId": i.coupon_id,
                "couponCode": i.coupon_code,
            }
            for i in items
        ),
        key=lambda x: x["productCode"],
    )
    key = json.dumps(norm, ensure_ascii=False, sort_keys=True)
    return Candidate(key=key, items=tuple(items), note=note)


def _estimated_price_fen(candidate: Candidate, menu: NormalizedMenu) -> int:
    """仅用于试算顺序安排的标价估计；不得用于剪除。"""
    total = 0
    for item in candidate.items:
        mi = menu.items.get(item.product_code)
        combo = menu.combos.get(item.product_code)
        if combo is not None:
            total += combo.base_price_fen
            for sel in item.rounds:
                r = next((x for x in combo.rounds if x.id == sel.round_id), None)
                if r is None:
                    continue
                choice = next((c for c in r.choices if c.code == sel.choice_code), None)
                if choice is not None:
                    total += choice.diff_price_fen
            continue
        if mi is not None:
            total += mi.current_price_fen * item.quantity
    return total


def generate_candidates(menu: NormalizedMenu, request: MealRequest, max_candidates: int = DEFAULT_MAX_CANDIDATES) -> list[Candidate]:
    f = _build_filter(menu, request)
    staples = _filtered_staples(menu, f)
    drinks = [mi for mi in menu.category_items("drink") if _item_ok(mi, f)]
    drinks.sort(key=lambda m: (m.current_price_fen, m.code))
    drinks = drinks[:MAX_DRINKS]
    snacks = [mi for mi in menu.category_items("snack") if _item_ok(mi, f)]
    snacks.sort(key=lambda m: (m.current_price_fen, m.code))
    snacks = snacks[:MAX_SNACKS]
    combos = _filtered_combos(menu, f)

    need_drink = "drink" in f.required_categories
    max_entries = f.max_items if f.max_items is not None else 3

    groups: list[tuple[int, list[Candidate]]] = []

    # 组 1：套餐默认搭配
    combo_defaults: list[Candidate] = []
    for combo in combos:
        combo_defaults.extend(_combo_variants(combo, f))
    if combo_defaults:
        groups.append((1, combo_defaults))

    # 组 2：券变体（券商品必须携带券引用；也可作为小食与主食搭配）
    coupon_variants: list[Candidate] = []
    for coupon in menu.coupons:
        for pcode in coupon.product_codes:
            mi = menu.items.get(pcode)
            if mi is None or not _coupon_item_ok(mi, f):
                continue
            coupon_variants.append(
                _make_candidate(
                    [OrderItem(product_code=pcode, quantity=1, coupon_id=coupon.coupon_id, coupon_code=coupon.coupon_code)],
                    note=f"使用优惠「{coupon.title}」",
                )
            )
            if mi.category == "snack" and not need_drink and max_entries >= 2:
                for staple in staples[:4]:
                    coupon_variants.append(
                        _make_candidate(
                            [
                                OrderItem(product_code=staple.code, quantity=1),
                                OrderItem(product_code=pcode, quantity=1, coupon_id=coupon.coupon_id, coupon_code=coupon.coupon_code),
                            ],
                            note=f"主食 + 优惠「{coupon.title}」",
                        )
                    )
    if coupon_variants:
        groups.append((2, coupon_variants))

    # 组 3：单点 —— 主食 alone / +饮料 / +小食 / +饮料+小食
    alone: list[Candidate] = []
    with_drink: list[Candidate] = []
    with_snack: list[Candidate] = []
    full: list[Candidate] = []
    for staple in staples:
        if max_entries >= 1 and not need_drink:
            alone.append(_make_candidate([OrderItem(product_code=staple.code, quantity=1)], note="单点主食"))
        if max_entries >= 2:
            for d in drinks:
                with_drink.append(
                    _make_candidate(
                        [OrderItem(product_code=staple.code, quantity=1), OrderItem(product_code=d.code, quantity=1)],
                        note="主食 + 饮料",
                    )
                )
        if max_entries >= 2 and not need_drink:
            for s in snacks:
                with_snack.append(
                    _make_candidate(
                        [OrderItem(product_code=staple.code, quantity=1), OrderItem(product_code=s.code, quantity=1)],
                        note="主食 + 小食",
                    )
                )
        if max_entries >= 3 and drinks and snacks:
            d0 = drinks[0]
            for s in snacks[:2]:
                full.append(
                    _make_candidate(
                        [
                            OrderItem(product_code=staple.code, quantity=1),
                            OrderItem(product_code=d0.code, quantity=1),
                            OrderItem(product_code=s.code, quantity=1),
                        ],
                        note="主食 + 饮料 + 小食",
                    )
                )
    for prio, group in ((3, alone), (4, with_drink), (5, with_snack), (6, full)):
        if group:
            groups.append((prio, group))

    # 汇总：按组优先级 + 组内标价估计排序，去重，截断
    ordered: list[Candidate] = []
    seen: set[str] = set()
    for _, group in sorted(groups, key=lambda g: g[0]):
        decorated = sorted(group, key=lambda c: (_estimated_price_fen(c, menu), c.key))
        for cand in decorated:
            if cand.key in seen:
                continue
            seen.add(cand.key)
            ordered.append(cand)
    return ordered[:max_candidates]


def search_scope_dict(request: MealRequest) -> dict:
    scope = dict(SEARCH_SCOPE_TEMPLATE)
    scope["requestVersion"] = request.request_version
    scope["storeId"] = request.context.store_id
    return scope
