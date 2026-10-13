"""约束引擎：三态评价与报价前剪除。

规则（实现路径文档 3 / 6.4）：
- 条件评价只有 satisfied / violated / unknown 三态；
- 包含未知硬条件的候选不进入「全部满足」列表；
- 报价未知（失败/无效）时，预算类条件记为 unknown，不得记为违反，也不得记为满足；
- 违反锁定条件的候选在排序时首先被拒绝。
"""

from __future__ import annotations

from typing import Optional

from .models import (
    Candidate,
    CandidateEvaluation,
    ComboDetail,
    ConditionOutcome,
    Constraint,
    ConstraintKind,
    EvalState,
    ExpandedContent,
    MealRequest,
    MenuItem,
    Negotiability,
    NormalizedMenu,
    OrderItem,
    Quote,
    QuoteStatus,
    Strength,
)


# ---------------------------------------------------------------------------
# 候选展开
# ---------------------------------------------------------------------------

def expand_candidate(candidate: Candidate, menu: NormalizedMenu) -> ExpandedContent:
    """把候选展开成具体餐品条目（套餐按轮次展开）。"""
    entries: list[dict] = []
    for item in candidate.items:
        combo = menu.combos.get(item.product_code)
        if combo is not None:
            if item.rounds:
                for sel in item.rounds:
                    selections = {c: (k, q) for c, k, q in sel.modifications}
                    _append_choice_entry(entries, combo, sel.round_id, sel.choice_code, sel.quantity, selections)
            else:
                # 未指定轮次：按默认选项展开
                for r in combo.rounds:
                    default = next((c for c in r.choices if c.is_default), None)
                    if default is not None:
                        _append_choice_entry(entries, combo, r.id, default.code, r.min_quantity or 1)
            continue
        mi = menu.items.get(item.product_code)
        if mi is None:
            entries.append(
                {
                    "code": item.product_code,
                    "name": item.product_code,
                    "category": "unknown",
                    "quantity": item.quantity,
                    "confidence": "unknown",
                    "basis": "门店数据中不存在该编码，无法判定",
                    "source": "item",
                }
            )
            continue
        entries.append(
            {
                "code": mi.code,
                "name": mi.name,
                "category": mi.category,
                "quantity": item.quantity,
                "confidence": mi.tag_confidence,
                "basis": mi.tag_basis,
                "source": "item",
            }
        )
    return ExpandedContent(entries=entries)


def _append_choice_entry(
    entries: list[dict],
    combo: ComboDetail,
    round_id: str,
    choice_code: str,
    quantity: int,
    selections: Optional[dict[str, tuple[str, int]]] = None,
) -> None:
    r = next((x for x in combo.rounds if x.id == round_id), None)
    if r is None:
        entries.append(
            {
                "code": choice_code,
                "name": choice_code,
                "category": "unknown",
                "quantity": quantity,
                "confidence": "unknown",
                "basis": f"套餐「{combo.name}」不存在轮次 {round_id}",
                "source": f"combo:{combo.code}",
            }
        )
        return
    choice = next((c for c in r.choices if c.code == choice_code), None)
    if choice is None:
        entries.append(
            {
                "code": choice_code,
                "name": choice_code,
                "category": "unknown",
                "quantity": quantity,
                "confidence": "unknown",
                "basis": f"套餐「{combo.name}」轮次「{r.name}」不存在选项 {choice_code}（非合法替换）",
                "source": f"combo:{combo.code}:{round_id}",
            }
        )
        return
    # 记录特制状态：候选显式选择 > 官方默认
    mods: list[dict] = []
    for group in choice.modifications:
        for mv in group.values:
            if selections and mv.code in selections:
                _, qty = selections[mv.code]
                selected = qty > 0
            else:
                selected = mv.selected_quantity > 0
            mods.append({"code": mv.code, "name": mv.name, "selected": selected})
    entries.append(
        {
            "code": choice.code,
            "name": choice.name,
            "category": choice.category,
            "quantity": quantity,
            "confidence": choice.tag_confidence,
            "basis": f"套餐「{combo.name}」轮次「{r.name}」：{choice.tag_basis}",
            "source": f"combo:{combo.code}:{round_id}",
            "modifications": mods,
        }
    )


# ---------------------------------------------------------------------------
# 单条约束评价
# ---------------------------------------------------------------------------

