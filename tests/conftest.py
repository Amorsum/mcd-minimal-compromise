"""测试共享工具：加载小型菜单夹具，提供模拟官方算价器。

模拟算价器复刻真实联调观察到官方行为：
- 单品按菜单现价；套餐按 base + 各轮 diffPrice；
- 券商品携带正确券 → 券价（X001: 2700→1350；X002: 2200→1590）；
- 券商品携带错误券 → success=false, code=600012；
- 空商品清单 → success=true 但 price=0、productList=[]（无效报价陷阱）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mcd_compromise.compromise import plan_next  # noqa: E402
from mcd_compromise.models import PlanStatus  # noqa: E402
from mcd_compromise.normalize import build_normalized_menu  # noqa: E402
from mcd_compromise.quote_state import QuoteBudget, QuoteManager  # noqa: E402

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "small_menu.json"

COUPON_PRICES = {
    "X001": (2700, 1350),  # 中薯条买一送一
    "X002": (2200, 1590),  # 王牌小食拼
}


@pytest.fixture(scope="session")
def fixture_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def menu(fixture_payload):
    mcp = fixture_payload["mcp"]
    return build_normalized_menu(
        store_id=fixture_payload["storeId"],
        menu_response=mcp["menu"],
        detail_responses=mcp["comboDetails"],
        coupons_response=mcp["coupons"],
        fetched_at=fixture_payload.get("fetchedAt", ""),
    )


def fake_calculate_price(candidate, menu) -> dict:
    """按夹具数据模拟官方 calculate-price 返回。"""
    product_list = []
    total = 0
    original = 0
    for item in candidate.items:
        is_coupon_product = item.product_code in COUPON_PRICES
        if is_coupon_product and not item.coupon_id:
            # 复刻真实陷阱：券商品不带券 → success=true 但空清单、价格 0
            return {"success": True, "code": 200, "data": {"price": 0, "originalPrice": 0, "discount": 0, "productList": []}}
        if item.coupon_id:
            prices = COUPON_PRICES.get(item.product_code)
            expected = {"X001": ("CP-TEST-1", "CC-TEST-1"), "X002": ("CP-TEST-2", "CC-TEST-2")}.get(item.product_code)
            if prices is None or expected is None:
                return {"success": False, "code": 600012, "message": "促销规则不支持该商品"}
            # 真实 API 同时校验 couponId 与 couponCode（曾因把券 ID 当券编码传入而失败）
            if item.coupon_id != expected[0] or item.coupon_code != expected[1]:
                return {"success": False, "code": 600012, "message": "促销规则不支持该商品"}
            orig, now = prices
            name = "中薯条买一送一" if item.product_code == "X001" else "王牌小食拼"
            product_list.append(
                {"productCode": item.product_code, "productName": name, "quantity": item.quantity, "subtotal": now, "originalSubtotal": orig}
            )
            total += now * item.quantity
            original += orig * item.quantity
            continue
        combo = menu.combos.get(item.product_code)
        mi = menu.items.get(item.product_code)
        if combo is not None:
            price = combo.base_price_fen
            for sel in item.rounds:
                r = next((x for x in combo.rounds if x.id == sel.round_id), None)
                choice = next((c for c in r.choices if c.code == sel.choice_code), None) if r else None
                if choice is None:
                    return {"success": False, "code": 404, "message": "非合法替换"}
                price += choice.diff_price_fen
            product_list.append(
                {"productCode": combo.code, "productName": combo.name, "quantity": item.quantity, "subtotal": price, "originalSubtotal": price}
            )
            total += price * item.quantity
            original += price * item.quantity
            continue
        if mi is None:
            return {"success": False, "code": 404, "message": "商品不存在"}
        product_list.append(
            {"productCode": mi.code, "productName": mi.name, "quantity": item.quantity, "subtotal": mi.current_price_fen, "originalSubtotal": mi.current_price_fen}
        )
        total += mi.current_price_fen * item.quantity
        original += mi.current_price_fen * item.quantity
    if not product_list:
        # 复刻真实陷阱：success=true 但空清单、价格 0
        return {"success": True, "code": 200, "data": {"price": 0, "originalPrice": 0, "discount": 0, "productList": []}}
    return {
        "success": True,
        "code": 200,
        "data": {
            "price": total,
            "originalPrice": original,
            "discount": original - total,
            "productList": product_list,
        },
    }


def run_until_final(menu, request, budget=None, max_rounds=12, quote_fn=None):
    """跑完整两阶段协议，返回 (PlanResult, QuoteManager)。"""
    quotes = QuoteManager(budget)
    fn = quote_fn or fake_calculate_price
    for _ in range(max_rounds):
        result = plan_next(menu, request, quotes, budget=budget)
        if result.status != PlanStatus.QUOTES_PENDING:
            return result, quotes
        for cand in result.pending_quotes:
            resp = fn(cand, menu)
            quotes.register_mcp_result(cand.key, resp, quoted_at="2026-10-12 16:00:00")
    raise AssertionError("协议轮次过多，未收敛")


def make_request(version=1, store_id="TEST001", constraints=None):
    from mcd_compromise.models import MealRequest, RequestContext

    return MealRequest(
        request_version=version,
        context=RequestContext(store_id=store_id, dining_mode="pickup", people=1),
        constraints=constraints or [],
    )
