# 输出契约（output-contract）

`scripts/plan.py` 的输入输出结构。所有金额单位为整数「分」；条件评价三态：`satisfied` / `violated` / `unknown`。

## 1. 输入

```json
{
  "storeId": "1950640",
  "fetchedAt": "2026-10-12 16:00:00",
  "mcp": {
    "menu": { "..." : "query-meals 原始返回" },
    "comboDetails": [ { "..." : "query-meal-detail 原始返回" } ],
    "coupons": { "..." : "query-store-coupons 原始返回" },
    "nutrition": { "..." : "list-nutrition-foods 原始返回（可选）" }
  },
  "request": {
    "requestVersion": 1,
    "context": { "storeId": "1950640", "diningMode": "pickup", "people": 1 },
    "constraints": [
      { "id": "budget", "kind": "total_price_max", "value": 2500, "unit": "fen",
        "strength": "hard", "negotiability": "ask", "priority": 50, "sourceText": "预算25元" },
      { "id": "no_fries", "kind": "exclude_category", "value": "fries",
        "strength": "hard", "negotiability": "locked", "priority": 10, "sourceText": "不要薯条" }
    ]
  },
  "quotes": { "budget": { "firstBatch": 8, "roundBudget": 24, "used": 0 }, "quotes": { "<candidateKey>": { "..." : "Quote" } } },
  "budget": { "firstBatch": 8, "roundBudget": 24 },
  "maxPlans": 3
}
```

### 约束类型（kind）

| kind | value 含义 | 说明 |
|---|---|---|
| `total_price_max` | 预算上限（分） | 依赖报价；无有效报价时记 unknown |
| `include_staple` | 餐品编码或 `staple_beef/chicken/fish/other` | 指定主食 |
| `exclude_category` | 品类标签 | 如 `fries`；近似品类记 unknown |
| `exclude_item` | 餐品编码或特制编码 | 特制编码仅在已展开详情时可判定 |
| `require_category` | 品类标签 | 如 `drink` |
| `max_items` | 整数 | 总件数上限 |
| `prefer_coupon` | 任意 | 软偏好，不影响可行性 |
| `energy_max` / `protein_min` / `fat_max` | kcal / g | 第二阶段；营养缺失记 unknown（菜单名失配时回退官方报价名，仍失败则 unknown，不当零） |

### 三个独立维度

- `strength`：`hard`（当前必须满足）/ `soft`（仅排序偏好）；
- `negotiability`：`locked`（不可讨论）/ `ask`（可建议但不经接受不生效）/ `open`；
- `priority`：数值小者先讨论。

## 2. 输出

```json
{
  "requestVersion": 1,
  "context": { "..." : "同上" },
  "searchScope": { "store": "single", "diningMode": "pickup", "maxStaples": 1, "..." : "边界声明" },
  "status": "feasible | compromise_proposed | not_found | data_incomplete | quotes_pending",
  "coverage": { "generatedCount": 62, "quotedCount": 24, "failedCount": 0, "truncated": true },
  "plans": [ {
      "planId": "P1",
      "items": [ { "productCode": "9900005466", "quantity": 1, "roundList": [ { "round": "2", "comboItemList": [ { "code": "4437", "quantity": 1 } ] } ] } ],
      "expanded": [ { "code": "1100", "name": "巨无霸", "category": "staple_beef", "quantity": 1, "confidence": "high", "basis": "名称含牛肉堡关键词：命中「牛堡」", "source": "combo:9900005466:1" } ],
      "officialQuote": { "candidateKey": "...", "status": "ok", "priceFen": 3600, "quotedAt": "...", "productNames": ["巨无霸三件套"], "productCodeNames": [["9900005466", "巨无霸三件套"]] },
      "satisfiedConstraints": [ { "constraintId": "budget", "state": "satisfied", "target": 2500, "actual": 3600, "evidence": "..." } ],
      "violatedConstraints": [],
      "proposedChanges": [ { "constraintId": "budget", "description": "预算从 25.00 元提高到 36.00 元（按试算价）", "magnitude": 1100 } ],
      "unknowns": [],
      "verified": true,
      "storeId": "1950640",
      "quotedAt": "..."
  } ],
  "closestOptions": [],
  "pendingQuotes": [ { "key": "...", "items": [ "..." ], "note": "默认搭配" } ],
  "nextActions": [ { "candidateKey": "...", "note": "...", "mcpCall": { "tool": "calculate-price", "params": { "storeCode": "1950640", "orderType": 1, "beType": 1, "items": [ "..." ] } } } ],
  "notes": [ "搜索范围不完整（受试算预算限制），以下为已核验候选中的推荐" ]
}
```

