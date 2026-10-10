# 麦麦拼单大脑（mcd-meal-brain）

> 面向多人场景的麦当劳智能点餐决策引擎 —— 一句话进、三套可执行方案出、**确认后才下单**的 Agent Skill。

**核心数据**：4 人拼单（不辣 + 每人 1 份主食 + 预算 ¥100），最省钱方案 **¥64.80 vs 基线 ¥86.00，省 24.7%**（人均 ¥16.20）；本地估算与官方核价**偏差 0%**（见 [docs/pricing-verification.md](./docs/pricing-verification.md)）。

**三步跑起来**（Python 3.10+，零第三方依赖，纯离线）：

```bash
git clone https://github.com/lqx-shuaige/mcd-meal-brain.git && cd mcd-meal-brain
python scripts/optimizer.py --selftest     # ① 断言自检
python scripts/optimizer.py "我们4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"   # ② 复现三套方案
python scripts/gen_demo.py                 # ③ 生成 docs/demo.html 可视化报告（浏览器打开）
```

**仓库结构**：

```
mcd-meal-brain/
├── SKILL.md               技能主文档（八步工作流 / 子代理编排 / 核心规则）
├── scripts/optimizer.py   核心引擎：解析 → 过滤 → 优化 → 核价（零依赖）
├── references/            workflow · schema · tools · strategy
├── docs/                  demo.html 可视化 · 真实核价记录 · 跨框架指南
├── tests/                 pytest 回归（43 用例全绿）+ GitHub Actions CI
└── MCP_INTEGRATION.md     MCP 接入记录（15 项实测 schema 偏差）
```

**参赛作品**：麦当劳 MCP 程序员创意开发大赛 · 作品形态：Agent Skill
**依赖**：Python 3.10+（纯标准库） · 麦当劳 MCP 服务（35 个工具）

---

## 一、解决什么问题

单人点餐只需要回答「想吃什么」；**多人点餐要同时满足三层互相打架的约束**：

| 层级 | 内容 | 特性 |
|---|---|---|
| **硬约束** | 忌口、过敏、预算上限、人数 | 违反即方案作废 |
| **软偏好** | 口味、热量、想吃辣的、要凑赠品 | 违反只扣分 |
| **团队目标** | 总价 / 人均、送达时间、凑券凑满减 | 需要全局寻优 |

难点在于**它们会互相冲突**：想省钱要凑单，凑单可能超热量；想健康要少油炸，少油炸又用不上大额满减券。

本项目做的事：**在约束空间里做多目标寻优，并给出可解释的取舍**。

---

## 二、核心能力

- **自然语言 → 结构化约束**：识别忌口成分（「不吃牛」「牛肉过敏」「不碰安格斯」归一为同一黑名单）、预算形式（总额 / 人均）、减脂档位、四种典型画像。
- **双预算约束**（方向 4 新增）：同时支持「总预算上限」与「每人预算上限」，两者都作为硬约束在优化中生效。例如「4 个人，总预算 100，每人不超过 30」。
- **券可用性估算**（方向 4 新增）：基于历史可用数据（MCP 若提供）或券特征（门槛高低、时段限制、门店限制）估算每张券的可用性（高/中/低），无数据时如实标注「未知」，绝不编造概率数字。
- **极端预算降级**（方向 4 新增）：当预算不足以满足所有人的最基本需求时，自动生成「减配方案」（预算内最便宜组合）并明确告知差距，同时建议「分步下单」或调整预算/人数，绝不输出无解崩溃。
- **硬约束一票否决**：先过滤解空间再优化，且每个被剔除的商品都能回答「为什么没有它」。
- **组合优化**：动态规划（无界背包 + 券组合 DP）+ 帕累托剪枝求最省钱；加权评分求最健康 / 最快。
- **券来源严格区分**：只用 `query-store-coupons`（门店真实可用）算价，绝不混用「账户可领券」。
- **官方核价**：每个方案都过 `calculate-price` 核价，偏差超 5% 以官方价为准。
- **得来速（DT）场景**：`beType=5` 全链路实测跑通（beType=3 不存在，传入会被直接拒绝）；同门店与到店自取价格一致，差异仅在取餐方式。
- **渠道比价（到手成本）**：同一篮子在 到店自取 / 麦乐送 / 得来速 三个**内部渠道**间对比
  「商品 + 运费 + 打包 + 餐具 − 优惠」完整成本结构；无法核价的渠道如实标注，**不编造**；
  美团 / 饿了么等外部平台不在 MCP 能力范围内，明确不做。
