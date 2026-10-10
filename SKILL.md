---
name: mcd-meal-brain
description: 面向多人场景的麦当劳智能点餐决策引擎。当用户需要多人拼单、优化点餐组合、在预算/口味/健康/活动之间做取舍，或积分换好物、预约取餐、按热量预算配餐时使用。
agent_created: true
---

# 麦麦拼单大脑

把「一群人吃什么、怎么点最划算」算成可执行方案。它不替用户下单，而是**算清楚、讲明白、等确认**。

> 工作流详解 / 各场景专题 / 输出字段细则 → `references/workflow.md`
> 结构化输出契约 → `references/schema.json`（与 `optimizer.py::PLAN_OUTPUT_SCHEMA` 同步）
> 工具参数速查 → `references/tools.md`；标签与约束逻辑 → `references/strategy.md`

---

## 触发条件

- 多人点餐：「多人」「拼单」「一起点」「宿舍」「同事」
- 约束取舍：预算限制、口味偏好、忌口/过敏、减脂
- 省钱导向：「怎么点最省」「怎么凑满减」「有什么活动」
- 场景/渠道：「得来速 / DT / Drive Through」「哪个渠道便宜」「自取和外卖差多少」
- 增值分支：积分兑换（→分支 A）、预约取餐（→分支 B）、活动抽奖（→分支 C）、热量预算（→分支 D）、评价领券（→分支 E）

**不触发**：单人单品简单点餐；用户已直接指定订单内容。

---

## 工作流（8 步）

1. **解析需求** —— 抽取场景/人数/预算/忌口/口味/健康目标；先锁场景（beType）再动其他参数。
2. **环境准备** —— 取服务器时间；按场景查门店（到店/得来速用 `query-nearby-stores`，外送/团餐走地址→`delivery-query-stores`），锁定 `storeCode` / `beCode`。
3. **菜单与偏好采集** —— `query-meals` 拉在售餐品，`list-nutrition-foods` 拿营养，把每位参与者转成画像 JSON。
4. **硬约束过滤** —— `optimizer.py::filter_menu()` 一票否决违规商品；被剔除项要能解释「为什么没有它」。
5. **券与促销盘点** —— `query-store-coupons` 是唯一权威；团餐另查 `query-promotions` / `query-meal-assistance`。
6. **组合优化** —— `optimizer.py::optimize_plans()`（默认 **NSGA-II 多目标优化**）：四目标帕累托前沿（价格↓/人均热量↓/油炸份数↓/人均蛋白↑）取「最省钱/最健康/最快」三极值点，固定种子可复现；加权评分保留为回退（`method="weighted"`）。
7. **核价校验** —— **默认只核 Top-1（最省钱）**，控制调用量；用户明确意向再核其他方案。偏差 >5% 以官方价为准并如实标注。
8. **确认下单** —— 展示方案 → 等用户明确确认 → `create-order`；下单后用 `query-order` 跟踪支付状态。

需要**渠道比价**时，同一篮子上调 `optimizer.py::compare_channels()`；范围仅限麦当劳内部渠道，外部平台不做（详见 workflow.md 专题）。

> 优先用一条 CLI 命令完成多步（`python scripts/optimizer.py "<需求>"`），减少多轮工具调用：
> 菜单/券/营养取数已在 `fetch_live_context` 内部**并行**完成，无需逐个手动调用。

---

## 扩展分支（主链路之外，按需启用；复用主链路的硬约束、写操作确认与异常处理规则）

### 分支 A · 积分换好物
**触发**：「积分怎么用」「积分商城有什么」「兑换什么划算」「查下我的积分」。
查余额（`query-my-account`，将过期积分 >0 必须提醒）→ 查商品（`mall-points-products`，
**仅 status=2 上架可兑**，核对兑换窗口）→ 兑换比价（券后到手价 vs 现金价，
**不折算积分货币价值**，只并列「省钱额 + 积分消耗」）→ 确认后 `mall-create-order`
（实物必传 addressId；多件分别下单且串行排队）。余额不足如实告知差额，
不为凑积分推荐超预算点单。→ 细节见 workflow.md §6.1

### 分支 B · 预约取餐
**触发**：「明天中午取」「预约周六 11 点」「提前预定」。
`now-time-info` 对齐「今天」→ 门店 `reservationTimeOptions`（**可预约时段只认门店返回，
不编造**）→ `query-meals(…, reservationDate="yyyy-MM-dd HH:mm")` 拉预约时段菜单
（与实时菜单**不可混用**）→ 走主链路第 4–7 步（`calculate-price` 同传 reservationDate）
→ 「预约取餐」与「立即取餐」并排展示 → 确认后 `create-order(…, reservationDate=…)`。
→ 细节见 workflow.md §6.2

