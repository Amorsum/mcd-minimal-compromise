"""规划编排：两阶段报价协议与最小妥协算法。

流程（实现路径文档 4 / 6）：
  生成候选 → 报价前剪除 → 安排试算 →（宿主报价）→ 条件评价 →
  有满足全部硬条件的已核验方案 → feasible；
  否则对允许讨论的条件构建修改请求，在相同基础菜单空间重新生成候选并试算 →
  按可解释顺序输出待接受的妥协方案。

协议：plan_next() 可重复调用。返回 QUOTES_PENDING 时，宿主对 pendingQuotes
调用官方 calculate-price，把报价写回输入后再次调用，直至得到终态。
"""

from __future__ import annotations

import copy
import itertools
from typing import Optional

from .candidates import (
    DEFAULT_MAX_CANDIDATES,
    generate_candidates,
    search_scope_dict,
)
from .constraints import evaluate_candidate, expand_candidate, precheck_candidate
from .models import (
    Candidate,
    CandidateEvaluation,
    ConditionOutcome,
    Constraint,
    ConstraintKind,
    Coverage,
    EvalState,
    MealRequest,
    Negotiability,
    NormalizedMenu,
    Plan,
    PlanResult,
    PlanStatus,
    ProposedChange,
    Quote,
    RequestContext,
    Strength,
    fen_to_yuan,
)
from .quote_state import QuoteBudget, QuoteManager

MAX_PLANS = 3
MAX_PAIR_DIRECTIONS = 6


# ---------------------------------------------------------------------------
# 需求变换
# ---------------------------------------------------------------------------

def relax_constraint(request: MealRequest, constraint_id: str) -> MealRequest:
    """复制需求并移除一条约束（用于生成妥协方向）。"""
    new = MealRequest(
        request_version=request.request_version,
        context=RequestContext(request.context.store_id, request.context.dining_mode, request.context.people),
        constraints=[c for c in request.constraints if c.id != constraint_id],
    )
    return new


def relax_all_negotiable(request: MealRequest) -> MealRequest:
    """移除全部「可讨论」的硬约束，得到基础菜单空间（锁定约束始终保留）。"""
    keep = [
        c
        for c in request.constraints
        if c.strength == Strength.SOFT or c.negotiability == Negotiability.LOCKED
    ]
    return MealRequest(
        request_version=request.request_version,
        context=RequestContext(request.context.store_id, request.context.dining_mode, request.context.people),
        constraints=keep,
    )


def negotiable_hard_constraints(request: MealRequest) -> list[Constraint]:
    """可提出修改建议的硬约束（锁定与偏好除外）。"""
    return [
        c
        for c in request.constraints
        if c.strength == Strength.HARD and c.negotiability != Negotiability.LOCKED
    ]


# ---------------------------------------------------------------------------
# 候选池构建
# ---------------------------------------------------------------------------

def build_candidate_pool(menu: NormalizedMenu, request: MealRequest, max_candidates: int) -> tuple[list[Candidate], int]:
    """原始需求候选 + 放松可讨论条件后的基础空间候选（A 在前，保证优先试算）。"""
    pool_a = generate_candidates(menu, request, max_candidates)
    relaxed = relax_all_negotiable(request)
    pool_b = generate_candidates(menu, relaxed, max_candidates)
    pool: list[Candidate] = []
    seen: set[str] = set()
    for cand in list(pool_a) + list(pool_b):
        if cand.key in seen:
            continue
        seen.add(cand.key)
        pool.append(cand)
    pruned = 0
    kept: list[Candidate] = []
    known_codes = set(menu.items) | set(menu.combos)
    for cand in pool:
        expanded = expand_candidate(cand, menu)
        reason = precheck_candidate(cand, expanded, request, known_codes)
        if reason is not None:
            pruned += 1
            continue
        kept.append(cand)
    return kept, pruned


# ---------------------------------------------------------------------------
# 评价辅助
# ---------------------------------------------------------------------------

def _evaluate_pool(
    pool: list[Candidate],
    menu: NormalizedMenu,
    request: MealRequest,
    quotes: QuoteManager,
    nutrition=None,
) -> list[CandidateEvaluation]:
    known_codes = set(menu.items) | set(menu.combos)
    out: list[CandidateEvaluation] = []
    for cand in pool:
        quote = quotes.get(cand.key)
        if quote is None:
            continue  # 未报价的候选不参与评价
        expanded = expand_candidate(cand, menu)
        out.append(evaluate_candidate(cand, expanded, quote, request, nutrition, known_codes))
    return out