- **写操作强确认**：下单、取消、领券、建地址、抽奖一律先展示、后确认，绝不擅自执行。
- **营养化输出**：热量 / 蛋白 / 脂肪 / 钠全部给数值，不给「较健康」这种空话。
- **结构化输出契约**：`--json` 输出符合 JSON Schema（`references/schema.json`）的 plans 文档
  （`name / items / coupons / original_total / final_total / per_person / nutrition / genui`），
  纯标准库校验器保证类型与必填字段，`--selftest` 附带负向用例与镜像同步检查。
- **模糊输入处理**：三层询问机制（静默默认 → 聚焦追问 ≤3 问 → 「你看着办」全默认），
  「随便」「都行」不再瞎猜也不卡死，默认偏好显式标注。
- **群体决策公平性**：`aggregate_preferences()` 硬约束交集（禁忌并集排除）、
  软约束自适应截尾均值（去极端取中间）、严重冲突检测（一半要辣一半不要辣 →
  「需协商」+ A/B 两套备选 + 妥协点说明）；每方案带逐成员公平性评分
  （0-1 满足度，整体取最低分——木桶效应），冲突场景 `--group` 可演示。
- **系统健壮性**：全 MCP 调用统一走 `_mcp_call`（10s 超时 + 指数退避重试
  1s/2s/4s 最多 3 次 + 总预算 20s；401/400/业务错误不重试）；超时按工具降级
  （菜单→缓存、券→跳过、核价→标注未验证、门店→问用户）并显式标注；
  `create_order_guarded()` 幂等下单（user+store+内容哈希+5 分钟窗口，
  命中复用已有订单，失败不记录）；JSONL 调用日志（`logs/mcp_calls.log`，
  Token/手机号/地址强制脱敏，`logs/` 已 gitignore）。
- **子代理编排设计**：解析 / 查询 / 优化三角色协作（见 SKILL.md），职责隔离，
  输入输出契约与现有函数一一对应，可直接落地。

---

## 三、更多运行入口

> 基础三步（clone → selftest → 跑场景）见文首；以下为进阶入口。

```bash
# 配置 MCP（把示例复制到本机配置，并填入自己的 Token）
cp mcp-config.example.json ~/.workbuddy/mcp.json
export MCD_MCP_TOKEN="<从 https://open.mcd.cn/mcp 获取>"

python scripts/optimizer.py --compare-channels  # 渠道比价演示（回放 2026-10-10 实测数据）
python scripts/optimizer.py --json     # 结构化输出（references/schema.json 契约）
python scripts/optimizer.py --clarify "随便"     # 模糊输入三层询问演示
python scripts/optimizer.py --group    # 群体决策演示（一半要辣一半不要辣 → 双方案）

# 核价联调（需在门店营业时段 07:00–22:00）
#    见 scripts/optimizer.py::verify_price(plan, store_code, token=...)
#    ✅ 已于 2026-10-10 完成端到端实测（¥44.00 = ¥44.00，偏差 0%），见 docs/pricing-verification.md
```

### 在其他框架中使用

本 Skill 遵循 **Agent Skills 开放规范**，不绑定 WorkBuddy，可被任何支持该规范 / MCP 的
框架加载（Claude Code、Cursor、Cline 等）：

```bash
# Claude Code（项目级 .claude/skills/；个人级 ~/.claude/skills/）
mkdir -p .claude/skills && cp -r mcd-meal-brain .claude/skills/mcd-meal-brain

# Cursor（.cursor/skills/，并兼容读取 .claude/skills/）
mkdir -p .cursor/skills && cp -r mcd-meal-brain .cursor/skills/mcd-meal-brain

# Cline 3.48.0+（项目级 .cline/skills/；需开启 Settings → Features → Enable Skills）
mkdir -p .cline/skills && cp -r mcd-meal-brain .cline/skills/mcd-meal-brain
```

