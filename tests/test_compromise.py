"""最小妥协算法端到端测试（对应实现路径文档第 11 节验收表）。"""

from __future__ import annotations

from conftest import make_request, run_until_final

from mcd_compromise.constraints import expand_candidate
from mcd_compromise.models import (
    Constraint,
    ConstraintKind,
    EvalState,
    Negotiability,
    PlanStatus,
    Strength,
)
from mcd_compromise.quote_state import QuoteBudget


def _budget(fen, locked=False, cid="budget", source="预算25元"):
    return Constraint(
        id=cid,
        kind=ConstraintKind.TOTAL_PRICE_MAX,
        value=fen,
        negotiability=Negotiability.LOCKED if locked else Negotiability.ASK,
        source_text=source,
    )


def _staple(tag_or_code, locked=False, cid="staple"):
    return Constraint(
        id=cid,
        kind=ConstraintKind.INCLUDE_STAPLE,
        value=tag_or_code,
        negotiability=Negotiability.LOCKED if locked else Negotiability.ASK,
        priority=80,
        source_text=f"主食:{tag_or_code}",
    )


def _no_fries(locked=True, cid="no_fries"):
    return Constraint(
        id=cid,
        kind=ConstraintKind.EXCLUDE_CATEGORY,
        value="fries",
        negotiability=Negotiability.LOCKED if locked else Negotiability.ASK,
        priority=10,
        source_text="不要薯条",
    )


def _need_drink(cid="need_drink"):
    return Constraint(
        id=cid,
        kind=ConstraintKind.REQUIRE_CATEGORY,
        value="drink",
        negotiability=Negotiability.ASK,
        priority=30,
        source_text="要一杯饮料",
    )


class TestFeasible:
    def test_all_constraints_satisfiable(self, menu):
        req = make_request(constraints=[_budget(3700), _staple("staple_beef"), _need_drink(), _no_fries()])
        result, quotes = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        assert result.plans, "应输出已核验方案"
        for plan in result.plans:
            assert plan.verified
            assert plan.quote.ok
            assert plan.quote.price_fen <= 3700
            cats = {e["category"] for e in plan.expanded.entries}
            assert "fries" not in cats
            assert any(c.startswith("staple_beef") for c in cats)
            assert "drink" in cats

    def test_plans_sorted_by_price(self, menu):
        req = make_request(constraints=[_budget(3700), _staple("staple_beef"), _need_drink(), _no_fries()])
        result, _ = run_until_final(menu, req)
        prices = [p.quote.price_fen for p in result.plans]
        assert prices == sorted(prices)

    def test_at_most_three_plans(self, menu):
        req = make_request(constraints=[_budget(5000)])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        assert len(result.plans) <= 3

    def test_single_feasible_plan_not_padded(self, menu):
        # 预算 36、只要巨无霸+饮料、不要可乐和雪碧：唯一恰好满足的是 B001+D003=3600
        req = make_request(
            constraints=[
                _budget(3600),
                _staple("B001"),
                _need_drink(),
                Constraint(id="no_cola", kind=ConstraintKind.EXCLUDE_ITEM, value="D001", source_text="不要可乐"),
                Constraint(id="no_sprite", kind=ConstraintKind.EXCLUDE_ITEM, value="D002", source_text="不要雪碧"),
            ]
        )
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        assert len(result.plans) == 1
        assert result.plans[0].quote.price_fen == 3600


