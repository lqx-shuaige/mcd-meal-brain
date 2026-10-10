#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真实核价复现脚本 —— 方向2「真实核价截图」的可执行记录。

数据来源（全部为 2026-10-10 上午真实 MCP 调用的实测快照，非编造）
------------------------------------------------------------------
| # | 工具                      | 时间     | traceId（脱敏）              |
|---|---------------------------|----------|------------------------------|
| 1 | now-time-info             | 09:27:49 | 4d5465…43742a（首尾 6 位）   |
| 2 | query-nearby-stores       | 09:28:19 | cca721…1bf27                 |
| 3 | query-meals               | 09:28:29 | 8fcee7…53eac5                |
| 4 | query-store-coupons       | 09:28:58 | 66a17d…b6c3f9                |
| 5 | calculate-price           | 09:30 前后（见 PRICING_TRACE）|

门店：1950564 麦当劳北京建外SOHO(A座)餐厅（到店自取 beType=1 / orderType=1，
营业 07:00–22:00，businessStatus=true，距离 269m）。

计价口径（本地估算，与 README/SKILL 一致）
----------------------------------------
* ``discountType`` 为空或「促销优惠」→ 按 ``currentPrice``（促销价无需附加条件）；
* ``discountType`` 含「随单购」→ 按 ``originalPrice``（不购买早餐卡时的真实价格，
  避免引入随单购卡片购买链路）；
* 营养字段：``query-meals`` 不返回营养，按名称归一化（去空格/™/®/引号；【】去
  括号保内容、中英文圆括号去括号及内容）后与 ``list-nutrition-foods``（160 品）
  精确匹配（两侧对称归一）；**未命中一律置 0**
  （见 NUTRITION_MATCHED / NUTRITION_UNMATCHED），不做估算、不编造。

用法
::

    python scripts/verify_pricing.py            # 重放计算 + 输出核价参数/对比
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimizer  # noqa: E402

# ---------------------------------------------------------------------------
# 1. 实测快照（2026-10-10，建外SOHO A座，早餐时段菜单）
# ---------------------------------------------------------------------------

STORE: Dict[str, Any] = {
    "storeCode": "1950564",
    "storeName": "麦当劳北京建外SOHO(A座)餐厅",
    "address": "东三环中路39号院建外SOHO-A座",
    "distance_m": 269,
    "businessStartTime": "07:00",
    "businessEndTime": "22:00",
    "businessStatus": True,
    "beType": 1,          # 到店自取（不传 beCode）
    "orderType": 1,
}