def _is_verified(ev: CandidateEvaluation) -> bool:
    """已核验且满足全部硬条件：无违反、无未知硬条件、报价有效。"""
    return (
        ev.hard_pass
        and not ev.unknowns
        and not ev.violations
        and ev.quote is not None
        and ev.quote.ok
    )


def _soft_score(ev: CandidateEvaluation) -> int:
    return sum(1 for s in ev.soft_results.values() if s == EvalState.SATISFIED)


def _price_of(ev: CandidateEvaluation) -> int:
    if ev.quote is not None and ev.quote.price_fen is not None:
        return ev.quote.price_fen
    return 1 << 62  # 无价格排最后（理论上不会进入排序）


# ---------------------------------------------------------------------------
# 方案构造
# ---------------------------------------------------------------------------

def _plan_from_eval(ev: CandidateEvaluation, plan_id: str, verified: bool, store_id: str, proposed: list[ProposedChange]) -> Plan:
    return Plan(
        plan_id=plan_id,
        items=list(ev.candidate.items),
        expanded=ev.expanded,
        quote=ev.quote,  # type: ignore[arg-type]
        satisfied=ev.satisfied,
        proposed_changes=proposed,
        unknowns=ev.unknowns,
        verified=verified,
        store_id=store_id,
        quoted_at=ev.quote.quoted_at if ev.quote else "",
    )


def _describe_change(c: Constraint, new_value=None, candidate_price: Optional[int] = None, alt_name: Optional[str] = None) -> tuple[str, Optional[int]]:
    """生成修改建议的人话描述与改动幅度。"""
    if c.kind == ConstraintKind.TOTAL_PRICE_MAX:
        old = int(c.value)
        new = candidate_price if candidate_price is not None else new_value
        if new is None:
            return ("调整预算", None)
        desc = f"预算从 {fen_to_yuan(old)} 提高到 {fen_to_yuan(new)}（按试算价）"
        return (desc, new - old)
    if c.kind == ConstraintKind.INCLUDE_STAPLE:
        wanted = str(c.value)
        label = alt_name or ("其他主食" if not wanted.startswith("staple_") else wanted)
        return (f"将指定主食（{c.source_text or wanted}）换为：{label}", None)
    if c.kind == ConstraintKind.EXCLUDE_CATEGORY:
        return (f"允许包含「{c.value}」类餐品", None)
    if c.kind == ConstraintKind.EXCLUDE_ITEM:
        return (f"允许包含被排除的餐品/特制项（{c.source_text or c.value}）", None)
    if c.kind == ConstraintKind.REQUIRE_CATEGORY:
        return (f"取消「必须包含{c.value}」的要求", None)
    if c.kind == ConstraintKind.MAX_ITEMS:
        return (f"件数上限从 {c.value} 放宽", None)
    return (f"调整条件 {c.id}", None)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _schedule_order(pool: list[Candidate], menu: NormalizedMenu, request: MealRequest) -> list[Candidate]:
    """按相关性分层的试算顺序：

    第 1 层：满足原需求全部非价格硬条件的候选（最可能直接给出已核验方案）；
    第 2 层：满足某个妥协方向（放松一条可讨论条件后）非价格条件的候选；
    第 3 层：其余候选。

    分层只安排试算顺序，不剪除候选；报价预算内优先覆盖各层关键候选，
    避免「冲突已定位但关键候选没排上号」。
    """
    known_codes = set(menu.items) | set(menu.combos)
    seen: set[str] = set()
    ordered: list[Candidate] = []

    def add_tier(req_variant: MealRequest, locked_only: bool) -> None:
        for cand in pool:
            if cand.key in seen:
                continue
            expanded = expand_candidate(cand, menu)
            if precheck_candidate(cand, expanded, req_variant, known_codes, locked_only=locked_only) is None:
                seen.add(cand.key)
                ordered.append(cand)

    add_tier(request, locked_only=False)
    for c in negotiable_hard_constraints(request):
        add_tier(relax_constraint(request, c.id), locked_only=False)
    for cand in pool:
        if cand.key not in seen:
            seen.add(cand.key)
            ordered.append(cand)
    return ordered


