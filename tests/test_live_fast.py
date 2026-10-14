"""快速宿主入口的受控端到端测试，不连接官方接口。"""

import copy
import json
import sys

import pytest

from conftest import ROOT
from mcd_compromise.models import Candidate, OrderItem, RoundSelection

sys.path.insert(0, str(ROOT / "scripts"))
import live_fast
from live_e2e import quote_dict
from conftest import fake_calculate_price


def request_file(tmp_path):
    payload = json.loads((ROOT / "examples/demo-feasible.json").read_text(encoding="utf-8"))["input"]
    request = copy.deepcopy(payload["request"])
    request["requestVersion"] = 7
    for c in request["constraints"]:
        c["negotiability"] = "locked"
    path = tmp_path / "meal-request.json"
    path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return path, request


def test_complete_request_rejects_conflicting_flags(tmp_path):
    path, _ = request_file(tmp_path)
    with pytest.raises(SystemExit):
        live_fast.parse_args(["--request", str(path), "--budget", "25"])
    with pytest.raises(SystemExit):
        live_fast.parse_args(["--request", str(path), "--store", "OTHER"])


def test_fast_entry_preserves_request_and_finishes_with_verified_quotes(tmp_path, monkeypatch, menu, fixture_payload):
    path, request = request_file(tmp_path)
    mcp = fixture_payload["mcp"]
    calls = []

    class FakeClient:
        def call(self, tool, args):
            calls.append(tool)
            if tool == "query-meals":
                return mcp["menu"]
            if tool == "query-store-coupons":
                return mcp["coupons"]
            if tool == "query-meal-detail":
                for detail in mcp["comboDetails"]:
                    if str(detail["data"]["code"]) == args["code"]:
                        return detail
                return {"success": False}
            assert tool == "calculate-price"
            items = []
            for item in args["items"]:
                rounds = tuple(RoundSelection(round_id=str(r["round"]), choice_code=str(c["code"]),
                                              quantity=c.get("quantity", 1))
                               for r in item.get("roundList", []) for c in r["comboItemList"])
                items.append(OrderItem(item["productCode"], item["quantity"], rounds,
                                       item.get("couponId", ""), item.get("couponCode", "")))
            return fake_calculate_price(Candidate("fake", tuple(items)), menu)

    class FakePool:
        def __init__(self, *args):
            pass

        def parallel_map(self, func, items):
            return [func(FakeClient(), item) for item in items]

    monkeypatch.setattr(live_fast, "ClientPool", FakePool)
    monkeypatch.setattr(live_fast, "load_token", lambda: "TEST-NO-REAL-TOKEN")
    out_dir = tmp_path / "outputs"
    assert live_fast.main(["--request", str(path), "--out-dir", str(out_dir)]) == 0
    saved = json.loads((out_dir / "request.json").read_text(encoding="utf-8"))
    result = json.loads((out_dir / "result.json").read_text(encoding="utf-8"))
    assert saved["request"] == request
    assert result["requestVersion"] == 7
    assert result["status"] == "feasible"
    assert result["quotes"]["context"]["storeId"] == request["context"]["storeId"]
    assert result["plans"] and all(p["verified"] for p in result["plans"])
    assert "calculate-price" in calls


def test_archive_redacts_result_and_preserves_unique_quote_keys(tmp_path):
    keys = [json.dumps({"couponId": "PRIVATE-ID", "couponCode": code}) for code in ("PRIVATE-A", "PRIVATE-B")]
    data = {"quotes": {key: {"candidateKey": key} for key in keys},
            "items": [{"couponId": "PRIVATE-ID", "couponCode": "PRIVATE-A"}]}
    for name in ("request.json", "result.json", "coupons.json"):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    live_fast.redact_artifacts(tmp_path)
    for path in tmp_path.glob("*.json"):
        text = path.read_text(encoding="utf-8")
        assert "PRIVATE-" not in text
        redacted = json.loads(text)
        assert len(redacted["quotes"]) == 2
        assert all(key == quote["candidateKey"] for key, quote in redacted["quotes"].items())
    assert (tmp_path / "request.json").read_bytes() == (tmp_path / "result.json").read_bytes()


def test_live_adapter_rejects_zero_with_products():
    quote = quote_dict("k", {"success": True, "data": {
        "price": 0, "productList": [{"productName": "x"}]}}, "2026-10-14T12:00:00+08:00")
    assert quote["status"] == "invalid"
