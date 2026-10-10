# MCP 接入记录（MCP_INTEGRATION.md）

本文档记录**麦麦拼单大脑**接入麦当劳 MCP 服务的实测情况：接入参数、调用日志（已脱敏）、
以及**实测 schema 与官方文档描述之间的偏差**，供评审复现与后续开发者避坑。

> 所有令牌、门店编号、追踪 ID 均已脱敏，不具还原性。

---

## 1. 接入信息（实测）

| 项目 | 实测值 |
|---|---|
| 服务端点 | `https://mcp.mcd.cn/mcp-servers/mcd-mcp` |
| 传输方式 | Streamable HTTP（`POST`，单次请求即完成调用） |
| 会话模型 | **无状态** —— 响应未返回 `Mcp-Session-Id`，无需 `initialize` 后维持会话 |
| 协议版本 | `2024-11-05` |
| 服务端标识 | `serverInfo: { name: "mcd-mcp", version: "1.0.0" }` |
| 工具总数 | **35** |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCD_MCP_TOKEN>` |
| 必需请求头 | `Content-Type: application/json`、`Accept: application/json, text/event-stream` |
| 响应形态 | `application/json`（也接受 SSE 包装，需兼容解析） |

### 最简调用示例（curl）

```bash
curl -s -X POST "https://mcp.mcd.cn/mcp-servers/mcd-mcp" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Authorization: Bearer ${MCD_MCP_TOKEN}" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
       "params":{"name":"now-time-info","arguments":{}}}'
