# MCP 工具清单（mcd-mcp）

> 来源：`https://mcp.mcd.cn/mcp-servers/mcd-mcp`，`tools/list` 实测抓取。
> 服务端版本：`mcd-mcp v1.0.0`，协议 `2024-11-05`，共 **35 个工具**。
> 抓取时间：2026-10-09。

## 0. 通用传参规则（所有业务工具共用）

**`orderType` 与 `beCode` 的联动规则（最容易出错的地方）**

| 场景 | beType | orderType | beCode |
|---|---|---|---|
| 到店自取 | 1 | 1 | **不传**（传了会报错） |
| 得来速 DT | **5** | 1 | **必传**（来自 `query-nearby-stores`） |
| 麦乐送 | 2 | 2 | **必传**（来自 `delivery-query-stores`） |
| 企业团餐 | **6** | **2** | **必传**（来自 `delivery-query-stores`） |

> ⚠️ **beType 合法枚举仅 `[1, 2, 5, 6]`，不存在 beType=3**（2026-10-10 实测：传 3 被参数校验
> 直接拒绝）。得来速 = **beType=5**，不是 3。

- `reservationDate`：仅预约场景传，格式 `yyyy-MM-dd HH:mm`；非预约不传。
  **四个工具一致支持**：`query-meals` / `calculate-price` / `create-order` / `query-promotions`
  （2026-10-10 schema 实测确认）。预约链路：可预约时段以 `query-nearby-stores` 返回的
  `reservationTimeOptions` 为准；预约菜单/核价/下单三步的 `reservationDate` 必须同一值，
  不可混用实时菜单。
- `storeCode`：门店编码，绝大多数业务工具的必填项。
- 团餐场景（beType=6）额外链路：`delivery-query-stores` → `query-meals` →（可选 `query-promotions` / `query-meal-assistance`）→ `calculate-price` → `create-order`。
- 得来速场景（beType=5）链路：`query-nearby-stores(beType=5)` → `query-meals` / `query-store-coupons` / `calculate-price`（均 orderType=1 + beCode）→ `create-order`（`takeWayCode=take-dt-quick`）。

## 1. 门店与地址

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `query-nearby-stores` | 到店场景查可点餐门店 | `beType`(1/5)、`searchType`(1=收藏,2=位置，默认1)；searchType=2 时 `city`+`keyword` 必填 | beType=1 返回门店**无 beCode**，后续不传；beType=5 返回**有 beCode**，后续必传。`reservation=true` 时需完整展示 `reservationTimeOptions`，`today=true` 标「(今天)」——**预约场景的取餐时段只认这里，不编造** |
| `delivery-query-stores` | 外送场景按地址查可配送门店 | `addressId`、`beType`(2=麦乐送 / 6=团餐) | 返回 `storeCode` + `beCode`，团餐链路起点 |
| `delivery-query-addresses` | 查用户配送地址列表 | 无 | 无地址时引导创建；团餐需再引导用户提供**预算金额与人数**以算人均 |
| `delivery-create-address` | 新建配送地址（写操作） | `city`/`contactName`/`phone`(11位纯数字)/`address`/`addressDetail` 必填，`gender` | 返回 `addressId`，供外送/团餐下单使用 |

