# 麦麦不将就（mcd-minimal-compromise）

> 预算、口味、营养难以兼得时，找到你最愿意接受的那一点改变。

一个基于**麦当劳官方 MCP** 的点餐协商 Skill：当你一句话说出配餐需求（预算、指定主食、饮料、排除项、用券偏好），而当前门店无法同时满足时，它说清楚**哪里冲突**，并给出**经过官方价格试算验证**的最小调整方案。

## 它解决什么问题

一个人吃饭，常常是「预算 25、想要牛肉汉堡、不要薯条、有券先用」——这些条件经常**无法同时满足**。常见做法是模型直接心算一个组合报个价，来源不明、条件丢失、冲突靠猜。

本项目的做法：

| 原则 | 含义 |
|---|---|
| 要求可保留 | 用户没同意改的条件，一个都不能动 |
| 冲突可解释 | 每个方案都能说清改了什么、保留了什么、依据是什么 |
| 改变可比较 | 待接受的修改按可解释规则排序，不混任意加权分 |
| 价格可验证 | 任何金额只来自官方 `calculate-price`，模型不口算 |

## 快速开始

### 环境要求

- Python ≥ 3.10（引擎仅用标准库；`pytest` 仅为开发依赖）
- 麦当劳 MCP Token（[申请地址](https://mcp.mcd.cn)，参赛者自行申请）
- 一个支持 Skill + MCP 的宿主（本仓库在 WorkBuddy 中开发验证）

### 连接麦当劳 MCP

参考 `mcp-config.example.json`（仅含环境变量占位符，不含真实凭证）：

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer ${MCD_MCP_TOKEN}" }
    }
  }
}
```

### 安装与测试

```bash
pip install -e ".[dev]"     # 安装引擎（无运行时依赖）
python -m pytest tests/     # 81 个测试：约束三态、套餐展开、最小妥协、报价预算、无解参考、营养匹配
```

### 演示（模拟数据，无需 Token）

`examples/` 下三个演示均基于**人工构造的小型菜单**（结构与官方返回一致，明确标注模拟数据）：

| 文件 | 场景 | 结果 |
|---|---|---|
| `demo-feasible.json` | 37 元内、牛肉汉堡、饮料、不要薯条 | `feasible`：3 个已核验方案 |
| `demo-conflict.json` | 25 元内、巨无霸、饮料、不要薯条 | `compromise_proposed`：A 预算提高到 35.00 元；B 换吉士汉堡包 |
| `demo-followup.json` | 「预算不能变，汉堡可以换，但饮料别取消」 | `feasible`：吉士汉堡包+可乐 22.00 元 |

复现任意一个演示：

```bash
python - <<'PY'
import json, pathlib
# 从演示文件取出 input，按其内嵌 quotes 直接跑到终态
data = json.loads(pathlib.Path("examples/demo-conflict.json").read_text(encoding="utf-8"))
pathlib.Path("req.json").write_text(json.dumps(data["input"], ensure_ascii=False), encoding="utf-8")
PY
python scripts/plan.py --input req.json --output res.json
```

### 真实数据验证（需要 Token）

`examples/real/` 保存了 2026-10-12 至 10-13 在真实门店 1950640 的端到端联调过程文件（凭证脱敏）：

| 文件 | 说明 |
|---|---|
| `e2e-request.json` + `e2e-result-1/2/3.json` | 冲突场景（预算 25 元）：26 候选、24 次真实报价 → `compromise_proposed`（巨无霸+雪碧 35.00，仅预算 25→35） |
| `e2e-feasible-request.json` + `e2e-feasible-result-1/2.json` | 可行场景（预算 40 元）：首批报价后即收敛 → `feasible`（3 个已核验方案） |
| `e2e-followup-request.json` + `e2e-followup-result-1.json` | 多轮第二轮（预算锁 25、主食开放）：跨轮复用报价，零新增报价 → `not_found` + `closestOptions` 两条代价路径 |
| `e2e-turn3-request.json` + `e2e-turn3-result.json` | 多轮第三轮（预算锁 32）：复用报价 → `compromise_proposed`（麦辣鸡腿汉堡+雪碧 32.00，只换主食） |
| `nutrition-foods.json` | `list-nutrition-foods` 真实返回全量 160 条（营养表样本，含两次调用间的数值波动记录） |
| `e2e-nutrition-request.json` + `e2e-nutrition-result-1/2/3.json` | 营养联调第一轮（热量≤900）：14 候选、14 次真实报价 → `feasible`（513/589/598 kcal，官方报价名回退补配规格） |
| `e2e-nutrition-turn2-request.json` + `e2e-nutrition-turn2-result.json` | 营养联调第二轮（热量锁 700）：复用报价零新增 → `feasible` |
| `e2e-nutrition-turn3-request.json` + `e2e-nutrition-turn3-result.json` | 营养联调第三轮（热量锁 500）：复用报价 → `not_found` + `closestOptions`（最近 513 kcal） |
| `store-menu-1950640.json` / `store-coupons-1950640.redacted.json` / `meal-detail-9900005466.json` / `quote-samples.json` | 脱敏真实样本 |

联调中的真实发现（券商品陷阱、600012 失败、官方返回名不一致等）记录在
`MCP_INTEGRATION.md`，完整验证结论见 `docs/validation-report.md`。

### 真实使用流程（宿主侧）

```
用户表达需求 → Skill 提取条件 → query-nearby-stores 选店 →
query-meals + query-store-coupons（并发）→ query-meal-detail（相关套餐）→
scripts/plan.py 首轮（返回待报价候选与 calculate-price 调用参数）→
宿主逐候选调用官方算价并写回 → scripts/plan.py 次轮（终态：方案或妥协）→
按 output-contract 展示
```

## 三个用例

1. **省钱配餐**：「预算 25，要牛肉汉堡和饮料，不要薯条」→ 已核验方案或最小妥协（见 demo-conflict）；
2. **用券搭配**：「优先用我的券」→ 券商品只能经优惠变体生成（携带券引用），用券与不用券分别核验，不假设用券更便宜；
3. **连续协商**：「预算不能变，汉堡可以换」→ 只更新被修改的条件，其余原样保留，重新试算（见 demo-followup）。

## 搜索范围（首版边界）

单门店、到店自取、一人用餐；一份主食 + 零/一份饮料 + 零/一份允许的小食；已知合法套餐组合与合法替换。

**不包含**：多人分餐、跨门店全局比价、自动领券、积分兑换、长期饮食计划、实时到餐时间预测。边界在每次输出的 `searchScope` 中可查——我们不会把有限搜索说成覆盖所有可能点法。

## 目录结构

```
├── README.md                    本文件
├── CONTEST_DECLARATION.md       参赛声明（官方原文，未改动）
├── MCP_INTEGRATION.md           真实接入证据：工具、调用流程、联调发现
├── workbuddy.md                 WorkBuddy 专项奖励说明（开发与运行上下文）
├── mcp-config.example.json      MCP 配置样例（仅环境变量占位符）
├── SKILL.md                     触发场景、需求分类、工具顺序、失败处理
├── pyproject.toml
├── references/                  conversation-rules / integration-map / output-contract
├── scripts/plan.py              两阶段报价协议入口
├── src/mcd_compromise/          计算引擎（标准库，无运行时依赖）
│   ├── models.py                数据模型（金额整数分、约束三维度、三态评价）
│   ├── normalize.py             官方返回标准化 + 品类标注（记录依据与置信度）
│   ├── candidates.py            候选生成（套餐变体、券变体、去重）
│   ├── constraints.py           三态条件评价与报价前剪除
│   ├── compromise.py            最小妥协算法与两阶段编排
│   ├── quote_state.py           试算去重、预算（首批 8 / 单轮 24）、无效报价识别
│   ├── nutrition.py             第二阶段营养映射（缺失不当零，官方报价名回退）
│   └── plan_cli.py              文件协议 CLI
├── tests/                       81 个测试 + 人工小型菜单夹具
├── examples/                    三个演示（模拟数据）+ real/ 脱敏真实样本
├── docs/                        architecture.md / validation-report.md
└── 麦麦不将就-具体实现路径.md    设计文档（约束模型、协议、验收标准）
```

## 目标用户

独自用餐的学生、上班族、经常使用优惠券的用户——任何一个会说「我想吃什么但预算/条件有限」的人。

## 已知限制

- 首版仅支持到店自取单人选餐；营养约束为第二阶段（数据可靠才接入）；
- 搜索受试算预算限制（首批 8、单轮 24 个候选），输出会标注覆盖度，不宣称全局最优；
- 官方接口行为以 2026-10-12 至 10-13 联调为准（见 `MCP_INTEGRATION.md`），后续可能变化；
- 输出仅供参考，不构成医疗或营养建议；餐品、价格与供应状态以麦当劳官方实时结果为准。

## 许可

MIT，见 [LICENSE](LICENSE)。
