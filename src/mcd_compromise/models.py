"""麦麦不将就 —— 核心数据模型。

设计原则（来自实现路径文档）：
- 金额统一以整数「分」存储与计算，禁止浮点累加。
- 条件评价只有三态：satisfied / violated / unknown；未知绝不当作满足或违反。
- 约束分三个独立维度：生效强度（hard/soft）、可讨论性（locked/ask/open）、优先级。
- 未提供的属性是 unknown，不能当成 false 或零。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def yuan_to_fen(text: str | int | float | Decimal) -> int:
    """把官方返回的价格字符串（元）转为整数分。

    官方菜单 currentPrice/originalPrice 是字符串（如 "33.5"），
    diffPrice 形如 "+ ¥1.5" / "- ¥0.5"；报价接口返回的已是整数分。
    用 Decimal 解析，避免二进制浮点误差。
    """
    if isinstance(text, int):
        return text
    if isinstance(text, Decimal):
        return int((text * 100).to_integral_value())
    if isinstance(text, float):  # 仅兜底，正常路径不经过
        return int((Decimal(str(text)) * 100).to_integral_value())
    s = str(text).strip()
    if not s:
        raise ValueError("empty price string")
    s = s.replace("¥", "").replace("￥", "").replace(",", "").strip()
    sign = 1
    if s.startswith("+"):
        s = s[1:].strip()
    elif s.startswith("-"):
        sign = -1
        s = s[1:].strip()
    return sign * int((Decimal(s) * 100).to_integral_value())


def fen_to_yuan(fen: int) -> str:
    """整数分转「x.y 元」展示字符串。"""
    sign = "-" if fen < 0 else ""
    fen = abs(fen)
    return f"{sign}{fen // 100}.{fen % 100:02d} 元"


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class Strength(str, Enum):
    HARD = "hard"    # 当前方案必须满足
    SOFT = "soft"    # 仅作为排序偏好


class Negotiability(str, Enum):
    LOCKED = "locked"  # 不可讨论，失败时也不得提出修改建议
    ASK = "ask"        # 可提出修改建议，但未经用户接受不生效
    OPEN = "open"      # 可自由调整


class ConstraintKind(str, Enum):
    TOTAL_PRICE_MAX = "total_price_max"   # 预算上限，value=分
    INCLUDE_STAPLE = "include_staple"     # 指定主食，value=餐品编码或品类标签
    EXCLUDE_CATEGORY = "exclude_category" # 排除品类，value=品类标签
    EXCLUDE_ITEM = "exclude_item"         # 排除指定餐品，value=餐品编码
    REQUIRE_CATEGORY = "require_category" # 必须包含某品类，value=品类标签
    MAX_ITEMS = "max_items"               # 总件数上限，value=int
    PREFER_COUPON = "prefer_coupon"       # 优先用券（偏好）
    ENERGY_MAX = "energy_max"             # P1 热量上限，value=kcal
    PROTEIN_MIN = "protein_min"           # P1 蛋白质下限，value=g
    FAT_MAX = "fat_max"                   # P1 脂肪上限，value=g


class EvalState(str, Enum):
    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


class QuoteStatus(str, Enum):
    OK = "ok"            # 官方试算成功且返回了有效商品清单
    FAILED = "failed"    # 官方试算失败（网络/限流/参数错误）
    INVALID = "invalid"  # 官方返回 success 但内容无效（如空商品清单、价格 0）


class PlanStatus(str, Enum):
    QUOTES_PENDING = "quotes_pending"          # 第一阶段：等待宿主补报价
    FEASIBLE = "feasible"                      # 找到满足全部硬条件的已核验方案
    COMPROMISE_PROPOSED = "compromise_proposed"  # 提出待接受的妥协方案
    NOT_FOUND = "not_found"                    # 本轮未找到（不等于证明不存在）
    DATA_INCOMPLETE = "data_incomplete"        # 数据缺失或查询失败导致无法判定


# ---------------------------------------------------------------------------
# 约束与需求
# ---------------------------------------------------------------------------

@dataclass
class Constraint:
    """一条用户需求条件。

    - strength: 当前是否必须满足
    - negotiability: 失败时能否提出修改建议
    - priority: 多个可讨论条件中先讨论哪个（数值小者先）
    - source_text: 用户原话，用于解释与回溯
    """

    id: str
    kind: ConstraintKind
    value: Any
    strength: Strength = Strength.HARD
    negotiability: Negotiability = Negotiability.ASK
    priority: int = 50
    source_text: str = ""
    unit: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind.value,
            "value": self.value,
            "unit": self.unit,
            "strength": self.strength.value,
            "negotiability": self.negotiability.value,
            "priority": self.priority,
            "sourceText": self.source_text,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Constraint":
        return cls(
            id=d["id"],
            kind=ConstraintKind(d["kind"]),
            value=d["value"],
            strength=Strength(d.get("strength", "hard")),
            negotiability=Negotiability(d.get("negotiability", "ask")),
            priority=int(d.get("priority", 50)),
            source_text=d.get("sourceText", ""),
            unit=d.get("unit", ""),
        )


@dataclass
class RequestContext:
    store_id: str
    dining_mode: str = "pickup"  # 首版仅支持到店自取
    people: int = 1

    def to_dict(self) -> dict:
        return {"storeId": self.store_id, "diningMode": self.dining_mode, "people": self.people}

    @classmethod
    def from_dict(cls, d: dict) -> "RequestContext":
        return cls(
            store_id=d["storeId"],
            dining_mode=d.get("diningMode", "pickup"),
            people=int(d.get("people", 1)),
        )


@dataclass
class MealRequest:
    """一次配餐需求（带版本号；多轮协商只改变化的条件）。"""

    request_version: int
    context: RequestContext
    constraints: list[Constraint] = field(default_factory=list)

    def constraint(self, cid: str) -> Optional[Constraint]:
        for c in self.constraints:
            if c.id == cid:
                return c
        return None

    def to_dict(self) -> dict:
        return {
            "requestVersion": self.request_version,
            "context": self.context.to_dict(),
            "constraints": [c.to_dict() for c in self.constraints],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MealRequest":
        return cls(
            request_version=int(d["requestVersion"]),
            context=RequestContext.from_dict(d["context"]),
            constraints=[Constraint.from_dict(c) for c in d.get("constraints", [])],
        )


# ---------------------------------------------------------------------------
# 标准化菜单（来自官方 MCP 返回）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MenuItem:
    """单品。category/tag_basis/tag_confidence 记录内部标准化结果与依据。"""

    code: str
    name: str
    current_price_fen: int
    original_price_fen: int
    category: str = "unknown"     # staple_beef/staple_chicken/staple_fish/staple_other/drink/fries/fries_like/snack/dessert/unknown
    tag_basis: str = ""           # 映射依据（命中的规则说明）
    tag_confidence: str = "unknown"  # high/medium/unknown
    tags: tuple[str, ...] = ()
    is_combo: bool = False


@dataclass(frozen=True)
class ModValue:
    """一个特制选项值（来自 query-meal-detail 的 modification.values）。"""

    code: str
    name: str
    price_fen: int
    selected_key: str
    unselected_key: str = ""
    selected_quantity: int = 0

    @property
    def is_selected_by_default(self) -> bool:
        return self.selected_quantity > 0


@dataclass(frozen=True)
class ModGroup:
    """一个特制组（对应官方 modification.items[]）。

    组内 values 为互斥或可多选的选项；含 unselectedKey 的组在算价时
    该组所有项都必须传（选中传 selectedKey，未选中传 unselectedKey）。
    """

    max_values: int
    min_values: int
    values: tuple[ModValue, ...]


@dataclass(frozen=True)
class ComboChoice:
    code: str
    name: str
    diff_price_fen: int
    is_default: bool
    support_modify: bool
    category: str = "unknown"
    tag_basis: str = ""
    tag_confidence: str = "unknown"
    modifications: tuple[ModGroup, ...] = ()


@dataclass(frozen=True)
class ComboRound:
    id: str
    name: str
    choices: tuple[ComboChoice, ...]
    min_quantity: int = 1
    max_quantity: int = 1


@dataclass(frozen=True)
class ComboDetail:
    code: str
    name: str
    base_price_fen: int
    rounds: tuple[ComboRound, ...]
    category: str = "combo"
    staple_tag: str = "unknown"   # 套餐主食的品类（由第一轮推导）
    staple_tag_basis: str = ""


@dataclass
class Coupon:
    coupon_id: str
    title: str
    product_codes: tuple[str, ...]
    trade_start: str = ""
    trade_end: str = ""
    coupon_code: str = ""


@dataclass
class NormalizedMenu:
    """一个门店上下文下的标准化数据。"""

    store_id: str
    items: dict[str, MenuItem] = field(default_factory=dict)       # 单品（含券商品）
    combos: dict[str, ComboDetail] = field(default_factory=dict)   # 套餐
    coupons: list[Coupon] = field(default_factory=list)
    fetched_at: str = ""
    coverage_note: str = ""   # 分页/缺失说明

    def all_staples(self) -> list[MenuItem]:
        return [i for i in self.items.values() if i.category.startswith("staple")]

    def category_items(self, category: str) -> list[MenuItem]:
        return [i for i in self.items.values() if i.category == category]


# ---------------------------------------------------------------------------
# 候选与报价
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RoundSelection:
    round_id: str
    choice_code: str
    quantity: int = 1
    modifications: tuple[tuple[str, str, int], ...] = ()  # (code, key, quantity)


@dataclass(frozen=True)
class OrderItem:
    """候选中的一行。套餐带 rounds；券商品带 coupon 引用。"""

    product_code: str
    quantity: int = 1
    rounds: tuple[RoundSelection, ...] = ()
    coupon_id: str = ""
    coupon_code: str = ""

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"productCode": self.product_code, "quantity": self.quantity}
        if self.rounds:
            d["roundList"] = [
                {
                    "round": r.round_id,
                    "comboItemList": [
                        {
                            "code": r.choice_code,
                            "quantity": r.quantity,
                            **(
                                {
                                    "modification": {
                                        "values": [
                                            {"code": c, "key": k, "quantity": q}
                                            for c, k, q in r.modifications
                                        ]
                                    }
                                }
                                if r.modifications
                                else {}
                            ),
                        }
                    ],
                }
                for r in self.rounds
            ]
        if self.coupon_id:
            d["couponId"] = self.coupon_id
        if self.coupon_code:
            d["couponCode"] = self.coupon_code
        return d


@dataclass(frozen=True)
class Candidate:
    """一个规范化候选；key 用于去重与报价复用。"""

    key: str
    items: tuple[OrderItem, ...]
    note: str = ""  # 生成方向说明（如「默认搭配」「换小食」）

    def to_dict(self) -> dict:
        return {"key": self.key, "items": [i.to_dict() for i in self.items], "note": self.note}


@dataclass
class Quote:
    candidate_key: str
    status: QuoteStatus
    price_fen: Optional[int] = None
    original_price_fen: Optional[int] = None
    discount_fen: Optional[int] = None
    quoted_at: str = ""
    error_code: str = ""
    error_message: str = ""
    product_names: tuple[str, ...] = ()
    # 官方报价按商品编码返回的商品名（含规格后缀，如 3010→「雪碧小杯」）。
    # 真实联调发现：query-meals 的单品名常缺规格（「雪碧」），而 calculate-price
    # 的 productName 带规格且与营养表精确一致；营养匹配在菜单名失配时回退到它。
    product_code_names: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == QuoteStatus.OK and type(self.price_fen) is int and self.price_fen > 0

    def official_name(self, code: str) -> str:
        """官方报价中该编码的商品名；无则空串。"""
        for c, n in self.product_code_names:
            if c == code and n:
                return n
        return ""

    def to_dict(self) -> dict:
        return {
            "candidateKey": self.candidate_key,
            "status": self.status.value,
            "priceFen": self.price_fen,
            "originalPriceFen": self.original_price_fen,
            "discountFen": self.discount_fen,
            "quotedAt": self.quoted_at,
            "errorCode": self.error_code,
            "errorMessage": self.error_message,
            "productNames": list(self.product_names),
            "productCodeNames": [list(p) for p in self.product_code_names],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Quote":
        pairs = []
        for p in d.get("productCodeNames", ()) or ():
            if isinstance(p, (list, tuple)) and len(p) == 2:
                pairs.append((str(p[0]), str(p[1])))
        return cls(
            candidate_key=d["candidateKey"],
            status=QuoteStatus(d["status"]),
            price_fen=d.get("priceFen"),
            original_price_fen=d.get("originalPriceFen"),
            discount_fen=d.get("discountFen"),
            quoted_at=d.get("quotedAt", ""),
            error_code=d.get("errorCode", ""),
            error_message=d.get("errorMessage", ""),
            product_names=tuple(d.get("productNames", ())),
            product_code_names=tuple(pairs),
        )


# ---------------------------------------------------------------------------
# 评价与结果
# ---------------------------------------------------------------------------

@dataclass
class ConditionOutcome:
    constraint_id: str
    state: EvalState
    target: Any = None
    actual: Any = None
    evidence: str = ""

    def to_dict(self) -> dict:
        return {
            "constraintId": self.constraint_id,
            "state": self.state.value,
            "target": self.target,
            "actual": self.actual,
            "evidence": self.evidence,
        }


@dataclass
class ExpandedContent:
    """候选展开后的内容（套餐按轮次展开成具体餐品）。"""

    entries: list[dict] = field(default_factory=list)  # {code,name,category,quantity,confidence,basis}

    def codes(self) -> list[str]:
        return [e["code"] for e in self.entries]

    def names(self) -> list[str]:
        return [e["name"] for e in self.entries]

    def categories(self) -> set[str]:
        return {e["category"] for e in self.entries}

    def has_category(self, category: str) -> bool:
        return any(e["category"] == category for e in self.entries)

    def category_unknown_entries(self) -> list[dict]:
        return [e for e in self.entries if e["category"] == "unknown" or e["confidence"] == "unknown"]


@dataclass
class CandidateEvaluation:
    candidate: Candidate
    expanded: ExpandedContent
    quote: Optional[Quote]
    hard_pass: bool
    locked_pass: bool
    violations: list[ConditionOutcome] = field(default_factory=list)
    unknowns: list[ConditionOutcome] = field(default_factory=list)
    satisfied: list[ConditionOutcome] = field(default_factory=list)
    soft_results: dict[str, EvalState] = field(default_factory=dict)
    pre_pruned_reason: str = ""  # 非空表示报价前已被硬条件剪除

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate.to_dict(),
            "expanded": self.expanded.entries,
            "quote": self.quote.to_dict() if self.quote else None,
            "hardPass": self.hard_pass,
            "lockedPass": self.locked_pass,
            "violations": [v.to_dict() for v in self.violations],
            "unknowns": [u.to_dict() for u in self.unknowns],
            "satisfied": [s.to_dict() for s in self.satisfied],
            "softResults": {k: v.value for k, v in self.soft_results.items()},
        }


@dataclass
class Coverage:
    generated_count: int = 0
    quoted_count: int = 0
    failed_count: int = 0
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "generatedCount": self.generated_count,
            "quotedCount": self.quoted_count,
            "failedCount": self.failed_count,
            "truncated": self.truncated,
        }


@dataclass
class ProposedChange:
    """一个待用户接受的修改建议。"""

    constraint_id: str
    description: str           # 人话解释，如「预算从 25.00 元提高到 27.00 元」
    modified_value: Any = None
    magnitude: Any = None      # 改动幅度（用于同方向比较），如增额分

    def to_dict(self) -> dict:
        return {
            "constraintId": self.constraint_id,
            "description": self.description,
            "modifiedValue": self.modified_value,
            "magnitude": self.magnitude,
        }


@dataclass
class Plan:
    """一个输出方案（已核验或待接受）。"""

    plan_id: str
    items: list[OrderItem]
    expanded: ExpandedContent
    quote: Quote
    satisfied: list[ConditionOutcome]
    violated: list[ConditionOutcome]
    proposed_changes: list[ProposedChange]
    unknowns: list[ConditionOutcome]
    verified: bool  # True=已核验且满足全部硬条件；False=待接受修改
    store_id: str = ""
    quoted_at: str = ""

    def to_dict(self) -> dict:
        return {
            "planId": self.plan_id,
            "items": [i.to_dict() for i in self.items],
            "expanded": self.expanded.entries,
            "officialQuote": self.quote.to_dict(),
            "satisfiedConstraints": [s.to_dict() for s in self.satisfied],
            "violatedConstraints": [v.to_dict() for v in self.violated],
            "proposedChanges": [p.to_dict() for p in self.proposed_changes],
            "unknowns": [u.to_dict() for u in self.unknowns],
            "verified": self.verified,
            "storeId": self.store_id,
            "quotedAt": self.quoted_at,
        }


@dataclass
class PlanResult:
    request_version: int
    context: RequestContext
    status: PlanStatus
    coverage: Coverage
    plans: list[Plan] = field(default_factory=list)
    closest_options: list[Plan] = field(default_factory=list)  # not_found 时最近失配的已核验参考
    pending_quotes: list[Candidate] = field(default_factory=list)  # 第一阶段待报价候选
    search_scope: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "requestVersion": self.request_version,
            "context": self.context.to_dict(),
            "searchScope": self.search_scope,
            "status": self.status.value,
            "coverage": self.coverage.to_dict(),
            "plans": [p.to_dict() for p in self.plans],
            "closestOptions": [p.to_dict() for p in self.closest_options],
            "pendingQuotes": [c.to_dict() for c in self.pending_quotes],
            "notes": self.notes,
        }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)
