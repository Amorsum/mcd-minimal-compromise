# workbuddy.md — WorkBuddy 专项奖励说明

> 本文件用于麦当劳程序员创意开发大赛 **WorkBuddy 专项奖励** 的联动活动，说明「麦麦不将就」（mcd-minimal-compromise）的 WorkBuddy 使用情况、开发上下文与后续修复来源。本文件是摘要，不是完整会话导出。

---

## 1. 本项目与 WorkBuddy 的关系

| 维度 | 说明 |
|---|---|
| 开发环境 | 据使用者说明及既有记录，初版引擎、测试与真实联调使用 **腾讯 WorkBuddy 智能体**（Agent 模式）完成；2026-10-14 后续评审、修复与回归验证由 Codex 协助完成，见 `docs/repair-review-2026-10-14.md` |
| 运行环境 | 设计为在 WorkBuddy 中作为 **Skill** 运行：由 WorkBuddy 读取 `SKILL.md` 理解触发场景与工具顺序，通过 **MCP 连接器**调用麦当劳官方 MCP（`mcd-mcp`），并执行 `scripts/plan.py` 两阶段报价协议 |
| 设计文档 | 使用者将此前由 AI 协助提供的 `麦麦不将就-具体实现路径.md` 交给 WorkBuddy，作为初版实现依据 |

**现有证据与待补材料**：真实样本和联调说明可以复现技术结果，但不能代替完整 WorkBuddy 开发对话。使用者仍需从原 WorkBuddy 会话导出 Markdown，脱敏后提交并在本文件链接；尚未导出的记录不得称为已归档。

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

将 `SKILL.md`、`references/`、`scripts/`、**`src/`** 一起放入 WorkBuddy 可识别的同一个 Skill 根目录。脚本依赖相邻的 `src/mcd_compromise/`，不能只复制脚本。

原开发记录使用用户级 `~/.workbuddy/skills/mcd-minimal-compromise/` 或项目级 `.workbuddy/skills/` 的路径约定，加载位置以本机配置为准。本次仅验证独立目录中的 Python 入口，未重新验证 WorkBuddy 界面的 Skill 识别。

宿主可使用本地凭证时，优先按 `SKILL.md` 5.1 执行 `scripts/live_fast.py --request meal-request.json`；仅宿主连接器可用时走下方两阶段流程。

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

以下为既有 WorkBuddy 开发记录的阶段摘要，后续修复内容见修复报告：

1. **任务起点**：使用者提供比赛仓库（M-China/mcd-developer-innovation-challenge）与此前 AI 协助提供的实现路径设计文档，要求逐步实现 Skill；麦当劳 `mcd-mcp` 已在 WorkBuddy 中连接，可真实调用验证。
2. **引擎实现**：按设计文档实现 9 个模块——数据模型（金额整数分、约束三维度、三态评价）、官方返回标准化（品类标注）、候选生成（套餐变体/券变体/特制去除）、三态约束评价、最小妥协算法、报价预算管理（首批 8 / 单轮 24）、营养映射、两阶段 CLI。
3. **测试体系**：84 个测试 + 人工小型菜单夹具，模拟算价器复刻官方行为（含券价、600012 失败、空清单陷阱、券双字段校验）。
4. **真实联调**（2026-10-12 至 10-13，门店 1950640）：门店查询 → 菜单（含「麦乐鸡套餐」官方标签修正）→ 门店优惠（凭证脱敏）→ 套餐详情（发现默认搭配含隐藏中薯条）→ 多轮共 63 次真实 `calculate-price`（冲突/可行/多轮/营养/组合约束 A/B 六组场景）→ `list-nutrition-foods` 全量 160 条营养表 → 第二门店查询。
5. **联调捕获的缺陷修复**：套餐名称误标为单品、券 `couponId` 误作 `couponCode` 传递、套餐单轮替换变体未校验其余轮（隐藏薯条）、券商品被当 0 元单品生成、官方报价名（带规格）未用于营养匹配、`closestOptions` 混入未知候选、营养约束的妥协描述退化为「调整条件 protein」、营养约束分派的隐式 else（扩展 `fat_max` 时修复，新 kind 曾会被误按蛋白质求值）——每项均有回归测试。
6. **端到端验证**：冲突场景（预算 25）→ `compromise_proposed`（巨无霸+雪碧 35.00，仅预算 25→35）；可行场景（预算 40）→ `feasible`（3 个已核验方案）；多轮三轮（25→not_found→32）跨轮复用报价零新增调用；营养三轮（900→700→500 锁定）→ `feasible`/`feasible`/`not_found`，方案热量 513/589/598 kcal 全部来自官方营养表汇总；组合约束两场景（排除薯条×热量×预算一轮 `feasible`；蛋白质×热量×预算两轮 `compromise_proposed`→`feasible`）。含券候选以脱敏凭证调用，官方真实返回 `600012 未找到优惠券`，引擎按失败正确处理。
7. **仓库就绪**：扁平化为比赛要求的标准结构（必需文件位于根目录），初始提交推送至公开仓库。

