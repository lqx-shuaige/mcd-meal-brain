---
name: mcd-meal-brain
description: 面向多人场景的麦当劳智能点餐决策引擎。当用户需要多人拼单、优化点餐组合、在预算/口味/健康/活动之间做取舍，或积分换好物、预约取餐、按热量预算配餐时使用。
agent_created: true
---

# 麦麦拼单大脑

把「一群人吃什么、怎么点最划算」算成可执行方案。它不替用户下单，而是**算清楚、讲明白、等确认**。

> 详细工作流、得来速/渠道比价专题、偏好解析 JSON、输出字段细则 → `references/workflow.md`
> 结构化输出契约（JSON Schema）→ `references/schema.json`（与 `optimizer.py::PLAN_OUTPUT_SCHEMA` 同步）

---

## 触发条件

- 用户提到「多人」「拼单」「一起点」「宿舍」「同事」等多人点餐场景
- 用户提出预算限制、口味偏好、饮食禁忌、减脂等约束
- 用户询问「怎么点最省」「有什么活动」「怎么凑满减」
- 用户提到「得来速」「车道取餐」「开车去取」「DT」「Drive Through」等驾车取餐场景
- 用户询问「哪个渠道便宜」「到手多少钱」「自取和外卖差多少」等渠道比价场景
- 用户提到「积分」「兑换」「积分商城」「麦麦商城」「积分有什么用」等积分场景（→ 扩展分支 A）
- 用户提到「预约」「预定」「明天中午取」「提前点」等预约取餐场景（→ 扩展分支 B）
- 用户询问「有什么活动」「能抽奖吗」「抽奖有什么奖品」等活动/抽奖场景（→ 扩展分支 C）
- 用户给出热量目标（「每人 500 大卡以内」「控制热量」「低卡配餐」）（→ 扩展分支 D）

**不触发**：单人单品简单点餐；用户直接指定了订单内容。

---

## 工作流概览（8 步）

每步的展开细节（传参规则、实测坑、示例）见 `references/workflow.md`。

1. **解析需求** —— 抽取场景/人数/预算/忌口/口味/健康目标；先锁场景（beType）再动其他参数。
2. **环境准备** —— 取服务器时间；按场景查门店（到店/得来速用 `query-nearby-stores`，外送/团餐走地址→`delivery-query-stores`），锁定 `storeCode` / `beCode`。
3. **菜单与偏好采集** —— `query-meals` 拉在售餐品，`list-nutrition-foods` 拿营养，把每位参与者转成画像 JSON。
4. **硬约束过滤** —— `optimizer.py::filter_menu()` 一票否决违规商品；被剔除项要能解释「为什么没有它」。
5. **券与促销盘点** —— `query-store-coupons` 是唯一权威；团餐另查 `query-promotions` / `query-meal-assistance`。
6. **组合优化** —— `optimizer.py::optimize_plans()`（默认 **NSGA-II 多目标优化**）：四目标帕累托前沿（价格↓/人均热量↓/油炸份数↓/人均蛋白↑）上取「最省钱/最健康/最快」三个极值点，固定种子可复现；原加权评分保留为回退（`method="weighted"`）；结构化输出遵循 `references/schema.json` 契约。
7. **核价校验** —— 候选方案逐个过 `calculate-price` 官方核价；偏差 >5% 以官方价为准并如实标注。
8. **确认下单** —— 展示方案 → 等用户明确确认 → `create-order`；下单后用 `query-order` 跟踪支付状态。

需要**渠道比价**（到手成本：到店自取 vs 麦乐送 vs 得来速）时，在同一篮子上调
`optimizer.py::compare_channels()`；范围仅限麦当劳内部渠道，外部平台不做（详见 workflow.md 专题）。

---

## 扩展分支（主链路之外的增值场景）

主链路（8 步）产出 Top-3 方案之外，以下分支按需启用。分支复用主链路的硬约束、
写操作确认与异常处理规则，不另起炉灶。

