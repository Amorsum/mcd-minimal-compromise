"""麦麦不将就规划引擎 CLI（两阶段报价协议）。

完整说明见 scripts/plan.py 的文档字符串；
scripts/plan.py 是本模块的薄包装，保证 Skill 调用路径稳定。
"""

from __future__ import annotations

import json
from pathlib import Path

from .compromise import plan_next
from .models import MealRequest, PlanStatus
from .normalize import build_normalized_menu
from .nutrition import NutritionMap
from .quote_state import QuoteBudget, QuoteManager


def build_menu(payload: dict):
    """从宿主采集的官方 MCP 原始返回构建标准化菜单。"""
    mcp = payload.get("mcp") or {}
    store_id = str(
        payload.get("storeId")
        or (payload.get("request", {}).get("context", {}).get("storeId", ""))
    )
    menu = build_normalized_menu(
        store_id=store_id,
        menu_response=mcp.get("menu") or {},
        detail_responses=mcp.get("comboDetails") or [],
        coupons_response=mcp.get("coupons"),
        fetched_at=str(payload.get("fetchedAt", "")),
    )
    nutrition = None
    if mcp.get("nutrition"):
        nutrition = NutritionMap.from_mcp_response(mcp["nutrition"])
    return menu, nutrition


def build_quote_call(store_id: str, candidate_dict: dict) -> dict:
    """为待报价候选生成官方 calculate-price 调用参数。"""
    items = []
    for it in candidate_dict.get("items", []):
        entry = {"productCode": it["productCode"], "quantity": it["quantity"]}
        if it.get("roundList"):
            entry["roundList"] = it["roundList"]
        if it.get("couponId"):
            entry["couponId"] = it["couponId"]
        if it.get("couponCode"):
            entry["couponCode"] = it["couponCode"]
        items.append(entry)
    return {
        "tool": "calculate-price",
        "params": {"storeCode": store_id, "orderType": 1, "beType": 1, "items": items},
    }


def _summary(out: dict) -> dict:
    status = out["status"]
    s = {"status": status, "coverage": out["coverage"], "notes": out.get("notes", [])}
    if status == "quotes_pending":
        s["pendingQuoteCount"] = len(out.get("nextActions", []))
        s["hint"] = "请按 nextActions 调用官方 calculate-price，并把返回写回 quotes 后重新运行"
    elif status == "feasible":
        s["plans"] = [
            {
                "planId": p["planId"],
                "items": [e["name"] for e in p["expanded"]],
                "priceYuan": (p["officialQuote"]["priceFen"] or 0) / 100,
                "quotedAt": p["officialQuote"].get("quotedAt", ""),
            }
            for p in out["plans"]
        ]
    elif status == "compromise_proposed":
        s["plans"] = [
            {
                "planId": p["planId"],
                "items": [e["name"] for e in p["expanded"]],
                "priceYuan": (p["officialQuote"]["priceFen"] or 0) / 100,
                "proposedChanges": [c["description"] for c in p["proposedChanges"]],
                "kept": [c2["constraintId"] for c2 in p["satisfiedConstraints"]],
            }
            for p in out["plans"]
        ]
    else:
        s["plans"] = []
    return s


def run(payload: dict) -> dict:
    """执行一轮规划，返回可序列化的结果字典。"""
    menu, nutrition = build_menu(payload)
    request = MealRequest.from_dict(payload["request"])

    quotes = QuoteManager.from_dict(payload["quotes"]) if payload.get("quotes") else QuoteManager()
    budget = None
    if payload.get("budget"):
        budget = QuoteBudget(
            first_batch=int(payload["budget"].get("firstBatch", 8)),
            round_budget=int(payload["budget"].get("roundBudget", 24)),
            used=int(payload["budget"].get("used", 0)),
        )
    max_plans = int(payload.get("maxPlans", 3))

    result = plan_next(
        menu=menu,
        request=request,
        quotes=quotes,
        budget=budget,
        max_plans=max_plans,
        nutrition=nutrition,
    )

    out = result.to_dict()
    out["quotes"] = quotes.to_dict()
    if result.status == PlanStatus.QUOTES_PENDING:
        out["nextActions"] = [
            {
                "candidateKey": c.key,
                "note": c.note,
                "mcpCall": build_quote_call(request.context.store_id, c.to_dict()),
            }
            for c in result.pending_quotes
        ]
    else:
        out["nextActions"] = []
    out["summary"] = _summary(out)
    return out


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="麦麦不将就规划引擎（两阶段报价协议）")
    parser.add_argument("--input", required=True, help="输入 JSON 路径")
    parser.add_argument("--output", required=True, help="输出 JSON 路径")
    args = parser.parse_args(argv)

    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    out = run(payload)
    Path(args.output).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
