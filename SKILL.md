---
name: mcd-meal-brain
description: 面向多人场景的麦当劳智能点餐决策引擎。当用户需要多人拼单、优化点餐组合、在预算/口味/健康/活动之间做取舍时使用。
---

# 麦麦拼单大脑

把「一群人吃什么、怎么点最划算」算成可执行方案。它不替用户下单，而是**算清楚、讲明白、等确认**。

---

## 触发条件

- 用户提到「多人」「拼单」「一起点」「宿舍」「同事」等多人点餐场景
- 用户提出预算限制、口味偏好、饮食禁忌、减脂等约束
- 用户询问「怎么点最省」「有什么活动」「怎么凑满减」

**不触发**：单人单品简单点餐；纯查询（「我有什么券」「订单到哪了」）；用户直接指定了订单内容。

---

## 工作流概览（8 步）

### 第 1 步 · 解析需求
抽取：**场景 / 人数 / 预算 / 时间 / 忌口 / 口味 / 健康目标**。
缺关键项（人数、场景）则追问一次；其余能从上下文推断的，标注假设后继续，不反复追问。

**先锁场景，它决定后续所有参数**：

| 场景 | beType | orderType | beCode |
|---|---|---|---|
| 到店自取 | 1 | 1 | 不传（传了会报错） |
| 得来速 DT | 5 | 1 | 必传 |
| 麦乐送 | 2 | 2 | 必传 |
| 企业团餐 | 6 | 2 | 必传 |

### 第 2 步 · 环境准备
- `now-time-info` 取服务器时间，作为「今天 / 可预约范围」基准。
- 到店 → `query-nearby-stores`；外送或团餐 → `delivery-query-addresses`（无地址先引导 `delivery-create-address`）→ `delivery-query-stores`。
- 锁定 `storeCode`、`beCode`（如适用）、`orderType`。

### 第 3 步 · 菜单与偏好采集
- `query-meals` 拉在售餐品，拿 `productCode`；
- 需要营养成分时用 `list-nutrition-foods`；
- 把每位参与者转成画像 JSON（见「多人偏好解析」）。

### 第 4 步 · 硬约束过滤
`scripts/optimizer.py::filter_menu()` —— 先剔除所有违反硬约束的商品，把解空间压下来再做优化。
被剔除项要能回答「为什么没有它」，便于向用户解释。

### 第 5 步 · 券与促销盘点
- `query-store-coupons`（**权威**：该门店 + 该订单类型真实可用的券）
- `query-promotions`（仅团餐 beType=6：满减 / 满折）
- `query-meal-assistance`（仅团餐：助餐服务 `gmServiceCode`）
- 卡包为空时，可**征得用户同意后**再调 `auto-bind-coupons`。

### 第 6 步 · 组合优化
`scripts/optimizer.py::optimize_plans()`：
- 最省钱 → 动态规划（无界背包 + 券组合 DP）+ 帕累托剪枝
- 最健康 / 最快 → 加权评分
- 输出 **Top-3** 方案。

### 第 7 步 · 核价校验
对候选方案调 `calculate-price` 核价（**必须**，优化器算的是估算值）。
用 `verify_price()` 比对「估算价 vs 官方核价」，偏差超 5% 时以官方价为准并如实标注。

### 第 8 步 · 确认下单
展示方案明细 → **等用户明确确认** → `create-order`。
到店场景 `takeWayCode` 取自 `calculate-price` 的 `data.takeWayList[].code`；团餐必传 `gmServiceCode`。
下单后引导支付；用户回复「已支付 / 支付失败」时用 `query-order` 刷新状态。

---

## 核心规则

### 规则 1 · 参数来源强制约束
**所有入参只能来自上游真实 MCP 返回，禁止推断、推测、硬编码。**

