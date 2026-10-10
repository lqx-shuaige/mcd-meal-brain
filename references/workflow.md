# 详细工作流（workflow.md）

> 本文件承接 SKILL.md「工作流概览」的展开细节，供执行时逐步对照。
> 工具参数速查见 `tools.md`；约束与标签逻辑见 `strategy.md`；
> 结构化输出契约见 `schema.json`（与 `optimizer.py::PLAN_OUTPUT_SCHEMA` 同步）。

---

## 1. 八步工作流详解

### 第 1 步 · 解析需求

抽取：**场景 / 人数 / 预算 / 时间 / 忌口 / 口味 / 健康目标**。
缺关键项（人数、场景）则追问一次；其余能从上下文推断的，标注假设后继续，不反复追问。

**先锁场景，它决定后续所有参数**：

| 场景 | beType | orderType | beCode |
|---|---|---|---|
| 到店自取 | 1 | 1 | 不传（传了会报错） |
| 得来速 DT | **5** | 1 | 必传 |
| 麦乐送 | 2 | 2 | 必传 |
| 企业团餐 | 6 | 2 | 必传 |

> ⚠️ **常见误区（实测纠偏）**：得来速**不是 beType=3**。beType 合法枚举仅 `[1, 2, 5, 6]`，
> 传 3 会被参数校验直接拒绝。得来速的正确编码是 **beType=5**（详见「得来速场景要点」）。

### 第 2 步 · 环境准备

- `now-time-info` 取服务器时间，作为「今天 / 可预约范围」基准。
- 到店自取 → `query-nearby-stores(beType=1)`；**得来速 → `query-nearby-stores(beType=5)`**
  （返回的门店带 `beCode`，后续必传）；外送或团餐 → `delivery-query-addresses`（无地址先引导
  `delivery-create-address`）→ `delivery-query-stores`。
- 锁定 `storeCode`、`beCode`（如适用）、`orderType`。

### 第 3 步 · 菜单与偏好采集

- `query-meals` 拉在售餐品，拿 `productCode`（返回为「分类 + 映射表」两级结构，
  先遍历 `categories[].meals` 取 code，再回查 `meals` 映射拿详情）；
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

`scripts/optimizer.py::optimize_plans()`（默认 **NSGA-II 多目标优化**，批 2 升级）：
- 四目标帕累托前沿（价格↓/人均热量↓/油炸份数↓/人均蛋白↑）演化，
  从第一前沿取「最省钱 / 最健康 / 最快」三个极值点；
- 固定随机种子，结果可复现；原加权评分路径保留（`method="weighted"`）；
- 输出 **Top-3** 方案；结构化输出遵循 `references/schema.json` 契约
  （`optimizer.py::plans_to_doc()` 生成、`validate_plans_doc()` 校验）。

### 第 7 步 · 核价校验

对候选方案调 `calculate-price` 核价（**必须**，优化器算的是估算值）。
用 `verify_price()` 比对「估算价 vs 官方核价」，偏差超 5% 时以官方价为准并如实标注。

### 第 8 步 · 确认下单

展示方案明细 → **等用户明确确认** → `create-order`。
到店场景 `takeWayCode` 取自 `calculate-price` 的 `data.takeWayList[].code`；团餐必传 `gmServiceCode`。
下单后引导支付；用户回复「已支付 / 支付失败」时用 `query-order` 刷新状态。

---

## 2. 得来速（DT）场景要点（2026-10-10 实测）

**结论：得来速真实可用，正确编码是 `beType=5`；`beType=3` 不存在**（传入会被参数校验直接拒绝，
合法枚举仅 `[1, 2, 5, 6]`）。

### 调用链路（全部实测通过）

```
query-nearby-stores(beType=5, searchType=2, city+keyword)
    → 返回得来速门店（storeCode + beCode 必带）
query-meals(storeCode, orderType=1, beType=5, beCode)     → 菜单与 productCode
query-store-coupons(storeCode, orderType=1, beType=5, beCode) → 渠道可用券
calculate-price(storeCode, orderType=1, beType=5, beCode, items) → 核价 + takeWayList
create-order(..., takeWayCode)                             → 下单（需用户确认）
```

### 与到店自取的关键差异（同门店实测对比）

| 维度 | 到店自取（beType=1） | 得来速（beType=5） |
|---|---|---|
| beCode | 不传（传了会报错） | **必传**（来自 `query-nearby-stores(beType=5)`） |
| 菜单与标价 | 与得来速**完全一致**（同门店实测） | 同左 |
| 官方核价 | ¥47.00（实测篮子） | ¥47.00（同一篮子，价格无差异） |
| `takeWayList` | `eat-in`(堂食) / `locker-in`(外带) / `take-dt-quick`(得来速) 三种 | **锁定 `take-dt-quick`（车道自提）** |
| 下单 `takeWayCode` | 从三种取餐方式中选 | 固定取 `take-dt-quick` |

