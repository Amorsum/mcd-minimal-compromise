"""约束引擎测试：三态评价、展开、品类标注、报价前剪除。"""

from __future__ import annotations

from mcd_compromise.constraints import (
    evaluate_candidate,
    expand_candidate,
    precheck_candidate,
)
from mcd_compromise.models import (
    Candidate,
    Constraint,
    ConstraintKind,
    EvalState,
    Negotiability,
    OrderItem,
    Quote,
    QuoteStatus,
    RoundSelection,
    Strength,
    yuan_to_fen,
)


def _ev(candidate, menu, request, quote=None):
    expanded = expand_candidate(candidate, menu)
    known = set(menu.items) | set(menu.combos)
    return evaluate_candidate(candidate, expanded, quote, request, None, known)


class TestPriceParsing:
    def test_menu_price_string(self):
        assert yuan_to_fen("33.5") == 3350
        assert yuan_to_fen("25.5") == 2550
        assert yuan_to_fen("13") == 1300

    def test_diff_price_with_sign(self):
        assert yuan_to_fen("+ ¥1.5") == 150
        assert yuan_to_fen("- ¥0.5") == -50
        assert yuan_to_fen("+ ¥0") == 0

    def test_integer_passthrough(self):
        assert yuan_to_fen(2550) == 2550