MCP 连接配置与环境变量 `MCD_MCP_TOKEN` 的设置方式、各框架配置入口，
详见 **[docs/portability.md](./docs/portability.md)**。

---

## 四、目录结构

```
mcd-meal-brain/
├── SKILL.md                      技能主文档：触发条件 / 8 步工作流概览 / 子代理编排 / 6 条核心规则
├── references/
│   ├── workflow.md               八步工作流详解 · 得来速/渠道比价专题 · 偏好解析 JSON · 输出字段细则
│   ├── schema.json               结构化输出契约（JSON Schema，与 optimizer.py::PLAN_OUTPUT_SCHEMA 同步）
│   ├── tools.md                  35 个 MCP 工具的参数与返回值注意事项 + 异常处理规则
│   └── strategy.md               菜品标签字典 / 营养约束映射 / 软硬约束处理 / 群体偏好聚合规则
├── scripts/
│   ├── optimizer.py              约束解析 · 硬约束过滤 · 组合优化 · 核价校验 · 渠道比价 · Schema 校验 · 健壮性
│   ├── gen_demo.py               演示报告生成器（跑真实优化链路 → docs/demo.html，纯标准库）
│   ├── verify_pricing.py         真实核价快照固化 + 一键重放（→ docs/pricing-verification.html）
│   └── validate_skill.py         Agent Skills 规范符合性检查器（frontmatter/渐进披露/零依赖/脱敏）
├── docs/
│   ├── demo.html                 演示可视化报告（三方案对比，零依赖单页，浏览器直接打开）
│   ├── pricing-verification.md   真实核价记录（5 次真实 MCP 调用链，脱敏）
│   ├── pricing-verification.html 核价对比页（本地估算 vs 官方核价）
│   └── portability.md            可移植性说明（各框架加载方式 / MCP 配置 / 环境变量）
├── tests/
│   └── test_regression.py        回归测试（pytest，5 场景 + 契约回归，25 用例）
├── .github/workflows/test.yml    CI：push/PR 自动跑 selftest + pytest + CLI 冒烟（Python 3.10/3.12）
├── mcp-config.example.json       MCP 配置示例（Token 用 ${MCD_MCP_TOKEN} 占位）
├── MCP_INTEGRATION.md            MCP 接入记录（脱敏日志 + 15 项实测 schema 偏差）
├── CONTEST_DECLARATION.md        参赛声明
└── README.md
```

---

## 五、演示场景

### 5.1 场景设定

> **用户输入**：「我们 4 个人，预算 100，一个减脂，一个不吃辣，一个要赠品，一个要便宜」

`parse_constraints()` 自动解析结果：

| 维度 | 解析值 |
|---|---|
| 人数 / 预算 | 4 人 / 总额 **¥100**（人均 ¥25） |
| 硬约束 | 排除标签 `spicy`（不吃辣） |
| 营养目标 | 标准减脂档：单餐 ≤600 kcal、脂肪 ≤25 g、蛋白 ≥20 g |
| 命中画像 | 减脂 / 忌口 / 要赠品 / 预算控（四类齐全） |
| 聚合权重 | cheap 0.50 · pref 0.25 · healthy 0.15 · speed 0.10 |

### 5.2 硬约束过滤结果

从 15 个候选餐品中**淘汰 2 个**并给出理由：

- ❌ 麦辣鸡腿汉堡、麦辣鸡翅-2块 → 命中排除标签 `spicy`
- ✅ 保留 **13 项**：板烧鸡腿堡、巨无霸、麦香鱼、双层吉士汉堡、中薯条、小杯玉米杯、苹果片、无糖可口可乐中杯、纯牛奶（盒装）、麦乐鸡5块、培根安格斯厚牛堡、圆筒冰淇淋、鲜蔬蛋沙拉叠叠卷

### 5.3 输出方案（Top-3）

