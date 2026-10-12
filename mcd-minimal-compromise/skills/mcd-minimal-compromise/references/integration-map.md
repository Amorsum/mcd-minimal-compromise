# 集成映射（integration-map）

官方麦当劳 MCP 工具 → 本 Skill 的用途与调用约束。接入地址 `https://mcp.mcd.cn`（Streamable HTTP + Bearer Token）；本 Skill 不持有凭证，凭证由宿主配置。

## 1. 工具用途总览

| 官方工具 | 本项目用途 | 阶段 | 依赖 |
|---|---|---|---|
| `query-nearby-stores` | 建立门店上下文，由用户选店 | P0 | 无 |
| `query-meals` | 获取门店可选菜单（餐品编码、标价、分类） | P0 | storeCode |
| `query-store-coupons` | 获取当前门店可用优惠（券 ID/券码/适用商品） | P0 | storeCode |
| `query-meal-detail` | 获取套餐组成、合法替换项、特制参数 | P0 | storeCode + 餐品编码 |
| `calculate-price` | 核验候选总价（唯一价格来源） | P0 | 合法候选 |
| `list-nutrition-foods` | 为可靠匹配的餐品附加营养数据 | P1 | 无 |
| `create-order` | 用户选定并确认后创建订单 | P2（范围外） | 重新核验后 |

## 2. 参数规则（以 2026-10-12 真实联调为准）

到店自取场景统一：`orderType=1, beType=1`，**不传 beCode**。门店标识用 `query-nearby-stores` 返回的 `storeCode`。

- `query-nearby-stores`：`searchType=2` 按位置搜索需 `city` + `keyword`；`beType=1` 返回门店无 beCode；
- `query-meals`：返回 `categories`（分类→编码）与 `meals`（编码→名称/现价/原价/canWithOrder）；
- `query-meal-detail`：套餐返回 `rounds[]`（轮次→choices）；单品返回无 rounds；
- `query-store-coupons`：同一券 ID 可对应多个券码；`products[].productCode` 是**独立券商品编码**，与常规菜单编码不同；
- `calculate-price`：金额单位为**分**；`items[].roundList` 用于套餐自定义（`round` 传 `rounds[].id` 的字符串，`comboItemList[].code` 传该轮选项编码）；`couponId`/`couponCode` 仅对该券指定商品有效。

## 3. 真实联调发现的行为（必须遵守）

1. **套餐默认小食常含薯条**：如「巨无霸三件套」第二轮默认「中薯条」——「不要薯条」必须展开 rounds 才能识别，名称里看不到；
2. **roundList 参与计价**：小食换「小杯玉米杯」（diffPrice -¥0.5）后总价 3650→3600 分；
3. **券只能用在其指定商品上**：对其他商品返回 `success=false, code=600012`（促销规则不支持该商品）；
4. **券商品不带券试算是陷阱**：返回 `success=true` 但 `productList` 为空、`price=0`——必须按无效报价处理，**绝不能当成 0 元**；
5. **试算幂等**：同一请求重复试算结果一致，未观察到状态变化；
6. **特制传参**：含 `unselectedKey` 的特制组，算价时该组所有项都必须传（选中传 `selectedKey`，未选中传 `unselectedKey`）；
7. 两门店（1950640/1951000）优惠一致，但券与账号绑定，换账号结果会变。

## 4. 调用流程

```
选门店 → 菜单 + 门店优惠（可并发）→ 相关套餐详情 → 标准化 →
生成候选 → 分批试算 → 条件检查 → 输出方案或进入协商
```

## 5. 错误分类与处理

| 官方返回 | 分类 | 处理 |
|---|---|---|
| `success=false` + 错误码 | failed | 记录错误码；限流时退避；不进入已核验列表 |
| `success=true` + 空清单/价格缺失 | invalid | 疑似券商品未带券；不当作 0 元；重试需带券 |
| `code=600012` | failed（券不适用） | 该候选失败；券仅适用于指定商品 |
| 超时/网络错误 | failed | 标记价格未知；不用估价替代 |

## 6. 缓存与凭证

- 菜单可按「账号 + 门店 + 方式 + 时间上下文」短时缓存；个人优惠隔离；
- 报价默认仅在本轮复用；用户选定后重新试算；
- 凭证使用环境变量或宿主安全配置；真实响应样本不进入公开仓库（见 examples/real 的脱敏说明）；
- 日志只记录必要状态、耗时与脱敏错误；不记录 Token、手机号、精确地址。