**给用户的建议**：同门店两渠道**到手价格无差异**，按取餐便利性选择即可
（开车 / 赶时间 → 得来速；顺路 / 堂食 → 到店自取，取餐方式更灵活）。

---

## 3. 渠道比价（到手成本）

### 能力边界（先说清楚，不越界）

- ✅ **可对比**：麦当劳 MCP 内部渠道 —— 到店自取（beType=1）/ 麦乐送（beType=2）/ 得来速（beType=5）。
- ❌ **不可对比**：美团、饿了么等第三方平台 —— MCP 的 35 个工具**不提供**任何外部平台
  比价接口。**不预估、不引用记忆中的价格、不编造外部数据**；用户问外部平台价格时如实说明
  该能力不在 MCP 范围内。

### 对比方法：同一篮子 × 各渠道核价

「到手成本」= **商品现价 + 运费 + 打包费 + 餐具费 − 优惠**（`calculate-price` 返回的
`productPrice / deliveryPrice / packingPrice / tablewarePrice / discount / price`，单位均为分）。

1. **同一篮子**（相同 productCode × 数量）在各渠道间对比才公平；
2. 各渠道前置条件：

| 渠道 | 前置条件 | 缺失时 |
|---|---|---|
| 到店自取 | `query-nearby-stores(beType=1)` 拿 storeCode | — |
| 得来速 | `query-nearby-stores(beType=5)` 拿 storeCode + beCode | 无法核价，如实标注 |
| 麦乐送 | 配送地址 → `delivery-query-stores(beType=2)` 拿 storeCode + beCode | 无法核价，如实标注 |

3. 实现：`scripts/optimizer.py::compare_channels()` —— 各渠道分别调 `calculate-price`，
   提取完整成本结构，返回最低价渠道（可并列）；
4. **无法核价的渠道如实标注原因，绝不编造数字**（如「麦乐送：账户无配送地址，无法核价」）；
5. 券池差异：各渠道可用券需**分别**调 `query-store-coupons`（orderType/beType 不同，券池可能不同）；
6. 输出用 `render_channel_comparison()`：对比表 + 最低价结论 + 同价取舍建议 + 凑单提示
   （`enjoyable.balance` = 距下一档优惠的门槛差额）。

### 实测锚点（2026-10-10，门店 1950936）

同一篮子（猪柳蛋麦满分 ×2 + 脆薯饼 ×1）：到店自取 **¥47.00** = 得来速 **¥47.00**
（运费 / 打包费 / 餐具费均为 0）；麦乐送因当前账户无配送地址无法核价。
可运行 `python scripts/optimizer.py --compare-channels` 复现（离线回放实测数据）。

---

## 4. 多人偏好解析

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

## 5. 输出格式与字段细则

每轮给出 **Top-3 方案**。人类可读输出（`render_plans()`）逐字段对照以下结构；
机器可读输出（`plans_to_doc()` → `--json`）必须通过 `references/schema.json` 校验。

| 文档字段（schema.json） | 渲染层含义 |
|---|---|
| `name` | 方案定位：最省钱 / 最健康 / 最快 |
| `items` | 商品明细：名称、数量、单价、小计（**元，2 位小数**） |
| `coupons` | 券组合：券名 + `couponId` + 各券优惠额（须注明出处，不可只写「用了张券」） |
| `original_total` | 原价 |
| `discount_cny` | 总优惠额 |
| `final_total` | 最终价 |
| `per_person` | 人均 |
| `people_count` / `mains` | 人数 / 主食份数（必须 `mains ≥ people_count`） |
| `nutrition` | 营养标签：总/人均 热量、蛋白、脂肪、钠（**必须有数值**，不能只写「较健康」） |
| `nutrition_flags` | 体检结论：营养达标 / 热量超标 / 脂肪超标 / 蛋白不足 |
| `tradeoffs` | 取舍说明：这套方案牺牲了什么、换来了什么 |
| `price_note` | 核价状态（未核价时必须标注「价格为估算，未核价」） |
| `genui` | 展示提示（本 Skill 自定义的渲染建议：卡片标题 / emoji / 标语 / 亮点） |

> 注：`genui` 是**本 Skill 自定义的展示提示字段**，供渲染层生成方案卡片；
> 与 MCP 响应中不存在的 `genui` 字段是两回事（见 `MCP_INTEGRATION.md` 偏差 3）。

最后附**对比表**与**推荐结论**（含取舍理由），不要把选择权完全甩给用户。
未通过官方核价的方案，必须标注「价格为估算，未核价」。

