# 真实核价记录（方向2 · pricing verification）

> **结论先行**：2026-10-10 上午营业时段，README 演示场景在真实门店端到端跑通。
> 最省钱方案「大脆鸡扒麦满分 ×4」**本地估算 ¥44.00 = 官方核价 ¥44.00，偏差 0.00%**。
> 本页所有数字固化自真实 MCP 调用快照（可由 `python scripts/verify_pricing.py` 一键重放），
> 无任何手编数据。脱敏约定与 `MCP_INTEGRATION.md` 一致：门店编号记为 `195****`，
> traceId 仅保留首尾 6 位，Token / 手机号 / 地址不出现在本文档中。

---

## 1. 营业时段检查（第一步）

| 项 | 值 |
|---|---|
| 工具 | `now-time-info`（无入参） |
| 服务器时间 | **2026-10-10 09:27:49**（星期六，GMT+8，UTC 01:27:49） |
| traceId | `4d5465…43742a` |
| 判定 | 09:27 ∈ [07:00, 22:00] → **营业时段，继续真实核价** |

## 2. 真实调用链路（第二步，5 次调用全部成功）

### 2.1 `query-nearby-stores` → 选定门店

- **入参**：`{ "beType": 1, "searchType": 2, "city": "北京市", "keyword": "国贸" }`
- **返回**（脱敏关键项）：5 家门店，全部 `businessStatus: true`，营业时间 07:00–22:00；
  选定距离最近（269m）的 **195**** 麦当劳北京建外SOHO(A座)餐厅**，`reservation: true`
  （当天可预约时段：早餐 07:14–10:15 / 午餐 10:44–14:15 / 下午茶 14:44–16:45 / 夜市 17:14–21:45）。
- **traceId**：`cca721…1bf27`，时间 09:28:19

### 2.2 `query-meals` → 真实菜单

- **入参**：`{ "storeCode": "195****", "orderType": 1, "beType": 1 }`（到店自取不传 beCode）
- **返回**：**早餐时段菜单**（09:28 处于早餐档 07:14–10:15），两级结构
  （`categories` 分类 + `meals` 商品映射），共 **96 个商品**；
  价格为字符串元（如 `"11"`、`"7.2"`），部分套餐带 `discountType`
  （`促销优惠` / `随单购早餐卡优惠`，后者需随单购卡才能享受 `currentPrice`）。
- **traceId**：`8fcee7…53eac5`，时间 09:28:29
- ⚠️ 真实时段差异：早餐菜单品类（麦满分/厚松饼/营养卷）与 README 正餐演示场景
  （汉堡/薯条）不同，本文如实按早餐菜单计算，不做替换、不编造正餐数据。

### 2.3 `query-store-coupons` → 门店券池

- **入参**：`{ "storeCode": "195****", "orderType": 1, "beType": 1 }`
- **返回**：`data: []` —— **该门店到店自取当前无可用券**（接口正常，券池为空）。
  因此本次最省钱方案不含券（券组合 DP 自然退化为纯最小成本组合）。
- **traceId**：`66a17d…b6c3f9`，时间 09:28:58

### 2.4 `list-nutrition-foods` → 营养匹配

- **入参**：无
- **返回**：160 个餐品营养数据（`productName, energyKcal, protein, fat, carbohydrate, sodium, calcium`）
- **traceId**：`8991e9…1e85e3`，时间 09:29:45
- **匹配规则**（`query-meals` 不返回营养字段）：名称归一化
  （去空格 / `™` / `【】` / 中英文括号及其内容 / 引号）后**精确匹配**；
  命中 23 项（如 猪柳蛋麦满分 387 kcal / 23 g 蛋白）；未命中的
  （套餐组合、三件套、毛绒周边、规格未标注的饮品甜品）**营养按 0 处理，不估算**。

### 2.5 optimizer.py 本地计算 → 最省钱方案

- **场景**：`我们4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜`
  （README 5.1 演示场景；解析：4 人 / 总额 ¥100 / 排除 `spicy` / 标准减脂档 / 四画像）