def _eval_total_price_max(c: Constraint, ev: CandidateEvaluation, nutrition=None) -> ConditionOutcome:
    quote = ev.quote
    if quote is None or not quote.ok or quote.price_fen is None:
        return ConditionOutcome(
            c.id,
            EvalState.UNKNOWN,
            target=c.value,
            actual=None,
            evidence="候选未获得有效官方报价，预算条件无法判定",
        )
    if quote.price_fen <= int(c.value):
        return ConditionOutcome(c.id, EvalState.SATISFIED, target=c.value, actual=quote.price_fen, evidence="试算总价不超过预算")
    return ConditionOutcome(
        c.id,
        EvalState.VIOLATED,
        target=c.value,
        actual=quote.price_fen,
        evidence=f"试算总价 {quote.price_fen} 分超过预算 {c.value} 分",
    )


def _eval_include_staple(c: Constraint, ev: CandidateEvaluation) -> ConditionOutcome:
    wanted = str(c.value)
    staples = [e for e in ev.expanded.entries if e["category"].startswith("staple")]
    unknown_staples = [e for e in ev.expanded.entries if e["category"] == "unknown"]
    if not staples:
        if unknown_staples:
            return ConditionOutcome(c.id, EvalState.UNKNOWN, target=wanted, actual=None, evidence="候选含无法判定品类的餐品，主食条件无法确认")
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=None, evidence="候选不含主食")
    if wanted.startswith("staple_"):
        matched = [e for e in staples if e["category"] == wanted]
        if matched:
            return ConditionOutcome(c.id, EvalState.SATISFIED, target=wanted, actual=matched[0]["name"], evidence=f"主食「{matched[0]['name']}」品类匹配")
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=staples[0]["name"], evidence=f"主食「{staples[0]['name']}」品类为 {staples[0]['category']}，与要求 {wanted} 不符")
    # 指定编码
    matched = [e for e in ev.expanded.entries if e["code"] == wanted]
    if matched:
        return ConditionOutcome(c.id, EvalState.SATISFIED, target=wanted, actual=matched[0]["name"], evidence=f"包含指定餐品「{matched[0]['name']}」")
    return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=None, evidence="候选不含指定主食编码")


def _eval_exclude_category(c: Constraint, ev: CandidateEvaluation) -> ConditionOutcome:
    wanted = str(c.value)
    hit = [e for e in ev.expanded.entries if e["category"] == wanted]
    if hit:
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=[e["name"] for e in hit], evidence=f"候选包含被排除品类：{ '、'.join(e['name'] for e in hit) }")
    # 近似品类（如 fries_like 对 fries）与未知品类都导致 unknown
    approx = [e for e in ev.expanded.entries if e["category"] == "fries_like" and wanted == "fries"]
    unknown = [e for e in ev.expanded.entries if e["category"] == "unknown"]
    if approx or unknown:
        names = [e["name"] for e in approx + unknown]
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=wanted, actual=names, evidence="存在近似或无法判定的餐品，无法确认不含该品类")
    return ConditionOutcome(c.id, EvalState.SATISFIED, target=wanted, actual=None, evidence="候选不含该品类")


def _eval_exclude_item(c: Constraint, ev: CandidateEvaluation, known_codes: Optional[set[str]] = None) -> ConditionOutcome:
    wanted = str(c.value)
    # 1) 商品编码命中
    hit = [e for e in ev.expanded.entries if e["code"] == wanted]
    if hit:
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=[e["name"] for e in hit], evidence=f"候选包含被排除餐品：{ '、'.join(e['name'] for e in hit) }")
    # 2) 特制/配料编码命中（默认选中或显式选中都算）
    mod_hit = [
        f"{e['name']}·{m['name']}"
        for e in ev.expanded.entries
        for m in e.get("modifications", [])
        if m["code"] == wanted and m["selected"]
    ]
    if mod_hit:
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=mod_hit, evidence=f"候选包含被排除特制项：{'、'.join(mod_hit)}")
    # 3) 编码不是已知商品 → 无法确认未展开详情里是否含该项
    if known_codes is not None and wanted not in known_codes:
        has_mod_info = any(e.get("modifications") for e in ev.expanded.entries)
        if not has_mod_info:
            return ConditionOutcome(c.id, EvalState.UNKNOWN, target=wanted, actual=None, evidence="该编码非门店商品编码（疑似配料/特制编码），当前数据未展开对应特制详情，无法确认")
    return ConditionOutcome(c.id, EvalState.SATISFIED, target=wanted, actual=None, evidence="候选不含该餐品/特制编码")