### 分支 C · 活动抽奖提示
**触发**：方案输出/下单完成后**追加提示**；用户问「有什么活动」「能抽奖吗」。
`query-lottery-info`（**只查不抽**）：仅 `activityStatusText`「进行中」才提示，末尾追加
「🎰 点餐后可参与抽奖：{活动名}，单次消耗 {nextConsumption.text}」。资格与消耗
**一律以 `drawDecision` 为准**（严禁用 `availableTimes=0/null` 自行推断）；
`resourceEligible=false` 时如实转述 reason。未经明确确认禁止 `draw-lottery`
（「试试看」不算确认）。→ 细节见 workflow.md §6.4

### 分支 D · 按热量预算配餐
**触发**：「4 个人，每人 500 大卡以内」「控制热量」。
`parse_constraints` 解析人均热量硬上限 → `optimizer.py::optimize_calorie_budget()`
生成「主餐 + 0–1 小食 + 0–1 饮品」组合（人均 ≤ 预算由构造保证）；输出 Top-3
（最省 / 高蛋白 / 均衡混搭）并标注人均热量与预算余量。预算过低无解时**不静默放宽**，
输出最低热量主餐 + 差距说明。→ 细节见 workflow.md §6.3

### 分支 E · 满意度评价领券（下单后闭环）
支付完成后（`query-order` 确认）追加提示「💬 完成本单满意度评价可领奖券」；
用户询问答卷/奖券时用 `query-survey-coupon(orderId)`（orderId 仅来自用户本人或
上游订单返回，**不得自行生成**），返回字段**原样转述**，未返回的不猜测。

---

## 模糊输入处理（三层询问机制）

用户说「随便」「都行」「你看着办」时，用 `optimizer.py::clarify_input()` 按三层处理：

| 层级 | 触发条件 | 行为 |
|---|---|---|
| **L1 · 静默默认** | 常识可推断的缺失 | 直接填默认（取餐方式缺失 → 到店自取），不打扰 |
| **L2 · 聚焦追问** | 关键项缺失 | **一次性**追问 ≤3 个：人数（无默认）、预算（默认不限）、口味（默认不辣不油炸） |
| **L3 · 你看着办** | 输入含「随便/都行/无所谓」 | 全默认不再追问，输出**必须标注**「基于默认偏好」 |

执行要点：`ready=False` 时先抛 `questions` 再优化，不拿不完整约束硬算；用户回答后
以 `answers={people_count/budget/taste}` 合并重跑（口味走同义词归一，可推翻预填默认）；
人数仅 L3 才默认 1 人；任何应用了默认值的方案须附 `banner` 与 `defaults_applied` 清单。

---

## 子代理编排设计（复杂会话的三角色拆分）

| 角色 | 职责 | 对应实现 |
|---|---|---|
| **解析 Agent** | 意图解析、多人偏好提取 → 约束 JSON | `parse_constraints` |
| **查询 Agent** | MCP 调用（门店/菜单/券/营养/核价），只供真实数据不做推荐 | 各 `fetch_*` |
| **优化 Agent** | 调 `optimizer.py` 求解，不与用户对话 | `optimize_plans` |

流水线：解析 → 查询 → 优化 → 主对话；无解时冲突清单回环到解析。写操作（下单等）
**始终留在主对话**（涉及用户确认）。任一子代理失败，主对话降级为单进程按八步走完，不中断。

---

## 核心规则

### 规则 1 · 参数来源强制约束
**所有入参只能来自上游真实 MCP 返回，禁止推断、推测、硬编码。**

| 参数 | 唯一合法来源 |
|---|---|
| `storeCode` / `beCode` | `query-nearby-stores` 或 `delivery-query-stores` |
| `productCode` | `query-meals` |
| `couponId` / `couponCode` | `query-store-coupons` |
| `takeWayCode` | `calculate-price` 的 `data.takeWayList[].code`（`eat-in` 堂食 / `locker-in` 外带 / `take-dt-quick` 得来速） |
| `gmServiceCode` | `query-meal-assistance` |
| `addressId` | `delivery-query-addresses` / `delivery-create-address` |
| `spuId` / `skuId` | `mall-points-products` 或 `mall-product-detail` |
| `dateStr` / 场次 `id` | `query-party-store-date` / `query-party-store-session` |
| `reservationDate` | 用户明确指定的未来取餐时间（先与 `now-time-info` 对齐）；可预约时段以 `reservationTimeOptions` 为准 |
| `orderId` | 用户提供，或上游订单查询返回 |

拿不到就回到对应步骤重新获取，**不许编**。