| 参数 | 唯一合法来源 |
|---|---|
| `storeCode` / `beCode` | `query-nearby-stores` 或 `delivery-query-stores` |
| `productCode` | `query-meals` |
| `couponId` / `couponCode` | `query-store-coupons` |
| `takeWayCode` | `calculate-price` 的 `data.takeWayList[].code` |
| `gmServiceCode` | `query-meal-assistance` |
| `addressId` | `delivery-query-addresses` / `delivery-create-address` |
| `spuId` / `skuId` | `mall-points-products` 或 `mall-product-detail` |
| `dateStr` / 场次 `id` | `query-party-store-date` / `query-party-store-session` |
| `orderId` | 用户提供，或上游订单查询返回 |

拿不到就回到对应步骤重新获取，**不许编**。

### 规则 2 · 价格单位
- `calculate-price` 返回的价格字段单位是「**分**」，展示前**必须 ÷100 转「元」**。
- `query-meals` / `create-order` 返回的价格是**「元」字符串**（如 `currentPrice`、`originalPrice`，实测 `type=string`），直接展示，**不要再除 100**。
- 两个来源对同一商品可能不一致 —— **一切以 `calculate-price` 核价为准**。

> 内部计算统一用「分」（避免浮点误差），只在最终展示层转「元」。

### 规则 3 · 券来源区分
| 工具 | 语义 | 能否用于算方案 |
|---|---|---|
| `available-coupons` | 账户**当前可领取**的券 | ❌ 只是「可领」 |
| `query-my-coupons` | 用户**卡包**里的券资产 | ❌ 不做门店/渠道校验，不承诺可用 |
| `query-store-coupons` | **指定门店 + 订单类型真实可用** | ✅ **唯一权威来源** |

任何「用券后的最终价」必须以 `query-store-coupons` 为依据；
`available-coupons` 只能用于提示用户「可以去领券」。

### 规则 4 · 写操作必须确认
以下工具会改变账户/订单状态，**一律先展示变更内容，再等用户明确确认**：

`auto-bind-coupons`、`delivery-create-address`、`create-order`、`cancel-order`、`mall-create-order`、`party-order-create`、`draw-lottery`

- 下单类：**禁止**未真实调用工具就输出「下单成功」；必须真实调用后回传结果。
- 抽奖类：必须先展示 `drawDecision.nextConsumption`，「试试看」**不算**确认。
- 多件兑换：必须**分别下单且串行排队**，前单成功后再下新单。

### 规则 5 · 限流与鉴权异常处理
- **429 / 「操作太频繁」**：优先读本地缓存；缓存未命中则友好提示「请求太频繁，请稍后再试」，不连续重试。
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

> 注：骨架中提到的 `genui` 字段，在实测的 35 个工具 schema 与真实响应中**均未出现**
> （检索命中 0 次），故本规则按「结构化字段 vs 展示文本」实施。

---

## 多人偏好解析

把每个人的自然语言拆成 **硬约束（hard）** 与 **软偏好（soft）**：
「不吃 / 不能 / 过敏 / 忌 / 必须」→ 硬；「喜欢 / 最好 / 想 / 尽量」→ 软；「随便 / 都行」→ 不设约束。