def _eval_require_category(c: Constraint, ev: CandidateEvaluation) -> ConditionOutcome:
    wanted = str(c.value)
    if ev.expanded.has_category(wanted):
        names = [e["name"] for e in ev.expanded.entries if e["category"] == wanted]
        return ConditionOutcome(c.id, EvalState.SATISFIED, target=wanted, actual=names, evidence=f"候选包含{wanted}：{'、'.join(names)}")
    unknown = [e for e in ev.expanded.entries if e["category"] == "unknown"]
    if unknown:
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=wanted, actual=None, evidence="存在无法判定的餐品，无法确认包含该品类")
    return ConditionOutcome(c.id, EvalState.VIOLATED, target=wanted, actual=None, evidence="候选不含该品类")


def _eval_max_items(c: Constraint, ev: CandidateEvaluation) -> ConditionOutcome:
    total = sum(int(e["quantity"]) for e in ev.expanded.entries)
    limit = int(c.value)
    if total <= limit:
        return ConditionOutcome(c.id, EvalState.SATISFIED, target=limit, actual=total, evidence="总件数不超过上限")
    return ConditionOutcome(c.id, EvalState.VIOLATED, target=limit, actual=total, evidence=f"总件数 {total} 超过上限 {limit}")


def _eval_prefer_coupon(c: Constraint, ev: CandidateEvaluation) -> ConditionOutcome:
    used = any(i.coupon_id for i in ev.candidate.items)
    if used:
        return ConditionOutcome(c.id, EvalState.SATISFIED, target="use_coupon", actual="used", evidence="候选使用了门店优惠")
    return ConditionOutcome(c.id, EvalState.VIOLATED, target="use_coupon", actual="not_used", evidence="候选未使用优惠（偏好项，不影响可行性）")


def _eval_nutrition(c: Constraint, ev: CandidateEvaluation, nutrition) -> ConditionOutcome:
    if nutrition is None:
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=c.value, actual=None, evidence="未提供营养数据，无法判定")
    # 官方报价商品名回退（菜单单品名常缺规格，报价名带规格且与营养表精确一致）
    overrides: dict[str, str] = {}
    if ev.quote is not None and ev.quote.ok:
        for code, name in ev.quote.product_code_names:
            overrides[code] = name
    totals = nutrition.totals_for(ev.expanded, overrides)
    if totals is None:
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=c.value, actual=None, evidence="营养数据不完整，缺失项不当零")
    if c.kind == ConstraintKind.ENERGY_MAX:
        kcal = totals.get("energy_kcal")
        if kcal is None:
            return ConditionOutcome(c.id, EvalState.UNKNOWN, target=c.value, actual=None, evidence="热量数据缺失")
        if kcal <= int(c.value):
            return ConditionOutcome(c.id, EvalState.SATISFIED, target=c.value, actual=kcal, evidence=f"组合热量 {kcal:g} kcal 不超过上限 {c.value} kcal")
        return ConditionOutcome(c.id, EvalState.VIOLATED, target=c.value, actual=kcal, evidence=f"组合热量 {kcal:g} kcal 超过上限 {c.value} kcal")
    protein = totals.get("protein")
    if protein is None:
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=c.value, actual=None, evidence="蛋白质数据缺失")
    if protein >= int(c.value):
        return ConditionOutcome(c.id, EvalState.SATISFIED, target=c.value, actual=protein, evidence=f"组合蛋白质 {protein:g} g 不低于下限 {c.value} g")
    return ConditionOutcome(c.id, EvalState.VIOLATED, target=c.value, actual=protein, evidence=f"组合蛋白质 {protein:g} g 低于下限 {c.value} g")


