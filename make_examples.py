"""生成 examples/ 下的三个演示文件（基于人工夹具，明确标注为模拟数据）。"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

from conftest import fake_calculate_price, make_request  # noqa: E402
from mcd_compromise.models import Constraint, ConstraintKind, Negotiability, PlanStatus  # noqa: E402
from mcd_compromise.plan_cli import run  # noqa: E402
from mcd_compromise.quote_state import QuoteManager  # noqa: E402

payload = json.loads((ROOT / "tests" / "fixtures" / "small_menu.json").read_text(encoding="utf-8"))
MCP = payload["mcp"]
STORE = payload["storeId"]


def protocol(request_dict, note):
    """跑完整两阶段协议，返回最终输出（附演示元信息）。"""
    quotes = QuoteManager()
    inp = {"storeId": STORE, "fetchedAt": "2026-10-12 16:00:00", "mcp": MCP, "request": request_dict}
    rounds = []
    while True:
        out = run(inp, replay=True)
        rounds.append({"status": out["status"], "pendingQuoteCount": len(out.get("nextActions", []))})
        if out["status"] != "quotes_pending":
            break
        for action in out["nextActions"]:
            resp = fake_calculate_price_from_action(action, MCP)
            inp.setdefault("quotes", {"budget": {"firstBatch": 8, "roundBudget": 24, "used": 0}, "quotes": {}})
            inp["quotes"]["quotes"][action["candidateKey"]] = resp
        if len(rounds) > 12:
            raise AssertionError("未收敛")
    out["demo"] = {"note": note, "protocolRounds": rounds}
    return inp, out


def fake_calculate_price_from_action(action, mcp):
    """用夹具模拟算价（与 tests/conftest.fake_calculate_price 同一逻辑）。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("conftest_mod", ROOT / "tests" / "conftest.py")
    # 直接复用 conftest 的模拟器：构造一个伪候选
    from types import SimpleNamespace

    from mcd_compromise.models import OrderItem, RoundSelection

    items = []
    for it in action["mcpCall"]["params"]["items"]:
        rounds = tuple(
            RoundSelection(
                round_id=r["round"],
                choice_code=r["comboItemList"][0]["code"],
                quantity=r["comboItemList"][0].get("quantity", 1),
                modifications=tuple(
                    (v["code"], v["key"], v.get("quantity", 0))
                    for v in (r["comboItemList"][0].get("modification", {}).get("values", []))
                ),
            )
            for r in it.get("roundList", [])
        )
        items.append(
            OrderItem(
                product_code=it["productCode"],
                quantity=it["quantity"],
                rounds=rounds,
                coupon_id=it.get("couponId", ""),
                coupon_code=it.get("couponCode", ""),
            )
        )
    cand = SimpleNamespace(key=action["candidateKey"], items=tuple(items), note=action.get("note", ""))

    from conftest import fake_calculate_price
    from mcd_compromise.normalize import build_normalized_menu

    menu = build_normalized_menu(store_id=STORE, menu_response=MCP["menu"], detail_responses=MCP["comboDetails"], coupons_response=MCP["coupons"])
    resp = fake_calculate_price(cand, menu)
    # 转成 Quote 字典（与 QuoteManager.from_dict 约定一致）
    data = resp.get("data") or {}
    ok = bool(resp.get("success")) and data.get("productList") and data.get("price") is not None
    return {
        "candidateKey": action["candidateKey"],
        "status": "ok" if ok else ("failed" if not resp.get("success") else "invalid"),
        "priceFen": data.get("price") if ok else None,
        "originalPriceFen": data.get("originalPrice") if ok else None,
        "discountFen": data.get("discount") if ok else None,
        "quotedAt": "2026-10-12 16:00:00",
        "errorCode": "" if ok else str(resp.get("code", "")),
        "errorMessage": "" if ok else str(resp.get("message", "")),
        "productNames": [p.get("productName", "") for p in data.get("productList", [])] if ok else [],
    }


