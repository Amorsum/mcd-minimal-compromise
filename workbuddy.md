# workbuddy.md — WorkBuddy 专项奖励说明

> 本文件用于麦当劳程序员创意开发大赛 **WorkBuddy 专项奖励** 的联动活动，说明「麦麦不将更」（mcd-minimal-compromise）与腾讯 WorkBuddy 智能体的关系、在 WorkBuddy 中的使用方式，以及本次开发的对话上下文。

---

## 1. 本项目与 WorkBuddy 的关系

| 维度 | 说明 |
|---|---|
| 开发环境 | 全程在 **腾讯 WorkBuddy 智能体**（Agent 模式）中完成：引擎编码、测试编写、真实 MCP 联调、缺陷修复与文档撰写均在本对话工作区内进行 |
| 运行环境 | 设计为在 WorkBuddy 中作为 **Skill** 运行：由 WorkBuddy 读取 `SKILL.md` 理解触发场景与工具顺序，通过 **MCP 连接器**调用麦当劳官方 MCP（`mcd-mcp`），并执行 `scripts/plan.py` 两阶段报价协议 |
| 设计文档 | 由使用者在 WorkBuddy 工作空间中撰写（`麦麦不将就-具体实现路径.md`），作为本次开发的实现路径依据 |

**真实使用证据**：本对话即开发过程本身——从读取比赛规则、按设计文档逐节实现，到连接麦当劳 MCP 完成真实门店（1950640）端到端联调，全部在 WorkBuddy 中完成。

---

## 2. 在 WorkBuddy 中安装与使用

### 2.1 配置麦当劳 MCP 连接器

在 WorkBuddy 连接器管理中添加麦当劳 MCP（参考 `mcp-config.example.json`，Token 通过环境变量注入，不写入任何文件）：

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

### 2.2 安装 Skill

将本仓库的 `SKILL.md`、`references/`、`scripts/` 放入 WorkBuddy 的 Skill 目录（用户级 `~/.workbuddy/skills/mcd-minimal-compromise/` 或项目级 `.workbuddy/skills/`）。

### 2.3 使用示例

用户在 WorkBuddy 中直接表达配餐需求即可触发：

> 「预算 25，要个牛肉汉堡和饮料，不要薯条」

WorkBuddy 的执行路径：

```
用户表达需求
  → Skill 提取条件（预算 25 / 牛肉汉堡 / 饮料 / 不要薯条[locked]）
  → query-nearby-stores 选店（不默认门店）
  → query-meals + query-store-coupons 并发获取菜单与优惠
  → query-meal-detail 获取相关套餐组成（展开隐藏薯条）
  → scripts/plan.py 首轮：返回待报价候选（含 calculate-price 完整参数）
  → WorkBuddy 逐候选调用官方 calculate-price 并写回报价
  → scripts/plan.py 次轮：得到终态与方案
  → 按 output-contract 展示（试算总价 / 保留条件 / 待接受改变）
```

---

## 3. 开发对话上下文（联动活动用）

本次开发在 WorkBuddy 单对话中完成，关键节点如下：

1. **任务起点**：使用者提供比赛仓库（M-China/mcd-developer-innovation-challenge）与自行撰写的实现路径设计文档，要求逐步实现 Skill；麦当劳 `mcd-mcp` 已在 WorkBuddy 中连接，可真实调用验证。
2. **引擎实现**：按设计文档实现 9 个模块——数据模型（金额整数分、约束三维度、三态评价）、官方返回标准化（品类标注）、候选生成（套餐变体/券变体/特制去除）、三态约束评价、最小妥协算法、报价预算管理（首批 8 / 单轮 24）、营养映射、两阶段 CLI。
3. **测试体系**：84 个测试 + 人工小型菜单夹具，模拟算价器复刻官方行为（含券价、600012 失败、空清单陷阱、券双字段校验）。
4. **真实联调**（2026-10-12 至 10-13，门店 1950640）：门店查询 → 菜单（含「麦乐鸡套餐」官方标签修正）→ 门店优惠（凭证脱敏）→ 套餐详情（发现默认搭配含隐藏中薯条）→ 多轮共 63 次真实 `calculate-price`（冲突/可行/多轮/营养/组合约束 A/B 六组场景）→ `list-nutrition-foods` 全量 160 条营养表 → 第二门店查询。
5. **联调捕获的缺陷修复**：套餐名称误标为单品、券 `couponId` 误作 `couponCode` 传递、套餐单轮替换变体未校验其余轮（隐藏薯条）、券商品被当 0 元单品生成、官方报价名（带规格）未用于营养匹配、`closestOptions` 混入未知候选、营养约束的妥协描述退化为「调整条件 protein」、营养约束分派的隐式 else（扩展 `fat_max` 时修复，新 kind 曾会被误按蛋白质求值）——每项均有回归测试。
6. **端到端验证**：冲突场景（预算 25）→ `compromise_proposed`（巨无霸+雪碧 35.00，仅预算 25→35）；可行场景（预算 40）→ `feasible`（3 个已核验方案）；多轮三轮（25→not_found→32）跨轮复用报价零新增调用；营养三轮（900→700→500 锁定）→ `feasible`/`feasible`/`not_found`，方案热量 513/589/598 kcal 全部来自官方营养表汇总；组合约束两场景（排除薯条×热量×预算一轮 `feasible`；蛋白质×热量×预算两轮 `compromise_proposed`→`feasible`）。含券候选以脱敏凭证调用，官方真实返回 `600012 未找到优惠券`，引擎按失败正确处理。
7. **仓库就绪**：扁平化为比赛要求的标准结构（必需文件位于根目录），初始提交推送至公开仓库。

---

## 4. WorkBuddy 能力使用清单

| WorkBuddy 能力 | 在本项目中的使用 |
|---|---|
| Agent 模式 | 全程自主执行：读写代码、运行测试、调用 MCP、生成文档 |
| MCP 连接器（mcd-mcp） | 真实调用官方 7 类工具完成联调验证 |
| 文件系统与终端 | 引擎开发、pytest 运行、git 仓库初始化与推送 |
| 多轮任务追踪 | 开发任务拆解与进度管理（引擎→测试→联调→文档→仓库） |

---

## 5. 声明

- 本项目为参赛独立开发作品，非麦当劳官方产品，亦非腾讯官方产品；
- 所有价格数据来自麦当劳官方 `calculate-price` 接口实时返回；
- 示例中的优惠凭证均已脱敏，仓库不含任何真实 Token。