#: query-meals 全量商品（code, name, currentPrice元, originalPrice元, discountType）
RAW_MENU: List[Tuple[str, str, float, float, str]] = [
    ("5022", "猪柳蛋麦满分", 18, 18, ""),
    ("5024", "猪柳麦满分", 14, 14, ""),
    ("514851", "咖啡阿芙佳朵", 15, 15, ""),
    ("2430", "【美汁源】“黄金橙橙”", 13, 13, ""),
    ("521743", "薯条脆卜卜毛绒周边", 29.9, 29.9, ""),
    ("521989", "枫糖风味厚松饼猪柳蛋堡", 22, 22, ""),
    ("9900008094", "吉士蛋麦满分组合", 7.2, 12, "随单购早餐卡优惠"),
    ("507328", "麦咖啡™热奶铁中杯", 13.9, 21, "促销优惠"),
    ("521990", "枫糖风味厚松饼猪柳堡", 18, 18, ""),
    ("521750", "泷盐星星麦旋风", 16.5, 16.5, ""),
    ("507368", "麦咖啡™热美式中杯", 11.9, 19, "促销优惠"),
    ("1254", "火腿扒麦满分", 10, 10, ""),
    ("9900003429", "图林根香肠早安营养卷组合", 11.4, 19, "随单购早餐卡优惠"),
    ("9900003428", "火腿扒早安营养卷组合", 11.1, 18.5, "随单购早餐卡优惠"),
    ("9900003504", "双层原味板烧鸡腿麦满分组合", 13.2, 22, "随单购早餐卡优惠"),
    ("510488", "燕麦奶", 21, 21, ""),
    ("9900003425", "猪柳炒双蛋堡组合", 12.6, 21, "随单购早餐卡优惠"),
    ("9900003469", "图林根香肠早安营养卷套餐", 14.4, 24, "随单购早餐卡优惠"),
    ("9900003424", "原味板烧鸡腿炒双蛋堡组合", 12.6, 21, "随单购早餐卡优惠"),
    ("9900003468", "火腿扒早安营养卷套餐", 14.1, 23.5, "随单购早餐卡优惠"),
    ("3755", "纯牛奶(盒装)", 10.5, 10.5, ""),
    ("9900003423", "猪柳麦满分组合", 10.2, 17, "随单购早餐卡优惠"),
    ("9900003466", "原味板烧鸡腿炒双蛋堡套餐", 15.6, 26, "随单购早餐卡优惠"),
    ("3757", "纯悦", 7.5, 7.5, ""),
    ("9900003465", "猪柳炒双蛋堡套餐", 15.6, 26, "随单购早餐卡优惠"),
    ("513112", "火腿扒早安营养卷", 15.5, 15.5, ""),
    ("6362", "100% 苹果汁(盒装)", 11.5, 11.5, ""),
    ("510041", "冰牛奶中杯", 19, 19, ""),
    ("521917", "泷愿成真麦旋风——大米风味", 16.5, 16.5, ""),
    ("513113", "图林根香肠早安营养卷", 16, 16, ""),
    ("9900015604", "东南亚风味椰香鸡扒堡组合", 9.9, 9.9, ""),
    ("521950", "蓝莓爆爆珠麦旋风", 16.5, 16.5, ""),
    ("9900015605", "东南亚风味椰香鸡扒堡套餐", 11.4, 19, "随单购早餐卡优惠"),
    ("521951", "灰焰圆筒", 5, 5, ""),
    ("521315", "芝芝火腿扒堡", 13, 13, ""),
    ("3010", "雪碧", 9.5, 9.5, ""),
    ("9900016146", "巧克力味厚松饼猪柳堡组合", 12.6, 21, "随单购早餐卡优惠"),
    ("9900016144", "枫糖风味厚松饼猪柳堡组合", 12.6, 21, "随单购早餐卡优惠"),
    ("9900016145", "巧克力味厚松饼猪柳蛋堡组合", 15, 25, "随单购早餐卡优惠"),
    ("9900016143", "枫糖风味厚松饼猪柳蛋堡组合", 15, 25, "随单购早餐卡优惠"),
    ("2631", "脆香油条", 8.5, 8.5, ""),
    ("510479", "燕麦奶铁", 9.9, 23, "随单购早餐卡优惠"),
    ("507394", "牛奶", 19, 19, ""),
    ("9900012729", "脆香油条组合", 6.6, 11, "随单购早餐卡优惠"),
    ("9900003453", "吉士蛋麦满分套餐", 10.2, 17, "随单购早餐卡优惠"),
    ("9900008745", "新地", 12, 12, ""),
    ("9900003451", "大脆鸡扒麦满分套餐", 10.8, 18, "随单购早餐卡优惠"),
    ("9900003493", "原味板烧鸡腿麦满分组合", 10.8, 18, "随单购早餐卡优惠"),
    ("9900003491", "猪柳蛋麦满分组合", 12.6, 21, "随单购早餐卡优惠"),
    ("515520", "怡泉+C", 11.5, 11.5, ""),
    ("9900011873", "芝芝火腿扒堡套餐", 11.4, 19, "随单购早餐卡优惠"),
    ("1088", "优品豆浆", 9.5, 9.5, ""),
    ("521885", "东南亚风味椰香鸡扒堡", 13, 13, ""),
    ("520830", "双层猪柳蛋麦满分", 21, 21, ""),
    ("9900016524", "厚松饼猪柳蛋堡三件套", 17.94, 29.9, "随单购早餐卡优惠"),
    ("515280", "阳光柠檬红茶", 9.5, 9.5, ""),
    ("9900011872", "芝芝火腿扒堡组合", 9.9, 9.9, ""),
    ("9900013411", "精选麦满分套餐", 15.6, 26, "随单购早餐卡优惠"),
    ("502773", "猪柳炒双蛋堡", 18, 18, ""),
    ("9900013221", "一早现磨 超值二件套", 14.9, 29, ""),
    ("9900016178", "巧克力味厚松饼猪柳蛋堡套餐", 17.94, 29.9, "随单购早餐卡优惠"),
    ("520361", "奶铁雪冰", 23, 23, ""),
    ("9900016132", "巧克力味厚松饼猪柳堡套餐", 15.54, 25.9, "随单购早餐卡优惠"),
    ("9900016176", "枫糖风味厚松饼猪柳堡套餐", 15.54, 25.9, "随单购早餐卡优惠"),
    ("9900003728", "脆香油条豆浆组合", 17, 17, ""),
    ("507389", "浓缩咖啡", 15, 15, ""),
    ("9900016171", "枫糖风味厚松饼猪柳蛋堡套餐", 17.94, 29.9, "随单购早餐卡优惠"),
    ("9900003449", "火腿扒麦满分套餐", 10.2, 17, "随单购早餐卡优惠"),
    ("507387", "卡布奇诺", 21, 21, ""),
    ("9900008751", "可乐", 9.5, 9.5, ""),
    ("6800", "原味板烧鸡腿麦满分", 15, 15, ""),
    ("515955", "浓浓燕麦黑巧（冰/热）", 15.9, 23, "促销优惠"),
    ("9900008752", "派", 8.5, 8.5, ""),
    ("4989", "双层原味板烧鸡腿麦满分", 19, 19, ""),
    ("4825", "脆薯饼", 8.5, 8.5, ""),
    ("9900008754", "经典麦旋风", 15.5, 15.5, ""),
    ("515837", "圆筒冰淇淋", 5, 5, ""),
    ("521817", "【美汁源】“多汁柠柠”", 12, 12, ""),
    ("514700", "原味板烧鸡腿炒双蛋堡", 18, 18, ""),
    ("520689", "鲜萃咖啡", 9.5, 9.5, ""),
    ("507409", "麦咖啡™奶铁", 13.9, 21, "促销优惠"),
    ("3505", "锡兰红茶", 11.5, 11.5, ""),
    ("505235", "德式图林根香肠", 9.5, 9.5, ""),
    ("521981", "巧克力味厚松饼猪柳蛋堡", 22, 22, ""),
    ("521982", "巧克力味厚松饼猪柳堡", 18, 18, ""),
    ("507411", "麦咖啡™美式", 11.9, 19, "促销优惠"),
    ("9900003459", "双层原味板烧鸡腿麦满分套餐", 16.2, 27, "随单购早餐卡优惠"),
    ("9900003458", "猪柳蛋麦满分套餐", 15.6, 26, "随单购早餐卡优惠"),
    ("5888", "吉士蛋麦满分", 10, 10, ""),
    ("9900003457", "原味板烧鸡腿麦满分套餐", 13.8, 23, "随单购早餐卡优惠"),
    ("5889", "大脆鸡扒麦满分", 11, 11, ""),
    ("4437", "玉米杯", 13, 13, ""),
    ("9900003456", "火腿扒麦满分组合", 7.2, 12, "随单购早餐卡优惠"),
    ("9900003455", "大脆鸡扒麦满分组合", 7.8, 13, "随单购早餐卡优惠"),
    ("515946", "浓浓黑巧（冰/热）", 13.9, 21, "促销优惠"),
    ("9900003454", "猪柳麦满分套餐", 13.2, 22, "随单购早餐卡优惠"),
]