```
🍔 方案 A｜最省钱              主食 4 份 ✅
├ 麦香鱼 ×3         ¥52.50
├ 双层吉士汉堡 ×1   ¥19.50
├ 券组合：9折券（C004）  −¥7.20
├ 原价：¥72.00 → 最终价：¥64.80
├ 人均：¥16.20（预算 ¥25/人 ✅）
└ 人均 351 kcal｜蛋白 18.8 g｜脂肪 15.2 g

🥗 方案 B｜最健康              主食 4 份 ✅
├ 板烧鸡腿堡 + 巨无霸 + 麦香鱼 + 双层吉士汉堡
├ 券组合：9折券（C004）  −¥8.30
├ 原价：¥83.00 → 最终价：¥74.70
├ 人均：¥18.68（预算 ✅）
└ 人均 414 kcal｜蛋白 23.2 g｜脂肪 19.5 g   ← 蛋白最高

⚡ 方案 C｜最快                主食 4 份 ✅
├ 板烧鸡腿堡 + 巨无霸 + 双层吉士汉堡 + 培根安格斯厚牛堡
├ 券组合：满99减15（C001）  −¥15.00
├ 原价：¥99.00 → 最终价：¥84.00｜人均 ¥21.00
└ 人均 510 kcal｜蛋白 27.8 g｜四个汉堡出餐快、无需等小食
```

> 以上结果可由 `python scripts/optimizer.py "我们4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"` **一键复现**。营养数据取自 `list-nutrition-foods` 的真实值（160 个餐品）；券为内置示例券，用于演示券组合 DP。

> **拼单底线约束**：算法强制 `主食份数 ≥ 人数`（每人至少 1 份汉堡/卷，小食、饮品、甜品不计入主食），
> 因此不会出现「2 个汉堡 + 2 份薯条分给 4 个人」这类分不下去的方案。

### 5.4 省钱对比数据

**基线**：4 人各点 1 份板烧鸡腿堡（¥21.50），不使用任何优惠 → **¥86.00**，人均 **¥21.50**

| 方案 | 原价 | 券优惠 | 最终价 | 人均 | 相比基线 | 降幅 |
|---|---|---|---|---|---|---|
| **最省钱** | ¥72.00 | −¥7.20 | **¥64.80** | **¥16.20** | **省 ¥21.20** | **↓24.7%** |
| 最健康 | ¥83.00 | −¥8.30 | ¥74.70 | ¥18.68 | 省 ¥11.30 | ↓13.1% |
| 最快 | ¥99.00 | −¥15.00 | ¥84.00 | ¥21.00 | 省 ¥2.00 | ↓2.3% |

**关键结论**

1. **最省钱方案比朴素点法省 24.7%**（人均从 ¥21.50 → ¥16.20），且同时满足「主食 ≥ 4 份 + 热量下限 + 不含辣 + 总预算 ¥100」。
2. **券的叠加能力被充分利用**：最快方案精准踩中「满 99 减 15」门槛（原价 ¥99.00）；券按 `exclusiveGroup` 互斥、按 `minSpend` 校验门槛、按 `appliesToProductCodes` 校验适用范围，避免把「麦乐鸡券」用在没有麦乐鸡的订单上。
3. **健康与便宜可以兼得，但有代价**：最健康方案人均蛋白 23.2 g（比最省钱高 23%），代价是人均多花 ¥2.48。
4. **预算约束全程有效**：三个方案人均均落在 ¥25（总额 ¥100 ÷ 4）预算内，且输出中显式标注了达标情况。

---

## 六、技术方案