_EVALUATORS = {
    ConstraintKind.TOTAL_PRICE_MAX: _eval_total_price_max,
    ConstraintKind.INCLUDE_STAPLE: _eval_include_staple,
    ConstraintKind.EXCLUDE_CATEGORY: _eval_exclude_category,
    ConstraintKind.EXCLUDE_ITEM: _eval_exclude_item,
    ConstraintKind.REQUIRE_CATEGORY: _eval_require_category,
    ConstraintKind.MAX_ITEMS: _eval_max_items,
    ConstraintKind.PREFER_COUPON: _eval_prefer_coupon,
    ConstraintKind.ENERGY_MAX: _eval_nutrition,
    ConstraintKind.PROTEIN_MIN: _eval_nutrition,
}


def evaluate_constraint(c: Constraint, ev: CandidateEvaluation, nutrition=None, known_codes: Optional[set[str]] = None) -> ConditionOutcome:
    fn = _EVALUATORS.get(c.kind)
    if fn is None:
        return ConditionOutcome(c.id, EvalState.UNKNOWN, target=c.value, actual=None, evidence=f"未支持的约束类型 {c.kind.value}")
    if c.kind in (ConstraintKind.ENERGY_MAX, ConstraintKind.PROTEIN_MIN):
        return fn(c, ev, nutrition)
    if c.kind == ConstraintKind.EXCLUDE_ITEM:
        return fn(c, ev, known_codes)
    return fn(c, ev)


# ---------------------------------------------------------------------------
# 候选整体评价
# ---------------------------------------------------------------------------

def evaluate_candidate(
    candidate: Candidate,
    expanded: ExpandedContent,
    quote: Optional[Quote],
    request: MealRequest,
    nutrition=None,
    known_codes: Optional[set[str]] = None,
) -> CandidateEvaluation:
    hard_violations: list[ConditionOutcome] = []
    locked_violations: list[ConditionOutcome] = []
    unknowns: list[ConditionOutcome] = []
    satisfied: list[ConditionOutcome] = []
    soft_results: dict[str, EvalState] = {}

    ev = CandidateEvaluation(candidate, expanded, quote, True, True)
    for c in request.constraints:
        outcome = evaluate_constraint(c, ev, nutrition, known_codes)
        is_locked = c.negotiability == Negotiability.LOCKED
        if c.strength == Strength.SOFT and not is_locked:
            soft_results[c.id] = outcome.state
            continue
        if outcome.state == EvalState.VIOLATED:
            hard_violations.append(outcome)
            if is_locked:
                locked_violations.append(outcome)
        elif outcome.state == EvalState.UNKNOWN:
            unknowns.append(outcome)
        else:
            satisfied.append(outcome)

    hard_pass = not hard_violations
    locked_pass = not locked_violations
    # 「全部满足」还要求没有未知硬条件 —— 由调用方结合 unknowns 判定
    return CandidateEvaluation(
        candidate=candidate,
        expanded=expanded,
        quote=quote,
        hard_pass=hard_pass,
        locked_pass=locked_pass,
        violations=hard_violations,
        unknowns=unknowns,
        satisfied=satisfied,
        soft_results=soft_results,
    )


# ---------------------------------------------------------------------------
# 报价前剪除（只用不依赖价格的硬条件，省报价预算）
# ---------------------------------------------------------------------------

def precheck_candidate(
    candidate: Candidate,
    expanded: ExpandedContent,
    request: MealRequest,
    known_codes: Optional[set[str]] = None,
    locked_only: bool = True,
) -> Optional[str]:
    """返回剪除原因；None 表示保留。

    默认只按「锁定」条件剪除：可讨论（ask）条件的违反候选必须保留——
    它们正是生成妥协方向（如「允许薯条」「换主食」）所需的候选。
    locked_only=False 时按全部硬条件剪除（调用方明确需要窄搜索时使用）。
    """
    ev = CandidateEvaluation(candidate, expanded, None, True, True)
    for c in request.constraints:
        is_locked = c.negotiability == Negotiability.LOCKED
        if c.strength == Strength.SOFT and not is_locked:
            continue
        if locked_only and not is_locked:
            continue
        if c.kind == ConstraintKind.TOTAL_PRICE_MAX:
            continue  # 依赖报价
        if c.kind in (ConstraintKind.ENERGY_MAX, ConstraintKind.PROTEIN_MIN):
            continue  # 依赖营养数据与展开，此处保守保留
        outcome = evaluate_constraint(c, ev, None, known_codes)
        if outcome.state == EvalState.VIOLATED:
            return f"报价前剪除：{outcome.evidence}"
    return None
