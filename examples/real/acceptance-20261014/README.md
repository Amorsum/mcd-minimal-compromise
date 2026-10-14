# 验收联调样本（脱敏）

来源：`.workbuddy/runs/acceptance/`（被 .gitignore 忽略，重建于 2026-10-14 真实联调）。
全部文件已脱敏：`traceId`/`couponId`/`couponCode`/`cardId`/手机号 已替换为 REDACTED-*。
门店编码（如 1990455）属公开营业信息，按惯例保留。

## 文件
- `r1-request.json` / `r1-result.json`：第①轮（预算25/牛肉/饮料/不要薯条 locked）完整需求快速入口联调
- `r2-request.json` / `r2-result.json`：第②轮（仅预算→32，其余保留）
- `r3-request.json` / `r3-pinned-p3-quote.json`：第③轮（接受 P3 = 选定方案，不锁预算/价格；对 P3 绑定餐品重调官方 calculate-price 核验 ¥14.90）。**收尾修正**：`r3-request.json` 曾误存「预算 1490 locked」，已更正为保持第二轮预算 **3200 分（ask）**、requestVersion=3，与「接受 P3 仅选定、不锁价」的结论一致
- `input-r2.json` / `input-r3.json`：手写输入请求
- `stores.json`：附近营业中门店发现结果

## 补充：真实妥协协商（Task 3，2026-10-14，已按收尾修复重建）
冲突需求：预算 12 元 / 要牛肉汉堡 / 要饮料 / 不要薯条（locked），门店 1990455。
- `conflict-request.json`：冲突输入（预算 1200 分，negotiability=ask，requestVersion=1），真实抓取菜单、券、套餐详情
- `conflict-result.json`：引擎返回 `compromise_proposed`
  - **C1**：预算 12.00 → **13.90 元**（保留牛肉/饮料/不要薯条）；`modifiedValue=1390`、`magnitude=190`，与文字一致
  - （收尾修复：原 C2 曾退化为「只有可乐中杯」却描述成「换为 staple_beef」，已修正为——放宽牛肉须仍含主食，故该门店 ¥12 内无更便宜主食+饮料组合时不再产出无主食提案，只剩 C1 一条诚实的最小妥协）
- `conflict-accepted-request.json`：用户明确接受 C1 后，用 `apply_accepted_change` 生成的新需求——`requestVersion=2`、预算=1390 分、**negotiability 保留 ask**（用户未要求锁定则不锁）、其余条件原样
- `conflict-accepted-result.json`：对接受后需求重报价 → `feasible`
  - P1：高达吉士双牛堡 + 阳光柠檬红茶中杯，官方试算 ¥13.90，四项条件（budget/staple/drink/exclude_fries）全部保留

流程严格遵守「先验证 compromise_proposed → 展示保留条件与待接受改变 → 用户明确接受后更新需求并重报价」，全程不下单、不支付。