| 模块 | 实现 |
|---|---|
| 约束解析 | 正则 + 关键词表；成分同义词归一（牛/猪/鸡/鱼/辣/油炸/奶/花生…） |
| 硬约束过滤 | 一票否决 + 原因记录，先压解空间再优化 |
| 最省钱求解 | **动态规划**：无界背包（状态 = 份数 × 热量桶）最小化成本；券组合另一层 DP（分组互斥 / 门槛 / 商品范围 / 最多叠加 2 张） |
| 多目标收敛 | **帕累托剪枝**（成本↓ + 热量接近目标）后按加权评分排序 |
| 加权评分 | `cheap / healthy / speed / pref` 四维，权重由画像决定 |
| 核价校验 | 极简 MCP Streamable HTTP 客户端；**分 → 元**换算；偏差 >5% 以官方为准 |
| 渠道比价 | 同一篮子 × 内部渠道（到店自取/麦乐送/得来速）到手成本对比；恒等式校验（商品+运费+打包+餐具−优惠=现价）；缺前置条件（如无配送地址）如实标「无法核价」 |
| 输出契约 | JSON Schema draft-07（`references/schema.json`）；`plans_to_doc()` 转换 + 纯标准库递归校验器（type/required/enum/const/边界），`--dump-schema` 生成镜像 |
| 模糊输入 | `clarify_input()` 三层询问：静默默认 / 聚焦追问（≤3 问）/ 你看着办（全默认 + banner 标注） |
| 群体公平 | `aggregate_preferences()`：硬约束禁忌并集、软偏好多数 + avoid 并集、口味自适应截尾均值（n≥4 去两端 n//4）；冲突检测（阵营各 ≥⌈n/3⌉ 且均值差 ≥3）→ A/B 双方案 + 妥协点；`fairness_score()` 逐成员 0-1 满足度，整体取最低（木桶效应） |
| 系统健壮 | `_mcp_call` 统一入口：10s 超时、指数退避（1s/2s/4s ×3 次 + 总预算 20s、401/400/业务错不重试）；`call_with_degrade()` 按工具降级 + 显式 banner；`create_order_guarded()` 幂等键（sha256(user+store+内容+5min 窗口)）防重复下单；`log_mcp_call()` JSONL 日志 + 三类敏感信息脱敏 |
| 错误处理 | 识别「业务错误不置 `isError`」的服务端行为，读 `structuredContent.success/code` |

**内建质量保障**：`--selftest` 覆盖 14 组断言，含 4 个已修 bug 的回归用例
（油炸误标、折扣券语义、浮点截断、券跨商品误用）、业务错误识别、渠道比价
（beCode 传递约定、到手成本恒等式、外部平台拒绝、无法核价兜底）、输出契约
（必填缺失 / 类型错误 / const 破坏 / 枚举越界 / 镜像同步）、模糊输入
（三层路径 / answers 合并 / 默认口味可解性）、群体公平性（硬约束交集 /
截尾均值 / 冲突双方案 / 木桶效应 / 硬违反零分 / 契约兼容）与系统健壮性
（幂等键去重与窗口 / 429 重试次数与 401 不重试 / 降级策略与 banner /
日志脱敏 / 重试条件分类）。

---

## 测试与质量保障

### 回归测试（pytest）

`tests/test_regression.py` 覆盖 5 个典型场景 + 输出契约回归，共 25 个用例：

| 场景 | 输入 | 关键断言 |
|---|---|---|
| 标准 4 人拼单 | 「4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜」 | 3 套方案（最省钱/最健康/最快）；每套主食 ≥4；总价 ≤¥100；忌辣零违反；群体入口逐成员公平性评分 |
| 单人点餐 | 「1个人，想吃板烧鸡腿堡」 | 3 套方案；主食 ≥1 且总份数 ≤6（不冗余）；人均=总价 |
| 模糊输入 | 「随便」 | L3 默认路径（不追问）；banner「基于默认偏好：不辣、不油炸」；默认口味下菜单仍可解；answers 合并后 ready 翻转 |
| 偏好冲突 | 「4个人，2个要辣2个不要辣，预算120」 | 检测 spicy 冲突（阵营均值差 ≥3）；A/B 双备选；各有妥协说明；A 不排除辣 / B 硬排除辣 |
| 极端预算 | 「4个人，预算30」 | 有解→守主食底线且预算可判读；无解→明确说明；预算 ¥1~¥10000 均不崩溃 |
| 输出契约 | —— | `--json` 文档通过 schema 校验；群体/单人路径兼容；未核价标注 |

运行方式：

```bash
python -m pip install pytest        # 仅测试期依赖（optimizer 本体零依赖）
python -m pytest tests/ -v          # 全部回归测试
python scripts/optimizer.py --selftest   # 内置 14 组断言自检
```

**测试通过标准**：`pytest tests/` 全绿（25 passed）且 `--selftest` 输出
「全部断言通过」；任一失败即视为核心逻辑被破坏，禁止合并/发布。
GitHub Actions（`.github/workflows/test.yml`）在 push / pull_request 时
自动跑 Python 3.10 + 3.12 双版本：selftest → pytest → 5 个 CLI 入口冒烟，
失败即红。

