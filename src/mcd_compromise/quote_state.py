"""试算管理：请求去重、调用预算、报价状态、失败恢复。

规则（实现路径文档 4.1 / 6.3 / 12）：
- 未成功试算的候选不能进入已核验列表；
- 同一候选在本轮复用报价，不重复试算；
- 官方返回 success 但内容无效（如空商品清单、价格 0）记为 invalid，不当 0 元；
- 报价默认仅在本轮复用；用户选定后重新试算；
- 初始配置：首批 8 个候选，单轮最多 24 个（项目配置，非官方性能承诺）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import Candidate, Quote, QuoteStatus

DEFAULT_FIRST_BATCH = 8
DEFAULT_ROUND_BUDGET = 24


@dataclass
class QuoteBudget:
    first_batch: int = DEFAULT_FIRST_BATCH
    round_budget: int = DEFAULT_ROUND_BUDGET
    used: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.round_budget - self.used)

    def can_quote(self) -> bool:
        return self.remaining > 0

    def consume(self, n: int = 1) -> None:
        self.used += n

    def to_dict(self) -> dict:
        return {
            "firstBatch": self.first_batch,
            "roundBudget": self.round_budget,
            "used": self.used,
            "remaining": self.remaining,
        }


class QuoteManager:
    """一轮协商内的报价登记与去重。"""

    def __init__(self, budget: Optional[QuoteBudget] = None):
        self.budget = budget or QuoteBudget()
        self._quotes: dict[str, Quote] = {}

    # -- 登记 ----------------------------------------------------------------

    def register(self, quote: Quote) -> None:
        self._quotes[quote.candidate_key] = quote
        self.budget.consume(1)  # 每次登记对应一次真实试算调用

    def register_mcp_result(self, candidate_key: str, response: dict, quoted_at: str = "") -> Quote:
        """把官方 calculate-price 返回登记为报价。

        有效性判定（基于真实联调发现）：
        - success=false → failed（记录错误码）；
        - success=true 但 productList 为空或 price 缺失/为 0 → invalid（陷阱：
          券商品不带券试算会返回空清单 + price 0，绝不能当成 0 元）。
        """
        ok = bool(response.get("success"))
        data = response.get("data") or {}
        if not ok:
            quote = Quote(
                candidate_key=candidate_key,
                status=QuoteStatus.FAILED,
                quoted_at=quoted_at,
                error_code=str(response.get("code", "")),
                error_message=str(response.get("message", "")),
            )
        else:
            product_list = data.get("productList") or []
            price = data.get("price")
            if not product_list or price is None:
                quote = Quote(
                    candidate_key=candidate_key,
                    status=QuoteStatus.INVALID,
                    price_fen=None,
                    quoted_at=quoted_at,
                    error_code="EMPTY_PRODUCT_LIST",
                    error_message="官方返回成功但商品清单为空或价格缺失，按无效报价处理（疑似券商品未带券）",
                )
            else:
                quote = Quote(
                    candidate_key=candidate_key,
                    status=QuoteStatus.OK,
                    price_fen=int(price),
                    original_price_fen=data.get("originalPrice"),
                    discount_fen=data.get("discount"),
                    quoted_at=quoted_at,
                    product_names=tuple(str(p.get("productName", "")) for p in product_list if isinstance(p, dict)),
                )
        self.register(quote)
        return quote

    # -- 查询 ----------------------------------------------------------------

    def get(self, candidate_key: str) -> Optional[Quote]:
        return self._quotes.get(candidate_key)

    def quoted_keys(self) -> set[str]:
        return set(self._quotes.keys())

    def ok_quotes(self) -> dict[str, Quote]:
        return {k: q for k, q in self._quotes.items() if q.ok}

    def stats(self) -> dict:
        ok = sum(1 for q in self._quotes.values() if q.status == QuoteStatus.OK)
        failed = sum(1 for q in self._quotes.values() if q.status == QuoteStatus.FAILED)
        invalid = sum(1 for q in self._quotes.values() if q.status == QuoteStatus.INVALID)
        return {"ok": ok, "failed": failed, "invalid": invalid, "total": len(self._quotes)}

    # -- 调度 ----------------------------------------------------------------

    def schedule(self, candidates: Iterable[Candidate], limit: Optional[int] = None) -> list[Candidate]:
        """挑出本批需要报价的候选：去重 + 跳过已报价 + 尊重预算。

        预算语义（项目配置）：本轮首次派出 first_batch 个；其后每批不超过
        round_budget 的剩余额度。已报价候选不重复派出。
        """
        need: list[Candidate] = []
        seen: set[str] = set()
        for cand in candidates:
            if cand.key in seen:
                continue
            seen.add(cand.key)
            if cand.key in self._quotes:
                continue
            need.append(cand)
        if limit is not None:
            cap = limit
        elif not self._quotes:
            cap = min(self.budget.first_batch, self.budget.remaining)
        else:
            cap = self.budget.remaining
        return need[:cap]

    def to_dict(self) -> dict:
        return {
            "budget": self.budget.to_dict(),
            "quotes": {k: q.to_dict() for k, q in sorted(self._quotes.items())},
        }

    @classmethod
    def from_dict(cls, d: dict) -> "QuoteManager":
        budget = QuoteBudget(
            first_batch=int(d.get("budget", {}).get("firstBatch", DEFAULT_FIRST_BATCH)),
            round_budget=int(d.get("budget", {}).get("roundBudget", DEFAULT_ROUND_BUDGET)),
            used=int(d.get("budget", {}).get("used", 0)),
        )
        mgr = cls(budget)
        # 直接恢复报价登记，不重复消耗预算（used 已在 budget 中恢复）
        for k, q in (d.get("quotes") or {}).items():
            quote = Quote.from_dict(q)
            quote.candidate_key = quote.candidate_key or k
            mgr._quotes[quote.candidate_key] = quote
        return mgr