### 分支 A · 积分换好物（积分商城）

**触发**：「积分怎么用」「积分商城有什么」「兑换什么划算」「积分换好物」「查下我的积分」。

1. **查余额**：`query-my-account` → `availablePoint`（可用）/ `currentMouthExpirePoint`
   （本月将过期，> 0 时优先提醒消耗快过期积分）；
2. **查商品**：`mall-points-products`（`catRuleIds=1>4` 商品券、`2` 实物、`2>8` 周边、
   `1>6>20` 生日派对等；不传查全部）——逐项核对 `status`（**仅 2=上架 可兑**，3 售罄/4 下架/5 预热
   要说明）与 `upTime`/`downTime`（不在兑换窗口内的跳过并注明原因）；
3. **兑换比价**（同一餐品「积分券 vs 现金点餐」）：
   - 券后到手价 = `price`（兑换时还需支付的现金，多为 0）+ `point`（所需积分）；
   - 用 `query-meals` 查同款/同组合的**现金单价** → 输出对比「券后到手价 vs 现金价，
     省 ¥X、需 Y 积分」；
   - ⚠️ MCP 不提供「积分 ↔ 现金」折算率：**不折算积分的货币价值**，只并列呈现
     「省钱额」与「积分消耗」两个量纲，取舍留给用户；
4. **余额不足**：如实告知「积分不够，还差 Z 分」，提示点餐可攒积分；**不**为凑积分推荐
   超预算点单（预算是更高优先级约束，见冲突优先级）；
5. **兑换下单**：用户选具体商品 → `mall-product-detail(spuId)` 展示 SKU → 用户选定 SKU 并
   **明确确认** → `mall-create-order`（写操作，走规则 4：spuCategory=2 实物必传 `addressId`；
   多件兑换分别下单且**串行排队**）。

> 实测锚点（2026-10-10）：账户可用积分 73.9；商品券在架 24 款，最低 50 积分（6.9 元派/麦炫酷），
> 最高 800 积分（28.8 元巨无霸四件套 / 28.9 元板烧四件套）。

### 分支 B · 预约取餐

**触发**：「明天中午取」「预约周六 11 点」「提前预定」「后天到了直接拿」。

1. `now-time-info` 取服务器时间确定「今天」；用户给的取餐时间先与其对齐，至少精确到分钟；
2. `query-nearby-stores(beType=1)` 查门店：返回中 `reservation=true` 的门店**完整展示**
   `reservationTimeOptions`（可预约时段），`today=true` 标注「(今天)」——
   **可预约时段以门店返回为准，不自行编造时间**；
3. `query-meals(storeCode, orderType, beType, reservationDate="yyyy-MM-dd HH:mm")` 拉取
   **预约时段菜单**（预约场景 `reservationDate` 必传；同一门店不同时段的菜单/价格可能不同，
   预约菜单与实时菜单不可混用）；
4. 走主链路第 4–7 步（过滤 / 券盘点 / 优化 / 核价）：`calculate-price` **同样传
   `reservationDate`**，保证核价口径与预约时段一致；
5. **方案输出增加「预约取餐」选项**：与「立即取餐」并排展示，标注预约时间；
   预约菜单与当前菜单有差异（缺货/价格变动）时如实说明；
6. 用户确认预约方案后 `create-order(..., reservationDate=...)`（写操作，规则 4；
   得来速/团餐按各自 beCode 规则传参）。

### 分支 C · 活动抽奖提示

**触发**：方案输出 / 下单完成后**追加提示**；用户问「有什么活动」「能抽奖吗」时主动查询。

1. `query-lottery-info` 查当前抽奖活动（**只查不抽**）：读 `activityStatusText`（仅「进行中」
   才提示）、`activityName`、`drawDecision.nextConsumption`（本次实际消耗，如「本次将消耗
   24 积分」）、`prizes`（挑 2–3 个代表性奖品展示）；