### 可观测性

- **调用日志**：每次 MCP 调用记 JSON Lines 至 `logs/mcp_calls.log`
  （工具名 / 耗时 / 状态 success·error·retry·degraded / 重试次数 / 入参快照）；
- **敏感信息脱敏**：Token→`[TOKEN]`、手机号→`[PHONE]`、地址→`[ADDRESS]`，
  写入前强制替换；`logs/` 目录已 gitignore，不入库；
- **幂等记录**：`logs/idempotency.json` 存幂等键→订单号映射（>1 天自动清理），
  防重复下单且可追溯。

---

## 七、兼容性验证（Agent Skills 规范）

**本 Skill 不是绑定 WorkBuddy 的，而是符合 Agent Skills 开放规范的可移植技能**：

1. **规范符合性**：`SKILL.md` frontmatter（`name` / `description`）符合 Agent Skills 规范
   （name 小写连字符 ≤64 字符且与目录名一致；description ≤1024 字符）。
   由 `python scripts/validate_skill.py` 自动检查，**8/8 项全部通过**
   （frontmatter / 渐进式披露 / references 引用完整 / 零依赖 / 敏感信息扫描）。
2. **渐进式披露**：`references/`（workflow / schema / tools / strategy）按需加载、
   不进初始上下文；SKILL.md 本体 204 行（≤500 行建议上限），三层结构符合规范。
3. **跨框架加载**：可被任何支持 MCP / Agent Skills 规范的框架加载——
   Claude Code（`.claude/skills/`，官方声明遵循 Agent Skills 开放标准）、
   Cursor（`.cursor/skills/`，并兼容读取 `.claude/skills/`）、
   Cline 3.48.0+（`.cline/skills/`）、WorkBuddy（`.workbuddy/skills/`）等。
   加载方式与 MCP 配置详见 [`docs/portability.md`](./docs/portability.md)。
4. **零依赖**：`optimizer.py` 纯 Python 标准库（hashlib/json/os/re/sys/time/typing/urllib），
   无任何第三方依赖，任何框架的沙箱 / 本地环境均可直接运行；无 MCP 连接时全部
   离线功能照常可用。

> **如实声明**：本项目开发与实测环境为 WorkBuddy（35 个 MCP 工具全链路实测，见
> `MCP_INTEGRATION.md`）；本机未安装 Claude Code / Cursor / Cline，未做跨框架加载实测，
> 上述框架的加载方式依据其官方文档（检索于 2026-10-10）。规范符合性检查可复现：
> `python scripts/validate_skill.py`。

---

## 八、合规与边界

- 本项目**不是**麦当劳官方产品，与麦当劳公司无隶属或背书关系。
- **不含任何真实凭证**：示例配置仅含 `${MCD_MCP_TOKEN}` 占位符。
- **不采集用户数据**，偏好信息仅在当次会话内存中参与计算。
- **写操作一律需用户确认**后才执行；不伪造「下单成功」。
- 营养与价格数据仅供参考，**不构成医疗、营养或财务建议**；最终以官方渠道结算为准。

详见 [`CONTEST_DECLARATION.md`](./CONTEST_DECLARATION.md)。

---

## 九、延伸阅读

- 工作流概览与核心规则 → [`SKILL.md`](./SKILL.md)（含子代理编排设计）
- 详细工作流与输出字段细则 → [`references/workflow.md`](./references/workflow.md)
- 结构化输出契约 → [`references/schema.json`](./references/schema.json)
- 工具参数速查 → [`references/tools.md`](./references/tools.md)
- 标签字典与约束逻辑 → [`references/strategy.md`](./references/strategy.md)（含群体偏好聚合规则）
- MCP 接入踩坑记录（15 项实测偏差）→ [`MCP_INTEGRATION.md`](./MCP_INTEGRATION.md)
- 演示可视化报告（三方案对比）→ [`docs/demo.html`](./docs/demo.html)
- 真实核价记录（¥44.00 = ¥44.00，偏差 0%）→ [`docs/pricing-verification.md`](./docs/pricing-verification.md)
- 可移植性与跨框架加载指南 → [`docs/portability.md`](./docs/portability.md)