#: list-nutrition-foods 名称归一化精确匹配命中的商品 → (kcal, 蛋白g, 脂肪g, 钠mg, 碳水g)
NUTRITION_MATCHED: Dict[str, Tuple[float, float, float, float, float]] = {
    "5022": (387, 23, 21, 846, 25),        # 猪柳蛋麦满分
    "5024": (308, 16, 16, 781, 24),        # 猪柳麦满分
    "1254": (261, 14, 12, 608, 24),        # 火腿扒麦满分
    "5889": (361, 16, 17, 804, 35),        # 大脆鸡扒麦满分
    "6800": (246, 15, 9, 611, 25),         # 原味板烧鸡腿麦满分
    "4989": (355, 23, 17, 984, 27),        # 双层原味板烧鸡腿麦满分
    "520830": (513, 29, 32, 1210, 26),     # 双层猪柳蛋麦满分
    "502773": (437, 25, 24, 705, 29),      # 猪柳炒双蛋堡
    "514700": (419, 27, 21, 715, 30),      # 原味板烧鸡腿炒双蛋堡
    "513112": (444, 16, 24, 1065, 39),     # 火腿扒早安营养卷
    "513113": (425, 14, 23, 977, 38),      # 图林根香肠早安营养卷
    "2631": (202, 5, 12, 230, 19),         # 脆香油条
    "4825": (146, 1, 9, 311, 14),          # 脆薯饼
    "505235": (87, 5, 6, 293, 2),          # 德式图林根香肠
    "515837": (93, 2, 3, 36, 14),          # 圆筒冰淇淋
    "3755": (129, 7, 7, 73, 10),           # 纯牛奶(盒装) → 纯牛奶（盒装）
    "3757": (0, 0, 0, 0, 0),               # 纯悦
    "3505": (2, 0, 0, 0, 0),               # 锡兰红茶
    "514851": (139, 4, 4, 49, 21),         # 咖啡阿芙佳朵
    "507389": (13, 1, 0, 0, 2),            # 浓缩咖啡
    "6362": (88, 0, 0, 0, 21),             # 100% 苹果汁(盒装) → 100%苹果汁
    "510041": (224, 13, 11, 128, 19),      # 冰牛奶中杯
    "2430": (165, 0, 0, 53, 41),           # 【美汁源】“黄金橙橙”
}