---

## 6. 扩展分支专题

### 6.1 积分商城（积分换好物）

**编排顺序**：`query-my-account`（余额）→ `mall-points-products`（商品）→
`query-meals`（同款现金价，用于比价）→ `mall-product-detail`（SKU）→
用户确认 → `mall-create-order`（写操作）。

**兑换比价算法**（分支 A 第 3 步的展开）：

| 量 | 来源 | 说明 |
|---|---|---|
| 券后到手价 | `mall-points-products` 的 `price` | 兑换时还需支付的现金（元字符串，多为 0） |
| 积分消耗 | `mall-points-products` 的 `point` | 所需积分（字符串） |
| 现金对比价 | `query-meals` 同款商品 `currentPrice` | 不含券直接点餐的价格 |
| 输出 | 上述三项并列 | 「省 ¥X、需 Y 积分」，**不折算积分货币价值**（MCP 无汇率） |

示例（2026-10-10 实测数据）：「18.8 元 2 份麦辣鸡翅」券需 **200 积分**；
「3.9 元圆筒冰淇淋」券需 **100 积分**；「28.8 元巨无霸四件套」券需 **800 积分**。
比价输出形如：「巨无霸四件套：现金点餐 ¥28.8 起，积分券后 ¥28.8 + 800 积分——
该券实际是『积分兑换购买资格』，是否划算取决于你对积分价值的心理定价。」

**边界**：
- `status ≠ 2`（售罄/下架/预热）一律不推荐，如实说明状态；
- 兑换窗口（`upTime`/`downTime`）外的不推荐；
- 积分不足：告知差额，提示点餐攒积分；不为凑积分推荐超预算点单（预算优先级更高）。

### 6.2 预约取餐

**完整链路**：

```
now-time-info                     →  服务器时间基准（确定「今天」/可预约范围）
query-nearby-stores(beType=1)     →  门店 + reservationTimeOptions（可预约时段）
query-meals(..., reservationDate) →  预约时段菜单（必传 yyyy-MM-dd HH:mm）
query-store-coupons(...)          →  券池（与预约场景同参数口径）
calculate-price(..., reservationDate) →  预约口径核价（单位仍为「分」）
create-order(..., reservationDate)    →  预约下单（写操作，需确认）
```

**要点**：
- `reservationDate` 格式 `yyyy-MM-dd HH:mm`，仅预约场景传（`query-meals` /
  `calculate-price` / `create-order` / `query-promotions` 四个工具一致）；
- 可预约时段**只认门店返回**的 `reservationTimeOptions`，`today=true` 标「(今天)」；
- 预约菜单与实时菜单**不可混用**——预约时段可能缺货或价格不同；
- 方案输出时「预约取餐」与「立即取餐」并排展示，预约差异（缺货/价格变动）如实标注。

### 6.3 按热量预算配餐

**算法**（`optimizer.py::optimize_calorie_budget()`）：
1. 解析「每人 X 大卡/千卡/kcal」→ 人均热量硬上限（tier=custom）；
2. 菜单按 **主餐（main）/ 小食（非主非饮）/ 饮品（drink）** 三类划分（标签来自
   `infer_tags`，与 strategy.md 字典同步）；
3. 枚举每人组合「1 主餐 + 0–1 小食 + 0–1 饮品」，仅保留 **组合热量 ≤ 人均预算** 的；
4. Top-3：**最省**（预算内成本最低）→ **高蛋白**（蛋白质最高）→ **均衡混搭**
   （多种组合分配到人，多样性最好）；
5. 每套方案标注「人均 kcal / 预算 kcal，余量」；
6. 无解（最便宜主餐也超预算）：不静默放宽，输出最低热量主餐 + 差距说明 + 调整建议。

**输出仍走 schema.json 契约**（`plans_to_doc()`），模式名为「热量预算配餐」。

### 6.4 活动抽奖与满意度评价领券

**抽奖提示**（方案/下单后追加，只查不抽）：
- `query-lottery-info` → `activityStatusText`「进行中」才提示；
- 提示模板：「🎰 点餐后可参与抽奖：{activityName}，单次消耗 {drawDecision.nextConsumption.text}，
  有机会抽 {prizes 挑 2–3 个代表奖品}」；
- 抽奖资格与消耗一律以 `drawDecision` 为准；未经明确确认不调 `draw-lottery`。

**满意度评价领券**（下单后闭环）：
- 支付完成后提示：「💬 完成本单满意度评价可领奖券」；
- 查询：`query-survey-coupon(orderId)`——orderId 仅来自用户本人或上游订单查询；
- 满意度描述 / 奖券标题 / 核销时间 / 核销状态 / 点餐方式**原样转述**，未返回字段不猜测。