```json
{
  "meal_context": {
    "scene": "企业团餐",
    "beType": 6,
    "orderType": 2,
    "people_count": 4,
    "budget": { "type": "total", "amount_cny": 160 },
    "per_capita_budget_cny": 40,
    "time": { "type": "immediate" }
  },
  "participants": [
    {
      "id": "p1",
      "name": "小李",
      "persona": "减脂",
      "hard": [],
      "soft": { "prefer": ["高蛋白", "低热量"], "avoid": ["油炸"] },
      "targets": { "max_calories_kcal": 500, "max_fat_g": 20, "min_protein_g": 25 },
      "weight": { "healthy": 0.7, "cheap": 0.2, "speed": 0.1 }
    },
    {
      "id": "p2",
      "name": "小王",
      "persona": "忌口",
      "hard": ["不吃牛肉", "不吃辣", "花生过敏"],
      "soft": { "prefer": ["鸡肉", "经典汉堡"], "avoid": ["辣"] },
      "targets": {},
      "weight": { "healthy": 0.2, "cheap": 0.5, "speed": 0.3 }
    },
    {
      "id": "p3",
      "name": "小张",
      "persona": "要赠品",
      "hard": [],
      "soft": { "prefer": ["凑满减", "拿赠品", "随单购"] },
      "targets": { "want_gift": true, "min_saving_cny": 5 },
      "weight": { "cheap": 0.8, "healthy": 0.1, "speed": 0.1 }
    },
    {
      "id": "p4",
      "name": "小陈",
      "persona": "预算控",
      "hard": ["人均不超过 35", "不点超过 30 元的单品"],
      "soft": { "prefer": ["性价比", "能吃饱"] },
      "targets": { "max_per_capita_cny": 35, "max_single_item_cny": 30 },
      "weight": { "cheap": 0.9, "healthy": 0.05, "speed": 0.05 }
    }
  ],
  "aggregated": {
    "hard_union": ["不吃牛肉", "不吃辣", "花生过敏"],
    "budget_ceiling_cny": 140,
    "must_have": [],
    "conflicts": ["减脂(低热量) 与 要赠品(凑满减) 存在张力：凑满减可能超热量"]
  }
}
```

**四种画像要点**

| 画像 | 硬约束 | 关键 targets | 主要权重 |
|---|---|---|---|
| 减脂 | 通常无 | `max_calories_kcal` / `max_fat_g` / `min_protein_g` | healthy 高 |
| 忌口 | **成分黑名单**（过敏项必须进硬约束） | —— | 视情况 |
| 要赠品 | 通常无 | `want_gift` / `min_saving_cny` | cheap 高 |
| 预算控 | **预算上限**（人均 + 单品双限） | `max_per_capita_cny` / `max_single_item_cny` | cheap 极高 |

**聚合规则**：硬约束取**并集**；预算取**最严者**；冲突不静默妥协，必须在 `conflicts` 中显式列出并给出取舍建议。

**冲突优先级**（高优先不可被低优先推翻）：
`安全(过敏) > 信仰/忌口 > 预算上限 > 人数 > 营养目标 > 口味偏好 > 凑券/赠品`

---

## 输出格式

每轮给出 **Top-3 方案**，每套方案**必须包含**以下字段：

| 字段 | 说明 |
|---|---|
| `label` | 方案定位：最省钱 / 最健康 / 最快 |
| `items` | 商品明细：名称、数量、单价、小计（**元，2 位小数**） |
| `coupons` | 券组合：券名 + `couponId` + 各券优惠额（须注明出处，不可只写「用了张券」） |
| `subtotal_cny` | 原价 |
| `discount_cny` | 总优惠额 |
| `final_price_cny` | 最终价 |
| `per_capita_cny` | 人均 |
| `people_count` | 人数 |
| `nutrition` | 营养标签：总/人均 热量、蛋白、脂肪、钠（**必须有数值**，不能只写「较健康」） |
| `nutrition_flags` | 体检结论：营养达标 / 热量超标 / 脂肪超标 / 蛋白不足 |
| `tradeoffs` | 取舍说明：这套方案牺牲了什么、换来了什么 |

最后附**对比表**与**推荐结论**（含取舍理由），不要把选择权完全甩给用户。

未通过官方核价的方案，必须标注「价格为估算，未核价」。

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
10. **不重复拉取** —— 同一轮对话内菜单、券、门店结果复用，不反复调同一接口。

---

## 参考文件

- `references/tools.md` —— 35 个 MCP 工具的参数与返回值注意事项
- `references/strategy.md` —— 菜品标签字典、营养约束映射、软硬约束处理逻辑
- `scripts/optimizer.py` —— 约束解析、硬约束过滤、组合优化、核价校验