2. 在方案末尾追加：「🎰 点餐后可参与抽奖：{活动名}，单次消耗 {nextConsumption.text}，
   有机会抽 {奖品示例}」；
3. **禁止**未经用户明确确认调用 `draw-lottery`（写操作，规则 4——「试试看」不算确认）；
   `drawDecision.resourceEligible=false` 时不引导抽奖，如实转述 `reason`（如积分不足）；
4. **严禁**用 `availableTimes=0/null` 自行推断不能抽——资格与消耗一律以 `drawDecision` 为准
   （混合规则下 `availableTimes=0` 且 `nextConsumption.type=POINTS` 仍可积分抽）。

> 实测锚点（2026-10-10）：活动「周六麦金喜抽奖」进行中，单次 24 积分
> （`drawDecision.nextConsumption` = POINTS 24），奖品含积分霸王餐、麦金卡 7 天、
> 麦辣鸡腿堡 5 折券、立减 3 元券等。

### 分支 D · 按热量预算配餐模式

**触发**：用户输入热量目标，如「4 个人，每人 500 大卡以内」「每人控制在 600 千卡」。

1. `optimizer.py::parse_constraints` 解析「每人 X 大卡 / 千卡 / kcal」→
   `targets.max_calories_kcal`（**人均硬上限**，tier=custom）；
2. 走主链路第 2–3 步拿真实菜单与营养表（`list-nutrition-foods`，名称精确匹配，不估算）；
3. `optimizer.py::optimize_calorie_budget()` 生成**主餐 + 小食 + 饮品**组合：
   - 每人组合 = 1 份主餐 + 0–1 份小食 + 0–1 份饮品，**人均热量 ≤ 预算**由构造保证
     （不是事后过滤）；
   - 输出 Top-3：最省（预算内成本最低）/ 高蛋白（蛋白密度最高）/ 均衡混搭（多样性）；
   - 预算过低（最便宜主餐都装不下）时：**不静默放宽**，输出最低热量主餐 + 差距说明，
     建议调整预算或减少人数；
4. 每套方案标注人均热量与预算余量（如「人均 444 / 500 kcal，余 56 kcal」）；
5. 验证：`python scripts/optimizer.py --calorie-budget "4个人，每人500大卡以内" [--demo]`。

### 分支 E · 满意度评价领券（下单后闭环）

1. 下单支付完成后（`query-order` 确认已支付），追加提示：「💬 完成本单满意度评价可领奖券」；
2. 用户询问答卷结果 / 奖券内容 / 核销状态时：`query-survey-coupon(orderId)`——
   `orderId` 只能来自用户本人提供或上游订单查询返回，**不得自行生成**；
3. 返回的满意度描述、奖券标题、核销时间 / 核销状态、点餐方式**原样转述**；
   未返回的原始答卷、会员号、couponId / couponCode 不猜测。

## 模糊输入处理（三层询问机制）

用户说「随便」「都行」「你看着办」时，**不瞎猜、不卡死**。用 `optimizer.py::clarify_input()`
分析模糊点，按三层机制处理：

| 层级 | 触发条件 | 行为 |
|---|---|---|
| **第一层 · 静默默认** | 常识可推断的缺失 | 直接填默认值，不打扰用户（取餐方式缺失 → 到店自取） |
| **第二层 · 聚焦追问** | 必须用户决定的关键项缺失 | **一次性**追问最多 3 个问题：人数（无默认）、预算（默认不限）、口味（默认不辣不油炸） |
| **第三层 · 你看着办** | 输入含「随便/都行/无所谓/看着办…」 | 全部走默认值，不再追问；输出方案时**必须标注**「基于默认偏好：不辣、不油炸」 |

**执行规则**：

1. `clarify_input()` 返回 `ready` 标志：`ready=False` 时先把 `questions` 一次性抛给用户，
   **不要**拿不完整约束去优化；
