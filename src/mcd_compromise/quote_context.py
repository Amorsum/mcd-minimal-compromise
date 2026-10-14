"""文件协议入口的报价复用校验。历史样本必须显式离线回放。"""

from __future__ import annotations

import datetime as dt
import hashlib
import json

from .quote_state import QuoteManager

CHINA_TZ = dt.timezone(dt.timedelta(hours=8))
QUOTE_TTL_SECONDS = 300  # 项目缓存策略，不是官方价格有效期承诺


def quote_context(payload: dict, request) -> dict:
    fingerprint = hashlib.sha256(
        json.dumps(payload.get("mcp") or {}, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"storeId": request.context.store_id,
            "diningMode": request.context.dining_mode,
            "fetchedAt": str(payload.get("fetchedAt", "")),
            "menuFingerprint": fingerprint}


def prepare_quotes(manager: QuoteManager, context: dict, *, replay: bool = False,
                   force_requote: bool = False, now: dt.datetime | None = None) -> list[str]:
    """上下文或时间无效时开启新报价轮次；需求条件变化本身不使报价失效。"""
    if replay:
        if force_requote:
            raise ValueError("离线回放不能与强制重新报价同时使用")
        return ["离线回放：使用历史或模拟报价，结果不代表当前可购买价格"]
    reason = None
    if force_requote:
        reason = "用户接受方案，强制重新报价"
    elif manager.quoted_keys():
        if not context["fetchedAt"] or manager.context != context:
            reason = "旧报价缺少上下文或门店、就餐方式、菜单快照已变化"
        else:
            current = now or dt.datetime.now(CHINA_TZ)
            if current.tzinfo is None:
                current = current.replace(tzinfo=CHINA_TZ)
            for key in manager.quoted_keys():
                quote = manager.get(key)
                try:
                    quoted_at = dt.datetime.fromisoformat(quote.quoted_at)
                    if quoted_at.tzinfo is None:
                        quoted_at = quoted_at.replace(tzinfo=CHINA_TZ)
                    age = (current - quoted_at).total_seconds()
                    if age < -30 or age >= QUOTE_TTL_SECONDS:
                        raise ValueError("expired or future quote")
                except (TypeError, ValueError):
                    reason = "旧报价时间缺失、异常或已超过 5 分钟缓存期限"
                    break
    manager.context = context
    if reason:
        # 新的上下文/有效期是一轮新的核验，不让旧轮次已用预算阻止刷新。
        manager.reset()
        return [reason + "；已清空旧报价并重置本轮报价预算"]
    return []
