"""最小妥协算法端到端测试（对应实现路径文档第 11 节验收表）。"""

from __future__ import annotations

from conftest import fake_calculate_price, make_request, run_until_final

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

    def test_nutrition_map_parses_real_sample(self):
        """真实 list-nutrition-foods 全量样本（examples/real/nutrition-foods.json）可被完整解析。"""
        import json
        from pathlib import Path

        from mcd_compromise.nutrition import NutritionMap

        sample_path = Path(__file__).resolve().parents[1] / "examples" / "real" / "nutrition-foods.json"
        sample = json.loads(sample_path.read_text(encoding="utf-8"))
        nm = NutritionMap.from_mcp_response(sample["response"])
        # 160 行数据、2 组重名（纯牛奶（盒装）/小杯玉米杯）→ 158 个唯一名称
        assert len(nm._by_name) == 158
        assert nm.lookup("巨无霸")["energy_kcal"] == 513
        assert nm.lookup("中薯条")["energy_kcal"] == 289
        assert nm.lookup("可乐中杯")["energy_kcal"] == 147
        # 0 是真实零值（无糖可口可乐/纯悦），必须解析为 0 而不是缺失
        assert nm.lookup("纯悦")["energy_kcal"] == 0
        assert nm.lookup("无糖可口可乐中杯")["energy_kcal"] == 0
        assert nm.lookup("无糖可口可乐中杯")["carbohydrate"] == 1
        # 全角/半角括号不失配为「同一名称」：营养表只有「纯牛奶（盒装）」，查「纯牛奶」必须查不到
        assert nm.lookup("纯牛奶") is None
        assert nm.lookup("纯牛奶（盒装）") is not None

    def test_totals_for_falls_back_to_official_quote_name(self):
        """菜单单品名缺规格（「雪碧」）时，官方报价名（「雪碧小杯」）可精确补配。"""
        from mcd_compromise.models import ExpandedContent
        from mcd_compromise.nutrition import NutritionMap

        text = "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n  巨无霸,null,2146,513,27,26,42,961,171\n  雪碧小杯,null,319,76,0,0,19,21,0\n"
        nm = NutritionMap.from_mcp_response({"data": text})
        expanded = ExpandedContent(
            entries=[
                {"code": "B1", "name": "巨无霸", "category": "staple_beef", "quantity": 1},
                {"code": "D1", "name": "雪碧", "category": "drink", "quantity": 1},
            ]
        )
        # 仅按菜单名：雪碧无规格 → 营养数据不完整（不当零）
        assert nm.totals_for(expanded) is None
        # 官方报价名回退：仍是精确匹配（3010→「雪碧小杯」76 kcal），合计 589
        totals = nm.totals_for(expanded, {"D1": "雪碧小杯"})
        assert totals["energy_kcal"] == 513 + 76
        # 回退名也查不到时仍然返回 None（不猜）
        assert nm.totals_for(expanded, {"D1": "雪碧大杯"}) is None

    def test_energy_eval_uses_official_quote_name(self):
        """评价集成：报价携带官方规格名时，热量条件由 UNKNOWN 变为可判定。"""
        from mcd_compromise.constraints import evaluate_candidate, expand_candidate
        from mcd_compromise.models import (
            Candidate,
            Constraint,
            ConstraintKind,
            MealRequest,
            OrderItem,
            Quote,
            QuoteStatus,
            RequestContext,
        )
        from mcd_compromise.normalize import build_normalized_menu
        from mcd_compromise.nutrition import NutritionMap

        # 本地构造迷你菜单：复刻真实联调观察——菜单单品名「雪碧」无规格
        menu = build_normalized_menu(
            store_id="T",
            menu_response={
                "data": {
                    "categories": [],
                    "meals": {
                        "B1": {"name": "巨无霸", "currentPrice": "25.5"},
                        "D1": {"name": "雪碧", "currentPrice": "9.5"},
                    },
                }
            },
            detail_responses=[],
        )
        nm = NutritionMap.from_mcp_response(
            {
                "data": (
                    "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                    "  巨无霸,null,2146,513,27,26,42,961,171\n"
                    "  雪碧小杯,null,319,76,0,0,19,21,0\n"
                )
            }
        )
        cand = Candidate(
            key="k1",
            items=(OrderItem(product_code="B1", quantity=1), OrderItem(product_code="D1", quantity=1)),
        )
        expanded = expand_candidate(cand, menu)
        req = MealRequest(
            request_version=1,
            context=RequestContext(store_id="T"),
            constraints=[Constraint(id="energy", kind=ConstraintKind.ENERGY_MAX, value=600, source_text="热量不超过600大卡")],
        )
        # 无报价（或报价无官方名）：雪碧失配 → 热量 UNKNOWN，不当作满足也不当作违反
        ev = evaluate_candidate(cand, expanded, None, req, nm)
        assert [u.constraint_id for u in ev.unknowns] == ["energy"]
        assert not ev.violations
        # 官方报价名「雪碧小杯」：513+76=589 ≤ 600 → 满足，证据带真实汇总值
        quote = Quote(
            candidate_key="k1",
            status=QuoteStatus.OK,
            price_fen=3500,
            product_code_names=(("B1", "巨无霸"), ("D1", "雪碧小杯")),
        )
        ev2 = evaluate_candidate(cand, expanded, quote, req, nm)
        assert not ev2.unknowns
        assert ev2.hard_pass
        assert ev2.satisfied[0].actual == 589
        assert "589" in ev2.satisfied[0].evidence

    def test_closest_options_excludes_unknown_candidates(self, menu):
        """not_found 的最近失配只收完全已知候选：热量 UNKNOWN 的候选不得冒充「最接近」。"""
        from mcd_compromise.compromise import plan_next
        from mcd_compromise.models import Constraint, ConstraintKind, Negotiability, PlanStatus
        from mcd_compromise.nutrition import NutritionMap
        from mcd_compromise.quote_state import QuoteBudget, QuoteManager

        # 只提供巨无霸的营养：其余餐品的热量一律 UNKNOWN
        nm = NutritionMap.from_mcp_response(
            {
                "data": (
                    "[1]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                    "  巨无霸,null,2146,513,27,26,42,961,171\n"
                )
            }
        )
        req = make_request(
            constraints=[
                _staple("B001"),
                Constraint(
                    id="energy",
                    kind=ConstraintKind.ENERGY_MAX,
                    value=400,
                    negotiability=Negotiability.LOCKED,
                    source_text="热量不超过400大卡",
                ),
            ]
        )
        budget = QuoteBudget(first_batch=8, round_budget=24)
        quotes = QuoteManager(budget)
        result = None
        for _ in range(12):
            result = plan_next(menu, req, quotes, budget=budget, nutrition=nm)
            if result.status != PlanStatus.QUOTES_PENDING:
                break
            for cand in result.pending_quotes:
                quotes.register_mcp_result(cand.key, fake_calculate_price(cand, menu), quoted_at="2026-10-13 15:00:00")
        assert result.status == PlanStatus.NOT_FOUND
        # 唯一完全已知的候选是「巨无霸」单品（513 > 400 违反锁定的热量条件）
        assert len(result.closest_options) == 1
        only = result.closest_options[0]
        assert [e["code"] for e in only.expanded.entries] == ["B001"]
        assert [v.constraint_id for v in only.violated] == ["energy"]
        # 含未知条件的候选（如巨无霸+饮料）不出现在最近失配里
        for opt in result.closest_options:
            assert not opt.unknowns

    def test_nutrition_proposal_describes_actual_value(self, menu):
        """联调场景 B 复现：蛋白质下限冲突时，修改建议须带该方案实测值。

        真实联调（2026-10-13，门店 1950640）发现：protein_min 造成违反时，
        妥协描述曾退化为笼统的「调整条件 protein」；修复后应按预算方向的
        同一模式写成「阈值调到该方案实测值」的最小改动。
        """
        from mcd_compromise.compromise import plan_next
        from mcd_compromise.models import Constraint, ConstraintKind, Negotiability, PlanStatus
        from mcd_compromise.nutrition import NutritionMap
        from mcd_compromise.quote_state import QuoteManager

        nm = NutritionMap.from_mcp_response(
            {
                "data": (
                    "[4]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                    "  巨无霸,null,2146,513,27,26,42,961,171\n"
                    "  中薯条,null,1210,289,4,12,38,165,18\n"
                    "  可乐中杯,null,616,147,0,0,36,0,10\n"
                    "  小杯玉米杯,null,223,53,2,1,7,1,6\n"
                )
            }
        )
        # 与场景 B 同构：巨无霸单品蛋白质 27g 不达 30g；三件套 31g 达标但 36.5 元超 33 元预算
        req = make_request(
            constraints=[
                _staple("B001", locked=True),
                Constraint(
                    id="protein",
                    kind=ConstraintKind.PROTEIN_MIN,
                    value=30,
                    negotiability=Negotiability.ASK,
                    priority=60,
                    source_text="蛋白质不低于30克",
                ),
                Constraint(
                    id="energy",
                    kind=ConstraintKind.ENERGY_MAX,
                    value=1000,
                    negotiability=Negotiability.ASK,
                    priority=55,
                    source_text="热量不超过1000大卡",
                ),
                _budget(3300),
            ]
        )
        quotes = QuoteManager()
        result = None
        for _ in range(12):
            result = plan_next(menu, req, quotes, nutrition=nm)
            if result.status != PlanStatus.QUOTES_PENDING:
                break
            for cand in result.pending_quotes:
                quotes.register_mcp_result(cand.key, fake_calculate_price(cand, menu), quoted_at="2026-10-13 15:00:00")
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        protein_changes = [c for p in result.plans for c in p.proposed_changes if c.constraint_id == "protein"]
        assert protein_changes, "应给出蛋白质方向的修改建议"
        change = protein_changes[0]
        # 巨无霸单品实测 27g：最小改动 = 下限降到 27g，幅度 3g
        assert "30" in change.description and "27" in change.description
        assert change.magnitude == 3
        # 预算方向描述不受影响（仍为「预算从 33.00 元提高到 36.50 元」）
        budget_changes = [c for p in result.plans for c in p.proposed_changes if c.constraint_id == "budget"]
        assert budget_changes, "应给出预算方向的修改建议"
        assert "33.00" in budget_changes[0].description and "36.50" in budget_changes[0].description
        assert budget_changes[0].magnitude == 350

    def test_fat_max_eval(self):
        """脂肪上限评价：实测汇总判定（含单位），缺失记 unknown 不当零。"""
        from mcd_compromise.constraints import evaluate_candidate, expand_candidate
        from mcd_compromise.models import (
            Candidate,
            Constraint,
            ConstraintKind,
            MealRequest,
            OrderItem,
            Quote,
            QuoteStatus,
            RequestContext,
        )
        from mcd_compromise.normalize import build_normalized_menu
        from mcd_compromise.nutrition import NutritionMap

        # 本地构造迷你菜单：复刻真实联调观察——菜单单品名「雪碧」无规格
        menu = build_normalized_menu(
            store_id="T",
            menu_response={
                "data": {
                    "categories": [],
                    "meals": {
                        "B1": {"name": "巨无霸", "currentPrice": "25.5"},
                        "D1": {"name": "雪碧", "currentPrice": "9.5"},
                    },
                }
            },
            detail_responses=[],
        )
        nm = NutritionMap.from_mcp_response(
            {
                "data": (
                    "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                    "  巨无霸,null,2146,513,27,26,42,961,171\n"
                    "  雪碧小杯,null,319,76,0,0,19,21,0\n"
                )
            }
        )
        cand = Candidate(
            key="k1",
            items=(OrderItem(product_code="B1", quantity=1), OrderItem(product_code="D1", quantity=1)),
        )
        expanded = expand_candidate(cand, menu)
        quote = Quote(
            candidate_key="k1",
            status=QuoteStatus.OK,
            price_fen=3500,
            product_code_names=(("B1", "巨无霸"), ("D1", "雪碧小杯")),
        )
        # 26+0=26 g：上限 30 满足（证据带实测值与 g 单位）
        req_ok = MealRequest(
            request_version=1,
            context=RequestContext(store_id="T"),
            constraints=[Constraint(id="fat", kind=ConstraintKind.FAT_MAX, value=30, source_text="脂肪不超过30克")],
        )
        ev = evaluate_candidate(cand, expanded, quote, req_ok, nm)
        assert ev.hard_pass and not ev.unknowns
        assert ev.satisfied[0].actual == 26
        assert "26" in ev.satisfied[0].evidence and "g" in ev.satisfied[0].evidence
        # 上限 20 → 违反，evidence 带实测值
        req_bad = MealRequest(
            request_version=1,
            context=RequestContext(store_id="T"),
            constraints=[Constraint(id="fat", kind=ConstraintKind.FAT_MAX, value=20, source_text="脂肪不超过20克")],
        )
        ev2 = evaluate_candidate(cand, expanded, quote, req_bad, nm)
        assert [v.constraint_id for v in ev2.violations] == ["fat"]
        assert ev2.violations[0].actual == 26
        assert "26" in ev2.violations[0].evidence
        # 无报价官方名（雪碧失配）→ unknown：不当作满足也不当作违反
        ev3 = evaluate_candidate(cand, expanded, None, req_ok, nm)
        assert [u.constraint_id for u in ev3.unknowns] == ["fat"]
        assert not ev3.violations

    def test_fat_max_proposal_describes_actual_value(self, menu):
        """脂肪上限冲突时，修改建议按该方案实测值写最小改动（与蛋白质方向同一模式）。"""
        from mcd_compromise.compromise import plan_next
        from mcd_compromise.models import Constraint, ConstraintKind, Negotiability, PlanStatus
        from mcd_compromise.nutrition import NutritionMap
        from mcd_compromise.quote_state import QuoteManager

        nm = NutritionMap.from_mcp_response(
            {
                "data": (
                    "[4]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
                    "  巨无霸,null,2146,513,27,26,42,961,171\n"
                    "  中薯条,null,1210,289,4,12,38,165,18\n"
                    "  可乐中杯,null,616,147,0,0,36,0,10\n"
                    "  小杯玉米杯,null,223,53,2,1,7,1,6\n"
                )
            }
        )
        # 巨无霸单品脂肪 26g 超过上限 20g；所有候选都含巨无霸 → 全部违反脂肪条件
        req = make_request(
            constraints=[
                _staple("B001", locked=True),
                Constraint(
                    id="fat",
                    kind=ConstraintKind.FAT_MAX,
                    value=20,
                    negotiability=Negotiability.ASK,
                    priority=60,
                    source_text="脂肪不超过20克",
                ),
                _budget(3300),
            ]
        )
        quotes = QuoteManager()
        result = None
        for _ in range(12):
            result = plan_next(menu, req, quotes, nutrition=nm)
            if result.status != PlanStatus.QUOTES_PENDING:
                break
            for cand in result.pending_quotes:
                quotes.register_mcp_result(cand.key, fake_calculate_price(cand, menu), quoted_at="2026-10-13 15:00:00")
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        fat_changes = [c for p in result.plans for c in p.proposed_changes if c.constraint_id == "fat"]
        assert fat_changes, "应给出脂肪方向的修改建议"
        change = fat_changes[0]
        # 巨无霸单品实测 26g：最小改动 = 上限提高到 26g，幅度 6g
        assert "20" in change.description and "26" in change.description
        assert change.magnitude == 6
        # 提案即巨无霸单品（25.50 元在预算内，其余条件保留）
        plan = next(p for p in result.plans if any(c.constraint_id == "fat" for c in p.proposed_changes))
        assert [e["code"] for e in plan.expanded.entries] == ["B001"]