2. 用户回答后，以 `answers={"people_count":…, "budget":…, "taste":…}` 合并重跑
   （口味回答走完整同义词归一，可推翻预填的默认口味；「都行」类回答视为接受默认）；
3. 第二层路径下口味默认会**预填**到约束中并标记「待确认」——用户明确给出口味后自动替换；
4. 人数只在第三层才默认（1 人）；第二层路径必须追问，因为人数没有合理默认值；
5. 输出任何应用了默认值的方案，须附 `banner`（「基于默认偏好：不辣、不油炸」）及
   `defaults_applied` 清单，让用户知道哪些是替他做的决定。

验证：`python scripts/optimizer.py --clarify "随便"`（可用 `--answers '{...}'` 演示追问合并）。

---

## 子代理编排设计

> 设计说明：当前为单 Skill 单进程实现；本节描述复杂会话下的三角色拆分，用于职责隔离与并行化。
> 三角色的输入输出契约与现有函数一一对应（解析 Agent ↔ `parse_constraints`，查询 Agent ↔ 各 `fetch_*`，
> 优化 Agent ↔ `optimize_plans`），无需重写即可落地。

| 角色 | 职责 | 输入 | 输出 |
|---|---|---|---|
| **解析 Agent** | 意图解析、多人偏好提取 | 用户自然语言 + 会话上下文 | 约束 JSON（`meal_context` / `participants` / `aggregated`，结构见 workflow.md） |
| **查询 Agent** | MCP 调用：门店、菜单、券、营养、促销 | 场景参数（beType / storeCode / beCode）+ 忌口成分并集 | 菜单 JSON（productCode / 价格 / 营养）+ 可用券池（`query-store-coupons` 为准） |
| **优化 Agent** | 调 `optimizer.py` 求解 | 约束 JSON + 菜单 JSON + 券池 | Top-3 方案（`schema.json` 契约）+ 帕累托前沿 |

**协作顺序（串行流水线 + 一条回环）**：

```
用户 ─自然语言→ 解析 Agent ─约束JSON→ 查询 Agent ─菜单+券→ 优化 Agent ─Top-3方案→ 主对话
                  ▲                                              │
                  └──────── 无解/约束冲突报告（需用户决策）──────┘
```

1. **解析 → 查询**：解析 Agent 输出的硬约束并集（成分黑名单）交给查询 Agent 过滤菜单；
2. **查询 → 优化**：查询 Agent 只提供**真实数据**（productCode / 价格 / 营养 / 券），不做任何推荐；
3. **优化 → 主对话**：优化 Agent 不与用户对话；无解时返回冲突清单，由主对话向用户复述并
   收集放宽决策，再回到解析 Agent 重新收敛（回环）；
4. **核价留在查询 Agent**：优化 Agent 产出估算方案后，主对话把方案篮子交回查询 Agent 调
   `calculate-price` 官方核价（MCP 调用归查询角色）；
5. **写操作不下放**：`create-order` 等写操作涉及用户确认，始终留在主对话执行。

**失败兜底**：任一子代理超时/报错，主对话降级为单进程模式（按 workflow.md 八步自己走一遍），
不中断会话。

---

## 核心规则

### 规则 1 · 参数来源强制约束
**所有入参只能来自上游真实 MCP 返回，禁止推断、推测、硬编码。**

| 参数 | 唯一合法来源 |
|---|---|
| `storeCode` / `beCode` | `query-nearby-stores` 或 `delivery-query-stores` |
| `productCode` | `query-meals` |
| `couponId` / `couponCode` | `query-store-coupons` |
| `takeWayCode` | `calculate-price` 的 `data.takeWayList[].code`（实测值：`eat-in` 堂食 / `locker-in` 外带 / `take-dt-quick` 得来速） |
| `gmServiceCode` | `query-meal-assistance` |
| `addressId` | `delivery-query-addresses` / `delivery-create-address` |
| `spuId` / `skuId` | `mall-points-products` 或 `mall-product-detail` |
| `dateStr` / 场次 `id` | `query-party-store-date` / `query-party-store-session` |
| `reservationDate` | 用户明确指定的未来取餐时间（先与 `now-time-info` 对齐）；可预约时段以 `query-nearby-stores` 返回的 `reservationTimeOptions` 为准 |
| `orderId` | 用户提供，或上游订单查询返回 |

