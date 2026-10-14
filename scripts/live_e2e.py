#!/usr/bin/env python3
"""麦当劳 Skill 真实端到端联调夹具（本地开发工具，不属于 Skill 运行时）。

复刻 SKILL.md 中「宿主」的完整职责：

  1. query-meals + query-store-coupons     门店菜单与可用优惠（第 4 节步骤 2/3）
  2. query-meal-detail                     相关套餐展开（步骤 4，自动发现套餐并按
                                           「主食关键词优先 + 价格升序」取前 N 个）
  3. scripts/plan.py 首轮                  → quotes_pending + nextActions
  4. 按 nextActions 逐候选调用官方 calculate-price，按 candidateKey 写回 quotes
  5. scripts/plan.py 次轮                  → 终态（feasible / compromise_proposed /
                                           not_found / data_incomplete）

凭证只用于调用官方接口，不写入任何输出文件：
  环境变量 MCD_MCP_TOKEN > ~/.workbuddy/mcp.json 中 mcd-mcp 的 Authorization。

用法：
  # 经典场景（README 用例）：预算 25 + 牛肉堡 + 一杯饮料 + 不要薯条
  python scripts/live_e2e.py --store 1950640

  # 自定义场景
  python scripts/live_e2e.py --store 1950640 --budget 30 --staple chicken --no-drink

  # 先发现门店（到店自取）
  python scripts/live_e2e.py --city 北京 --keyword 中关村

注意：产物目录内含券 ID/券码等引用信息，对外分享前请按
examples/real/*.redacted.json 的惯例脱敏。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "src"))
PLAN_SCRIPT = SKILL_ROOT / "scripts" / "plan.py"
DEFAULT_MCP_URL = "https://mcp.mcd.cn"

STAPLE_KEYWORDS = {
    "beef": ("牛堡", "吉士", "安格斯", "牛肉"),
    "chicken": ("鸡腿", "鸡翅", "鸡肉", "麦辣"),
    "fish": ("鳕鱼", "麦香鱼"),
    "other": (),
}
COMBO_NAME_HINTS = (
    "三件套", "四件套", "随心选", "随心配", "套餐", "单人餐",
    "分享餐", "双人餐", "随心拼", "拼盘",
)


def now_str() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def load_token() -> str:
    token = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if token:
        return token
    cfg = Path.home() / ".workbuddy" / "mcp.json"
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            auth = (
                data.get("mcpServers", {})
                .get("mcd-mcp", {})
                .get("headers", {})
                .get("Authorization", "")
            )
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        except Exception:
            token = ""
    if not token:
        raise SystemExit(
            "未找到麦当劳 MCP Token：请设置环境变量 MCD_MCP_TOKEN，"
            "或在 ~/.workbuddy/mcp.json 配置 mcd-mcp"
        )
    return token


class McdClient:
    """最小 MCP streamable HTTP 客户端（仅标准库）。"""

    def __init__(self, url: str, token: str, timeout: int = 60):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.session_id: str | None = None
        self._req_id = 0
        self._initialize()

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                ctype = resp.headers.get("Content-Type", "")
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            raise RuntimeError(f"MCP HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"MCP 网络错误: {e}") from e
        if "text/event-stream" in ctype:
            return self._parse_sse(raw)
        raw = raw.strip()
        if not raw:
            return {}  # 通知类请求可能返回 202/204 空 body
        return json.loads(raw)

    @staticmethod
    def _parse_sse(raw: str) -> dict:
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        for line in reversed(data_lines):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        raise RuntimeError(f"无法解析 SSE 响应: {raw[:300]}")

    def _initialize(self) -> None:
        last_err: Exception | None = None
        for version in ("2025-03-26", "2024-11-05"):
            try:
                self._post(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": version,
                            "capabilities": {},
                            "clientInfo": {"name": "mcd-live-e2e", "version": "1.0"},
                        },
                    }
                )
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
        else:
            raise RuntimeError(f"MCP initialize 失败: {last_err}")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict) -> dict:
        self._req_id += 1
        resp = self._post(
            {
                "jsonrpc": "2.0",
                "id": self._req_id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        if "error" in resp:
            raise RuntimeError(f"MCP 工具 {tool} 报错: {json.dumps(resp['error'], ensure_ascii=False)[:300]}")
        result = resp.get("result", {})
        if result.get("isError"):
            raise RuntimeError(
                f"MCP 工具 {tool} 返回错误: {json.dumps(result, ensure_ascii=False)[:300]}"
            )
        # 官方返回的 JSON 在 structuredContent；content[].text 是给人看的说明
        structured = result.get("structuredContent")
        if isinstance(structured, dict) and structured:
            return structured
        for block in result.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                try:
                    return json.loads(block["text"])
                except json.JSONDecodeError:
                    return {"_raw_text": block["text"]}
        return result


# ---------------------------------------------------------------------------
# 套餐发现与约束构造
# ---------------------------------------------------------------------------


def discover_combos(menu_resp: dict, staple: str | None, limit: int) -> list[dict]:
    """从菜单中识别套餐（含 rounds 选配），按「主食关键词优先 + 价格升序」排序。"""
    data = (menu_resp or {}).get("data") or {}
    details = data.get("meals") or {}
    combos: list[dict] = []
    seen: set[str] = set()
    for cat in data.get("categories") or []:
        for m in cat.get("meals") or []:
            code = str(m.get("code", ""))
            info = details.get(code) or {}
            name = str(info.get("name", ""))
            tags = [str(t) for t in (m.get("tags") or [])]
            is_combo = ("套餐" in tags or "随心配" in tags
                        or any(h in name for h in COMBO_NAME_HINTS))
            if not is_combo or not name or code in seen:
                continue
            seen.add(code)
            try:
                price_fen = int(round(float(str(info.get("currentPrice", "0") or "0")) * 100))
            except ValueError:
                price_fen = 0
            combos.append(
                {"code": code, "name": name, "priceFen": price_fen, "category": str(cat.get("name", ""))}
            )
    keywords = STAPLE_KEYWORDS.get(staple or "", ())
    combos.sort(key=lambda c: (0 if (keywords and any(k in c["name"] for k in keywords)) else 1, c["priceFen"]))
    return combos[:limit]


def staple_constraint_value(staple: str) -> str:
    if staple in ("beef", "chicken", "fish", "other"):
        return f"staple_{staple}"
    return staple  # 视为具体餐品编码


def build_constraints(args: argparse.Namespace) -> list[dict]:
    constraints: list[dict] = []
    if args.budget is not None:
        constraints.append(
            {
                "id": "budget",
                "kind": "total_price_max",
                "value": int(round(args.budget * 100)),
                "unit": "fen",
                "strength": "hard",
                "negotiability": "locked" if args.lock_budget else "ask",
                "priority": 50,
                "sourceText": f"预算{args.budget:g}元",
            }
        )
    if args.staple:
        constraints.append(
            {
                "id": "staple",
                "kind": "include_staple",
                "value": staple_constraint_value(args.staple),
                "strength": "hard",
                "negotiability": "locked" if args.lock_staple else "ask",
                "priority": 80,
                "sourceText": args.staple_text or f"要{args.staple}",
            }
        )
    if args.drink:
        constraints.append(
            {
                "id": "need_drink",
                "kind": "require_category",
                "value": "drink",
                "strength": "hard",
                "negotiability": "ask",
                "priority": 30,
                "sourceText": "要一杯饮料",
            }
        )
    for i, cat in enumerate(args.exclude or []):
        constraints.append(
            {
                "id": f"exclude_{cat}",
                "kind": "exclude_category",
                "value": cat,
                "strength": "hard",
                "negotiability": "locked" if args.lock_exclude else "ask",
                "priority": 10 + i,
                "sourceText": f"不要{cat}",
            }
        )
    return constraints


# ---------------------------------------------------------------------------
# 两阶段报价协议
# ---------------------------------------------------------------------------


def quote_dict(key: str, resp: dict, quoted_at: str) -> dict:
    """把官方 calculate-price 返回转换为输入协议中的 Quote 字典。

    有效性与 QuoteManager.register_mcp_result 保持一致：
    success=false → failed；空商品清单或缺价格 → invalid（不当 0 元）。
    """
    from mcd_compromise.quote_state import QuoteManager

    return QuoteManager().register_mcp_result(key, resp, quoted_at).to_dict()


def run_plan(req_path: Path, out_path: Path) -> dict:
    proc = subprocess.run(
        [sys.executable, str(PLAN_SCRIPT), "--input", str(req_path), "--output", str(out_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"plan.py 运行失败:\n{proc.stderr[-800:]}")
    return json.loads(out_path.read_text(encoding="utf-8"))


def print_result(result: dict) -> None:
    status = result.get("status")
    cov = result.get("coverage", {})
    print(f"\n===== 终态：{status} =====")
    print(
        "覆盖度：生成 {generatedCount} 候选 / 已报价 {quotedCount} / 失败 {failedCount}"
        " / 截断={truncated}".format(**{**{"generatedCount": 0, "quotedCount": 0, "failedCount": 0, "truncated": False}, **cov})
    )
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


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="麦当劳 Skill 真实端到端联调夹具")
    parser.add_argument("--store", help="门店编码（storeCode，纯数字）")
    parser.add_argument("--city", help="发现门店：城市（searchType=2）")
    parser.add_argument("--keyword", help="发现门店：位置关键词")
    parser.add_argument("--mcp-url", default=DEFAULT_MCP_URL, help="MCP 服务地址")
    parser.add_argument("--budget", type=float, default=25.0, help="预算（元），默认 25")
    parser.add_argument("--staple", default="beef",
                        help="主食：beef/chicken/fish/other 或具体餐品编码，默认 beef")
    parser.add_argument("--staple-text", help="主食条件的用户原话（用于解释）")
    parser.add_argument("--no-drink", action="store_true", help="不加「要一杯饮料」条件")
    parser.add_argument("--exclude", action="append", default=None,
                        help="排除品类（可多次），默认 fries；传 none 清空")
    parser.add_argument("--lock-budget", action="store_true", help="预算设为 locked（不提议改预算）")
    parser.add_argument("--lock-staple", action="store_true", help="主食设为 locked")
    parser.add_argument("--lock-exclude", action="store_true", help="排除项设为 locked（默认即锁定）")
    parser.add_argument("--max-combos", type=int, default=10, help="最多拉取多少个套餐详情")
    parser.add_argument("--first-batch", type=int, default=8, help="首批报价候选数")
    parser.add_argument("--round-budget", type=int, default=24, help="单轮报价预算")
    parser.add_argument("--max-rounds", type=int, default=8, help="报价循环最大轮数")
    parser.add_argument("--max-plans", type=int, default=3, help="最多输出方案数")
    parser.add_argument("--delay", type=float, default=0.2, help="官方接口调用间隔（秒）")
    parser.add_argument("--out-dir", help="产物目录（默认 examples/real/live-<时间戳>/）")
    args = parser.parse_args(argv)
    if args.exclude is None:
        args.exclude = ["fries"]
    elif args.exclude == ["none"]:
        args.exclude = []
    args.drink = not args.no_drink
    if not args.store and not (args.city and args.keyword):
        parser.error("请提供 --store，或提供 --city + --keyword 以发现门店")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    token = load_token()
    client = McdClient(args.mcp_url, token)

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = SKILL_ROOT / "examples" / "real" / f"live-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"产物目录：{out_dir}")

    # 0. 可选：发现门店
    if not args.store:
        stores = client.call(
            "query-nearby-stores",
            {"searchType": 2, "city": args.city, "keyword": args.keyword, "beType": 1},
        )
        (out_dir / "nearby-stores.json").write_text(
            json.dumps(stores, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("附近门店（请选择其一，用 --store 重跑）：")
        for s in stores.get("data") or []:
            if not isinstance(s, dict):
                continue
            open_mark = "营业中" if s.get("businessStatus") else "已休息"
            print(
                f"  {s.get('storeCode')}  {s.get('storeName', '')}"
                f"  [{open_mark} {s.get('businessStartTime', '')}-{s.get('businessEndTime', '')}]"
                f"  {s.get('address', '')}"
            )
        return 0

    store = str(args.store)
    print(f"门店：{store}（到店自取 orderType=1, beType=1）")

    # 1. 菜单 + 优惠券
    menu = client.call("query-meals", {"storeCode": store, "orderType": 1, "beType": 1})
    if not menu.get("success"):
        raise SystemExit(
            f"query-meals 失败：code={menu.get('code')} message={menu.get('message')}"
            "（若提示门店关闭，请换一门店或用 --city/--keyword 发现营业中的门店）"
        )
    coupons = client.call("query-store-coupons", {"storeCode": store, "orderType": 1, "beType": 1})
    (out_dir / "menu.json").write_text(json.dumps(menu, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "coupons.json").write_text(json.dumps(coupons, ensure_ascii=False, indent=2), encoding="utf-8")
    coupon_titles = [c.get("title", "") for c in (coupons.get("data") or [])]
    print(f"菜单餐品数：{len((menu.get('data') or {}).get('meals') or {})}；可用优惠：{coupon_titles or '无'}")

    # 2. 相关套餐详情
    combos = discover_combos(menu, args.staple, args.max_combos)
    if not combos:
        raise SystemExit("菜单中未发现套餐，无法生成候选")
    print(f"\n拉取 {len(combos)} 个套餐详情：")
    details = []
    for c in combos:
        d = client.call(
            "query-meal-detail", {"storeCode": store, "orderType": 1, "beType": 1, "code": c["code"]}
        )
        details.append(d)
        print(f"  {c['code']}  {c['name']}  ¥{c['priceFen'] / 100:g}")
        time.sleep(args.delay)
    (out_dir / "combo-details.json").write_text(
        json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 3. 组装 plan.py 输入
    constraints = build_constraints(args)
    print("\n需求条件：")
    for c in constraints:
        print(f"  [{c['id']}] {c['kind']}={c['value']}（{c['negotiability']}，{c['sourceText']}）")
    payload = {
        "storeId": store,
        "fetchedAt": now_str(),
        "mcp": {"menu": menu, "comboDetails": details, "coupons": coupons},
        "request": {
            "requestVersion": 1,
            "context": {"storeId": store, "diningMode": "pickup", "people": 1},
            "constraints": constraints,
        },
        "quotes": {
            "budget": {"firstBatch": args.first_batch, "roundBudget": args.round_budget, "used": 0},
            "quotes": {},
        },
        "budget": {"firstBatch": args.first_batch, "roundBudget": args.round_budget, "used": 0},
        "maxPlans": args.max_plans,
    }
    req_path = out_dir / "request.json"
    req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. 两阶段报价循环
    result = run_plan(req_path, out_dir / "result-1.json")
    payload["quotes"] = result["quotes"]
    round_no = 0
    while result.get("status") == "quotes_pending" and round_no < args.max_rounds:
        round_no += 1
        actions = result.get("nextActions") or []
        print(f"\n第 {round_no} 轮报价：{len(actions)} 个候选")
        quotes_map = payload["quotes"]["quotes"]
        for a in actions:
            key = a["candidateKey"]
            resp = client.call("calculate-price", a["mcpCall"]["params"])
            quotes_map[key] = quote_dict(key, resp, now_str())
            price = quotes_map[key].get("priceFen")
            print(f"  {key[:60]}... → {quotes_map[key]['status']}"
                  + (f" ¥{price / 100:g}" if price else ""))
            time.sleep(args.delay)
        used = int(payload["quotes"]["budget"].get("used", 0)) + len(actions)
        payload["quotes"]["budget"]["used"] = used
        payload["budget"]["used"] = used
        req_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        result = run_plan(req_path, out_dir / f"result-{round_no + 1}.json")
        payload["quotes"] = result["quotes"]

    if result.get("status") == "quotes_pending":
        print(f"\n已达最大报价轮数（{args.max_rounds}），仍有候选待报价；可加大 --max-rounds 或 --round-budget 重试")
    print_result(result)
    print(f"过程文件（请求/每轮结果）已保存到：{out_dir}")
    print("提醒：目录内含券 ID/券码引用，对外分享前请脱敏（参照 examples/real/*.redacted.json）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
