"""试算管理测试：去重、预算、无效报价识别、序列化。"""

from __future__ import annotations

from mcd_compromise.models import Candidate, OrderItem, Quote, QuoteStatus
from mcd_compromise.quote_state import DEFAULT_FIRST_BATCH, DEFAULT_ROUND_BUDGET, QuoteBudget, QuoteManager


def _cand(key: str) -> Candidate:
    return Candidate(key=key, items=(OrderItem(product_code="B001", quantity=1),))


class TestBudget:
    def test_defaults(self):
        b = QuoteBudget()
        assert b.first_batch == DEFAULT_FIRST_BATCH == 8
        assert b.round_budget == DEFAULT_ROUND_BUDGET == 24

    def test_first_batch_limit(self):
        mgr = QuoteManager(QuoteBudget(first_batch=2, round_budget=10))
        cands = [_cand(f"k{i}") for i in range(5)]
        batch = mgr.schedule(cands)
        assert len(batch) == 2  # 首批最多 2
        for c in batch:
            mgr.register_mcp_result(c.key, {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        # 报价后进入后续批次，受单轮预算约束
        batch2 = mgr.schedule(cands)
        assert len(batch2) == 3
        for c in batch2:
            mgr.register_mcp_result(c.key, {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        assert mgr.budget.used == 5
        assert mgr.budget.remaining == 5

    def test_round_budget_limit(self):
        mgr = QuoteManager(QuoteBudget(first_batch=2, round_budget=3))
        cands = [_cand(f"k{i}") for i in range(10)]
        first = mgr.schedule(cands)
        assert len(first) == 2  # 首批 2
        for c in first:
            mgr.register_mcp_result(c.key, {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        second = mgr.schedule(cands)
        assert len(second) == 1  # 单轮共 3，只剩 1 个额度
        for c in second:
            mgr.register_mcp_result(c.key, {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        assert mgr.schedule(cands) == []  # 预算耗尽
        assert mgr.budget.remaining == 0

    def test_schedule_skips_quoted(self):
        mgr = QuoteManager(QuoteBudget(first_batch=8, round_budget=8))
        cands = [_cand("k1"), _cand("k2")]
        mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        batch = mgr.schedule(cands)
        assert [c.key for c in batch] == ["k2"]


class TestRegisterResult:
    def test_ok(self):
        mgr = QuoteManager()
        q = mgr.register_mcp_result(
            "k1",
            {"success": True, "code": 200, "data": {"price": 2550, "originalPrice": 2550, "discount": 0, "productList": [{"productName": "巨无霸"}]}},
            "2026-10-12T16:00:00",
        )
        assert q.status == QuoteStatus.OK
        assert q.price_fen == 2550
        assert q.product_names == ("巨无霸",)

    def test_failed(self):
        mgr = QuoteManager()
        q = mgr.register_mcp_result("k1", {"success": False, "code": 600012, "message": "促销规则不支持该商品"}, "t")
        assert q.status == QuoteStatus.FAILED
        assert q.error_code == "600012"
        assert not q.ok

    def test_invalid_empty_product_list(self):
        # 真实陷阱：success=true 但空清单、price=0（券商品未带券）
        mgr = QuoteManager()
        q = mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"price": 0, "productList": []}}, "t")
        assert q.status == QuoteStatus.INVALID
        assert q.price_fen is None
        assert not q.ok

    def test_invalid_missing_price(self):
        mgr = QuoteManager()
        q = mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"productList": [{"productName": "x"}]}}, "t")
        assert q.status == QuoteStatus.INVALID

    def test_duplicate_registration_reuses(self):
        mgr = QuoteManager()
        mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"price": 100, "productList": [{"productName": "x"}]}}, "t")
        mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"price": 200, "productList": [{"productName": "y"}]}}, "t")
        assert len(mgr.quoted_keys()) == 1
        assert mgr.get("k1").price_fen == 200  # 同 key 后者覆盖前者（同轮重试）


class TestSerialization:
    def test_roundtrip(self):
        mgr = QuoteManager(QuoteBudget(first_batch=4, round_budget=12))
        mgr.register_mcp_result("k1", {"success": True, "code": 200, "data": {"price": 2550, "productList": [{"productName": "巨无霸"}]}}, "t")
        mgr.register_mcp_result("k2", {"success": False, "code": 500, "message": "timeout"}, "t")
        restored = QuoteManager.from_dict(mgr.to_dict())
        assert restored.budget.first_batch == 4
        assert restored.budget.round_budget == 12
        assert restored.get("k1").price_fen == 2550
        assert restored.get("k2").status == QuoteStatus.FAILED
        assert restored.stats() == {"ok": 1, "failed": 1, "invalid": 0, "total": 2}