### `proposedChanges` 中营养条件的描述口径

营养类条件（`protein_min` / `energy_max` / `fat_max`）的 `proposedChanges` 按**该方案的实测汇总值**描述最小改动：如「蛋白质下限从 30 g 降至 27 g（按该方案实测）」（真实联调实例）、「热量上限从 X kcal 提高到 Y kcal（按该方案实测）」、「脂肪上限从 X g 提高到 Y g（按该方案实测）」，`magnitude` 为阈值改动量。实测值由营养表按展开组成汇总（官方报价名回退后），缺失组成记 unknown 时不生成此类描述（真实联调见 docs/validation-report.md 3.6）。

### `closestOptions`（仅 `not_found` 时非空）

无解时给出「最近失配」的已核验参考：结构与 `plans` 相同，但 `verified=false` 且 `violatedConstraints` 非空。按违反条件组合分组、每条组合取价低者，组间按（锁定违反数 → 总违反数 → 价格）排序——不同代价路径（如「加钱保留原主食」与「加钱换主食」）各取一条，支撑「改变可比较」。宿主应明确提示：这些选项**未满足锁定条件**，改变对应锁定条件后才可能成立。

**只收完全已知的候选**：存在 unknown 硬条件的候选不进 `closestOptions`——未知不等于「差一点」，列入会伪造确定性（真实联调发现：营养名缺规格的候选热量 unknown，曾被排到第一位）。

`officialQuote.productCodeNames`：官方 `calculate-price` 返回按商品编码的商品名（带规格后缀，如 `3010→「雪碧小杯」`）。用途有二：① 展示层商品名以它为准（官方返回名可能与菜单名不一致）；② 营养匹配的回退来源——菜单单品名常缺规格（「雪碧」），营养匹配在菜单名失配时按编码回退到官方报价名（仍是精确匹配，见 MCP_INTEGRATION.md 4.13）。

## 3. 状态机

```
quotes_pending ──(宿主补报价后再次运行)──▶ 重新进入 plan_next
     │
     ├─▶ feasible            存在满足全部硬条件且无未知的已核验方案
     ├─▶ compromise_proposed 无已核验方案，但存在「只改变一个可讨论条件」的待接受方案
     ├─▶ not_found           已核验候选中无可行方案且无可接受调整（附覆盖度说明）
     └─▶ data_incomplete     门店数据为空 / 报价全部失败或无效 / 条件因数据缺失无法判定
```

`not_found` 不等于已证明不存在；证明范围通过 `coverage` 与 `searchScope` 单独表达。

## 4. 展示映射（宿主对话层）

| 输出字段 | 对话展示 |
|---|---|
| `expanded` | 餐品与规格（套餐展开为具体组成） |
| `officialQuote.priceFen` | 试算总价（分 ÷ 100 = 元） |
| `satisfiedConstraints` | 保留条件 |
| `violatedConstraints` | 违反的条件（`closestOptions` 中说明「差在哪」） |
| `proposedChanges[].description` | 待接受改变 |
| `closestOptions` | 无解时的最近失配参考（须提示未满足锁定条件） |
| `quotedAt` / `storeId` | 查询时间与门店 |
| `candidateKey` / `evidence` | 可展开的「依据」 |
| `coverage.truncated` | 为 true 时必须提示「已核验候选中推荐」 |

## 5. 不变量（测试保障）

- 金额只来自 `officialQuote`；报价失败/无效的候选不出现在 `plans`；
- `verified=true` 的方案满足全部硬条件且无未知硬条件；
- `compromise_proposed` 的方案不违反锁定条件，且除 `proposedChanges` 外满足其余原条件；
- `not_found` 的 `closestOptions` 全部 `verified=false` 且 `violatedConstraints` 非空，违反模式两两不同；
- 同一输入重复运行，方案顺序一致（确定性）；
- 同一候选在本轮只报价一次（`candidateKey` 去重）。