拿不到就回到对应步骤重新获取，**不许编**。

### 规则 2 · 价格单位
- `calculate-price` 返回的价格字段单位是「**分**」，展示前**必须 ÷100 转「元」**——
  包括到手成本全结构：`productPrice`（商品）/ `deliveryPrice`（运费）/ `packingPrice`（打包费）/
  `tablewarePrice`（餐具费）/ `discount`（优惠）/ `price`（现价）。
- `query-meals` / `create-order` 返回的价格是**「元」字符串**（如 `currentPrice`、`originalPrice`，实测 `type=string`），直接展示，**不要再除 100**。
- 两个来源对同一商品可能不一致 —— **一切以 `calculate-price` 核价为准**。
- 到手成本恒等式：`商品 + 运费 + 打包 + 餐具 − 优惠 = 现价`；分项之和与现价不符时**以现价为准**并标注。

> 内部计算统一用「分」（避免浮点误差），只在最终展示层转「元」。

### 规则 3 · 券来源区分
| 工具 | 语义 | 能否用于算方案 |
|---|---|---|
| `available-coupons` | 账户**当前可领取**的券 | ❌ 只是「可领」 |
| `query-my-coupons` | 用户**卡包**里的券资产 | ❌ 不做门店/渠道校验，不承诺可用 |
| `query-store-coupons` | **指定门店 + 订单类型真实可用** | ✅ **唯一权威来源** |

任何「用券后的最终价」必须以 `query-store-coupons` 为依据；
`available-coupons` 只能用于提示用户「可以去领券」。

### 规则 4 · 写操作必须确认 + 幂等防重
以下工具会改变账户/订单状态，**一律先展示变更内容，再等用户明确确认**：

`auto-bind-coupons`、`delivery-create-address`、`create-order`、`cancel-order`、`mall-create-order`、`party-order-create`、`draw-lottery`

- 下单类：**禁止**未真实调用工具就输出「下单成功」；必须真实调用后回传结果。
- 抽奖类：必须先展示 `drawDecision.nextConsumption`，「试试看」**不算**确认。
- 多件兑换：必须**分别下单且串行排队**，前单成功后再下新单。

**下单幂等机制**（防重复下单）：调 `create-order` 前必须走
`optimizer.py::create_order_guarded(user_id, store_code, items, arguments)`——
幂等键 = `sha256(user_id + store_code + 订单内容哈希 + 5 分钟时间窗口)`：

1. 键命中已有**成功**订单 → **不再调用 MCP**，直接返回已有订单并告知用户；
2. 下单失败 / 业务错误 → **不记录**幂等键（用户重试不受影响）；
3. 响应中拿不到订单号 → 视为未确认成功，不记录（宁可重复检查，不可漏单）；
4. 记录落盘 `logs/idempotency.json`（键 → 订单号 + 时间戳，>1 天自动清理）；
5. 同一订单内容隔 5 分钟后再点 = 新窗口 = 允许再下一单（正常复购不受影响）。

### 规则 5 · 限流与鉴权异常处理
- **429 / 「操作太频繁」**：优先读本地缓存——`optimizer.py` 的 **ReflectCache** 已在
  `_mcp_call` 统一入口实现参数级缓存（工具名 + 参数精确匹配，只读工具、成功响应、
  TTL 300s，`MCD_REFLECT_CACHE=0` 可关）；缓存未命中则友好提示「请求太频繁，请稍后再试」，不连续重试。
  （`draw-lottery` 有频控，重复调用返回「操作太频繁」，**不扣积分**。）