- **计价口径**：`discountType` 为空或「促销优惠」→ 按 `currentPrice`；
  含「随单购」→ 按 `originalPrice`（不购买早餐卡时的真实价格）
- **硬约束过滤**：96 项全部保留（早餐菜单无辣味、无单品超限）
- **Top-1（最省钱）**：

| 商品编码 | 名称 | 数量 | 小计 |
|---|---|---|---|
| 5889 | 大脆鸡扒麦满分 | 4 | ¥44.00 |
| | **合计（本地估算）** | | **¥44.00** |

  - 主食 4 份（每人至少 1 份 ✅）｜人均 ¥11.00（预算 ¥25/人 ✅）
  - 总热量 1444 kcal（人均 361 kcal，落在 [350, 600] 目标区间 ✅）
  - 求解方式：动态规划（无界背包，状态 = 份数 × 热量桶，区间 [1400, 2400] kcal）
  - 备选 Top-2/Top-3：¥47.50（火腿扒麦满分 + 营养卷 + 大脆鸡扒×2）、¥51.00（火腿扒×4 + 大脆鸡扒×1）

### 2.6 `calculate-price` → 官方核价

- **入参**（由 `verify_price` 契约构造）：

```json
{
  "storeCode": "195****",
  "orderType": 1,
  "beType": 1,
  "items": [ { "productCode": "5889", "quantity": 4 } ]
}
```

- **返回关键字段**（单位：分；到店自取无运费/打包费字段）：

```json
{
  "productOriginalPrice": 4400,
  "productPrice": 4400,
  "originalPrice": 4400,
  "discount": 0,
  "price": 4400,
  "productList": [
    { "productCode": "5889", "productName": "大脆鸡扒麦满分",
      "quantity": 4, "originalSubtotal": 4400, "subtotal": 4400 }
  ],
  "takeWayList": [
    { "code": "eat-in", "title": "堂食", "subtitle": "店内用餐" },
    { "code": "take-in-store", "title": "外带", "subtitle": "店内自提" }
  ]
}
```

- **traceId**：`5b8f62…65116c`，时间 09:33:53

## 3. 本地计算价 vs 官方核价对比（第三步）

| 口径 | 金额 | 说明 |
|---|---|---|
| 本地估算（optimizer.py） | **¥44.00** | DP 最小成本：大脆鸡扒麦满分 ¥11.00 × 4 |
| 官方核价（calculate-price） | **¥44.00** | `price` 4400 分；`discount` 0（无券） |
| **偏差** | **¥0.00（0.00%）** | **一致 ✅**（≤5% 阈值，无需以官方价覆盖） |

- 恒等式校验（渠道比价口径）：商品 4400 + 运费 0 + 打包 0 + 餐具 0 − 优惠 0 = 现价 4400 ✅
- 与 `MCP_INTEGRATION.md` 偏差 4（价格单位为「分」）一致：4400 分 ÷ 100 = ¥44.00。
- 与 2026-10-10 08:21 得来速实测（¥47.00）同口径可比：不同篮子不同价，单位换算规则一致。

## 4. 可复现方式

```bash
# 一键重放（纯标准库，不联网：固化快照 + optimizer 完整链路）
python scripts/verify_pricing.py          # 控制台输出：Top-3 + 核价请求 + 本地 vs 官方对比
python scripts/verify_pricing.py --html   # 另生成 docs/pricing-verification.html 对比页
```

- 实测快照（菜单 / 营养匹配 / 券池 / 核价响应）全部固化在
  `scripts/verify_pricing.py` 顶部，来源与 traceId 一一标注；
- 联网重跑真实调用需在营业时段（07:00–22:00）进行，Token 获取方式见
  `MCP_INTEGRATION.md` 第 4 节。

## 5. 截图素材清单

| 素材 | 位置 |
|---|---|
| 对比页面（浏览器渲染，含调用链路/请求/响应/对比） | `docs/pricing-verification.html` |
| 本文档（Markdown 记录） | `docs/pricing-verification.md` |
| 可执行复现脚本 | `scripts/verify_pricing.py` |