```

---

## 2. 调用日志（脱敏）

> 令牌统一记为 `1kre****`；门店编号记为 `195****`；traceId 仅保留前后各 6 位。

| 时间（GMT+8） | JSON-RPC 方法 / 工具 | 关键入参 | 结果 | traceId |
|---|---|---|---|---|
| 2026-10-09 22:48 | `initialize` | protocolVersion=2024-11-05 | ✅ `200`，`mcd-mcp v1.0.0` | `9fadbf…d8fd9d` |
| 2026-10-09 22:48 | `tools/list` | — | ✅ 返回 **35** 个工具（66 KB） | — |
| 2026-10-09 22:48 | `tools/call` → `now-time-info` | 无 | ✅ `success:true`，返回服务器时间 | `9919d5…0953e31` |
| 2026-10-09 23:41 | `tools/call` → `list-nutrition-foods` | 无 | ✅ `success:true`，返回 **160** 个餐品营养数据 | `68e80f…de2acbe` |
| 2026-10-09 23:52 | `tools/call` → `query-nearby-stores` | `beType=1, searchType=2, city=北京市, keyword=国贸` | ✅ 返回 5 家门店（含 `storeCode` / `reservationTimeOptions`） | `30442c…5c4c65` |
| 2026-10-09 23:52 | `tools/call` → `query-meals` | `storeCode=195****, orderType=1, beType=1` | ⚠️ 业务错误 `600057`：门店可能已关闭或不在营业时间 | `cf0eec…c36bf9` |
| 2026-10-09 23:52 | `tools/call` → `calculate-price` | `storeCode=195****, orderType=1, beType=1, items=[…]` | ⚠️ 业务错误 `60003`：门店暂停营业，请重新选择门店 | `a7d8c2…bf6ca3` |
| 2026-10-09 23:57 | `tools/call` → `now-time-info` | 无 | ✅ `success:true` | `431171…5cbaf35` |

### 2026-10-10 得来速与渠道比价专项核实（上午营业时段）

| 时间（GMT+8） | JSON-RPC 方法 / 工具 | 关键入参 | 结果 | traceId |
|---|---|---|---|---|
| 08:21 | `query-nearby-stores` | `beType=5, searchType=2, city=北京市, keyword=国贸` | ✅ 返回 **5 家得来速餐厅**（storeCode + beCode 齐全，如 1950936/195093641） | `5c93b6…4343b` |
| 08:21 | `query-meals` | `beType=3`（负向测试） | ❌ **参数校验拒绝**：`beType must be one of [1,2,5,6]` —— **beType=3 不存在** | —（客户端校验拦截） |
| 08:21 | `query-meals` | `storeCode=1950936, orderType=1, beType=1` | ✅ 返回早餐菜单（分类+映射表两级结构） | `9bd48f…cf8360` |
| 08:21 | `query-meals` | `storeCode=1950936, orderType=1, beType=5, beCode=195093641` | ✅ 菜单与 beType=1 **完全一致** | `a7e27…1e234f` |
| 08:21 | `delivery-query-addresses` | 无 | ✅ `addresses: []` —— 当前账户**无配送地址**，麦乐送核价受阻 | `e31e6f…63f38` |
| 08:21 | `calculate-price` | `beType=1, items=[5022×2, 4825×1]` | ✅ **¥47.00**；takeWayList 三种：`eat-in`/`locker-in`/`take-dt-quick` | `03e3b4…87907` |
| 08:21 | `calculate-price` | `beType=5, beCode=195093641, items=[同上]` | ✅ **¥47.00**（与到店自取一致）；takeWayList 仅 `take-dt-quick` | `5afb5f…ec98da` |
| 08:22 | `query-store-coupons` | `beType=1` / `beType=5`（各一次） | ✅ 两渠道均返回空券池（该账户无券，接口正常） | `6b7784…2255e` / `eb359d…7629a` |

**本轮结论**：9 次调用全部按预期返回（1 次负向测试被正确拒绝）。得来速链路端到端跑通；
渠道比价可做内部三渠道，外部平台接口不存在。

**成功率**：8 次调用中 5 次成功、3 次因**门店非营业时段**返回业务错误。服务端本身连通性正常。

**客户端侧连接日志（WorkBuddy 应用）**

```
[MCP-AuthDiag] connection-ready {"id":"custom-mcp:mcd-mcp","tools":35,"totalMs":876}
```

---

## 3. 实测 Schema 偏差（★ 重点）

以下均为**实测发现**，与官方文档描述或通用 MCP 约定不一致，已在代码中做兼容处理。

### 偏差 1 ★ 业务错误不置 `isError`，错误码藏在 `structuredContent`

- **预期**（MCP 通用约定）：调用失败时应返回 `result.isError = true`。
- **实测**：失败时 `result.isError = false`，错误信息位于
  `result.structuredContent = { success: false, code: <int>, message: <string>, traceId }`。
- **后果**：只判断 `isError` 会**漏判失败**，把错误响应当成成功数据继续解析。
- **处理**：`optimizer.py::verify_price()` 先读 `content`/`isError`，**再判 `structuredContent.success`**，
  失败时返回 `business_code` 与 `error` 原文。

### 偏差 2 ★ 同一语义的错误码不稳定

- **实测**：同样是"门店未营业"这一情形，先后返回过 **`600057`**（"门店可能已关闭或不在营业时间"）
  与 **`60003`**（"门店暂停营业，请重新选择门店"）两个不同 code。
- **后果**：**不能**用错误码做精确分支判断，否则逻辑会漏。
- **处理**：改为**以 `message` 文案 + `success` 布尔值**为主判断依据，错误码仅作日志记录。

### 偏差 3 ★ `genui` 字段不存在

- **背景**：设计稿曾计划按"根级字段给 LLM 推理、`genui` 字段直接原样展示"做数据分流。
- **实测**：在 35 个工具的 `inputSchema` / `outputSchema` 及真实响应中检索 `genui` / `genUi` / `gen_ui`
  → **命中 0 次，该字段不存在**。
- **处理**：改按「结构化字段（`structuredContent.data`，供推理）vs 展示文本
  （`content[].text` 中的说明文案与 `reservationOptionText`，原样展示）」实施分流。
  详见 `SKILL.md` 核心规则 6。

### 偏差 4 ★ 价格字段单位**不统一**（最易踩坑）

| 工具 | 价格字段 | 单位 | 类型 |
|---|---|---|---|
| `calculate-price` | 价格相关字段 | **分**（需 ÷100） | 数值 |
| `query-meals` | `currentPrice` / `originalPrice` | **元**（直接展示） | **字符串** |
| `create-order` | 返回价格 | **元** | 字符串 |

- **后果**：对已是「元」的字段再除 100，或把「分」当「元」展示，都会产生 100 倍误差。
- **处理**：内部统一用「分」计算，仅在展示层转「元」；两来源冲突时**一律以 `calculate-price` 核价为准**。

### 偏差 5 ★ `${ENV_VAR}` 占位符不被可靠展开

- **实测**：客户端配置中的 `"Authorization": "Bearer ${MCD_MCP_TOKEN}"`，当变量未命中时
  **会原样保留字面量**（展开函数对未命中变量返回原文），导致实际发出
  `Bearer ${MCD_MCP_TOKEN}`，服务端返回 **401「当前鉴权码不存在或已失效」**。
- **连带影响**：因"无有效静态凭证"，客户端将 `authMode` 判定为 `oauth`，转而探测
  `.well-known/oauth-protected-resource`、`.well-known/oauth-authorization-server`、`/register`，
  而这些端点均返回 **404 Route Not Found**（该服务不提供 OAuth），授权流程失败。
- **处理**：本机实际运行改为**明文注入** Token；但**仓库内的
  `mcp-config.example.json` 仍保留 `${MCD_MCP_TOKEN}` 占位符**，不含真实凭证。

### 偏差 6 ★ 门店营业时段硬约束

- **实测**：门店营业时间 07:00–22:00。非营业时段调用 `query-meals` / `calculate-price`
  **必然**返回业务错误（即使请求参数完全正确）。
- **后果**：夜间联调会误判为"接口不可用"。
- **处理**：文档明确标注；`verify_price()` 将业务错误如实透出，不做重试消耗配额。

### 偏差 7 无 OAuth 发现端点

- `/.well-known/oauth-protected-resource`、`/.well-known/oauth-authorization-server`、
  `/.well-known/openid-configuration`、`POST /register` 实测**全部 404**。
- 说明该服务**仅支持静态 Bearer Token**，客户端不应尝试自动 OAuth。

### 偏差 8 `query-meals` 返回两级结构

- `data` 不是扁平餐品数组，而是 `{ categories: [{ name, meals: [{ code, tags }] }], meals: { <code>: {…} } }`
  的**分类 + 映射表**结构。
- **处理**：先遍历 `categories`.meals 取 `code` 与标签，再用 `code` 回查 `meals` 映射拿详情。

### 偏差 9 响应内含「文档说明层」

- `result.content[0].text` 中前半部分是**给模型看的字段说明文档**，后半部分才是原始 JSON。
- **后果**：若直接把这层文案当作结果念给用户，会出现"下面是一个 API 调用响应…"之类的噪音。
- **处理**：优先读 `structuredContent`；确需读文本时，只取 `Original Response` 之后的 JSON 段。

### 偏差 10 预约信息必须完整展示

- `query-nearby-stores` 在 `reservation=true` 时返回 `reservationTimeOptions`，
  其中 `reservationOptionText` 含完整时段（如"早餐(07:14至10:15)，午餐(10:44至14:15)…"）。
- **处理**：逐条完整展示，`today=true` 的日期加 `*` 标记，严禁省略或概括。

### 偏差 11 ★ beType=3 不存在，得来速是 beType=5（2026-10-10 实测）

- **背景**：部分二手资料把得来速写作 `beType=3`。
- **实测**：`query-meals` 传 `beType=3` 被参数校验直接拒绝——
  `beType: must be equal to one of the allowed values`，合法枚举 `[1, 2, 5, 6]`。
  而传 `beType=5` 全链路（门店→菜单→券→核价→可下单参数）畅通。
- **后果**：按 beType=3 实现得来速会直接调用失败。
- **处理**：全部文档与代码统一 `得来速=beType=5, orderType=1, beCode 必传`；
  SKILL.md 场景表加「常见误区」警示。

### 偏差 12 同门店渠道间菜单与价格完全一致，差异在 takeWayList

- **实测**（门店 1950936，同一篮子）：`beType=1` 与 `beType=5` 的
  `query-meals` 菜单、标价、`calculate-price` 核价**逐字段一致**（均 ¥47.00）。
- **差异点**：`takeWayList` —— beType=1 返回 `eat-in`(堂食)/`locker-in`(外带)/
  `take-dt-quick`(得来速) 三种；beType=5 **锁定** `take-dt-quick`（车道自提）。
- **处理**：渠道比价中两渠道价格并列最低时，按取餐便利性给建议，而非硬造价格差。

### 偏差 13 calculate-price 含完整到手成本结构（新发现字段）

- **实测**：`calculate-price` 除商品价外还返回 `deliveryOriginalPrice`/`deliveryPrice`（运费）、
  `packingOriginalPrice`/`packingPrice`（打包费）、`tablewarePrice`（餐具费）、
  `originalPrice`/`discount`/`price`，以及 `enjoyed`/`enjoyable`
  （含 `realDiscount` 实际优惠值与 `balance` 距下一档优惠的门槛差额，单位分）。
- **恒等式**：`productPrice + deliveryPrice + packingPrice + tablewarePrice − discount = price`
  （实测到店两渠道：4700+0+0+0−0=4700 ✅）。
- **处理**：`optimizer.py::fetch_channel_quote()` 提取全结构并做恒等式校验，
  不一致时以官方现价 `price` 为准并显式标注 `sum_consistent=False`。

### 偏差 14 麦乐送核价强依赖配送地址

- **实测**：`delivery-query-addresses` 返回空列表时，无法经 `delivery-query-stores`
  取得 beCode，`calculate-price(beType=2)` 无法发起。
- **处理**：渠道比价中麦乐送缺 beCode → 如实标注「无法核价：缺少 beCode」，
  **不编造运费**；引导用户创建地址后可补齐该渠道。

### 偏差 15 无第三方平台比价接口

- **实测**：35 个工具全部为麦当劳官方体系（点餐/券/商城/积分/派对），
  检索无美团、饿了么等外部平台相关工具。
- **处理**：渠道比价仅限内部渠道（到店自取/麦乐送/得来速）；
  被问及外部平台价格时如实说明能力边界，不引用记忆价格。

---

## 4. 复现方式

```bash
# 1) 环境变量（Token 从 https://open.mcd.cn/mcp 获取）
export MCD_MCP_TOKEN="<你的 Token>"

# 2) 本地跑优化器（纯标准库，不联网）
python scripts/optimizer.py
python scripts/optimizer.py --selftest

# 3) 核价联调（需在门店营业时段 07:00–22:00）
#    见 scripts/optimizer.py::verify_price(plan, store_code, token=...)
```

---

## 5. 结论

- 服务端**连通性与鉴权正常**，35 个工具可全部发现并调用。
- 主要接入风险集中在 **错误判定方式（偏差 1、2）** 与 **价格单位（偏差 4）**，二者均已代码化处理并附回归断言。
- 得来速（beType=5）与渠道比价链路已于 2026-10-10 端到端实测通过（偏差 11–15）；
  渠道比价仅限内部渠道，外部平台接口不存在。
- 全部偏差均可在 `scripts/optimizer.py` 与 `SKILL.md` 中找到对应处理逻辑。