class TestHiddenFries:
    def test_combo_default_excluded(self, menu):
        req = make_request(constraints=[_no_fries(locked=True)])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        for plan in result.plans:
            names = [e["name"] for e in plan.expanded.entries]
            assert "中薯条" not in names and "大薯条" not in names

    def test_combo_with_corn_cup_allowed(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = make_request(constraints=[_no_fries(locked=True)])
        cands = generate_candidates(menu, req, 64)
        # 「不要薯条」下，套餐必须通过换小食（玉米杯）产生合法候选
        corn_combos = [
            c
            for c in cands
            if c.items[0].product_code in ("C001", "C002")
            and any(sel.choice_code == "F002" for it in c.items for sel in it.rounds)
        ]
        assert corn_combos, "应生成把套餐小食换成玉米杯的候选"
        # 默认含薯条的套餐变体不得出现
        bad = [
            c
            for c in cands
            if c.items[0].product_code in ("C001", "C002")
            and any(sel.choice_code == "F001" for it in c.items for sel in it.rounds)
        ]
        assert not bad

    def test_combo_default_excluded_from_plans(self, menu):
        req = make_request(constraints=[_no_fries(locked=True)])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        for plan in result.plans:
            names = [e["name"] for e in plan.expanded.entries]
            assert "中薯条" not in names and "大薯条" not in names


class TestBudgetConflict:
    def test_budget_relaxation_proposed(self, menu):
        # 预算 30、指定巨无霸、要饮料：最便宜满足其他条件的方案 35.00
        req = make_request(constraints=[_budget(3000), _staple("B001"), _need_drink()])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        assert not any(p.verified for p in result.plans), "超预算方案不得标记为满足原需求"
        budget_plans = [p for p in result.plans if any(pc.constraint_id == "budget" for pc in p.proposed_changes)]
        assert budget_plans, "应提供预算修改建议"
        desc = budget_plans[0].proposed_changes[0].description
        assert "30.00 元" in desc and "35.00 元" in desc

    def test_budget_proposal_keeps_other_conditions(self, menu):
        req = make_request(constraints=[_budget(3000), _staple("B001"), _need_drink(), _no_fries()])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        assert result.plans
        # 每个方案必须满足除「被修改条件」以外的全部原条件
        for plan in result.plans:
            changed = {pc.constraint_id for pc in plan.proposed_changes}
            cats = {e["category"] for e in plan.expanded.entries}
            names = [e["name"] for e in plan.expanded.entries]
            if "no_fries" not in changed:
                assert "fries" not in cats, "妥协方案也不得违反锁定条件"
            if "need_drink" not in changed:
                assert "drink" in cats
            if "staple" not in changed:
                assert "巨无霸" in names
            if "budget" not in changed:
                assert plan.quote.price_fen <= 3000
        # 预算方向的方案：改预算、保留巨无霸/饮料/无薯条
        budget_plans = [p for p in result.plans if any(pc.constraint_id == "budget" for pc in p.proposed_changes)]
        assert budget_plans, "应提供预算修改建议"
        for p in budget_plans:
            names = [e["name"] for e in p.expanded.entries]
            assert "巨无霸" in names
            cats = {e["category"] for e in p.expanded.entries}
            assert "drink" in cats and "fries" not in cats

    def test_no_budget_proposal_when_locked(self, menu):
        req = make_request(constraints=[_budget(3000, locked=True), _staple("B001"), _need_drink()])
        result, _ = run_until_final(menu, req)
        assert result.status in (PlanStatus.COMPROMISE_PROPOSED, PlanStatus.NOT_FOUND)
        for plan in result.plans:
            assert all(pc.constraint_id != "budget" for pc in plan.proposed_changes), "锁定预算不得提出修改"
            assert plan.quote.price_fen <= 3000, "锁定预算下不得推荐超预算方案"

    def test_staple_change_proposal(self, menu):
        # 预算锁 30、指定巨无霸（可讨论）、要饮料：换主食方向
        req = make_request(constraints=[_budget(3000, locked=True), _staple("B001"), _need_drink()])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        staple_plans = [p for p in result.plans if any(pc.constraint_id == "staple" for pc in p.proposed_changes)]
        assert staple_plans, "应提供主食替换建议"
        desc = staple_plans[0].proposed_changes[0].description
        assert "换为" in desc
        assert staple_plans[0].quote.price_fen <= 3000


class TestAllLocked:
    def test_not_found_with_locked_explanation(self, menu):
        req = make_request(
            constraints=[
                _budget(2500, locked=True),
                _staple("B001", locked=True),
                _need_drink(),
                _no_fries(locked=True),
            ]
        )
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.NOT_FOUND
        assert result.plans == []
        assert any("锁定条件" in n for n in result.notes)
        assert any("不等于证明无解" in n or "未找到" in n for n in result.notes)

    def test_not_found_reports_closest_options(self, menu):
        # 全锁定且无解时，必须给出「最近失配」的已核验参考，并说明差在哪
        req = make_request(
            constraints=[
                _budget(2500, locked=True),
                _staple("B001", locked=True),
                _need_drink(),
                _no_fries(locked=True),
            ]
        )
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.NOT_FOUND
        assert result.closest_options, "not_found 时应给出最近失配的参考选项"
        for opt in result.closest_options:
            assert not opt.verified, "参考选项不是可接受方案"
            assert opt.violated, "参考选项必须展示违反的条件"
            assert all(v.state == EvalState.VIOLATED for v in opt.violated)
        # 最近失配：巨无霸+饮料只违反锁定预算，其余条件全部满足
        first = result.closest_options[0]
        assert first.quote.price_fen == 3500
        assert [v.constraint_id for v in first.violated] == ["budget"]
        assert any(s.constraint_id == "staple" for s in first.satisfied)
        # 违反模式两两不同（每条代价路径只取最优），且按（锁定违反数 → 总违反数）升序
        patterns = [frozenset(v.constraint_id for v in o.violated) for o in result.closest_options]
        assert len(set(patterns)) == len(patterns), "同一违反模式只应出现一次"
        locked_ids = {"budget", "staple", "no_fries"}
        counts = [
            (sum(1 for c in p if c in locked_ids), len(p))
            for p in patterns
        ]
        assert counts == sorted(counts)
        assert any("最接近" in n for n in result.notes)
        # 序列化字段齐全
        d = result.to_dict()
        assert len(d["closestOptions"]) == len(result.closest_options)
        assert d["closestOptions"][0]["violatedConstraints"]


class TestQuoteFailures:
    def test_all_quotes_failed_is_data_incomplete(self, menu):
        def always_fail(cand, m):
            return {"success": False, "code": 500, "message": "internal error"}

        req = make_request(constraints=[_budget(3000)])
        result, _ = run_until_final(menu, req, quote_fn=always_fail)
        assert result.status == PlanStatus.DATA_INCOMPLETE
        assert any("暂时无法验证" in n for n in result.notes)

    def test_invalid_quote_not_treated_as_free(self, menu):
        def invalid(cand, m):
            return {"success": True, "code": 200, "data": {"price": 0, "originalPrice": 0, "discount": 0, "productList": []}}

        req = make_request(constraints=[_budget(3000)])
        result, _ = run_until_final(menu, req, quote_fn=invalid)
        assert result.status == PlanStatus.DATA_INCOMPLETE
        assert not any(p.verified for p in result.plans)

    def test_partial_failure_keeps_verified(self, menu):
        def fail_some(cand, m):
            if any(it.product_code.startswith("C0") for it in cand.items):
                return {"success": False, "code": 500, "message": "timeout"}
            return __import__("conftest").fake_calculate_price(cand, m)

        req = make_request(constraints=[_budget(3700), _staple("staple_beef"), _need_drink(), _no_fries()])
        result, _ = run_until_final(menu, req, quote_fn=fail_some)
        assert result.status == PlanStatus.FEASIBLE
        assert result.coverage.failed_count > 0
        # 失败的候选不得进入已核验列表
        for plan in result.plans:
            assert plan.quote.ok


class TestBudgetCap:
    def test_quote_cap_reports_incomplete_search(self, menu):
        budget = QuoteBudget(first_batch=1, round_budget=1)
        req = make_request(constraints=[_budget(2500), _staple("B001", locked=True), _need_drink()])
        result, _ = run_until_final(menu, req, budget=budget)
        assert result.status in (PlanStatus.NOT_FOUND, PlanStatus.DATA_INCOMPLETE, PlanStatus.COMPROMISE_PROPOSED)
        assert result.coverage.truncated or result.coverage.generated_count > result.coverage.quoted_count
        # 达到报价上限时不得宣称全局最优或绝无解
        joined = " ".join(result.notes)
        assert ("不完整" in joined) or ("不等于证明无解" in joined) or ("暂时无法验证" in joined)


class TestMultiRound:
    def test_user_changes_only_drink(self, menu):
        v1 = make_request(
            version=1,
            constraints=[_budget(3700), _staple("staple_beef"), _need_drink(), _no_fries()],
        )
        result1, _ = run_until_final(menu, v1)
        assert result1.status == PlanStatus.FEASIBLE

        # 用户追加「不要可乐」：只新增一条约束，其余原样
        v2 = make_request(
            version=2,
            constraints=[
                _budget(3700),
                _staple("staple_beef"),
                _need_drink(),
                _no_fries(),
                Constraint(
                    id="no_cola",
                    kind=ConstraintKind.EXCLUDE_ITEM,
                    value="D001",
                    negotiability=Negotiability.LOCKED,
                    priority=20,
                    source_text="不要可乐",
                ),
            ],
        )
        result2, _ = run_until_final(menu, v2)
        assert result2.request_version == 2
        assert result2.status == PlanStatus.FEASIBLE
        for plan in result2.plans:
            names = [e["name"] for e in plan.expanded.entries]
            assert "可乐中杯" not in names
            assert "雪碧中杯" in names or "纯牛奶" in names  # 饮料要求保留
            cats = {e["category"] for e in plan.expanded.entries}
            assert "fries" not in cats
            assert plan.quote.price_fen <= 3700

    def test_plans_carry_request_version(self, menu):
        req = make_request(version=7, constraints=[_budget(3700)])
        result, _ = run_until_final(menu, req)
        assert result.request_version == 7
        for plan in result.plans:
            assert plan is not None
        # 方案与版本绑定：v7 的方案不能在 v1 下被误用
        assert result.to_dict()["requestVersion"] == 7

    def test_old_version_plan_requires_requote(self, menu):
        # 新版本重新规划时使用新的报价登记（旧报价不跨版本复用）
        v1 = make_request(version=1, constraints=[_budget(3700)])
        r1, q1 = run_until_final(menu, v1)
        assert r1.status == PlanStatus.FEASIBLE
        v2 = make_request(version=2, constraints=[_budget(3700), _no_fries()])
        r2, q2 = run_until_final(menu, v2)
        assert q2 is not q1
        assert r2.request_version == 2


class TestDeterminism:
    def test_repeated_runs_identical(self, menu):
        req = make_request(constraints=[_budget(3700), _staple("staple_beef"), _need_drink(), _no_fries()])
        r1, _ = run_until_final(menu, req)
        r2, _ = run_until_final(menu, req)
        keys1 = [p.items[0].product_code for p in r1.plans]
        keys2 = [p.items[0].product_code for p in r2.plans]
        assert keys1 == keys2

    def test_generation_order_independent(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = make_request(constraints=[_budget(3700)])
        a = generate_candidates(menu, req, 64)
        b = generate_candidates(menu, req, 64)
        assert [c.key for c in a] == [c.key for c in b]


class TestDuplicateCandidates:
    def test_same_candidate_quoted_once(self, menu):
        from mcd_compromise.candidates import generate_candidates
        from mcd_compromise.quote_state import QuoteManager

        req = make_request(constraints=[_budget(3700)])
        cands = generate_candidates(menu, req, 64)
        mgr = QuoteManager(QuoteBudget(first_batch=64, round_budget=64))
        first = mgr.schedule(cands)
        assert len(first) == len(cands)
        # 宿主完成报价后，同一候选重复出现不得再排程
        for c in first:
            mgr.register_mcp_result(c.key, {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        second = mgr.schedule(cands + cands)
        assert second == []
        assert len(mgr.quoted_keys()) == len(cands)
        assert mgr.stats()["total"] == len(cands)


class TestCouponVariants:
    def test_coupon_products_only_with_coupon_ref(self, menu):
        # 回归：券商品不得作为普通单品生成（不带券官方返回空清单，曾被误判为 0 元）
        from mcd_compromise.candidates import generate_candidates

        req = make_request(constraints=[_budget(5000)])
        cands = generate_candidates(menu, req, 200)
        for c in cands:
            for it in c.items:
                if it.product_code in ("X001", "X002"):
                    assert it.coupon_id, f"券商品 {it.product_code} 必须携带券引用"

    def test_coupon_variant_quoted_with_coupon(self, menu):
        from mcd_compromise.candidates import generate_candidates

        req = make_request(constraints=[_budget(5000)])
        cands = generate_candidates(menu, req, 200)
        coupon_cands = [c for c in cands if any(i.coupon_id for i in c.items)]
        assert coupon_cands, "应生成携带券引用的优惠变体"
        names = {e["name"] for c in coupon_cands for e in expand_candidate(c, menu).entries}
        assert "王牌小食拼" in names

    def test_prefer_coupon_soft_ranks_coupon_plan_first(self, menu):
        from mcd_compromise.constraints import expand_candidate

        req = make_request(
            constraints=[
                _budget(5000),
                Constraint(id="prefer", kind=ConstraintKind.PREFER_COUPON, value=True, strength=Strength.SOFT, source_text="优先用券"),
            ]
        )
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.FEASIBLE
        # 软偏好不改变可行性，但同样价格下优先用券的方案排前
        prices = [(p.quote.price_fen, any(i.coupon_id for i in p.items)) for p in result.plans]
        assert prices == sorted(prices, key=lambda t: (t[0], not t[1]))


class TestNutrition:
    def test_missing_nutrition_is_unknown_not_satisfied(self, menu):
        req = make_request(
            constraints=[
                Constraint(
                    id="energy",
                    kind=ConstraintKind.ENERGY_MAX,
                    value=500,
                    source_text="热量不超过500大卡",
                )
            ]
        )
        result, _ = run_until_final(menu, req)  # 不提供营养数据
        assert result.status == PlanStatus.DATA_INCOMPLETE
        assert not any(p.verified for p in result.plans)

    def test_nutrition_map_parsing(self):
        from mcd_compromise.nutrition import NutritionMap

        # 与官方 list-nutrition-foods 相同的 9 列 CSV 风格文本
        text = (
            "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
            "  巨无霸,null,2146,513,27,26,42,961,171\n"
            "  中薯条,null,1210,289,4,12,38,165,18\n"
        )
        nm = NutritionMap.from_mcp_response({"data": text})
        assert nm.lookup("巨无霸")["energy_kcal"] == 513
        assert nm.lookup("巨无霸")["protein"] == 27
        assert nm.lookup("中薯条")["energy_kcal"] == 289
        assert nm.lookup("不存在的餐品") is None
