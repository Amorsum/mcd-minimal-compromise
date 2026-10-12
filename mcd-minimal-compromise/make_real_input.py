"""构造真实数据输入（来自 2026-10-12 对门店 1950640 的真实 MCP 调用）。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REAL = ROOT / "examples" / "real"

# 门店菜单（query-meals 真实返回，截取本文用到的字段原样保留）
menu_sample = json.loads((REAL / "store-menu-1950640.json").read_text(encoding="utf-8"))
coupons_sample = json.loads((REAL / "store-coupons-1950640.redacted.json").read_text(encoding="utf-8"))
combo_sample = json.loads((REAL / "meal-detail-9900005466.json").read_text(encoding="utf-8"))

request = {
    "requestVersion": 1,
    "context": {"storeId": "1950640", "diningMode": "pickup", "people": 1},
    "constraints": [
        {"id": "budget", "kind": "total_price_max", "value": 2500, "unit": "fen", "strength": "hard", "negotiability": "ask", "priority": 50, "sourceText": "预算25元"},
        {"id": "staple", "kind": "include_staple", "value": "staple_beef", "strength": "hard", "negotiability": "ask", "priority": 80, "sourceText": "要一个牛肉汉堡"},
        {"id": "need_drink", "kind": "require_category", "value": "drink", "strength": "hard", "negotiability": "ask", "priority": 30, "sourceText": "要一杯饮料"},
        {"id": "no_fries", "kind": "exclude_category", "value": "fries", "strength": "hard", "negotiability": "locked", "priority": 10, "sourceText": "不要薯条"},
    ],
}

inp = {
    "storeId": "1950640",
    "fetchedAt": "2026-10-12 16:00:00",
    "mcp": {
        "menu": menu_sample["response"],
        "comboDetails": [combo_sample["response"]],
        "coupons": coupons_sample["response"],
    },
    "request": request,
    "budget": {"firstBatch": 8, "roundBudget": 24},
    "maxPlans": 3,
}

out = ROOT / "examples" / "real" / "e2e-request.json"
out.write_text(json.dumps(inp, ensure_ascii=False, indent=2), encoding="utf-8")
print("written:", out)