### 规则 2 · 价格单位
- `calculate-price` 返回价格单位是「**分**」，展示前**必须 ÷100 转「元」**——包括到手成本全结构（`productPrice`/`deliveryPrice`/`packingPrice`/`tablewarePrice`/`discount`/`price`）。
- `query-meals` / `create-order` 返回的价格是**「元」字符串**，直接展示，不要再除 100。
- 两来源不一致时**一切以 `calculate-price` 核价为准**；到手成本恒等式 `商品+运费+打包+餐具−优惠=现价`，分项之和与现价不符时以现价为准并标注。
- 内部计算统一用「分」（避免浮点误差），只在最终展示层转「元」。

### 规则 3 · 券来源区分
| 工具 | 语义 | 能否用于算方案 |
|---|---|---|
| `available-coupons` | 账户**当前可领取**的券 | ❌ 只能用于提示「可以去领券」 |
| `query-my-coupons` | 用户**卡包**里的券资产 | ❌ 不做门店/渠道校验 |
| `query-store-coupons` | **指定门店 + 订单类型真实可用** | ✅ **唯一权威来源** |

### 规则 4 · 写操作必须确认 + 幂等防重
以下工具改变账户/订单状态，**一律先展示变更内容，再等用户明确确认**：
`auto-bind-coupons`、`delivery-create-address`、`create-order`、`cancel-order`、
`mall-create-order`、`party-order-create`、`draw-lottery`。

- 下单类：**禁止**未真实调用工具就输出「下单成功」；
- 抽奖类：必须先展示 `drawDecision.nextConsumption`，「试试看」**不算**确认；
- 多件兑换：**分别下单且串行排队**；
- 下单幂等：调 `create-order` 前必须走 `optimizer.py::create_order_guarded()`
  （幂等键 = user+store+订单内容哈希+5 分钟窗口；命中成功订单直接复用不再调用；
  失败不记录；记录落盘 `logs/idempotency.json`，>1 天自动清理）。

### 规则 5 · 限流与鉴权异常处理
- **429**：优先读本地缓存（`optimizer.py::ReflectCache` 参数级缓存，TTL 300s，
  `MCD_REFLECT_CACHE=0` 可关）；未命中则提示稍后再试，不连续重试。
- **401**：提示重新获取 Token；**400 / 业务错误**：不重试。
- **网络超时 / 5xx**：指数退避重试（1s→2s→4s，最多 3 次，总预算 20s），禁止并发轰炸。
- 业务错误**不置 `isError=true`**，而是 `structuredContent.{success:false, code, message}`——
  必须读 `success` 判断。

### 规则 6 · 数据分流
- **根级结构化字段**（`structuredContent.data` 及各类 code/id/数值）→ 供推理与计算；
- **展示型字段**（说明文案、`imageUrl`、`reservationOptionText` 等）→ **原样展示**，不加工不改写；
- `result.content[].text` 里的字段说明是给模型看的文档，**不要念给用户**。
- MCP 响应中**不存在** `genui` 字段；输出契约中的 `genui` 是本 Skill 自定义展示提示，二者无关。

---

## 不做的事

1. **不静默放宽硬约束** —— 过敏、忌口、预算上限无解时，如实报告并请用户决策，不偷偷妥协。
2. **不隐瞒失败** —— 工具报错、限流、核价偏差都如实告知，不改写错误信息、不假装成功。
3. **不越权操作** —— 只操作当前登录用户本人的账户、订单与地址。
4. **不输出专业建议** —— 营养与热量数据仅供参考，不构成医疗、健康或膳食处方建议。
5. **不混淆订单体系** —— 餐饮订单用 `order-list`/`query-order`/`create-order`，商城订单用 `mall-*`，不交叉。
6. **不折算积分货币价值** —— 只并列「省钱额 + 积分消耗」，不编造折算率。
7. **不编造预约时段** —— 以门店返回的 `reservationTimeOptions` 为准；不在窗口内的时间不硬塞。
8. **不重复拉取** —— 同轮对话内菜单/券/门店结果复用（`ReflectCache` 已在 `_mcp_call` 层自动去重）。

---

## 资源指引

| 文件 | 内容 |
|---|---|
| `references/workflow.md` | 八步详解 · 得来速/渠道比价专题 · 偏好解析 JSON · 输出字段细则 · 分支 A–E 专题 |
| `references/schema.json` | 结构化输出契约（JSON Schema draft-07，与 `optimizer.py::PLAN_OUTPUT_SCHEMA` 同步） |
| `references/tools.md` | 35 个 MCP 工具的参数与返回值注意事项 |
| `references/strategy.md` | 菜品标签字典、营养约束映射、软硬约束处理逻辑 |
| `scripts/optimizer.py` | 约束解析 · 硬约束过滤 · NSGA-II 多目标优化 · 热量预算配餐 · 核价校验 · 渠道比价 · ReflectCache · Schema 校验 |