---

## 3.1 会话材料导出与链接

| 材料 | 状态 | 说明 |
|---|---|---|
| 本次验收会话（2026-10-14） | **已整理为摘要** | [`docs/acceptance-session-2026-10-14.md`](docs/acceptance-session-2026-10-14.md)：按时间顺序记录用户下发任务、3 个决策点（门店 1990455 / 第2轮预算32 / 第3轮接受P3）与助手核验结论；为**验收摘要**（非原始逐字全文），已脱敏，不含 Token/券码/手机号。脱敏联调样本（R1/R2/R3、真实妥协协商的请求与结果、门店发现）见 [`examples/real/acceptance-20261014/`](examples/real/acceptance-20261014/README.md) |
| 原开发聊天（2026-10-12 至 10-13） | **待人工导出** | 助手无法直接从本会话导出原开发对话（`conversation_search` 返回 400，且仅返回片段不足以复原完整对话）；**不生成虚构会话代替** |

**原开发聊天人工导出步骤（请在 WorkBuddy 界面完成）**：

1. 在 WorkBuddy 左侧/历史列表中打开对应的「麦麦不将就」开发会话（初版引擎、测试与真实联调所在会话）；
2. 使用会话菜单中的「导出 / 复制为 Markdown」功能，将完整对话保存为 `.md`；
3. 脱敏处理：删除或替换所有 `Authorization`/`Bearer` Token、券 `couponId`/`couponCode`、手机号、精确经纬度与家庭/学校门牌等个人信息（门店编码属公开营业信息，可保留）；
4. 将文件放入仓库 `docs/` 并命名为如 `dev-session-2026-10-12.md`；
5. 回到本文件，把上方表格第二行状态改为「已导出」并补上链接。

> 注意：本文件和修复报告均为摘要或忠实记录，**均不冒充原始开发会话**。在 `docs/dev-session-*.md` 就绪前，原开发聊天不得声称为已归档。

---

## 4. WorkBuddy 能力使用清单

| WorkBuddy 能力 | 在本项目中的使用 |
|---|---|
| Agent 模式 | 初版开发中的读写代码、运行测试、调用 MCP、生成文档 |
| MCP 连接器（mcd-mcp） | 真实调用官方 7 类工具完成联调验证 |
| 文件系统与终端 | 引擎开发、pytest 运行、git 仓库初始化与推送 |
| 多轮任务追踪 | 开发任务拆解与进度管理（引擎→测试→联调→文档→仓库） |

---

## 5. 声明

- 最新公开规则要求专项奖励作品真实使用 WorkBuddy 并提交上下文，未规定开发工具独占；本项目如实披露来源，资格以主办方核验为准：[官方规则](https://github.com/M-China/mcd-developer-innovation-challenge/blob/main/activityGuidelines.md)。
- 会话材料：本次验收会话已导出并链接（见 3.1 节 [`docs/acceptance-session-2026-10-14.md`](docs/acceptance-session-2026-10-14.md)）；**原开发聊天仍待人工导出**（助手无法直接导出，不虚构代替，步骤见 3.1 节）。本文件和修复报告均不冒充原始开发会话。

- 本项目为参赛独立开发作品，非麦当劳官方产品，亦非腾讯官方产品；
- 所有价格数据来自麦当劳官方 `calculate-price` 接口实时返回；
- 示例中的优惠凭证均已脱敏，仓库不含任何真实 Token。