def plan_next(
    menu: NormalizedMenu,
    request: MealRequest,
    quotes: QuoteManager,
    budget: Optional[QuoteBudget] = None,
    max_plans: int = MAX_PLANS,
    nutrition=None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
) -> PlanResult:
    """推进一轮规划。可能返回 QUOTES_PENDING（待宿主报价）或终态。"""
    if budget is not None:
        quotes.budget = budget
    scope = search_scope_dict(request)

    pool, pruned = build_candidate_pool(menu, request, max_candidates)
    coverage = Coverage(generated_count=len(pool), truncated=False)

    if not pool:
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.DATA_INCOMPLETE,
            coverage=coverage,
            search_scope=scope,
            notes=["门店数据为空或不含满足搜索边界的主食，暂时无法验证"],
        )

    # -- 阶段零：已有报价中是否已出现已核验方案（有证据即停止新增试算） --------
    evaluated = _evaluate_pool(pool, menu, request, quotes, nutrition)
    if any(_is_verified(ev) for ev in evaluated):
        return _finalize(menu, request, pool, quotes, evaluated, coverage, scope, max_plans, nutrition)

    # -- 阶段一：按相关性分层安排试算 ----------------------------------------
    pending = quotes.schedule(_schedule_order(pool, menu, request))
    if pending:
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.QUOTES_PENDING,
            coverage=Coverage(generated_count=len(pool), quoted_count=len(quotes.quoted_keys()), failed_count=0, truncated=len(pool) > len(quotes.quoted_keys()) + len(pending)),
            plans=[],
            pending_quotes=pending,
            search_scope=scope,
            notes=[
                f"本批安排 {len(pending)} 个候选试算（本轮预算 {quotes.budget.round_budget}，已用 {quotes.budget.used}）",
                f"报价前剪除 {pruned} 个违反锁定条件的候选",
            ],
        )

    # -- 阶段二及以后：评价与终态 --------------------------------------------
    return _finalize(menu, request, pool, quotes, evaluated, coverage, scope, max_plans, nutrition)


def _finalize(
    menu: NormalizedMenu,
    request: MealRequest,
    pool: list[Candidate],
    quotes: QuoteManager,
    evaluated: list[CandidateEvaluation],
    coverage: Coverage,
    scope: dict,
    max_plans: int,
    nutrition,
) -> PlanResult:
    """基于当前报价给出终态（feasible / compromise_proposed / not_found / data_incomplete）。"""
    stats = quotes.stats()
    coverage = Coverage(
        generated_count=len(pool),
        quoted_count=len(quotes.quoted_keys()),
        failed_count=stats["failed"] + stats["invalid"],
        truncated=len(quotes.quoted_keys()) < len(pool),
    )

    if stats["ok"] == 0:
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.DATA_INCOMPLETE,
            coverage=coverage,
            search_scope=scope,
            notes=["所有候选报价失败或无效，暂时无法验证；这不代表要求无法满足"],
        )

    verified = [ev for ev in evaluated if _is_verified(ev)]
    if verified:
        verified.sort(key=lambda ev: (_price_of(ev), -_soft_score(ev), ev.candidate.key))
        plans = [
            _plan_from_eval(ev, f"P{i + 1}", True, request.context.store_id, [])
            for i, ev in enumerate(verified[:max_plans])
        ]
        notes = []
        if coverage.truncated:
            notes.append("搜索范围不完整（受试算预算限制），以下为已核验候选中的推荐")
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.FEASIBLE,
            coverage=coverage,
            plans=plans,
            search_scope=scope,
            notes=notes,
        )

    # -- 阶段三：最小妥协 ----------------------------------------------------
    proposals = _build_proposals(menu, request, pool, quotes, nutrition, max_plans, evaluated)
    if proposals:
        notes = []
        if coverage.truncated:
            notes.append("搜索范围不完整（受试算预算限制），冲突结论仅限已核验候选")
        notes.append("以下方案均未满足原需求全部条件；用户接受后新条件才生效，并将重新试算")
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.COMPROMISE_PROPOSED,
            coverage=coverage,
            plans=proposals,
            search_scope=scope,
            notes=notes,
        )

    # -- 阶段四：未找到 / 数据不完整 -----------------------------------------
    # 全部已评价候选都只因「未知」受阻（无任何违反）→ 数据不完整，不误报为无法满足
    if evaluated and all(ev.unknowns and not ev.violations for ev in evaluated):
        unknown_notes = sorted({u.evidence for ev in evaluated for u in ev.unknowns})
        return PlanResult(
            request_version=request.request_version,
            context=request.context,
            status=PlanStatus.DATA_INCOMPLETE,
            coverage=coverage,
            search_scope=scope,
            notes=["有条件因数据缺失无法判定，暂时无法验证；这不代表要求无法满足"] + unknown_notes[:3],
        )

    notes = ["本轮在已核验候选中未找到可行方案，也未找到可接受的调整方向"]
    if coverage.truncated or quotes.budget.remaining == 0:
        notes.append("受试算预算限制搜索不完整，可继续扩大搜索；这不等于证明无解")
    locked = [c for c in request.constraints if c.negotiability == Negotiability.LOCKED]
    if locked:
        notes.append("当前锁定条件：" + "、".join(c.source_text or c.id for c in locked))
    return PlanResult(
        request_version=request.request_version,
        context=request.context,
        status=PlanStatus.NOT_FOUND,
        coverage=coverage,
        search_scope=scope,
        notes=notes,
    )