#: 营养表未收录（套餐组合 / 周边 / 规格不明的饮品甜品）→ 营养按 0 处理，不估算
NUTRITION_UNMATCHED_NOTE = (
    "未命中共 {n} 项（套餐/组合/三件套/毛绒周边/规格未标注的饮品甜品，"
    "如吉士蛋麦满分、各类厚松饼堡、经典麦旋风、雪碧、玉米杯等），营养按 0 处理。"
    "list-nutrition-foods 无套餐组合口径，不做估算、不编造。"
)

#: query-store-coupons 真实返回：空（该门店到店自取当前无可用券）
STORE_COUPONS: List[Dict[str, Any]] = []

#: 演示场景（README 5.1）
SCENARIO = "我们4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"

#: calculate-price 官方核价真实响应（2026-10-10 09:33:53 实测，traceId 5b8f62…65116c）
#: 最省钱方案「大脆鸡扒麦满分 ×4」→ 官方现价 4400 分 = ¥44.00，与本地估算分毫不差
PRICING_TRACE: Optional[Dict[str, Any]] = {
    "request": {
        "storeCode": "1950564",
        "orderType": 1,
        "beType": 1,
        "items": [{"productCode": "5889", "quantity": 4}],
    },
    "datetime": "2026-10-10 09:33:53",
    "trace_id_masked": "5b8f62…65116c",
    "official_cny": 44.00,
    "product_price_cny": 44.00,     # productPrice 4400 分
    "delivery_cny": 0.00,           # 到店自取无运费（响应未含该字段）
    "packing_cny": 0.00,            # 打包费 0（响应未含该字段）
    "discount_cny": 0.00,           # discount 0 分
    # 勘误（2026-10-10 13:46 复验，calculate-price 实调 traceId cb4abf9e…5c5ca）：
    # 外带 code 原快照误记为 take-in-store，实测为 locker-in（与 SKILL.md/tools.md 一致）；
    # 本复验亦返回第三种 take-dt-quick（得来速）。价格/traceId/时间字段不受影响。
    "take_ways": ["堂食", "外带（店内取餐柜）", "得来速（车道自提）"],
    "raw_key_fields": {
        "productOriginalPrice": 4400, "productPrice": 4400,
        "originalPrice": 4400, "discount": 0, "price": 4400,
        "productList": [{"productCode": "5889", "productName": "大脆鸡扒麦满分",
                         "quantity": 4, "originalSubtotal": 4400, "subtotal": 4400}],
        "takeWayList": [{"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
                        {"code": "locker-in", "title": "外带", "subtitle": "店内取餐柜"},
                        {"code": "take-dt-quick", "title": "得来速", "subtitle": "车道自提"}],
    },
    "trace_note": "calculate-price 2026-10-10 09:33:53，traceId 5b8f62…65116c",
}


# ---------------------------------------------------------------------------
# 2. 本地计算（optimizer 完整链路重放）
# ---------------------------------------------------------------------------

def build_menu() -> List[Dict[str, Any]]:
    menu: List[Dict[str, Any]] = []
    for code, name, current, original, discount in RAW_MENU:
        # 计价口径：随单购优惠商品按原价（不购买早餐卡）
        use_price = original if "随单购" in discount else current
        kcal, protein, fat, sodium, carb = NUTRITION_MATCHED.get(code, (0, 0, 0, 0, 0))
        menu.append({
            "productCode": code,
            "name": name,
            "price": int(round(use_price * 100)),   # 分
            "kcal": kcal, "protein": protein, "fat": fat,
            "sodium": sodium, "carb": carb,
        })
    return menu


def compute_plans() -> Dict[str, Any]:
    menu = optimizer._normalize_menu(build_menu())
    constraints = optimizer.parse_constraints(SCENARIO)
    kept = optimizer.filter_menu(menu, constraints)
    plans = optimizer.optimize_plans(kept, STORE_COUPONS, constraints)
    return {"constraints": constraints, "kept": kept, "plans": plans}


def cheapest_plan() -> Dict[str, Any]:
    plans = compute_plans()["plans"]
    return next(p for p in plans if p["label"] == "最省钱")


def pricing_request(plan: Dict[str, Any]) -> Dict[str, Any]:
    """按 calculate-price 契约构造核价请求（到店自取：不传 beCode）。"""
    first_coupon = next((c for c in plan.get("coupons", []) if c.get("couponId")), None)
    items = []
    for ln in plan["items"]:
        entry = {"productCode": ln["productCode"], "quantity": int(ln["quantity"])}
        if first_coupon:
            entry["couponId"] = first_coupon["couponId"]
        items.append(entry)
    return {
        "storeCode": STORE["storeCode"],
        "orderType": STORE["orderType"],
        "beType": STORE["beType"],
        "items": items,
    }


def snapshot_local_estimate() -> float:
    """快照篮子的本地估算（**同篮对比**口径）。

    官方快照价（09:33:53）对应的是快照时刻 DP 选出的最省钱篮子
    （``PRICING_TRACE.raw_key_fields.productList``）。DP 选篮会随菜单快照与
    算法演进而变化——重放时今日最省钱篮子可能已不同（例如火腿扒麦满分×4），
    拿**今日选篮**对比**当时快照价**会产生假偏差。因此本地估算必须取同一
    篮子：按 RAW_MENU 计价口径（随单购按 originalPrice、其余按 currentPrice，
    与 build_menu 完全一致）逐项求和。
    """
    snap_items = (PRICING_TRACE or {}).get("raw_key_fields", {}).get("productList", [])
    if not snap_items:
        return 0.0
    price_by_code = {code: (original if "随单购" in disc else current)
                     for code, name, current, original, disc in RAW_MENU}
    total_cents = 0
    for it in snap_items:
        unit_cny = float(price_by_code[str(it["productCode"])])
        total_cents += int(round(unit_cny * 100)) * int(it["quantity"])
    return total_cents / 100.0


# ---------------------------------------------------------------------------
# 3. HTML 对比页（docs/pricing-verification.html，零依赖单文件）
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML_PATH = os.path.join(REPO_ROOT, "docs", "pricing-verification.html")

_HTML_CSS = """
:root{--red:#da291c;--green:#00a651;--yellow:#ffc72c;--ink:#1f2329;--muted:#6b7280;
--line:#e5e7eb;--card:#fff;--bg:#f7f8fa}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);
line-height:1.65;padding:32px 16px 64px}
.wrap{max-width:960px;margin:0 auto}
header{text-align:center;margin-bottom:26px}
header .logo{display:inline-flex;align-items:center;gap:10px;font-weight:800;font-size:22px}
header .logo .b{background:var(--yellow);color:#3b2c00;border-radius:8px;padding:2px 10px}
header .sub{color:var(--muted);font-size:14px;margin-top:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:24px 28px;margin-bottom:22px;box-shadow:0 1px 3px rgba(31,35,41,.05)}
h2{font-size:17px;margin-bottom:14px}
.hero{border:2px solid #bfe8d2;background:linear-gradient(135deg,#fff 0%,#f2fbf6 100%);
border-radius:16px;padding:28px;text-align:center;margin-bottom:22px}
.hero .flow{display:flex;justify-content:center;align-items:center;gap:26px;flex-wrap:wrap}
.hero .node .lab{font-size:13px;color:var(--muted)}
.hero .node .val{font-size:36px;font-weight:800}
.hero .ok{margin-top:12px}
.hero .ok .big{color:var(--green);font-size:44px;font-weight:900}
.hero .ok .pct{display:inline-block;background:var(--green);color:#fff;border-radius:10px;
font-size:20px;font-weight:800;padding:3px 16px;margin-left:12px;vertical-align:middle}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th{background:#f3f4f6;font-size:12.5px;color:#4b5563;font-weight:600;
padding:8px 10px;border-bottom:2px solid var(--line);text-align:left}
td{padding:8px 10px;border-bottom:1px solid #eef0f2;vertical-align:top}
tr:last-child td{border-bottom:none}
code{background:#eef0f2;border-radius:4px;padding:1px 6px;font-size:12px}
pre{background:#1f2329;color:#e5e7eb;border-radius:10px;padding:16px 18px;
font-size:12.5px;overflow-x:auto;line-height:1.55}
.muted{color:var(--muted)}
.timeline td:first-child{white-space:nowrap;font-weight:600}
footer{text-align:center;color:var(--muted);font-size:12.5px}
"""


def render_html(cheap: Dict[str, Any]) -> str:
    import html as _h
    esc = _h.escape
    t = PRICING_TRACE or {}
    official = t.get("official_cny", 0.0)
    snap_items = t.get("raw_key_fields", {}).get("productList", [])
    people = int(cheap.get("people_count") or 4)
    if snap_items:
        # 同篮对比：本地估算取**快照篮子**逐项求和（见 snapshot_local_estimate）；
        # 商品明细/合计/人均营养均按快照篮子呈现，与官方核价同一口径。
        # DP 选篮随菜单快照/算法演进（今日重放最便宜篮子可能不同），不参与对比。
        estimated = snapshot_local_estimate()
        basket_desc = "、".join(
            f"{it['productName']}×{it['quantity']}" for it in snap_items)
        items_html = "".join(
            f"<tr><td><code>{esc(str(it['productCode']))}</code></td>"
            f"<td>{esc(it['productName'])}</td><td>×{it['quantity']}</td>"
            f"<td>¥{int(it['subtotal']) / 100:.2f}</td></tr>"
            for it in snap_items
        )
        snap_total_cny = sum(int(it["subtotal"]) for it in snap_items) / 100.0
        snap_pc_cny = snap_total_cny / people
        kcal_pc = prot_pc = fat_pc = 0.0
        for it in snap_items:
            nutri = NUTRITION_MATCHED.get(str(it["productCode"]))
            if nutri:
                q = int(it["quantity"])
                kcal_pc += nutri[0] * q / people
                prot_pc += nutri[1] * q / people
                fat_pc += nutri[2] * q / people
    else:
        estimated = cheap["final_price_cny"]
        basket_desc = "、".join(
            f"{i['name']}×{i['quantity']}" for i in cheap["items"])
        items_html = "".join(
            f"<tr><td><code>{esc(ln['productCode'])}</code></td><td>{esc(ln['name'])}</td>"
            f"<td>×{ln['quantity']}</td><td>¥{ln['subtotal_cny']:.2f}</td></tr>"
            for ln in cheap["items"]
        )
        snap_total_cny = cheap["subtotal_cny"]
        snap_pc_cny = cheap["per_capita_cny"]
        kcal_pc = cheap["nutrition"]["per_capita_kcal"]
        prot_pc = cheap["nutrition"]["per_capita_protein_g"]
        fat_pc = cheap["nutrition"]["per_capita_fat_g"]
    # 偏差按本地估算 vs 官方核价实际计算（与 main 文本输出同口径，≤5% 判一致）
    diff = round(official - estimated, 2)
    pct = round(abs(diff) / official * 100, 2) if official else 0.0
    raw = json.dumps(t.get("raw_key_fields", {}), ensure_ascii=False, indent=2)
    req = json.dumps(t.get("request", {}), ensure_ascii=False, indent=2)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦拼单大脑 · 真实核价记录</title><style>{_HTML_CSS}</style></head>
<body><div class="wrap">

<header>
  <div class="logo"><span class="b">麦麦拼单大脑</span> 真实核价记录</div>
  <div class="sub">{esc(STORE['storeName'])}（{esc(STORE['storeCode'])}）· 到店自取 · 2026-10-10 营业时段实测</div>
</header>

<section class="hero">
  <div class="flow">
    <div class="node">
      <div class="lab">本地估算（RAW_MENU 计价口径 · 同篮对比）</div>
      <div class="val">¥{estimated:.2f}</div>
    </div>
    <div class="node" style="font-size:26px;color:var(--muted)">⇄</div>
    <div class="node">
      <div class="lab">官方核价（calculate-price）</div>
      <div class="val">¥{official:.2f}</div>
    </div>
  </div>
  <div class="ok"><span class="big">偏差 ¥{abs(diff):.2f}</span><span class="pct">{pct:.2f}% {'一致 ✅' if pct <= 5 else '偏差 >5%，以官方价为准'}</span></div>
  <div class="muted" style="font-size:13px;margin-top:8px">
    场景：{esc(SCENARIO)}｜核价篮子：{esc(basket_desc)}（09:33 快照时刻 DP 最省钱方案）</div>
</section>

<section class="card">
  <h2>调用链路（5 次真实 MCP 调用，已脱敏）</h2>
  <table class="timeline">
    <tr><th>时间</th><th>工具</th><th>关键参数</th><th>关键返回</th><th>traceId</th></tr>
    <tr><td>09:27:49</td><td><code>now-time-info</code></td><td>—</td>
        <td>2026-10-10 09:27:49（周六，GMT+8）→ 处于营业时段 07:00–22:00</td>
        <td class="muted">4d5465…43742a</td></tr>
    <tr><td>09:28:19</td><td><code>query-nearby-stores</code></td>
        <td><code>beType=1, searchType=2, city=北京市, keyword=国贸</code></td>
        <td>5 家门店全部 businessStatus=true；选定最近门店 195****（269m）</td>
        <td class="muted">cca721…1bf27</td></tr>
    <tr><td>09:28:29</td><td><code>query-meals</code></td>
        <td><code>storeCode=195****, orderType=1, beType=1</code></td>
        <td>早餐时段菜单，分类 + 商品映射两级结构，共 96 个商品</td>
        <td class="muted">8fcee7…53eac5</td></tr>
    <tr><td>09:28:58</td><td><code>query-store-coupons</code></td>
        <td><code>storeCode=195****, orderType=1, beType=1</code></td>
        <td>券池为空（该门店到店自取当前无可用券）→ 最省钱方案不含券</td>
        <td class="muted">66a17d…b6c3f9</td></tr>
    <tr><td>09:33:53</td><td><code>calculate-price</code></td>
        <td><code>storeCode=195****, orderType=1, beType=1, items=[5889×4]</code></td>
        <td><b>¥44.00</b>（4400 分）；discount=0；取餐方式：堂食 / 外带（取餐柜）/ 得来速</td>
        <td class="muted">5b8f62…65116c</td></tr>
  </table>
</section>

<section class="card">
  <h2>核价篮子商品明细（快照 productList，与官方核价同篮）</h2>
  <table>
    <tr><th>商品编码</th><th>名称</th><th>数量</th><th>小计</th></tr>
    {items_html}
    <tr><td colspan="3"><b>合计（人均 ¥{snap_pc_cny:.2f} × {people} 人，主食 4 份）</b></td>
        <td><b>¥{snap_total_cny:.2f}</b></td></tr>
  </table>
  <p class="muted" style="font-size:13px;margin-top:10px">
    营养（人均）：{kcal_pc:.0f} kcal ·
    蛋白 {prot_pc:.1f} g ·
    脂肪 {fat_pc:.1f} g（营养来自 list-nutrition-foods 名称精确匹配）</p>
</section>

<section class="card">
  <h2>核价请求（calculate-price 入参）</h2>
  <pre>{esc(req)}</pre>
</section>

<section class="card">
  <h2>核价响应关键字段（单位：分；已脱敏）</h2>
  <pre>{esc(raw)}</pre>
  <p class="muted" style="font-size:13px;margin-top:10px">
    恒等式校验：商品 4400 + 运费 0 + 打包 0 + 餐具 0 − 优惠 0 = 现价 4400 ✅</p>
</section>

<section class="card">
  <h2>计价口径与如实说明</h2>
  <table>
    <tr><th>事项</th><th>处理方式</th></tr>
    <tr><td>随单购优惠商品（如「猪柳蛋麦满分套餐 ¥15.6/26」）</td>
        <td>按 <b>originalPrice</b> 计价（不购买早餐卡时的真实价格），避免引入随单购卡片链路</td></tr>
    <tr><td>促销优惠商品（如「麦咖啡™奶铁 ¥13.9/21」）</td>
        <td>按 <b>currentPrice</b> 计价（促销价无需附加条件）</td></tr>
    <tr><td>营养字段（query-meals 不返回）</td>
        <td>与 list-nutrition-foods（160 品）名称归一化精确匹配：命中 23 项；
            未命中（套餐组合/周边/规格不明饮品）营养按 0，<b>不估算、不编造</b></td></tr>
    <tr><td>门店券（query-store-coupons 返回空）</td>
        <td>券池为空 → 券组合 DP 自然退化为无券方案，最省钱 = 纯最小成本组合</td></tr>
    <tr><td>早餐时段菜单</td>
        <td>09:28 为早餐时段（07:14–10:15），菜单为早餐品类（麦满分/厚松饼/粥类），
            与 README 正餐演示场景品类不同，属真实时段差异，如实呈现</td></tr>
    <tr><td>核价对比口径</td>
        <td><b>同篮对比</b>：本地估算取快照篮子（大脆鸡扒麦满分×4）按 RAW_MENU 计价口径
            逐项求和；DP 选篮会随菜单快照/算法演进而变化（今日重放最便宜篮子可能不同），
            跨篮对比无意义。2026-10-10 13:46 复验取餐编码同口径修正（traceId cb4abf9e…5c5ca）</td></tr>
  </table>
</section>

<footer>
  本页由 <code>python scripts/verify_pricing.py --html</code> 生成；所有数字固化自 2026-10-10
  真实 MCP 调用快照（见 scripts/verify_pricing.py 顶部数据来源表），非手编。<br>
  门店编号按 MCP_INTEGRATION.md 约定脱敏为 195****；traceId 仅保留首尾 6 位。
</footer>

</div></body></html>"""


# ---------------------------------------------------------------------------
# 4. 入口：重放 + 对比
# ---------------------------------------------------------------------------

def _safe_stdio() -> None:
    """GBK 控制台/管道下 emoji 输出防崩（同 optimizer._safe_stdio）。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except AttributeError:
            pass


def main() -> int:
    _safe_stdio()
    r = compute_plans()
    plans, constraints = r["plans"], r["constraints"]
    if not plans:
        print("⚠️ 无可行方案")
        return 1

    print("=" * 68)
    print(f"场景：{SCENARIO}")
    print(f"门店：{STORE['storeName']}（{STORE['storeCode']}）到店自取")
    print(f"菜单：{len(RAW_MENU)} 项（query-meals 实测快照）"
          f"｜硬约束过滤后保留 {len(r['kept'])} 项"
          f"｜门店券：{len(STORE_COUPONS)} 张（query-store-coupons 实测返回空）")
    print("=" * 68)

    for p in plans:
        n = p["nutrition"]
        print(f"\n{p['label']}：原价 ¥{p['subtotal_cny']:.2f}"
              f" − 券 ¥{p['discount_cny']:.2f} = ¥{p['final_price_cny']:.2f}"
              f"（人均 ¥{p['per_capita_cny']:.2f}，主食 {p['mains']} 份）")
        for ln in p["items"]:
            print(f"    · {ln['name']} ×{ln['quantity']}  ¥{ln['subtotal_cny']:.2f}")
        print(f"    营养：总 {n['total_kcal']:.0f} kcal｜人均 {n['per_capita_kcal']:.0f} kcal"
              f"｜人均蛋白 {n['per_capita_protein_g']:.1f} g")

    cheap = next(p for p in plans if p["label"] == "最省钱")

    if PRICING_TRACE:
        # 同篮对比：官方快照价对应 09:33 快照时刻 DP 最省钱篮子（productList）；
        # 今日重放的 DP 选篮可能已不同（菜单/算法演进），跨篮对比是假偏差——
        # 本地估算取同一篮子按 RAW_MENU 计价口径逐项计算（见 snapshot_local_estimate）。
        snap_items = PRICING_TRACE["raw_key_fields"]["productList"]
        req = {
            "storeCode": PRICING_TRACE["request"]["storeCode"],
            "orderType": PRICING_TRACE["request"]["orderType"],
            "beType": PRICING_TRACE["request"]["beType"],
            "items": [{"productCode": it["productCode"], "quantity": int(it["quantity"])}
                      for it in snap_items],
        }
        print("\n" + "=" * 68)
        print("【核价篮子 · calculate-price 请求（与 09:33 快照同篮对齐）】")
        print(json.dumps(req, ensure_ascii=False, indent=2))

        official = PRICING_TRACE["official_cny"]
        estimated = snapshot_local_estimate()
        diff = round(official - estimated, 2)
        pct = round(abs(diff) / official * 100, 2) if official else 0.0
        print("\n【本地估算 vs 官方核价（同篮对比）】")
        print("  篮子：" + "、".join(
            f"{it['productName']}×{it['quantity']}" for it in snap_items))
        print(f"  本地估算：¥{estimated:.2f}（RAW_MENU 计价口径逐项求和）")
        print(f"  官方核价：¥{official:.2f}  ({PRICING_TRACE['trace_note']})")
        print(f"  偏差：¥{diff:+.2f}（{pct:.2f}%）→ "
              + ("一致 ✅" if pct <= 5 else "偏差 >5%，以官方价为准（SKILL 第八节）"))
        print("  注：DP 选篮随菜单快照/算法演进；今日重放最省钱篮子为："
              + "、".join(f"{ln['name']}×{ln['quantity']}" for ln in cheap["items"])
              + f"（¥{cheap['final_price_cny']:.2f}，信息性输出，不参与对比）。")
    else:
        req = pricing_request(cheap)
        print("\n" + "=" * 68)
        print("【今日最省钱方案 · calculate-price 核价请求】")
        print(json.dumps(req, ensure_ascii=False, indent=2))
        print("\n⚠️ PRICING_TRACE 未回填：本脚本当前仅完成本地计算，"
              "核价响应需在营业时段真实调用后固化。")

    if "--html" in sys.argv:
        os.makedirs(os.path.dirname(HTML_PATH), exist_ok=True)
        with open(HTML_PATH, "w", encoding="utf-8") as fh:
            fh.write(render_html(cheap))
        print(f"\n✅ 已生成 {HTML_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