- **401**：提示用户重新获取 Token，并检查 MCP 配置中的 `Authorization` 是否正确注入。
- **通用**：失败后指数退避重试（1s → 2s → 4s），最多 3 次；禁止并发轰炸；仍失败则暂停并报告错误原文。
- **业务错误码**：本 MCP 的业务错误**不会**置 `isError=true`，而是返回
  `result.structuredContent.{success:false, code, message}`（实测门店打烊会返回
  `60003` / `600057` 等不同 code）。必须读取 `success` 判断，不能只看 `isError`。

### 规则 6 · 数据分流
- **根级结构化字段**（`structuredContent.data` 及各类 `code` / `id` / 数值）→ 供 LLM 推理、计算、填空。
- **展示型字段**（`content[].text` 中的说明文案、`imageUrl`、`reservationOptionText` 等）→ **原样展示**给用户，不加工、不改写、不省略。
- `result.content[].text` 里的「字段说明」是给模型看的文档，**不要把说明文字当数据念给用户**。

> 注：MCP 响应中**不存在** `genui` 字段（35 个工具 schema 实测命中 0 次）；
> 本 Skill 输出契约中的 `genui` 是自定义展示提示字段，二者无关（见 workflow.md 第 5 节）。

---

## 不做的事

1. **不臆造参数** —— `storeCode`、`productCode`、`couponId`、`spuId`、`takeWayCode`、`gmServiceCode` 等一律来自真实返回，禁止推断或占位。
2. **不擅自下单** —— 未获用户明确确认前，不调用任何写操作；不伪造「下单成功」。
3. **不混用券来源** —— 不用 `available-coupons` / `query-my-coupons` 计算优惠。
4. **不搞错价格单位** —— 不把「分」当「元」展示，也不对已是「元」的字段再除 100。
5. **不静默放宽硬约束** —— 过敏、忌口、预算上限无解时，如实报告无解并请用户决策，不偷偷妥协。
6. **不隐瞒失败** —— 工具报错、限流、核价偏差都如实告知，不改写错误信息、不假装成功。
7. **不越权操作** —— 只操作当前登录用户本人的账户、订单与地址，不查询、不推测他人数据。
8. **不输出专业建议** —— 营养与热量数据仅供参考，不构成医疗、健康或膳食处方建议。
9. **不混淆订单体系** —— 餐饮订单用 `order-list` / `query-order` / `create-order`，商城订单用 `mall-*`，不可交叉。
10. **不重复拉取** —— 同一轮对话内菜单、券、门店结果复用，不反复调同一接口
    （`optimizer.py::ReflectCache` 参数级缓存已在 `_mcp_call` 层自动去重：同工具同参数直接复用）。
11. **不折算积分货币价值** —— MCP 不提供积分 ↔ 现金汇率；兑换比价只并列「省钱额 + 积分消耗」，不编造折算率。
12. **不编造预约时段** —— 可预约时间以门店返回的 `reservationTimeOptions` 为准；不在窗口内的时间不硬塞。

---

## 资源指引

| 文件 | 内容 |
|---|---|
| `references/workflow.md` | 八步工作流详解 · 得来速场景要点 · 渠道比价专题 · 多人偏好解析 JSON · 输出字段细则 · 积分商城/预约取餐/热量预算/抽奖与评价领餐券专题 |
| `references/schema.json` | 结构化输出契约（JSON Schema draft-07，`plans` 数组：name / items / coupons / original_total / final_total / per_person / nutrition / genui） |
| `references/tools.md` | 35 个 MCP 工具的参数与返回值注意事项 |
| `references/strategy.md` | 菜品标签字典、营养约束映射、软硬约束处理逻辑 |
| `scripts/optimizer.py` | 约束解析 · 硬约束过滤 · NSGA-II 多目标优化 · 热量预算配餐 · 核价校验 · 渠道比价 · ReflectCache 参数级缓存 · Schema 校验 |