class TestProposalConsistencyAndAcceptance:
    """发布前收尾：结构化新值一致性 / 换主食不退化 / 接受后需求状态。"""

    def test_describe_change_modified_value_contract(self):
        """Task1：_describe_change 的 modifiedValue 必须与文字描述/幅度一致，覆盖各约束类型。

        历史缺陷：C1 文字写「预算从 12.00 提高到 13.90」但 modifiedValue 仍是旧值 1200。
        """
        from mcd_compromise.compromise import _describe_change
        from mcd_compromise.models import Constraint, ConstraintKind, Negotiability

        # 预算：modifiedValue = 试算价（非旧值），幅度 = 差价
        c = Constraint(id="budget", kind=ConstraintKind.TOTAL_PRICE_MAX, value=1200,
                       negotiability=Negotiability.ASK, source_text="预算12元")
        desc, mag, mv = _describe_change(c, candidate_price=1390)
        assert mv == 1390 and mag == 190
        assert "12.00 元" in desc and "13.90 元" in desc

        # 营养阈值：modifiedValue = 该方案实测值
        c = Constraint(id="protein", kind=ConstraintKind.PROTEIN_MIN, value=30, negotiability=Negotiability.ASK)
        desc, mag, mv = _describe_change(c, actual_value=27)
        assert mv == 27 and mag == 3 and "27" in desc
        c = Constraint(id="energy", kind=ConstraintKind.ENERGY_MAX, value=1000, negotiability=Negotiability.ASK)
        desc, mag, mv = _describe_change(c, actual_value=1050)
        assert mv == 1050 and mag == 50
        c = Constraint(id="fat", kind=ConstraintKind.FAT_MAX, value=20, negotiability=Negotiability.ASK)
        desc, mag, mv = _describe_change(c, actual_value=26)
        assert mv == 26 and mag == 6

        # 换主食：modifiedValue = 替代主食品类；描述用具体餐品名，不再回退成原始 staple_beef
        c = Constraint(id="staple", kind=ConstraintKind.INCLUDE_STAPLE, value="staple_beef",
                       negotiability=Negotiability.ASK, source_text="要牛肉汉堡")
        desc, mag, mv = _describe_change(c, alt_name="麦辣鸡腿汉堡", alt_category="staple_chicken")
        assert mv == "staple_chicken" and "麦辣鸡腿汉堡" in desc and "staple_beef" not in desc
        # 无 alt_name 时回退「其他主食」，绝不回退成原始 staple_beef
        desc, mag, mv = _describe_change(c)
        assert "其他主食" in desc and "staple_beef" not in desc and mv is None

        # 取消类：modifiedValue = None（表示接受时移除约束）
        c = Constraint(id="nd", kind=ConstraintKind.REQUIRE_CATEGORY, value="drink", negotiability=Negotiability.ASK)
        _, _, mv = _describe_change(c)
        assert mv is None
        c = Constraint(id="nf", kind=ConstraintKind.EXCLUDE_CATEGORY, value="fries", negotiability=Negotiability.ASK)
        _, _, mv = _describe_change(c)
        assert mv is None

    def test_budget_proposal_modified_value_is_new_price(self, menu):
        """Task1（集成）：预算方向的 modifiedValue = 该方案试算价，与描述一致。"""
        from mcd_compromise.models import fen_to_yuan

        req = make_request(constraints=[_budget(3000), _staple("B001"), _need_drink()])
        result, _ = run_until_final(menu, req)
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        budget_plans = [p for p in result.plans if any(pc.constraint_id == "budget" for pc in p.proposed_changes)]
        assert budget_plans
        for p in budget_plans:
            for pc in p.proposed_changes:
                if pc.constraint_id != "budget":
                    continue
                assert pc.modified_value == p.quote.price_fen, "modifiedValue 应等于试算价而非旧预算"
                assert pc.modified_value != 3000
                assert pc.magnitude == p.quote.price_fen - 3000
                assert fen_to_yuan(pc.modified_value) in pc.description

    def test_staple_relaxation_keeps_a_staple(self):
        """Task2：放宽指定主食须换成「其他主食」，不得默默退化为只有饮料的候选。

        合成菜单含一个「饮料券单品」（便宜、无主食），它会在放宽主食后骗取
        「已满足」；修复后换主食方向必须仍含至少一种主食。
        """
        from mcd_compromise.models import Coupon, MenuItem, NormalizedMenu

        menu = NormalizedMenu(
            store_id="T-STAPLE",
            items={
                "BEEF": MenuItem("BEEF", "牛肉汉堡", 2000, 2000, category="staple_beef"),
                "CHICK": MenuItem("CHICK", "鸡肉汉堡", 900, 900, category="staple_chicken"),
                "COLA": MenuItem("COLA", "可乐中杯", 600, 600, category="drink"),
                # 饮料券单品：带「券商品」标签，只经优惠变体生成（便宜，诱导「只有饮料」）
                "DCOLA": MenuItem("DCOLA", "可乐(券)", 300, 600, category="drink", tags=("券商品",)),
            },
            combos={},
            coupons=[Coupon("CP-DCOLA", "饮料券", ("DCOLA",), coupon_code="CC-DCOLA")],
        )

        def quote_fn(cand, m):
            for it in cand.items:
                if it.coupon_id and it.product_code == "DCOLA":
                    return {"success": True, "code": 200, "data": {
                        "price": 300, "originalPrice": 600, "discount": 300,
                        "productList": [{"productCode": "DCOLA", "productName": "可乐(券)",
                                         "quantity": 1, "subtotal": 300, "originalSubtotal": 600}]}}
            return fake_calculate_price(cand, m)

        # 预算 16、指定牛肉堡、要饮料、不要薯条：牛肉+可乐 26 超预算；鸡肉+可乐 15 可行
        req = make_request(constraints=[_budget(1600), _staple("staple_beef"), _need_drink(), _no_fries()])
        result, _ = run_until_final(menu, req, quote_fn=quote_fn)
        assert result.status == PlanStatus.COMPROMISE_PROPOSED
        staple_plans = [p for p in result.plans if any(pc.constraint_id == "staple" for pc in p.proposed_changes)]
        assert staple_plans, "应提供换主食方向的建议"
        for p in staple_plans:
            cats = {e["category"] for e in p.expanded.entries}
            names = [e["name"] for e in p.expanded.entries]
            assert any(c.startswith("staple") for c in cats), "放宽主食不得退化为无主食/只有饮料"
            assert "drink" in cats, "换主食方向仍应满足饮料要求"
            assert "牛肉汉堡" not in names, "换主食方向不应仍是指定牛肉堡"
            for pc in p.proposed_changes:
                if pc.constraint_id == "staple":
                    assert pc.modified_value and pc.modified_value.startswith("staple_") and pc.modified_value != "staple_beef"

    def test_apply_accepted_change_budget(self, menu):
        """Task3：接受预算提议 → 版本+1、只改预算值、保留原 negotiability。"""
        from mcd_compromise.compromise import apply_accepted_change
        from mcd_compromise.models import ProposedChange

        req = make_request(version=1, constraints=[_budget(1200), _staple("staple_beef"), _need_drink(), _no_fries()])
        change = ProposedChange(constraint_id="budget",
                                description="预算从 12.00 元 提高到 13.90 元（按试算价）",
                                modified_value=1390, magnitude=190)
        new = apply_accepted_change(req, change)
        assert new.request_version == 2
        assert new.constraint("budget").value == 1390
        # 未显式要求锁定 → 保留原 negotiability（ask）
        assert new.constraint("budget").negotiability == Negotiability.ASK
        # 其余条件原样保留
        assert new.constraint("staple").value == "staple_beef"
        assert new.constraint("need_drink") is not None
        assert new.constraint("no_fries").negotiability == Negotiability.LOCKED
        # 原需求对象不被修改
        assert req.request_version == 1 and req.constraint("budget").value == 1200

    def test_apply_accepted_change_removal(self):
        """Task3：接受「取消类」改变（modified_value=None）→ 移除该约束、版本+1。"""
        from mcd_compromise.compromise import apply_accepted_change
        from mcd_compromise.models import ProposedChange

        req = make_request(version=3, constraints=[_budget(1200), _need_drink()])
        change = ProposedChange(constraint_id="need_drink",
                                description="取消「必须包含drink」的要求", modified_value=None, magnitude=None)
        new = apply_accepted_change(req, change)
        assert new.request_version == 4
        assert new.constraint("need_drink") is None
        assert new.constraint("budget").value == 1200
