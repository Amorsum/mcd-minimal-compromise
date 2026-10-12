"""麦麦不将就（mcd-minimal-compromise）计算引擎。

预算、口味、营养难以兼得时，找到你最愿意接受的那一点改变。

模块边界见 docs/architecture.md；本包不持有任何凭证，也不直接调用官方 MCP
（由宿主 Skill 负责工具调用，本引擎只做标准化数据上的确定性计算）。
"""

from .models import (  # noqa: F401
    Candidate,
    ComboChoice,
    ComboDetail,
    ComboRound,
    ConditionOutcome,
    Constraint,
    ConstraintKind,
    Coverage,
    Coupon,
    EvalState,
    ExpandedContent,
    MealRequest,
    MenuItem,
    ModGroup,
    ModValue,
    Negotiability,
    NormalizedMenu,
    OrderItem,
    Plan,
    PlanResult,
    PlanStatus,
    ProposedChange,
    Quote,
    QuoteStatus,
    RequestContext,
    RoundSelection,
    Strength,
    fen_to_yuan,
    yuan_to_fen,
)
from .normalize import (  # noqa: F401
    build_normalized_menu,
    normalize_coupons,
    normalize_meal_detail,
    normalize_menu,
    tag_item,
)
from .candidates import generate_candidates, search_scope_dict  # noqa: F401
from .constraints import evaluate_candidate, expand_candidate, precheck_candidate  # noqa: F401
from .compromise import plan_next, relax_constraint, relax_all_negotiable  # noqa: F401
from .quote_state import QuoteBudget, QuoteManager  # noqa: F401
from .nutrition import NutritionMap  # noqa: F401

__version__ = "0.1.0"
