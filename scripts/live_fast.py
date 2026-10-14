#!/usr/bin/env python3
"""麦当劳 Skill 一键快速联调：单进程、单回合跑完全流程。

相对 scripts/live_e2e.py 的「分步手工编排」，本脚本针对**用户感知耗时**优化
（2026-10-14 早餐联调复盘：官方接口合计仅 ~60s，其余 ~15 分钟消耗在宿主的
~10 个编排回合上——每回合的模型推理 + 长输出导致的上下文膨胀）：

  1. 全流程合并：抓取 → 套餐详情 → 两阶段报价循环 → 终态 → 脱敏，
     一个进程一次跑完，宿主只需 1 个工具回合；
  2. 并发抓取：菜单/券/营养并行；套餐详情与报价按 --workers 并发
     （官方 RTT ~0.4s，串行 14 个详情 7s → 并发约 2s；并发度保守以防限流）；
  3. 引擎进程内调用（mcd_compromise.plan_cli.run），省掉每次 ~1.5s 子进程启动；
  4. 报价节流默认 0（--delay 可调）；
  5. 只打印摘要行，大 JSON 不进对话上下文；
  6. 结束后自动脱敏券引用并重键券商品报价（candidateKey 内嵌 couponId/couponCode，
     仅脱敏 coupons 数据会导致重跑退化为 quotes_pending，见 MCP_INTEGRATION.md 4.16）。

用法：
  # 早餐减脂场景（营养三约束，无预算）
  python scripts/live_fast.py --store 1990455 --protein-min 30 --fat-max 25 --energy-max 650

  # 经典场景（预算 + 牛肉堡 + 饮料 + 不要薯条）
  python scripts/live_fast.py --store 1950822 --budget 25 --staple beef

  # 快速档（更少主食/更少报价，牺牲覆盖度换速度）
  python scripts/live_fast.py --store 1990455 --mode fast --protein-min 30

  # 先发现营业中门店
  python scripts/live_fast.py --city 南京市 --keyword 南京邮电大学仙林校区
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Queue

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SKILL_ROOT / "src"))

from live_e2e import (  # noqa: E402
    DEFAULT_MCP_URL,
    McdClient,
    discover_combos,
    load_token,
    now_str,
    quote_dict,
    staple_constraint_value,
)
from mcd_compromise.plan_cli import run as plan_run  # noqa: E402
from mcd_compromise.models import MealRequest  # noqa: E402

MODE_PRESETS = {
    "fast": {"max_combos": 8, "first_batch": 16, "round_budget": 32},
    "full": {"max_combos": 14, "first_batch": 8, "round_budget": 96},
}

NUTRITION_SPECS = (
    ("protein_min", "protein_min", "蛋白质不低于", "g"),
    ("fat_max", "fat_max", "脂肪不超过", "g"),
    ("energy_max", "energy_max", "热量不超过", "kcal"),
)


class ClientPool:
    """固定数量的 MCP 客户端池，供并发抓取/报价复用（每个客户端独立会话）。"""

    def __init__(self, url: str, token: str, size: int):
        self.size = max(1, size)
        self._q: Queue = Queue()
        for _ in range(self.size):
            self._q.put(McdClient(url, token))

    @contextmanager
    def client(self):
        c = self._q.get()
        try:
            yield c
        finally:
            self._q.put(c)

    def parallel_map(self, func, items):
        if not items:
            return []

        def work(item):
            with self.client() as c:
                return func(c, item)

        with ThreadPoolExecutor(max_workers=self.size) as ex:
            return list(ex.map(work, items))


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="麦当劳 Skill 一键快速联调")
    p.add_argument("--store", help="门店编码（storeCode）")
    p.add_argument("--request", help="完整 MealRequest JSON 文件；保留全部约束和 requestVersion")
    p.add_argument("--city", help="发现门店：城市")
    p.add_argument("--keyword", help="发现门店：位置关键词")
    p.add_argument("--mcp-url", default=DEFAULT_MCP_URL)
    p.add_argument("--mode", choices=("fast", "full"), default="full",
                   help="full=完整覆盖（默认）；fast=更少主食与报价预算，更快")
    p.add_argument("--budget", type=float, default=None, help="预算（元）；不传则不设价格条件")
    p.add_argument("--staple", default=None, help="主食：beef/chicken/fish/other 或餐品编码")
    p.add_argument("--no-drink", action="store_true", help="不加「要一杯饮料」条件")
    p.add_argument("--exclude", action="append", default=None,
                   help="排除品类（可多次）；不传则不加排除条件，传 none 亦不加")
    p.add_argument("--protein-min", type=float, default=None, help="蛋白质下限（g）")
    p.add_argument("--fat-max", type=float, default=None, help="脂肪上限（g）")
    p.add_argument("--energy-max", type=float, default=None, help="热量上限（kcal）")
    p.add_argument("--lock-protein", action="store_true", help="蛋白质下限锁定（不可提议放宽）")
    p.add_argument("--lock-fat", action="store_true", help="脂肪上限锁定")
    p.add_argument("--lock-energy", action="store_true", help="热量上限锁定")
    p.add_argument("--workers", type=int, default=4, help="并发数（默认 4，防限流）")
    p.add_argument("--delay", type=float, default=0.0, help="每次官方调用后的节流（秒）")
    p.add_argument("--max-plans", type=int, default=3)
    p.add_argument("--max-rounds", type=int, default=8)
    p.add_argument("--out-dir", help="产物目录（默认 examples/real/live-<时间戳>/）")
    args = p.parse_args(argv)
    args.request_data = None
    if args.request:
        # 完整需求模式不能混用简写条件，防止用户原有条件被悄悄覆盖。
        if (args.budget is not None or args.staple or args.no_drink or args.exclude is not None
                or any(getattr(args, k) is not None for k in ("protein_min", "fat_max", "energy_max"))
                or args.lock_protein or args.lock_fat or args.lock_energy):
            p.error("--request 不能与预算、主食、饮料、排除或营养简写参数混用")
        args.request_data = json.loads(pathlib.Path(args.request).read_text(encoding="utf-8"))
        request = MealRequest.from_dict(args.request_data)
        if request.context.dining_mode != "pickup" or request.context.people != 1:
            p.error("首版仅支持到店自取、单人选餐")
        if not request.context.store_id:
            p.error("完整需求必须包含用户选定的门店")
        if args.store and str(args.store) != request.context.store_id:
            p.error("--store 与需求文件中的门店不一致")
        args.store = request.context.store_id
    if not args.store and not (args.city and args.keyword):
        p.error("请提供 --store，或提供 --city + --keyword 以发现门店")
    if args.exclude == ["none"]:
        args.exclude = []
    args.drink = not args.no_drink
    return args


def build_constraints(args) -> list[dict]:
    cs: list[dict] = []
    if args.budget is not None:
        cs.append({"id": "budget", "kind": "total_price_max",
                   "value": int(round(args.budget * 100)), "unit": "fen",
                   "strength": "hard", "negotiability": "ask", "priority": 50,
                   "sourceText": f"预算{args.budget:g}元"})
    if args.staple:
        cs.append({"id": "staple", "kind": "include_staple",
                   "value": staple_constraint_value(args.staple),
                   "strength": "hard", "negotiability": "ask", "priority": 80,
                   "sourceText": f"要{args.staple}"})
    if args.drink:
        cs.append({"id": "need_drink", "kind": "require_category", "value": "drink",
                   "strength": "hard", "negotiability": "ask", "priority": 30,
                   "sourceText": "要一杯饮料"})
    for i, cat in enumerate(args.exclude or []):
        cs.append({"id": f"exclude_{cat}", "kind": "exclude_category", "value": cat,
                   "strength": "hard", "negotiability": "locked", "priority": 10 + i,
                   "sourceText": f"不要{cat}"})
    values = {"protein_min": args.protein_min, "fat_max": args.fat_max, "energy_max": args.energy_max}
    locks = {"protein_min": args.lock_protein, "fat_max": args.lock_fat, "energy_max": args.lock_energy}
    for prio, (cid, kind, label, unit) in enumerate(NUTRITION_SPECS, start=60):
        val = values[cid]
        if val is None:
            continue
        cs.append({"id": cid, "kind": kind, "value": val, "unit": unit,
                   "strength": "hard",
                   "negotiability": "locked" if locks[cid] else "ask",
                   "priority": prio,
                   "sourceText": f"{label}{val:g}{unit}（可改）"})
    return cs


def redact_artifacts(run_dir: pathlib.Path) -> None:
    """同步脱敏全部 JSON 产物中的券字段、候选键及报价键，保持引用一致。"""
    paths = sorted(run_dir.glob("*.json"))
    documents = [(path, json.loads(path.read_text(encoding="utf-8"))) for path in paths]
    aliases = {}
    pattern = re.compile(r'("coupon(?:Id|Code)":\s*")([^"\\]*)(")')

    def alias(value):
        if not value or value.startswith("REDACTED"):
            return value
        if value not in aliases:
            aliases[value] = f"REDACTED-LIVE-{len(aliases) + 1:04d}"
        return aliases[value]

    def redact(obj):
        if isinstance(obj, dict):
            return {redact(k): (alias(v) if k.lower() in {"couponid", "couponcode"}
                                and isinstance(v, str) else redact(v))
                    for k, v in obj.items()}
        if isinstance(obj, list):
            return [redact(v) for v in obj]
        if isinstance(obj, str):
            return pattern.sub(lambda m: m[1] + alias(m[2]) + m[3], obj)
        return obj

    for path, data in documents:
        path.write_text(json.dumps(redact(data), ensure_ascii=False, indent=2), encoding="utf-8")


def quote_one(client, action):
    """带一次重试的官方报价；异常按 failed 处理（不当 0 元）。"""
    key = action["candidateKey"]
    last_err = None
    for attempt in range(2):
        try:
            resp = client.call("calculate-price", action["mcpCall"]["params"])
            return quote_dict(key, resp, now_str())
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0)
    return {"candidateKey": key, "quotedAt": now_str(), "status": "failed",
            "priceFen": None, "originalPriceFen": None, "discountFen": None,
            "errorCode": "CALL_EXCEPTION", "errorMessage": f"调用异常: {last_err}",
            "productNames": [], "productCodeNames": []}


def print_result(result: dict) -> None:
    status = result.get("status")
    cov = result.get("coverage", {})
    print(f"\n===== 终态：{status} =====")
    print(f"覆盖度：生成 {cov.get('generatedCount', 0)} / 已报价 {cov.get('quotedCount', 0)}"
          f" / 失败 {cov.get('failedCount', 0)} / 截断={cov.get('truncated')}")
    for note in result.get("notes", []):
        print(f"备注：{note}")
    for p in result.get("plans", []):
        items = "、".join(e["name"] for e in p.get("expanded", []))
        price = (p.get("officialQuote", {}).get("priceFen") or 0) / 100
        print(f"\n[{p['planId']}] {items}  试算总价 ¥{price:.2f}")
        for c in p.get("proposedChanges", []):
            print(f"    待接受改变：{c['description']}")
        kept = [c["constraintId"] for c in p.get("satisfiedConstraints", [])]
        if kept:
            print(f"    保留条件：{', '.join(kept)}")
        for u in p.get("unknowns", []):
            print(f"    未知项：{u}")
    for c in result.get("closestOptions", []):
        items = "、".join(e["name"] for e in c.get("expanded", []))
        price = (c.get("officialQuote", {}).get("priceFen") or 0) / 100
        violated = [v["constraintId"] for v in c.get("violatedConstraints", [])]
        print(f"\n[参考·未满足锁定条件] {items}  试算总价 ¥{price:.2f}  违反：{', '.join(violated)}")
    print("=======================\n")


def main(argv=None) -> int:
    args = parse_args(argv)
    t0 = time.time()
    token = load_token()
    preset = MODE_PRESETS[args.mode]
    constraints = (args.request_data["constraints"] if args.request_data is not None
                   else build_constraints(args))

    if args.out_dir:
        out_dir = pathlib.Path(args.out_dir)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = SKILL_ROOT / "examples" / "real" / f"live-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"模式：{args.mode}（max_combos={preset['max_combos']}, "
          f"firstBatch={preset['first_batch']}, roundBudget={preset['round_budget']}）")
    print(f"产物目录：{out_dir}")

    # 0. 发现门店
    if not args.store:
        client = McdClient(args.mcp_url, token)
        stores = client.call("query-nearby-stores",
                             {"searchType": 2, "city": args.city, "keyword": args.keyword, "beType": 1})
        (out_dir / "nearby-stores.json").write_text(
            json.dumps(stores, ensure_ascii=False, indent=2), encoding="utf-8")
        print("附近门店（请选择其一，用 --store 重跑）：")
        for s in stores.get("data") or []:
            if not isinstance(s, dict):
                continue
            mark = "营业中" if s.get("businessStatus") else "已休息"
            print(f"  {s.get('storeCode')}  {s.get('storeName', '')}"
                  f"  [{mark} {s.get('businessStartTime', '')}-{s.get('businessEndTime', '')}]"
                  f"  {s.get('address', '')}")
        return 0

    store = str(args.store)
    pool = ClientPool(args.mcp_url, token, args.workers)

    # 1. 并发抓取：菜单 / 优惠券 / 营养表
    t = time.time()
    specs = [("query-meals", {"storeCode": store, "orderType": 1, "beType": 1}, "menu.json")]
    specs.append(("query-store-coupons", {"storeCode": store, "orderType": 1, "beType": 1}, "coupons.json"))
    need_nutrition = any(c["kind"] in {"protein_min", "fat_max", "energy_max"} for c in constraints)
    if need_nutrition:
        specs.append(("list-nutrition-foods", {}, "nutrition.json"))

    def fetch(client, spec):
        tool, arguments, name = spec
        resp = client.call(tool, arguments)
        (out_dir / name).write_text(json.dumps(resp, ensure_ascii=False, indent=2), encoding="utf-8")
        return name, resp

    fetched = pool.parallel_map(fetch, specs)
    raw = {name: resp for name, resp in fetched}
    menu = raw["menu.json"]
    if not menu.get("success"):
        raise SystemExit(f"query-meals 失败：code={menu.get('code')} {menu.get('message')}"
                         "（门店可能已关闭，请换店或用 --city/--keyword 发现营业中门店）")
    coupons = raw.get("coupons.json") or {"data": []}
    nutrition = raw.get("nutrition.json")
    print(f"抓取完成（{len(specs)} 个调用并发，{time.time() - t:.1f}s）："
          f"餐品 {len((menu.get('data') or {}).get('meals') or {})} 项，"
          f"优惠 {len(coupons.get('data') or [])} 项"
          + (f"，营养表 {len(str(nutrition.get('data') or '').splitlines()) - 1} 条" if nutrition else ""))

    # 2. 套餐详情（并发）
    t = time.time()
    combos = discover_combos(menu, args.staple, preset["max_combos"])
    print(f"拉取 {len(combos)} 个套餐详情（并发 {args.workers}）…")

    def fetch_detail(client, combo):
        return client.call("query-meal-detail",
                           {"storeCode": store, "orderType": 1, "beType": 1, "code": combo["code"]})

    details = pool.parallel_map(fetch_detail, combos)
    (out_dir / "combo-details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"套餐详情完成（{time.time() - t:.1f}s）")

    # 3. 组装请求
    print("需求条件：")
    for c in constraints:
        print(f"  [{c['id']}] {c['kind']}={c['value']}{c.get('unit', '')}（{c['negotiability']}）")
    if not constraints:
        print("  （无条件：按试算价从低到高展示）")

    payload = {
        "storeId": store, "fetchedAt": now_str(),
        "mcp": {"menu": menu, "comboDetails": details, "coupons": coupons},
        "request": args.request_data if args.request_data is not None else {"requestVersion": 1,
                    "context": {"storeId": store, "diningMode": "pickup", "people": 1},
                    "constraints": constraints},
        "quotes": {"budget": {"firstBatch": preset["first_batch"],
                              "roundBudget": preset["round_budget"], "used": 0},
                   "quotes": {}},
        "budget": {"firstBatch": preset["first_batch"],
                   "roundBudget": preset["round_budget"], "used": 0},
        "maxPlans": args.max_plans,
    }
    if nutrition is not None:
        payload["mcp"]["nutrition"] = nutrition
    req_path = out_dir / "request.json"
    req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. 两阶段报价循环（引擎进程内调用 + 报价并发）
    t = time.time()
    result = plan_run(payload)
    payload["quotes"] = result["quotes"]
    round_no = 0
    while result.get("status") == "quotes_pending" and round_no < args.max_rounds:
        round_no += 1
        actions = result.get("nextActions") or []
        print(f"第 {round_no} 轮报价：{len(actions)} 个候选（并发 {args.workers}）…")

        def quote_action(client, action):
            if args.delay:
                time.sleep(args.delay)
            return quote_one(client, action)

        quotes_list = pool.parallel_map(quote_action, actions)
        for a, q in zip(actions, quotes_list):
            payload["quotes"]["quotes"][a["candidateKey"]] = q
        used = payload["quotes"]["budget"]["used"] + len(actions)
        payload["quotes"]["budget"]["used"] = used
        payload["budget"]["used"] = used
        req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = plan_run(payload)
        payload["quotes"] = result["quotes"]

    if result.get("status") == "quotes_pending":
        print(f"已达最大报价轮数（{args.max_rounds}），仍有候选待报价")
    print(f"报价循环耗时：{time.time() - t:.1f}s")

    # 5. 脱敏归档 + 结果（request.json 已是最新状态，脱敏原地进行，不再回写未脱敏 payload）
    req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    redact_artifacts(out_dir)
    print_result(result)
    print(f"总耗时：{time.time() - t0:.1f}s | 过程文件：{out_dir}")
    print("提醒：目录已按惯例脱敏券引用；对外分享前请复查")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
