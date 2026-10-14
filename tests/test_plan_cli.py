"""文件协议回归：缓存隔离、有效时间、接受后重报及历史回放。"""

import copy
import datetime as dt
import json

import pytest

from conftest import ROOT
from mcd_compromise.plan_cli import run
from mcd_compromise.quote_context import CHINA_TZ

NOW = dt.datetime(2026, 10, 14, 12, 0, tzinfo=CHINA_TZ)


def live_payload():
    payload = json.loads((ROOT / "examples/demo-feasible.json").read_text(encoding="utf-8"))["input"]
    # 首轮输出绑定上下文，宿主随后写回成功报价。
    old_quotes = payload.pop("quotes")
    first = run(payload, now=NOW)
    payload["quotes"] = first["quotes"]
    payload["quotes"]["quotes"] = old_quotes["quotes"]
    payload["quotes"]["budget"]["used"] = len(old_quotes["quotes"])
    for quote in payload["quotes"]["quotes"].values():
        quote["quotedAt"] = NOW.isoformat()
    return payload


def test_same_context_reuses_quotes_after_requirement_change():
    payload = live_payload()
    payload["request"]["requestVersion"] += 1
    for c in payload["request"]["constraints"]:
        if c["kind"] == "total_price_max":
            c["value"] = 5000
    result = run(payload, now=NOW + dt.timedelta(seconds=60))
    assert result["status"] == "feasible"
    assert not result["nextActions"]
    assert result["requestVersion"] == payload["request"]["requestVersion"]


@pytest.mark.parametrize("change", ["store", "snapshot", "menu", "missing_context", "expired", "future", "missing_time"])
def test_changed_or_stale_quotes_are_never_reused(change):
    payload = live_payload()
    if change == "store":
        payload["storeId"] = payload["request"]["context"]["storeId"] = "OTHER"
    elif change == "snapshot":
        payload["fetchedAt"] = "2026-10-14T12:00:01+08:00"
    elif change == "menu":
        payload["mcp"]["menu"]["probe"] = "new menu response"
    elif change == "missing_context":
        payload["quotes"].pop("context")
    else:
        value = {"expired": (NOW - dt.timedelta(seconds=300)).isoformat(),
                 "future": (NOW + dt.timedelta(seconds=60)).isoformat(), "missing_time": ""}[change]
        for quote in payload["quotes"]["quotes"].values():
            quote["quotedAt"] = value
    result = run(payload, now=NOW)
    assert result["status"] == "quotes_pending"
    assert not result["plans"]
    assert result["quotes"]["quotes"] == {}
    assert result["quotes"]["budget"]["used"] == 0
    assert result["nextActions"]


def test_acceptance_forces_new_quote():
    result = run(live_payload(), now=NOW, force_requote=True)
    assert result["status"] == "quotes_pending"
    assert result["quotes"]["quotes"] == {}


def test_mismatched_store_rejected():
    payload = live_payload()
    payload["storeId"] = "OTHER"
    with pytest.raises(ValueError, match="storeId"):
        run(payload, now=NOW)


def test_missing_snapshot_time_rejected_before_quoting():
    payload = live_payload()
    payload.pop("fetchedAt")
    with pytest.raises(ValueError, match="fetchedAt"):
        run(payload, now=NOW)


def test_unsupported_dining_mode_rejected():
    payload = live_payload()
    payload["request"]["context"]["diningMode"] = "delivery"
    with pytest.raises(ValueError, match="到店自取"):
        run(payload, now=NOW)


@pytest.mark.parametrize("name,status", [("demo-feasible", "feasible"),
                                       ("demo-conflict", "compromise_proposed"),
                                       ("demo-followup", "feasible")])
def test_historical_demo_requires_explicit_replay(name, status):
    payload = json.loads((ROOT / "examples" / f"{name}.json").read_text(encoding="utf-8"))["input"]
    before = copy.deepcopy(payload)
    result = run(payload, replay=True)
    assert result["status"] == status
    assert result["replay"] is True
    assert any("离线回放" in note for note in result["notes"])
    assert payload == before
    assert run(payload, now=NOW)["status"] == "quotes_pending"
