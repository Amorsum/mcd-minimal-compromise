# 验收联调样本（脱敏）

来源：`.workbuddy/runs/acceptance/`（被 .gitignore 忽略，重建于 2026-10-14 真实联调）。
全部文件已脱敏：`traceId`/`couponId`/`couponCode`/`cardId`/手机号 已替换为 REDACTED-*。
门店编码（如 1990455）属公开营业信息，按惯例保留。

## 文件
- `r1-request.json` / `r1-result.json`：第①轮（预算25/牛肉/饮料/不要薯条 locked）完整需求快速入口联调
- `r2-request.json` / `r2-result.json`：第②轮（仅预算→32，其余保留）
- `r3-request.json` / `r3-pinned-p3-quote.json`：第③轮（接受 P3 = 选定方案，不锁预算/价格；对 P3 绑定餐品重调官方 calculate-price）
- `input-r2.json` / `input-r3.json`：手写输入请求
- `stores.json`：附近营业中门店发现结果