def _build_proposals(
    menu: NormalizedMenu,
    request: MealRequest,
    pool: list[Candidate],
    quotes: QuoteManager,
    nutrition,
    max_plans: int,
    evaluated: list[CandidateEvaluation],
) -> list[Plan]:
    """对每个可讨论且实际造成违反的硬约束构建「只改变一个条件」的修改请求。

    只有被违反过的条件才构成修改方向：某条件在所有已评价候选中都是 unknown
    （如营养数据缺失）时，不建议「取消该条件」，而应由调用方报数据不完整。
    """
    violated_ids = {v.constraint_id for ev in evaluated for v in ev.violations}
    directions = [c for c in negotiable_hard_constraints(request) if c.id in violated_ids]
    if not directions:
        return []

    proposals: list[Plan] = []
    seen_keys: set[str] = set()

    def collect(constraint_sets: list[list[Constraint]]) -> None:
        for cset in constraint_sets:
            ids = {c.id for c in cset}
            relaxed_request = copy.deepcopy(request)
            relaxed_request.constraints = [c for c in relaxed_request.constraints if c.id not in ids]
            evs = _evaluate_pool(pool, menu, relaxed_request, quotes, nutrition)
            feas = [ev for ev in evs if _is_verified(ev)]
            if not feas:
                continue
            # 该方向按可解释顺序取最优候选
            def sort_key(ev: CandidateEvaluation):
                prio = min(c.priority for c in cset)
                mag = 0
                for c in cset:
                    if c.kind == ConstraintKind.TOTAL_PRICE_MAX and ev.quote and ev.quote.price_fen is not None:
                        mag = max(mag, ev.quote.price_fen - int(c.value))
                return (prio, mag, _price_of(ev), -_soft_score(ev), ev.candidate.key)

            feas.sort(key=sort_key)
            best = feas[0]
            changes: list[ProposedChange] = []
            for c in cset:
                alt_name = None
                if c.kind == ConstraintKind.INCLUDE_STAPLE:
                    staples = [e for e in best.expanded.entries if e["category"].startswith("staple")]
                    alt_name = staples[0]["name"] if staples else None
                desc, magnitude = _describe_change(
                    c,
                    candidate_price=best.quote.price_fen if best.quote else None,
                    alt_name=alt_name,
                )
                changes.append(
                    ProposedChange(constraint_id=c.id, description=desc, modified_value=c.value, magnitude=magnitude)
                )
            if best.candidate.key in seen_keys:
                continue
            seen_keys.add(best.candidate.key)
            proposals.append(_plan_from_eval(best, f"C{len(proposals) + 1}", False, request.context.store_id, changes))

    # 先单条件，再多条件（成对）
    collect([[c] for c in directions])
    if not proposals and len(directions) >= 2:
        pairs = list(itertools.combinations(directions, 2))[:MAX_PAIR_DIRECTIONS]
        collect([list(p) for p in pairs])

    # 排序：改动条件数量 → 用户优先级 → 幅度 → 价格 → 偏好 → 编号
    def rank(plan: Plan) -> tuple:
        n_changes = len(plan.proposed_changes)
        prio = min(
            (request.constraint(pc.constraint_id).priority if request.constraint(pc.constraint_id) else 50)
            for pc in plan.proposed_changes
        )
        mags = [pc.magnitude for pc in plan.proposed_changes if pc.magnitude is not None]
        mag = min(mags) if mags else 0
        price = plan.quote.price_fen if plan.quote and plan.quote.price_fen is not None else 1 << 62
        return (n_changes, prio, mag, price, plan.plan_id)

    proposals.sort(key=rank)
    return proposals[:max_plans]