# ---- 场景 1：可行 -----------------------------------------------------------
req1 = {
    "requestVersion": 1,
    "context": {"storeId": STORE, "diningMode": "pickup", "people": 1},
    "constraints": [
        {"id": "budget", "kind": "total_price_max", "value": 3700, "unit": "fen", "strength": "hard", "negotiability": "ask", "priority": 50, "sourceText": "预算37元"},
        {"id": "staple", "kind": "include_staple", "value": "staple_beef", "strength": "hard", "negotiability": "ask", "priority": 80, "sourceText": "要一个牛肉汉堡"},
        {"id": "need_drink", "kind": "require_category", "value": "drink", "strength": "hard", "negotiability": "ask", "priority": 30, "sourceText": "要一杯饮料"},
        {"id": "no_fries", "kind": "exclude_category", "value": "fries", "strength": "hard", "negotiability": "locked", "priority": 10, "sourceText": "不要薯条"},
    ],
}
inp1, out1 = protocol(req1, "可行场景：37 元内、牛肉汉堡、饮料、不要薯条（模拟数据）")
assert out1["status"] == "feasible", out1["status"]
(ROOT / "examples" / "demo-feasible.json").write_text(json.dumps({"input": inp1, "output": out1}, ensure_ascii=False, indent=2), encoding="utf-8")
print("demo-feasible:", out1["status"], [p["planId"] for p in out1["plans"]])

# ---- 场景 2：冲突 → 最小妥协 -------------------------------------------------
req2 = {
    "requestVersion": 1,
    "context": {"storeId": STORE, "diningMode": "pickup", "people": 1},
    "constraints": [
        {"id": "budget", "kind": "total_price_max", "value": 2500, "unit": "fen", "strength": "hard", "negotiability": "ask", "priority": 50, "sourceText": "预算25元"},
        {"id": "staple", "kind": "include_staple", "value": "B001", "strength": "hard", "negotiability": "ask", "priority": 80, "sourceText": "要巨无霸"},
        {"id": "need_drink", "kind": "require_category", "value": "drink", "strength": "hard", "negotiability": "ask", "priority": 30, "sourceText": "要一杯饮料"},
        {"id": "no_fries", "kind": "exclude_category", "value": "fries", "strength": "hard", "negotiability": "locked", "priority": 10, "sourceText": "不要薯条"},
    ],
}
inp2, out2 = protocol(req2, "冲突场景：25 元内、巨无霸、饮料、不要薯条——最便宜满足其他条件的方案 35.00 元（模拟数据）")
assert out2["status"] == "compromise_proposed", out2["status"]
(ROOT / "examples" / "demo-conflict.json").write_text(json.dumps({"input": inp2, "output": out2}, ensure_ascii=False, indent=2), encoding="utf-8")
print("demo-conflict:", out2["status"], [(p["planId"], [c["description"] for c in p["proposedChanges"]]) for p in out2["plans"]])

# ---- 场景 3：连续协商 --------------------------------------------------------
# 用户说：「预算不能变，汉堡可以换，但饮料别取消。」
req3 = {
    "requestVersion": 2,
    "context": {"storeId": STORE, "diningMode": "pickup", "people": 1},
    "constraints": [
        {"id": "budget", "kind": "total_price_max", "value": 2500, "unit": "fen", "strength": "hard", "negotiability": "locked", "priority": 50, "sourceText": "预算25元（绝对不能变）"},
        {"id": "staple", "kind": "include_staple", "value": "staple_beef", "strength": "hard", "negotiability": "open", "priority": 80, "sourceText": "汉堡可以换（牛肉优先）"},
        {"id": "need_drink", "kind": "require_category", "value": "drink", "strength": "hard", "negotiability": "locked", "priority": 30, "sourceText": "饮料别取消"},
        {"id": "no_fries", "kind": "exclude_category", "value": "fries", "strength": "hard", "negotiability": "locked", "priority": 10, "sourceText": "不要薯条"},
    ],
}
inp3, out3 = protocol(req3, "连续协商：预算锁 25、汉堡可换、饮料保留、不要薯条——换吉士汉堡包+可乐 22.00 元（模拟数据）")
assert out3["status"] == "feasible", out3["status"]
(ROOT / "examples" / "demo-followup.json").write_text(json.dumps({"input": inp3, "output": out3}, ensure_ascii=False, indent=2), encoding="utf-8")
print("demo-followup:", out3["status"], [(p["planId"], [e["name"] for e in p["expanded"]], p["officialQuote"]["priceFen"]) for p in out3["plans"]])
print("OK")