## 2. 餐品与价格

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `query-meals` | 查餐品列表（搭单基础） | `storeCode`+`orderType`+`beType`；`beCode` 按上表；`reservationDate` | 返回商品 `productCode`，下单/核价前**必须先查**。描述内含团餐搭单价位规则（见下） |
| `query-meal-detail` | 查餐品详情/套餐组成/特调 | `storeCode`+`orderType`+`beType`+`code` | `data.supportModify=true` → 商品名后标【可特调】；`choice.supportModify=true` 同样标注。**不主动展开特调列表**，用户问才展示；末尾总结 `isDefault=1` 的默认搭配并提示「回复'查看特调'」 |
| `calculate-price` | 计算含优惠价格 | `storeCode`+`orderType`+`beType`；`items[]`(productCode/quantity/couponId/couponCode/modification)、`gmServiceCode`(团餐助餐码)、`withOrder`、`needTableware` | **⚠️ 返回价格单位是「分」，展示前必须 ÷100 转元**。返回**到手成本全结构**（2026-10-10 实测新增）：`productPrice`(商品)/`deliveryPrice`(运费)/`packingPrice`(打包费)/`tablewarePrice`(餐具费)/`discount`(优惠)/`price`(现价)，恒等式 `商品+运费+打包+餐具−优惠=现价`。`takeWayList[].code` 实测值：`eat-in`(堂食)/`locker-in`(外带)/`take-dt-quick`(得来速)。`enjoyed`/`enjoyable` 含 `realDiscount`(实际优惠值)与 `balance`(距下一档优惠门槛差额，分——凑单提示用) |
| `list-nutrition-foods` | 查餐品营养成分 | 无 | 含能量/蛋白质/脂肪/碳水/钠/钙 —— 减脂与健康评分的**数据源** |

**团餐搭单价位规则**（来自 `query-meals` 描述，用于生成方案时参考）
- < 20 元：小食
- 20–30 元：汉堡+小食 或 汉堡+饮料
- 30–40 元：汉堡+薯条/小食+饮料
- 40–50 元：汉堡+薯条+小食+饮料
- > 50 元：丰富组合，优先不重复小食

## 3. 优惠券与促销 ★ 重点区分

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `available-coupons` | 查**账户当前可领取**的券 | 无 | 只是「可领」，**不等于门店可用** |
| `auto-bind-coupons` | **一键领券**（写操作） | 无 | 自动领取所有可领券，返回 `successCount`/`failedCount` 及明细 |
| `query-my-coupons` | 查**用户卡包**里的券资产 | `page`(默认1，最多5)、`pageSize`(默认200，最大200) | **不做门店/渠道/配送校验，不承诺可用于当前订单** |
| `query-store-coupons` | 查**【指定门店+订单类型】真实可用券** ★ | `storeCode`(必须全数字)+`orderType`+`beType`；`beCode` 按上表 | **这才是下单可用券的权威来源**。算券组合必须用它，不能用 `available-coupons`/`query-my-coupons` 代替 |
| `query-promotions` | 查门店促销规则（满减/满折） | `storeCode`+`beCode`+`orderType=2`+`beType=6`；预约传 `reservationDate` | **仅企业团餐(beType=6)可用**。返回规则原始数据：`promotionType` 31=满额减 / 33=订单折扣；`ruleCategory` 30=满折、40=满减；`products.type` 1=包含/2=不包含/3=全部。**工具不做最优推荐，需模型自行解读计算** |
| `query-survey-coupon` | 按订单号查 CSAT 满意度答卷与关联奖券 | `orderId`（用户提供，勿自行生成） | 仅可查本人订单；未返回的原始答卷/会员号/couponId 等**不要猜测**。**下单后闭环**（扩展分支 E）：支付完成后主动提示「完成本单满意度评价可领奖券」；用户询问答卷/奖券/核销状态时用本工具查询，返回字段**原样转述** |

## 4. 下单与订单（写操作，必须用户确认）

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `create-order` | **创建餐品订单**（写操作）★ | `storeCode`+`orderType`+`beType`；`orderType=2` 必传 `addressId`；`orderType=1` 必传 `takeWayCode`（来自 calculate-price 的 takeWayList）；团餐(beType=6)必传 `gmServiceCode`（来自 query-meal-assistance）；`items[]`、`remark`(≤50字，仅外送)、`needTableware`、**`reservationDate`(预约场景必传，yyyy-MM-dd HH:mm)** | 返回支付链接；需引导用户扫码/APP 支付，之后用 `query-order` 查状态 |
| `cancel-order` | 取消订单（写操作） | `orderId`+`cancelReasonCode`(1=改主意/2=重复/3=点错/4=地址电话错/5=时间错/-1=其它) | 成功后提示「订单已取消」 |
| `query-order` | 查订单详情/状态 | `orderId` | 用户说「已支付/支付失败」时用它刷新状态 |
| `order-list` | 查**快餐历史订单** | 无 | 商城订单要用 `mall-order-list`，勿混用 |
| `now-time-info` | 获取服务器当前时间 | 无 | 返回毫秒时间戳/格式化时间/时区；用于确定「今天」及活动日历查询 |