class TestTagging:
    def test_beef_burger(self, menu):
        assert menu.items["B001"].category == "staple_beef"
        assert menu.items["B001"].tag_confidence == "high"

    def test_fries(self, menu):
        assert menu.items["F001"].category == "fries"

    def test_corn_is_snack_not_fries(self, menu):
        assert menu.items["F002"].category == "snack"

    def test_drink(self, menu):
        assert menu.items["D001"].category == "drink"

    def test_official_combo_tag_overrides_name(self, menu):
        # 「麦乐鸡套餐」名称含「麦乐鸡」，但官方标签为「套餐」→ 必须按套餐处理
        assert menu.items["C003"].category == "combo"
        assert menu.items["C003"].is_combo
        assert "套餐" in menu.items["C003"].tag_basis

    def test_unfetched_combo_not_in_singles(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = _req([])
        cands = generate_candidates(menu, req, 200)
        for c in cands:
            for it in c.items:
                assert it.product_code != "C003", "未获取详情套餐不得进入候选"

    def test_unknown_item(self):
        from mcd_compromise.normalize import tag_item

        cat, basis, conf = tag_item("麦当劳联名复古棒球帽")
        assert cat == "unknown"
        assert conf == "unknown"

    def test_fries_like_is_medium(self):
        from mcd_compromise.normalize import tag_item

        cat, _, conf = tag_item("月下相思扭扭薯")
        assert cat == "fries_like"
        assert conf == "medium"


class TestExpansion:
    def test_combo_default_expansion(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = _req([])
        cands = generate_candidates(menu, req, 64)
        # 默认搭配变体（显式选择各轮默认项）；套餐不再被当作单品生成
        combo_cand = next(c for c in cands if c.items[0].product_code == "C001" and c.note == "默认搭配")
        assert all(it.rounds for it in combo_cand.items), "套餐候选必须带轮次选择"
        expanded = expand_candidate(combo_cand, menu)
        codes = expanded.codes()
        assert codes == ["B001", "F001", "D001"]  # 默认：巨无霸 + 中薯条 + 可乐
        # 隐藏薯条：名称里看不到，展开后才能识别
        assert expanded.has_category("fries")

    def test_combo_not_generated_as_single(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = _req([])
        cands = generate_candidates(menu, req, 200)
        singles = [
            c
            for c in cands
            if len(c.items) == 1 and c.items[0].product_code in ("C001", "C002") and not c.items[0].rounds
        ]
        assert not singles, "套餐不得被当作单品生成（组成未知，无法做条件检查）"

    def test_combo_round_selection_expansion(self, menu):
        cand = Candidate(
            key="k1",
            items=(
                OrderItem(
                    product_code="C001",
                    quantity=1,
                    rounds=(
                        RoundSelection(round_id="1", choice_code="B001"),
                        RoundSelection(round_id="2", choice_code="F002"),
                        RoundSelection(round_id="3", choice_code="D002"),
                    ),
                ),
            ),
        )
        expanded = expand_candidate(cand, menu)
        assert expanded.codes() == ["B001", "F002", "D002"]
        assert not expanded.has_category("fries")

    def test_illegal_replacement_is_unknown(self, menu):
        cand = Candidate(
            key="k2",
            items=(OrderItem(product_code="C001", quantity=1, rounds=(RoundSelection(round_id="2", choice_code="ZZZ"),)),),
        )
        expanded = expand_candidate(cand, menu)
        # 轮次 1、3 缺省展开，轮次 2 是非法替换 → unknown
        unknown_entries = [e for e in expanded.entries if e["category"] == "unknown"]
        assert len(unknown_entries) == 1
        assert "非合法替换" in unknown_entries[0]["basis"]

    def test_modifications_recorded(self, menu):
        cand = Candidate(
            key="k3",
            items=(OrderItem(product_code="C001", quantity=1, rounds=(RoundSelection(round_id="1", choice_code="B001"),)),),
        )
        expanded = expand_candidate(cand, menu)
        b001 = next(e for e in expanded.entries if e["code"] == "B001")
        mods = {m["code"]: m["selected"] for m in b001["modifications"]}
        assert mods == {"M001": True, "M002": True, "M003": True}


class TestBudgetEvaluation:
    def _quote(self, price):
        return Quote(candidate_key="k", status=QuoteStatus.OK, price_fen=price, quoted_at="t")

    def test_satisfied(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="budget", kind=ConstraintKind.TOTAL_PRICE_MAX, value=3000)
        out = _ev(cand, menu, _req([c]), self._quote(2550))
        assert out.violations == []
        assert out.hard_pass

    def test_violated(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="budget", kind=ConstraintKind.TOTAL_PRICE_MAX, value=2000)
        out = _ev(cand, menu, _req([c]), self._quote(2550))
        assert len(out.violations) == 1
        assert out.violations[0].state == EvalState.VIOLATED
        assert out.violations[0].actual == 2550

    def test_unknown_without_quote(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="budget", kind=ConstraintKind.TOTAL_PRICE_MAX, value=3000)
        out = _ev(cand, menu, _req([c]), None)
        assert out.violations == []
        assert len(out.unknowns) == 1
        assert out.unknowns[0].state == EvalState.UNKNOWN

    def test_failed_quote_is_unknown_not_violated(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="budget", kind=ConstraintKind.TOTAL_PRICE_MAX, value=3000)
        failed = Quote(candidate_key="k", status=QuoteStatus.FAILED, error_code="500")
        out = _ev(cand, menu, _req([c]), failed)
        assert not out.violations
        assert len(out.unknowns) == 1


class TestExcludeCategory:
    def test_fries_hit_is_violated(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="F001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries", negotiability=Negotiability.LOCKED)
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1
        assert not out.locked_pass

    def test_combo_hidden_fries_detected(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="C001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries", negotiability=Negotiability.LOCKED)
        out = _ev(cand, menu, _req([c]))
        assert not out.locked_pass  # 三件套默认含中薯条

    def test_fries_like_is_unknown(self, menu):
        # 构造一个 fries_like 单品
        menu.items["F009"] = type(menu.items["F001"])(
            code="F009", name="月下相思扭扭薯", current_price_fen=1500, original_price_fen=1500,
            category="fries_like", tag_basis="b", tag_confidence="medium",
        )
        cand = Candidate(key="k", items=(OrderItem(product_code="F009", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries")
        out = _ev(cand, menu, _req([c]))
        assert not out.violations
        assert len(out.unknowns) == 1  # 近似品类不得判为满足

    def test_clean_candidate_satisfied(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="F002", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries")
        out = _ev(cand, menu, _req([c]))
        assert out.violations == [] and out.unknowns == []


class TestIncludeStaple:
    def test_tag_match(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="s", kind=ConstraintKind.INCLUDE_STAPLE, value="staple_beef")
        out = _ev(cand, menu, _req([c]))
        assert out.violations == [] and out.unknowns == []

    def test_tag_mismatch(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B002", quantity=1),))
        c = Constraint(id="s", kind=ConstraintKind.INCLUDE_STAPLE, value="staple_beef")
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1

    def test_no_staple_is_violated(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="D001", quantity=1),))
        c = Constraint(id="s", kind=ConstraintKind.INCLUDE_STAPLE, value="staple_beef")
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1

    def test_specific_code(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="C001", quantity=1),))
        c = Constraint(id="s", kind=ConstraintKind.INCLUDE_STAPLE, value="B001")
        out = _ev(cand, menu, _req([c]))
        assert out.violations == [] and out.unknowns == []


class TestExcludeItem:
    def test_product_code(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="F001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_ITEM, value="F001")
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1

    def test_mod_code_default_selected_is_violated(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="C001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_ITEM, value="M003")  # 酸黄瓜
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1

    def test_mod_code_deselected_is_satisfied(self, menu):
        cand = Candidate(
            key="k",
            items=(
                OrderItem(
                    product_code="C001",
                    quantity=1,
                    rounds=(
                        RoundSelection(
                            round_id="1",
                            choice_code="B001",
                            modifications=(("M001", "0-1", 1), ("M002", "0-1", 1), ("M003", "0-0", 0)),
                        ),
                    ),
                ),
            ),
        )
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_ITEM, value="M003")
        out = _ev(cand, menu, _req([c]))
        assert out.violations == [] and out.unknowns == []

    def test_unknown_mod_code_without_detail_is_unknown(self, menu):
        # 单品未展开特制详情，配料编码无法确认
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_ITEM, value="M003")
        out = _ev(cand, menu, _req([c]))
        assert not out.violations
        assert len(out.unknowns) == 1


class TestRequireAndMax:
    def test_require_drink(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="r", kind=ConstraintKind.REQUIRE_CATEGORY, value="drink")
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1

    def test_max_items(self, menu):
        cand = Candidate(
            key="k",
            items=(OrderItem(product_code="B001", quantity=1), OrderItem(product_code="D001", quantity=1)),
        )
        c = Constraint(id="m", kind=ConstraintKind.MAX_ITEMS, value=1)
        out = _ev(cand, menu, _req([c]))
        assert len(out.violations) == 1
        assert out.violations[0].actual == 2

    def test_prefer_coupon_is_soft(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="B001", quantity=1),))
        c = Constraint(id="p", kind=ConstraintKind.PREFER_COUPON, value=True, strength=Strength.SOFT)
        out = _ev(cand, menu, _req([c]))
        assert out.soft_results["p"] == EvalState.VIOLATED
        assert out.hard_pass  # 偏好不影响可行性


class TestPrecheck:
    def test_prunes_fries_when_locked(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="C001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries", negotiability=Negotiability.LOCKED)
        expanded = expand_candidate(cand, menu)
        reason = precheck_candidate(cand, expanded, _req([c]), set(menu.items) | set(menu.combos))
        assert reason is not None and "剪除" in reason

    def test_budget_not_prechecked(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="C001", quantity=1),))
        c = Constraint(id="b", kind=ConstraintKind.TOTAL_PRICE_MAX, value=100)
        expanded = expand_candidate(cand, menu)
        reason = precheck_candidate(cand, expanded, _req([c]), set(menu.items) | set(menu.combos))
        assert reason is None  # 价格条件必须等报价

    def test_ask_level_violation_not_pruned(self, menu):
        # 可讨论条件的违反候选必须保留：它们是妥协方向的来源
        cand = Candidate(key="k", items=(OrderItem(product_code="F001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries")  # 默认 ask
        expanded = expand_candidate(cand, menu)
        reason = precheck_candidate(cand, expanded, _req([c]), set(menu.items) | set(menu.combos))
        assert reason is None

    def test_locked_only_flag(self, menu):
        cand = Candidate(key="k", items=(OrderItem(product_code="F001", quantity=1),))
        c = Constraint(id="x", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries")  # ask
        expanded = expand_candidate(cand, menu)
        reason = precheck_candidate(cand, expanded, _req([c]), set(menu.items) | set(menu.combos), locked_only=False)
        assert reason is not None  # 明确要求按全部硬条件剪除时才剪


def _req(constraints):
    from mcd_compromise.models import MealRequest, RequestContext

    return MealRequest(request_version=1, context=RequestContext(store_id="TEST001"), constraints=constraints)