## 5. 麦麦商城 · 积分与抽奖

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `query-my-account` | 查积分账户详情 | 无 | 字段均为**字符串**：`availablePoint`(可用)/`accumulativePoint`(累计)/`usedPoint`(已用)/`currentMouthExpirePoint`(本月将过期)/`nextMouthExpirePoint`(下月将过期)/`expiredPoint`(已过期)/`frozenPoint`(冻结)。**本月将过期 > 0 时必须提醒用户优先消耗**。实测锚点(2026-10-10)：可用 73.9 麦享会积分 |
| `mall-points-products` | 查可兑换商品/券列表 | `catRuleIds`（多选逗号分隔） | 类目规则：1>4 商品券、2 实物、2>8 周边、2>9 实物礼品卡、1>6>20 生日派对、1>6>21 主题派对、1>6>22 麦麦体验营、1>6>25 品鉴会、1>6>34 读书会、1>6>40 积分兑换活动。**兑换前必须核对**：`status` 枚举（1 仓库中/2 上架/**3 售罄/4 下架/5 预热**，仅 2 可兑）；`upTime`/`downTime` 兑换窗口（窗口外跳过并说明）；`point`(所需积分)与 `price`(还需支付的现金，元字符串，多为 0)均为**字符串**。**兑换比价**：`query-meals` 查同款现金单价 → 对比「券后到手价 vs 现金价」，输出省 ¥X + 需 Y 积分；MCP 无积分↔现金汇率，**不折算积分货币价值**。实测锚点(2026-10-10)：商品券在架 24 款，50–800 积分档 |
| `mall-product-detail` | 查兑换商品详情与 SKU | `spuId` | 返回所有 SKU 规格信息，用户选择具体 SKU 后才能下单兑换。**前提**：spuId 必须来自 `mall-points-products` |
| `mall-create-order` | 创建**积分兑换订单**（写操作） | `skuId`+`spuCategory`(1虚拟/2实物)；spuCategory=2 必传 `addressId`；`count` | 仅 `shopId=2`。**必须真实调用下单，禁止直接编造「下单成功」**。多商品必须**分别下单且串行排队**。不适用派对/活动类 → 用 `party-order-create` |
| `mall-order-list` | 查商城订单列表 | `lastId`、`size`(默认10，>10 按10) | — |
| `mall-order-detail` | 查商城订单详情 | `orderId` | 引导去麦当劳 APP 看凭证 |
| `query-lottery-info` | 查积分抽奖活动信息（不抽） | 无 | 必须看 `drawDecision`：`resourceEligible` 判资格、`nextConsumption` 是本次实际消耗。**严禁**用 `availableTimes=0/null` 自行推断能否抽。**方案输出/下单后追加提示**（只查不抽）：`activityStatusText`「进行中」才提示「点餐后可参与抽奖：{activityName}，单次消耗 {nextConsumption.text}」。实测锚点(2026-10-10)：「周六麦金喜抽奖」进行中，单次 24 积分（POINTS），奖品含积分霸王餐/麦金卡7天/麦辣鸡腿堡5折券等 |
| `draw-lottery` | **执行抽奖**（写操作） | 无 | 必须先展示 `nextConsumption` 并获用户**明确确认**（"试试看"不算）。有频控，短时间重复调用返回「操作太频繁」，不扣积分 |
| `query-my-prizes` | 查抽奖所得奖品 | `pageNum`、`pageSize`(默认10，最大50) | 按中奖时间倒序；含可用/已用/过期等状态 |

## 6. 主题活动（派对 / 体验营 / 品鉴会）

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `campaign-calendar` | 查当月营销活动日历 | `specifiedDate`(yyyy-MM-dd，可选，作为锚点返回前后各一个有活动的日子，共3天) | 含进行中/往期/未来活动与订阅状态 |
| `query-party-city` | 查活动可举办城市 | `spuId` | 返回城市 `code` 与经纬度，供下一步查门店 |
| `query-party-store` | 查城市下支持活动的门店 | `code`+`latitude`+`longitude`+`spuId` | 返回 `storeCode` |
| `query-party-store-date` | 查门店可预约日期 | `storeCode`+`spuId` | 返回 `dateStr` |
| `query-party-store-session` | 查某日场次列表 | `spuId`+`storeCode`+`dateStr` | 返回场次 `id`/`timeStart`/`timeEnd`/`leftNum` |
| `party-order-create` | 创建派对/体验营订单（写操作） | `partyType`(1包场/2拼团) 必填；`spuId`/`skuId`/`code`/`storeCode`/`dateStr`/`id`/`timeStart`/`timeEnd`/`leftNum`/`count` | 仅 `shopId=5`。每一步完成后**必须等用户明确选择**，不得跳步 |

## 7. 企业团餐专题（beType=6）

团餐是本 Skill 的核心场景之一，串起来是：

```
delivery-query-addresses        →  选/建地址
delivery-query-stores(beType=6) →  storeCode + beCode
query-meals(...beType=6...)     →  餐品与 productCode
query-promotions                →  满减/满折规则（可选）
query-meal-assistance           →  助餐服务 gmServiceCode（可选）
query-store-coupons             →  真实可用券
calculate-price                 →  含券价（分→元）
create-order                    →  下单（需用户确认）
```

| 工具名 | 用途 | 关键参数 | 返回值注意点 |
|---|---|---|---|
| `query-meal-assistance` | 查团餐可用助餐服务 | `storeCode`；**beType 固定 6**；orderType=2 | 用户选中后把 `gmServiceCode` 传给 `calculate-price` / `create-order` |

> `query-meal-assistance`、`query-promotions` **仅团餐(beType=6)可用**，其他场景调用会失败。

## 8. 渠道比价（到手成本对比）

**能力边界（2026-10-10 实测确认）**：35 个工具全部为麦当劳官方体系（点餐 / 券 / 商城 / 积分 / 派对），
**不提供美团、饿了么等第三方平台比价接口**。渠道比价只能在内部渠道间进行：

| 渠道 | beType | orderType | beCode 来源 | 到手成本特有项 |
|---|---|---|---|---|
| 到店自取 | 1 | 1 | 无需 beCode | 运费/打包费/餐具费为 0（实测） |
| 麦乐送 | 2 | 2 | `delivery-query-stores`（前置：账户有配送地址） | **运费 + 打包费** |
| 得来速 | 5 | 1 | `query-nearby-stores(beType=5)` | 运费/打包费/餐具费为 0（实测） |

**实现**：`scripts/optimizer.py::compare_channels(items, store_code, be_codes)` —— 同一篮子
逐渠道调 `calculate-price`，提取 `商品+运费+打包+餐具−优惠=现价` 完整结构，
`render_channel_comparison()` 输出对比表与最低价结论。

**实测锚点（门店 1950936，2026-10-10）**：
- 同一门店 beType=1 与 beType=5 的**菜单、标价、核价完全一致**（篮子核价均为 ¥47.00）；
- 差异在 `takeWayList`：beType=1 返回堂食/外带/得来速三种，beType=5 锁定 `take-dt-quick`；
- 麦乐送核价依赖配送地址（`delivery-query-addresses` 为空则取不到 beCode）→ 如实标「无法核价」。

---

## 9. 异常处理（重试 / 超时 / 降级 / 幂等）

> 实现：`scripts/optimizer.py` 第 4.5 节（`_mcp_call` / `call_with_degrade` /
> `create_order_guarded` / `log_mcp_call`）。所有 MCP 调用方自动获得以下能力。

### 9.1 指数退避重试

| 规则 | 内容 |
|---|---|
| **重试条件** | HTTP 429（限流）、网络超时（TimeoutError）、HTTP 5xx（服务端错误） |
| **不重试条件** | HTTP 401（未授权，提示重新获取 Token）、HTTP 400（参数错误）、业务逻辑错误（`structuredContent.success=false`，如门店打烊 60003/600057）——重试不会有不同结果，只浪费配额 |
| **重试策略** | 最多 3 次，间隔 1s → 2s → 4s；**每次重试前检查总预算 20s**，超预算立即放弃 |
| **禁止** | 并发轰炸式重试；写操作的重试必须串行 |

### 9.2 超时与降级

单次 MCP 调用超时 **10 秒**。超时/网络故障后按工具走降级：

| 工具 | 降级行为 | 用户提示要点 |
|---|---|---|
| `query-meals` / `list-nutrition-foods` | 读本地缓存菜单/营养表 | 标注缓存来源 |
| `query-store-coupons` | 跳过券优化，仅按满减计算 | 价格可能偏高 |
| `query-promotions` | 跳过满减规则 | 不影响下单，只影响凑单建议 |
| `calculate-price` | 标注「价格以实际下单为准」 | 不编造核价结果 |
| `query-nearby-stores` / `delivery-query-*` | 提示用户手动输入门店/地址 | 不猜门店 |
| `create-order` | **不降级**（写操作必须真实成功或如实失败） | —— |

**降级必须显式标注**：输出中附「该结果基于缓存/降级逻辑，可能与实际有偏差」。
业务错误（门店打烊等）**不走降级**——如实返回错误码与文案，不编数据。

### 9.3 下单幂等（防重复下单）

`create-order` 前必须经 `create_order_guarded()`：

1. 幂等键 = `sha256(user_id + store_code + 订单内容哈希 + 5 分钟时间窗口)`；
2. 键命中已有成功订单 → 不调 MCP，直接返回已有订单（防手抖双击/重试风暴）；
3. 下单失败 → 不记录幂等键（允许用户修复后重试）；
4. 记录存 `logs/idempotency.json`（键→订单号+时间戳，>1 天自动清理）；
5. 同内容隔 5 分钟再点 = 新窗口 = 正常复购。

### 9.4 调用日志与脱敏

- 每次 MCP 调用记录 JSON Lines 至 `logs/mcp_calls.log`：
  `{"ts", "tool", "duration_ms", "status"(success|error|retry|degraded), "retries", "args", "detail"}`；
- **脱敏规则**（写入前强制执行）：Token/Authorization → `[TOKEN]`、手机号（11 位）→ `[PHONE]`、
  address/addressDetail/fullAddress → `[ADDRESS]`；
- `logs/` 目录已在 `.gitignore` 中排除，不入库；
- 日志写失败**静默跳过**，绝不阻断业务主流程。

---

## 附：工具总数与命名核对

35 个工具：`auto-bind-coupons`、`query-promotions`、`query-my-prizes`、`query-party-store`、`query-my-coupons`、`query-meals`、`query-meal-detail`、`calculate-price`、`order-list`、`mall-create-order`、`query-party-store-session`、`query-store-coupons`、`create-order`、`party-order-create`、`campaign-calendar`、`now-time-info`、`delivery-create-address`、`query-party-store-date`、`query-lottery-info`、`mall-points-products`、`draw-lottery`、`mall-order-detail`、`available-coupons`、`list-nutrition-foods`、`delivery-query-addresses`、`delivery-query-stores`、`cancel-order`、`query-my-account`、`query-party-city`、`query-order`、`query-nearby-stores`、`query-meal-assistance`、`query-survey-coupon`、`mall-product-detail`、`mall-order-list`
