#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦拼单大脑 —— 组合优化核心脚本。

设计目标
--------
1. ``parse_constraints``  把用户的自然语言解析成结构化约束（硬约束 + 软偏好 + 画像）。
2. ``filter_menu``        按硬约束过滤菜单，把解空间先压下来（一票否决）。
3. ``optimize_plans``     多目标寻优（默认 NSGA-II）：四目标帕累托前沿（价格/热量/油炸/蛋白）
                          上取「最省钱 / 最健康 / 最快」极值并按前沿有效多样性自适应补席；加权评分路径保留为回退
                          （method="weighted"）。另有 ``optimize_calorie_budget`` 按热量预算配餐。
4. ``verify_price``       调用 MCP 的 ``calculate-price`` 做官方核价（单位：分 → 元）。
5. ``compare_channels``   渠道比价：同一篮子在 到店自取 / 麦乐送 / 得来速 三个**内部渠道**的
                          「到手成本」对比（商品 + 运费 + 打包费 + 餐具费 − 优惠）。
                          ⚠️ MCP 不提供美团/饿了么等第三方平台接口，外部渠道不做、不编造。

约定
----
* **内部所有金额一律用「分」（int）**，避免浮点误差；仅在输出层转成「元」。
* 不依赖任何第三方库（仅标准库），便于在沙箱内直接运行。
* 常量表（标签关键词、成分同义词）与 ``references/strategy.md`` 保持一致。

命令行自测::

    python optimizer.py                            # 任意输入默认走【实时 MCP 数据】
    python optimizer.py "我们4个人，人均40，一个减脂，一个不吃辣"   # 自定义输入（真实数据）
    python optimizer.py --demo [需求]              # 强制内置样例（显式标注演示数据）
    python optimizer.py --strict [需求]            # MCP 不可用即报错，绝不兜底
    python optimizer.py --verify [需求]            # 最省钱方案加 calculate-price 官方核价
    python optimizer.py --store 1950936 [需求]     # 指定门店（缺省自动选址）
    python optimizer.py --selftest                 # 只跑断言自检
    python optimizer.py --compare-channels         # 渠道比价演示（回放 2026-10-10 实测数据）
    python optimizer.py --json [需求]              # 结构化输出（references/schema.json 契约）
    python optimizer.py --dump-schema              # 输出契约镜像（重定向到 references/schema.json）

数据真实性契约（方向 5.5 全链路真实化）::

    任意用户输入默认触发真实 MCP 调用（query-meals 菜单 / query-store-coupons 券 /
    list-nutrition-foods 营养 / query-nearby-stores 选址）。MCP 不可用时如实报告
    原因；非 --strict 模式才允许回退到内置样例，且输出中强制标注「兜底数据，非实时」。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# 0. 常量表（与 references/strategy.md 同步）
# ---------------------------------------------------------------------------

#: 名称关键词 → 标签
TAG_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "fried": ("薯条", "薯饼", "油条", "鸡排", "麦乐鸡", "鸡翅", "V翅", "脆汁鸡", "趣鸡球", "双翅"),
    "spicy": ("辣", "V翅", "焦香", "椒"),
    "vegetable": ("玉米", "苹果", "沙拉", "蔬", "笋"),
    "drink": ("可乐", "雪碧", "咖啡", "红茶", "牛奶", "豆浆", "果汁", "纯悦",
              "奶铁", "玛奇朵", "卡布奇诺", "雪冰", "阿芙佳朵"),
    "dessert": ("冰淇淋", "新地", "麦旋风", "派", "拉明顿", "奶冻"),
}

#: 主食关键词：拼单场景下「每人至少 1 份主食」的判定依据（汉堡 / 卷 / 早餐堡）
MAIN_KEYWORDS: Tuple[str, ...] = (
    "汉堡", "堡", "卷", "麦满分", "巨无霸", "麦香鱼", "麦香鸡", "炒双蛋", "粥",
)

#: 用户口语 → 标准成分标签（避免"不吃牛 / 牛肉过敏 / 不碰安格斯"漏判）
INGREDIENT_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    "beef": ("牛", "牛肉", "牛排", "安格斯", "牛堡", "巨无霸"),
    "pork": ("猪", "猪肉", "培根", "猪柳", "火腿", "香肠", "图林根"),
    "chicken": ("鸡", "鸡肉", "鸡腿", "鸡翅", "板烧", "麦乐鸡", "脆汁鸡", "趣鸡球"),
    "fish": ("鱼", "鳕鱼", "麦香鱼", "鱼排"),
    "seafood": ("虾", "海鲜"),
    "dairy": ("奶", "牛奶", "芝士", "吉士", "冰淇淋", "奶铁", "奶冻"),
    "egg": ("蛋",),
    "peanut": ("花生", "坚果"),
}

#: 场景关键词 → beType
SCENE_RULES: Tuple[Tuple[str, int], ...] = (
    ("团餐", 6), ("企业", 6), ("拼单", 6),
    ("外送", 2), ("麦乐送", 2), ("送到", 2),
    ("得来速", 5), ("车道", 5),
    ("到店", 1), ("自取", 1), ("自提", 1),
)
SCENE_ORDERTYPE: Dict[int, int] = {1: 1, 5: 1, 2: 2, 6: 2}

#: 减脂档位（单餐上限），对应 strategy.md 第 2 章
DIET_TIERS: Dict[str, Dict[str, float]] = {
    "strict": {"kcal": 500, "fat": 20, "protein": 25, "sodium": 800},
    "standard": {"kcal": 600, "fat": 25, "protein": 20, "sodium": 1000},
    "normal": {"kcal": 750, "fat": 30, "protein": 15, "sodium": 1200},
}

PERSONA_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "减脂": ("减脂", "控卡", "低脂", "低卡", "健身", "热量", "卡路里", "清淡"),
    "忌口": ("不吃", "不能吃", "忌", "过敏", "素食", "戒"),
    "要赠品": ("凑", "赠品", "满减", "优惠", "省钱", "划算", "用券"),
    "预算控": ("预算", "人均", "便宜", "不超过", "控制在"),
}

#: 每份热量下限（保证"吃得饱"）与单份上限参考
KCAL_PER_PERSON_MIN = 350
KCAL_PER_PERSON_MAX = 900

#: MCP 端点
MCP_URL = "https://mcp.mcd.cn/mcp-servers/mcd-mcp"

#: 健壮性参数：单次 MCP 调用超时（秒）、重试次数上限与退避间隔（秒）、总预算（秒）
MCP_TIMEOUT_S = 10
MCP_RETRY_MAX = 3
MCP_BACKOFF_S: Tuple[float, ...] = (1.0, 2.0, 4.0)
MCP_TOTAL_BUDGET_S = 20.0

#: 幂等键有效窗口（秒）：同窗口内同内容下单直接复用已有订单
IDEMPOTENCY_WINDOW_S = 5 * 60

#: 日志目录与文件（logs/ 已在 .gitignore 中排除）
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
LOG_FILE = os.path.join(LOG_DIR, "mcp_calls.log")

#: 脱敏占位符
REDACT_TOKEN = "[TOKEN]"
REDACT_PHONE = "[PHONE]"
REDACT_ADDRESS = "[ADDRESS]"

#: 手机号正则（中国大陆 11 位）
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


# ---------------------------------------------------------------------------
# 1. 约束解析
# ---------------------------------------------------------------------------

def _find_number(text: str, patterns: Sequence[str]) -> Optional[float]:
    """按给定正则顺序找第一个数字。"""
    for p in patterns:
        m = re.search(p, text)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError, IndexError):
                continue
    return None


def _extract_ingredients(text: str) -> List[str]:
    """从文本里抽取被"不吃 / 忌 / 过敏"等否定的成分标签。"""
    found: List[str] = []
    trigger = re.compile(r"(不吃|不能吃|不要|别|忌|过敏|戒|拒绝|讨厌)")
    for m in trigger.finditer(text):
        window = text[m.end(): m.end() + 12]
        for tag, words in INGREDIENT_SYNONYMS.items():
            if any(w in window for w in words) and tag not in found:
                found.append(tag)
    # 过敏是最高优先级：整句命中即视为硬约束
    if "过敏" in text:
        for tag, words in INGREDIENT_SYNONYMS.items():
            if any(w in text for w in words) and tag not in found:
                found.append(tag)
    return found


def _estimate_coupon_availability(coupon: Dict[str, Any]) -> Dict[str, Any]:
    """估算单张券的当前可用性（有真实历史数据 → 用数据；无 → 特征推断）。
    
    Returns
    -------
    dict
        ``{"level": "高"/"中"/"低"/"未知", "reason": str,
           "historical": Optional[float]}`` —— ``historical`` 是 MCP 历史可用率
        （0~1），仅当 MCP 券数据中确实存在该字段时才填充；**绝不编造**。
    """
    # ① MCP 历史可用数据：字段名兼容多种可能的 schema 命名
    for k in ("successRate", "availabilityRate", "historicalSuccessRate",
              "usageRate", "usableRate", "historical"):
        v = coupon.get(k)
        if isinstance(v, (int, float)) and 0 <= v <= 1:
            rate = float(v)
            level = "高" if rate >= 0.75 else ("中" if rate >= 0.4 else "低")
            return {
                "level": level,
                "reason": f"历史可用率 {rate * 100:.0f}%（MCP 实测数据）",
                "historical": rate,
            }
    # MCP 有时返回带百分号的字符串（如 "82%"）
    for k in ("successRate", "availabilityRate", "historicalSuccessRate"):
        v = coupon.get(k)
        if isinstance(v, str) and v.endswith("%"):
            try:
                rate = float(v.rstrip("%")) / 100.0
                if 0 <= rate <= 1:
                    level = "高" if rate >= 0.75 else ("中" if rate >= 0.4 else "低")
                    return {
                        "level": level,
                        "reason": f"历史可用率 {rate * 100:.0f}%（MCP 实测数据）",
                        "historical": rate,
                    }
            except ValueError:
                pass
    # ② 无历史数据 → 基于券特征推断（门槛 / 时段限制 / 门店限制）
    min_spend_yuan = float(coupon.get("minSpend") or 0) / 100.0
    time_restricted = bool(coupon.get("timeRestriction")
                           or coupon.get("timeLimited")
                           or coupon.get("validTimeSlot"))
    store_restricted = bool(coupon.get("storeRestriction")
                            or coupon.get("storeLimited")
                            or (coupon.get("appliesToStores") is not None))
    product_restricted = bool(coupon.get("appliesToProductCodes"))
    if time_restricted or store_restricted:
        return {"level": "低", "reason": "存在时段或门店使用限制，可用性较低",
                "historical": None}
    if min_spend_yuan >= 60:
        return {"level": "低", "reason": f"满 ¥{min_spend_yuan:.0f} 才可用，门槛较高",
                "historical": None}
    if product_restricted:
        return {"level": "中", "reason": "仅限指定商品可用，需确认篮子是否包含目标商品",
                "historical": None}
    if min_spend_yuan >= 30:
        return {"level": "中", "reason": f"满 ¥{min_spend_yuan:.0f} 可用，门槛适中",
                "historical": None}
    return {"level": "高", "reason": "门槛低且无额外限制，可用性高",
            "historical": None}


def _estimate_coupons_availability(coupons: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """批量估算券组合的当前可用性（Task 2）。
    
    输入：从 ``_coupon_dp`` 选出的券组合列表（已应用）。
    输出：``{"overall": "高"/"中"/"低"/"未知", "coupons": [...]}``，
    overall 取组合中最低可用性。
    无券时返回 None。
    """
    if not coupons:
        return None
    levels = {"高": 3, "中": 2, "低": 1, "未知": 0}
    details = []
    for c in coupons:
        avail = _estimate_coupon_availability(c)
        details.append({
            "couponId": c.get("couponId"),
            "name": c.get("name"),
            "level": avail["level"],
            "reason": avail["reason"],
        })
    overall = min(details, key=lambda d: levels.get(d["level"], 0))["level"]
    return {"overall": overall, "coupons": details}


def _generate_degraded_plans(
    items: List[Dict[str, Any]],
    people: int,
    max_budget_cny: float,
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """预算不足时生成降级方案（Task 3：极端预算处理）。

    策略：
    1. **减配方案**：按价格升序贪心选择，生成预算内最便宜的可行篮子。
       若主食不足，明确标注差距并建议调整预算或分批下单。
    2. 若预算连最便宜的商品都买不起，生成一个说明性的空方案，
       明确报告预算差距，不崩溃。

    Parameters
    ----------
    items : 已归一化的菜单列表（价格单位为分）
    people : 用餐人数
    max_budget_cny : 有效预算上限（元）
    coupons : 可用券列表
    constraints : 结构化约束

    Returns
    -------
    list[dict]
        降级方案列表（含 degraded=True 与 degrade_reason 说明），
        或预算不足的说明方案。
    """
    max_budget_cents = int(round(max_budget_cny * 100))

    # --- 减配方案：贪心选择最便宜商品填满预算 ---
    # 按 (价格, 原始索引) 排序，保留原始索引以便 _basket_to_plan 查找
    sorted_with_idx = sorted(
        [(items[i]["price"], i) for i in range(len(items))],
        key=lambda x: x[0]
    )

    basket: List[int] = []
    total_cost_cents = 0
    mains_count = 0

    for price, orig_idx in sorted_with_idx:
        if total_cost_cents + price <= max_budget_cents:
            basket.append(orig_idx)
            total_cost_cents += price
            if "main" in (items[orig_idx].get("tags") or []):
                mains_count += 1
        else:
            break  # 已排序，后续更贵，直接停止

    if basket:
        # 构建减配方案（真实篮子，在预算内）
        plan = _basket_to_plan(basket, items, coupons, constraints, "减配方案")
        plan["degraded"] = True

        # 明确说明降级原因
        if mains_count < people:
            cheapest_main_price = min(
                (items[i]["price"] for i in range(len(items))
                 if "main" in (items[i].get("tags") or [])),
                default=0
            ) / 100.0
            gap = people - mains_count
            plan["degrade_reason"] = (
                f"当前预算 ¥{max_budget_cny:.2f} 下，最优方案超出预算，"
                f"已自动减配为预算内组合（¥{total_cost_cents / 100.0:.2f}）。"
                f"⚠️ 主食 {mains_count} 份 < {people} 人（差 {gap} 份）。"
                f"建议：1) 调整预算至 ¥{cheapest_main_price * people:.2f} 以上；"
                f"2) 减少用餐人数；"
                f"3) 分批下单（每批控制在预算内）。"
            )
        else:
            plan["degrade_reason"] = (
                f"当前预算 ¥{max_budget_cny:.2f} 下，最优方案超出预算，"
                f"已自动减配为预算内组合（¥{total_cost_cents / 100.0:.2f}），"
                f"主食 {mains_count} 份已满足 {people} 人。"
            )

        # 若预算充足且人数 ≥ 2，追加「分步下单方案」建议
        # （分两批可分别凑满减门槛，可能更划算）
        if people >= 2 and max_budget_cny / people >= 15:
            half_people = people // 2
            half_budget = max_budget_cny / 2
            split_plan = {
                "label": "分步下单方案",
                "degraded": True,
                "degrade_reason": (
                    f"若不想减配，建议分两批下单："
                    f"第一批 {half_people} 人（预算 ¥{half_budget:.2f}），"
                    f"第二批 {people - half_people} 人（预算 ¥{max_budget_cny - half_budget:.2f}）。"
                    f"两批可分别使用满减券，可能比一次性下单更划算。"
                ),
                "items": [],
                "coupons": [],
                "subtotal_cny": 0,
                "discount_cny": 0,
                "final_price_cny": 0,
                "per_capita_cny": 0,
                "people_count": people,
                "mains": 0,
                "units": 0,
                "nutrition": {},
                "nutrition_flags": ["建议方案：分批下单，详见降级说明"],
                "coupon_availability": None,
            }
            return [plan, split_plan]
        return [plan]
    else:
        # 预算极低，连最便宜的商品都买不起
        cheapest_price = sorted_with_idx[0][0] if sorted_with_idx else 0
        cheapest_name = items[sorted_with_idx[0][1]]["name"] if sorted_with_idx else "无商品"
        gap_cny = (cheapest_price - max_budget_cents) / 100.0
        return [{
            "label": "预算不足",
            "degraded": True,
            "degrade_reason": (
                f"当前预算 ¥{max_budget_cny:.2f} 过低，"
                f"最便宜的商品「{cheapest_name}」需 ¥{cheapest_price / 100.0:.2f}，"
                f"超出预算 ¥{gap_cny:.2f}。"
                f"建议：1) 调整预算至 ¥{cheapest_price / 100.0:.2f} 以上；"
                f"2) 减少用餐人数。"
            ),
            "items": [],
            "coupons": [],
            "subtotal_cny": 0,
            "discount_cny": 0,
            "final_price_cny": 0,
            "per_capita_cny": 0,
            "people_count": people,
            "mains": 0,
            "units": 0,
            "nutrition": {},
            "nutrition_flags": ["预算不足，无法生成可行方案"],
            "coupon_availability": None,
        }]

def parse_constraints(user_input: str) -> Dict[str, Any]:
    """把用户自然语言解析为结构化约束。

    Returns
    -------
    dict
        含 ``people_count / scene / beType / orderType / budget / hard / soft /
        targets / personas / weights`` 等字段。
    """
    text = (user_input or "").strip()

    # --- 人数 -------------------------------------------------------------
    people = _find_number(text, [
        r"(\d+)\s*(?:个|位)?\s*(?:人|位)",   # 「4个人」「4人」「4位」都命中
        r"我们\s*(\d+)",
        r"(\d+)\s*份",
    ])
    people_count = int(people) if people and people > 0 else 1

    # --- 场景 -------------------------------------------------------------
    scene: Optional[str] = None
    be_type: Optional[int] = None
    for kw, bt in SCENE_RULES:
        if kw in text:
            scene, be_type = kw, bt
            break
    order_type = SCENE_ORDERTYPE.get(be_type) if be_type else None

    # --- 预算 -------------------------------------------------------------
    # 总预算上限（如「总预算100」「预算100」「控制在100」）
    total = _find_number(
        text,
        [r"(?:总预算|总价|一共|总共|控制在)\s*(\d+(?:\.\d+)?)", r"预算\s*(\d+(?:\.\d+)?)"],
    )
    # 每人预算上限（如「人均30」「每人不超过30」「每人最多25」「人均最多50」）
    # 可与总预算同时存在，两者同时作为硬约束生效
    per_person = _find_number(text, [
        r"每人\s*(?:不超过|最多|上限|至多|花)\s*(\d+(?:\.\d+)?)",
        r"人均\s*(?:不超过|最多|上限|至多|花)\s*(\d+(?:\.\d+)?)",
        r"人均\s*(\d+(?:\.\d+)?)",
        r"平均\s*(\d+(?:\.\d+)?)",
    ])
    if per_person is not None or total is not None:
        # 优先记录总额；无总额但有每人上限时以 per_capita 类型记录
        if total is not None:
            budget = {
                "type": "total",
                "amount_cny": total,
                "total_cny": total,
                "per_person_cny": per_person,
            }
        else:
            budget = {
                "type": "per_capita",
                "amount_cny": per_person,
                "total_cny": None,
                "per_person_cny": per_person,
            }
    else:
        budget = {
            "type": "total",
            "amount_cny": None,
            "total_cny": None,
            "per_person_cny": None,
        }

    # --- 硬约束 -----------------------------------------------------------
    exclude_ingredients = _extract_ingredients(text)
    exclude_tags: List[str] = []
    if re.search(r"(不吃|不能吃|忌|不要|别)\s*辣", text):
        exclude_tags.append("spicy")
    if re.search(r"(不吃|不能吃|忌|不要|别)\s*(油炸|炸的|煎炸)", text):
        exclude_tags.append("fried")

    max_per_capita = per_person
    max_single = _find_number(
        text, [r"不点超过\s*(\d+(?:\.\d+)?)", r"单品不超过\s*(\d+(?:\.\d+)?)"]
    )

    # --- 软偏好 -----------------------------------------------------------
    prefer_tags: List[str] = []
    avoid_tags: List[str] = []
    if any(k in text for k in ("减脂", "控卡", "低脂", "低卡", "健身", "清淡")):
        prefer_tags += ["high_protein", "low_calorie"]
        avoid_tags.append("fried")
    if any(k in text for k in ("凑", "满减", "赠品", "划算", "优惠")):
        prefer_tags.append("coupon_friendly")
    if "辣" in text and "spicy" not in exclude_tags:
        prefer_tags.append("spicy")
    if any(k in text for k in ("快", "赶时间", "着急")):
        prefer_tags.append("fast")

    # --- 营养目标 ---------------------------------------------------------
    targets: Dict[str, Any] = {}
    tier: Optional[str] = None
    if re.search(r"(严格|严一点|狠一点)", text):
        tier = "strict"
    elif any(k in text for k in ("减脂", "控卡", "低脂", "低卡", "健身")):
        tier = "standard"
    if tier:
        # ⚠️ 必须输出与消费方一致的键名（max_calories_kcal / max_fat_g / min_protein_g），
        #    否则档位阈值会被静默忽略（历史 bug：此处曾用 kcal/fat/protein 导致约束失效）。
        t = DIET_TIERS[tier]
        targets = {
            "max_calories_kcal": t["kcal"],
            "max_fat_g": t["fat"],
            "min_protein_g": t["protein"],
            "max_sodium_mg": t["sodium"],
            "tier": tier,
        }
    # 热量预算（扩展分支 D）：
    #   「每人500大卡以内」→ 人均硬上限；「总热量2000大卡」→ 按人数换算为人均；
    #   其余「500大卡」类表达沿用既有语义（人均上限）。
    kcal_pp = _find_number(text, [
        r"(?:每人|人均|平均|一个人)\s*(?:不超过|最多|至多|上限|控制在)?\s*(\d+(?:\.\d+)?)\s*(?:大卡|千卡|kcal|卡)",
    ])
    kcal_total = _find_number(text, [
        r"(?:总热量|总卡路里|总共|一共)\s*(?:不超过|最多|至多)?\s*(\d+(?:\.\d+)?)\s*(?:大卡|千卡|kcal|卡)",
    ])
    if kcal_pp:
        kcal_cap = kcal_pp
    elif kcal_total:
        kcal_cap = kcal_total / max(people_count, 1)
    else:
        kcal_cap = _find_number(text, [r"(\d+(?:\.\d+)?)\s*(?:大卡|千卡|kcal|卡)"])
    if kcal_cap:
        targets["max_calories_kcal"] = kcal_cap
        targets["kcal_scope"] = "per_person"
        targets.setdefault("tier", "custom")
    fat_cap = _find_number(text, [r"脂肪\s*(?:不超过|低于|少于)?\s*(\d+)", r"(\d+)\s*g?\s*脂肪"])
    if fat_cap:
        targets["max_fat_g"] = fat_cap

    # --- 画像与权重 -------------------------------------------------------
    personas: List[str] = []
    for persona, kws in PERSONA_KEYWORDS.items():
        if any(k in text for k in kws):
            personas.append(persona)

    weights = {"cheap": 0.34, "healthy": 0.33, "speed": 0.18, "pref": 0.15}
    if "预算控" in personas:
        weights.update(cheap=0.55, healthy=0.15, speed=0.10, pref=0.20)
    if "减脂" in personas:
        weights.update(cheap=0.20, healthy=0.55, speed=0.10, pref=0.15)
    if "要赠品" in personas:
        weights.update(cheap=0.50, healthy=0.15, speed=0.10, pref=0.25)

    return {
        "raw": text,
        "people_count": people_count,
        "scene": scene,
        "beType": be_type,
        "orderType": order_type,
        "budget": budget,
        "hard": {
            "exclude_ingredients": sorted(set(exclude_ingredients)),
            "exclude_tags": sorted(set(exclude_tags)),
            "max_per_capita_cny": max_per_capita,
            "max_single_item_cny": max_single,
        },
        "soft": {
            "prefer_tags": sorted(set(prefer_tags)),
            "avoid_tags": sorted(set(avoid_tags)),
        },
        "targets": targets,
        "personas": personas,
        "weights": weights,
    }


# ---------------------------------------------------------------------------
# 1.1 模糊输入处理（三层询问机制）
#
#     目标：用户说「随便」「都行」「你看着办」时，不瞎猜、不卡死。
#
#     第一层（静默默认）：常识默认值直接填充，不打扰用户
#         - 取餐方式缺失   → 默认「到店自取」(beType=1)
#     第二层（聚焦追问）：必须用户决定的事项，≤3 个关键问题**一次性**追问
#         - 人数缺失       → 追问（无合理默认值）
#         - 预算缺失       → 默认「不限预算」，但追问确认
#         - 口味完全空白   → 默认「不辣、不油炸」，但追问确认
#     第三层（你看着办）：用户明确放弃决策权 → 全部走默认值，
#         输出方案时必须标注「基于默认偏好：不辣、不油炸」
#
#     ``ready`` 字段告诉调用方：False = 先把 ``questions`` 抛给用户、拿到答案后
#     经 ``answers`` 参数合并重跑；True = 约束完整，可直接进入过滤与优化。
# ---------------------------------------------------------------------------

#: 第三层触发词（用户明确把决策权交给系统）
YOU_DECIDE_PATTERNS: Tuple[str, ...] = (
    "随便", "都行", "都可以", "无所谓", "你看着办", "看着办", "你决定", "随你", "你定", "你选",
)

#: 口味完全空白时的默认口味（保守取向：宁可少选项，不冒犯用户）
DEFAULT_TASTE_EXCLUDE_TAGS: Tuple[str, ...] = ("spicy", "fried")

#: 第三层标注文案（固定，供渲染层展示）
CLARIFY_BANNER = "基于默认偏好：不辣、不油炸"

#: 人数是否在文本中有明确表达
_PEOPLE_RE = re.compile(r"(\d+)\s*(?:人|位)|我们\s*(\d+)|(\d+)\s*份")


def _taste_blank(constraints: Dict[str, Any]) -> bool:
    """口味偏好是否完全空白（无忌口、无偏好、无避免项、无营养目标）。"""
    hard, soft = constraints.get("hard") or {}, constraints.get("soft") or {}
    return not (hard.get("exclude_ingredients")
                or hard.get("exclude_tags")
                or soft.get("prefer_tags")
                or soft.get("avoid_tags")
                or constraints.get("targets"))


def clarify_input(
    user_input: str,
    answers: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """分析输入中的模糊点，产出三层询问机制的决策结果。

    Parameters
    ----------
    user_input : str
        用户原始输入（自然语言）。
    answers : dict, 可选
        第二轮合并参数：上一轮 ``questions`` 的用户回答，支持键
        ``people_count``（int）/ ``budget``（数值；或含数字的字符串，如
        ``"100"``、``"预算100元"``；含「每人/人均/平均」字样时按人均口径
        记录；None=不限）/ ``taste``（自然语言，
        如「无辣不欢」「不吃香菜」，走完整同义词归一）。

    Returns
    -------
    dict
        ``{input, you_decide, layer, ambiguities, questions, defaults_applied,
        banner, ready, constraints}``。``constraints`` 已应用默认值，
        可直接喂给 ``filter_menu`` / ``optimize_plans``（``ready=True`` 时）。
    """
    text = (user_input or "").strip()
    answers = dict(answers or {})
    c = parse_constraints(text)
    defaults_applied: List[str] = []

    # --- 第二轮：合并上一轮追问的回答 ------------------------------------
    accept_default_taste = False
    if answers:
        if answers.get("people_count"):
            c["people_count"] = int(answers["people_count"])
        if "budget" in answers and answers.get("budget") is not None:
            _raw = answers["budget"]
            _is_pp = isinstance(_raw, str) and re.search(r"每人|人均|平均", _raw) is not None
            try:
                if isinstance(_raw, str):
                    _amount = float(re.search(r"\d+(?:\.\d+)?", _raw).group())
                else:
                    _amount = float(_raw)
            except (TypeError, ValueError, AttributeError):
                pass  # 非数值（如「不限」）→ 维持不限预算
            else:
                # canonical 结构与 parse_constraints 一致（L441-454）：
                # 下游预算硬约束读 total_cny / per_person_cny（缺键即静默失效）。
                # 注意：总预算 ≠ 人均上限，不再写入 hard.max_per_capita_cny（语义错位）。
                if _is_pp:
                    c["budget"] = {"type": "per_capita", "amount_cny": _amount,
                                   "total_cny": None, "per_person_cny": _amount}
                else:
                    c["budget"] = {"type": "total", "amount_cny": _amount,
                                   "total_cny": _amount, "per_person_cny": None}
        taste_answer = str(answers.get("taste") or "").strip()
        if taste_answer and not any(p in taste_answer for p in YOU_DECIDE_PATTERNS):
            # 口味回答走完整解析（同义词归一/档位识别），只取偏好相关字段
            rp = parse_constraints(taste_answer)
            c["hard"]["exclude_ingredients"] = sorted(set(
                (c["hard"]["exclude_ingredients"] or [])
                + (rp["hard"]["exclude_ingredients"] or [])))
            c["hard"]["exclude_tags"] = sorted(set(
                (c["hard"]["exclude_tags"] or []) + (rp["hard"]["exclude_tags"] or [])))
            c["soft"]["prefer_tags"] = sorted(set(
                (c["soft"]["prefer_tags"] or []) + (rp["soft"]["prefer_tags"] or [])))
            c["soft"]["avoid_tags"] = sorted(set(
                (c["soft"]["avoid_tags"] or []) + (rp["soft"]["avoid_tags"] or [])))
        elif "taste" in answers:
            # 显式空回答 / 「随便都行」类回答 = 接受默认口味
            accept_default_taste = True

    # --- 模糊点检测 -------------------------------------------------------
    people_stated = bool(_PEOPLE_RE.search(text))
    people_missing = not people_stated and "people_count" not in answers
    budget_missing = (c["budget"].get("amount_cny") is None) and "budget" not in answers
    scene_missing = c["scene"] is None
    taste_missing = _taste_blank(c)

    # --- 第三层判定：用户明确说「你看着办」 --------------------------------
    you_decide = any(p in text for p in YOU_DECIDE_PATTERNS)

    # --- 第一层：常识默认值（静默填充，不打扰） ----------------------------
    if scene_missing:
        c["scene"] = "到店自取"
        c["beType"] = 1
        c["orderType"] = 1
        defaults_applied.append("取餐方式：到店自取")
    if you_decide and people_missing:
        # 仅第三层才敢默认人数（保守 1 人）；第二层路径必须追问
        defaults_applied.append("人数：默认 1 人")
    if you_decide and budget_missing:
        defaults_applied.append("预算：不限")
    if (you_decide and taste_missing) or (taste_missing and accept_default_taste):
        # 口味空白 → 默认「不辣、不油炸」。
        # you_decide 路径：默认即为最终值；第二层路径：先填默认，同时追问确认。
        c["hard"]["exclude_tags"] = sorted(set(
            c["hard"]["exclude_tags"] + list(DEFAULT_TASTE_EXCLUDE_TAGS)))
        defaults_applied.append("口味：不辣、不油炸")
    elif taste_missing and "taste" not in answers and not you_decide:
        # 第二层路径且用户尚未回答口味 → 同样先填默认（追问后可推翻）
        c["hard"]["exclude_tags"] = sorted(set(
            c["hard"]["exclude_tags"] + list(DEFAULT_TASTE_EXCLUDE_TAGS)))
        defaults_applied.append("口味：不辣、不油炸（待确认）")

    # --- 第二层：聚焦追问（≤3 个关键问题，一次性抛出） ----------------------
    questions: List[str] = []
    if not you_decide:
        if people_missing:
            questions.append("几位一起吃？")
        if budget_missing:
            questions.append("预算大概多少？（不限也行，默认按不限处理）")
        if taste_missing and "taste" not in answers:
            questions.append("有任何忌口或口味偏好吗？没有的话默认按「不辣、不油炸」搭配")
        questions = questions[:3]

    banner = CLARIFY_BANNER if you_decide else None
    layer = 3 if you_decide else (2 if questions else 1)

    ambiguities = {
        "people_count": {"missing": people_missing,
                          "default": 1 if people_missing else None,
                          "ask": people_missing and not you_decide},
        "budget": {"missing": budget_missing,
                    "default": "不限预算" if budget_missing else None,
                    "ask": budget_missing and not you_decide},
        "scene": {"missing": scene_missing,
                   "default": "到店自取" if scene_missing else None,
                   "ask": False},  # 第一层：静默默认，永不追问
        "taste": {"missing": taste_missing,
                   "default": "不辣、不油炸" if taste_missing else None,
                   "ask": taste_missing and not you_decide},
    }

    return {
        "input": text,
        "you_decide": you_decide,
        "layer": layer,
        "ambiguities": ambiguities,
        "questions": questions,
        "defaults_applied": defaults_applied,
        "banner": banner,
        "ready": not questions,       # False = 先追问再优化
        "constraints": c,
    }


# ---------------------------------------------------------------------------
# 1.2 群体决策公平性（偏好聚合 + 公平性评分）
#
#     目标：多人偏好冲突时，输出让各方都能接受的方案，而不是只满足多数人。
#
#     ① 硬约束「交集」= 禁忌并集排除：A 不吃辣、B 不吃牛肉 → 菜单同时排除辣和牛肉
#        （每个人「能吃集合」的交集 = 全员禁忌并集的补集；一票否决，不可被多数推翻）
#     ② 软约束自适应截尾均值：口味强度排序后去掉两端各 n//4 个极端值（n≥4 时），
#        取中间均值。例：很辣4/中辣2/不辣0/不辣0 → 截尾后 [0,2] → 1.0（微辣以下）
#     ③ 严重冲突检测：口味维度两阵营（强度≥3 vs ≤1）各达投票人数 1/3 以上且
#        阵营均值差 ≥3 → 标记「需协商」，输出 A/B 两套备选约束（各自妥协点显式说明）
#     ④ 公平性评分：每成员 0-1 满足度（口味接近度 0.5 + 偏好命中 0.3 + 避免命中 0.2；
#        硬约束被违反直接 0 分），整体取最低分（木桶效应）
# ---------------------------------------------------------------------------

#: 口味维度（当前实际使用 spicy；结构支持任意维度扩展）
TASTE_DIMENSIONS: Tuple[str, ...] = ("spicy",)

#: 口味强度标尺：0 不辣 … 5 变态辣
TASTE_SCALE_MAX = 5

#: 自然语言 → 口味强度（顺序敏感：否定式必须先于泛化式，避免「不吃辣」误判为「吃辣」；
#: 「不怕辣」为肯定式，靠 (?<!不) 负向后行断言从否定式中排除，落入末条正向规则）
_TASTE_RULES: Tuple[Tuple[str, int], ...] = (
    (r"不吃辣|不能吃辣|忌辣|不要辣|别放辣|(?<!不)怕辣|不辣", 0),
    (r"变态辣|特辣|非常辣|无辣不欢", 5),
    (r"很辣|超辣|重辣|越辣越好", 4),
    (r"中辣", 3),
    (r"微辣|小辣|一点点辣", 2),
    (r"不怕辣|相当能吃辣|能吃辣|喜欢辣|爱吃辣|要辣|吃辣", 3),
)

#: 中文词 → 标准标签（由 INGREDIENT_SYNONYMS + 常用口味词构建）
_CN2TAG: Dict[str, str] = {}
for _tag, _words in list(INGREDIENT_SYNONYMS.items()) + [
    ("spicy", ("辣", "辣的", "麻辣", "香辣")), ("fried", ("油炸", "炸的", "煎炸")),
]:
    for _w in _words:
        _CN2TAG.setdefault(_w, _tag)


def _norm_tags(vals: Iterable[str]) -> List[str]:
    """把中文口味/成分词归一为标准标签（辣→spicy、牛肉→beef…），去重排序。"""
    out: set = set()
    for v in vals or []:
        v = str(v).strip()
        if not v:
            continue
        out.add(_CN2TAG.get(v, v))
    return sorted(out)


def _infer_taste(text: str) -> Dict[str, int]:
    """从自然语言推断口味强度（只推断有明确信号的维度）。"""
    taste: Dict[str, int] = {}
    if re.search(_TASTE_RULES[0][0], text):        # 否定式优先
        taste["spicy"] = 0
        return taste
    for pat, lv in _TASTE_RULES[1:]:
        if re.search(pat, text):
            taste["spicy"] = lv
            break
    return taste


def _member_name(text: str, idx: int) -> str:
    """「小李：不吃辣」→ 小李；无前缀 → 成员N。"""
    m = re.match(r"^\s*([\u4e00-\u9fa5A-Za-z0-9_]{1,12})\s*[:：]", text or "")
    return m.group(1) if m else f"成员{idx}"


def _normalize_member(member: Any, idx: int) -> Dict[str, Any]:
    """成员输入归一：支持 dict（结构化）或 str（自然语言，走 parse_constraints）。"""
    if isinstance(member, str):
        c = parse_constraints(member)
        hard, soft = c["hard"], c["soft"]
        return {
            "name": _member_name(member, idx),
            "exclude_ingredients": sorted(set(hard["exclude_ingredients"] or [])),
            "exclude_tags": sorted(set(hard["exclude_tags"] or [])),
            "prefer_tags": sorted(set(soft["prefer_tags"] or [])),
            "avoid_tags": sorted(set(soft["avoid_tags"] or [])),
            "taste": _infer_taste(member),
            "weight": c.get("weights") or {},
            "budget_cny": (c.get("budget") or {}).get("amount_cny"),
            "max_per_capita_cny": hard.get("max_per_capita_cny"),
            "targets": c.get("targets") or {},
        }
    m = dict(member or {})
    return {
        "name": str(m.get("name") or f"成员{idx}"),
        "exclude_ingredients": _norm_tags(m.get("exclude_ingredients") or []),
        "exclude_tags": _norm_tags(m.get("exclude_tags") or []),
        "prefer_tags": _norm_tags(m.get("prefer_tags") or []),
        "avoid_tags": _norm_tags(m.get("avoid_tags") or []),
        "taste": {str(k): int(v) for k, v in (m.get("taste") or {}).items()
                  if v is not None},
        "weight": m.get("weight") or {},
        "budget_cny": m.get("budget_cny"),
        "max_per_capita_cny": m.get("max_per_capita_cny"),
        "targets": m.get("targets") or {},
    }


def _trimmed_mean(values: Sequence[float]) -> Optional[float]:
    """自适应截尾均值：n≥4 时两端各去 ``n//4`` 个，n<4 不截尾。"""
    n = len(values)
    if n == 0:
        return None
    s = sorted(values)
    k = n // 4 if n >= 4 else 0
    core = s[k: n - k] if k else s
    return round(sum(core) / len(core), 2)


def _detect_taste_conflicts(members: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """检测口味维度的严重对立（无法用截尾均值调和的）。"""
    conflicts: List[Dict[str, Any]] = []
    for dim in TASTE_DIMENSIONS:
        voted = [(m["name"], m["taste"][dim]) for m in members
                 if dim in (m.get("taste") or {})]
        if len(voted) < 2:
            continue
        hi = [(nm, lv) for nm, lv in voted if lv >= 3]
        lo = [(nm, lv) for nm, lv in voted if lv <= 1]
        if not hi or not lo:
            continue
        quota = max(1, (len(voted) + 2) // 3)      # ⌈n/3⌉
        if len(hi) < quota or len(lo) < quota:
            continue
        mean_hi = sum(lv for _, lv in hi) / len(hi)
        mean_lo = sum(lv for _, lv in lo) / len(lo)
        if mean_hi - mean_lo < 3:
            continue
        conflicts.append({
            "dimension": dim,
            "camp_a": {"members": [nm for nm, _ in hi], "mean_level": round(mean_hi, 2),
                        "stance": "要辣"},
            "camp_b": {"members": [nm for nm, _ in lo], "mean_level": round(mean_lo, 2),
                        "stance": "不要辣"},
            "gap": round(mean_hi - mean_lo, 2),
        })
    return conflicts


def _merge_targets(targets_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    """营养目标聚合：max_* 取最小（最严），min_* 取最大（最严）。"""
    merged: Dict[str, Any] = {}
    for t in targets_list:
        if not t:
            continue
        for k, v in t.items():
            if not isinstance(v, (int, float)) or v is None:
                merged.setdefault(k, v)
                continue
            if k.startswith("max_"):
                merged[k] = min(merged.get(k, v), v)
            elif k.startswith("min_"):
                merged[k] = max(merged.get(k, v), v)
            else:
                merged.setdefault(k, v)
    return merged


def aggregate_preferences(members: List[Any]) -> Dict[str, Any]:
    """多人偏好聚合：硬约束取交集（禁忌并集排除）、软约束自适应截尾均值、
    严重冲突检测（标记「需协商」并给出 A/B 两套备选约束）。

    Parameters
    ----------
    members : list
        每项可为 dict（结构化，键 name/exclude_ingredients/exclude_tags/
        prefer_tags/avoid_tags/taste{spicy:0-5}/weight/budget_cny/targets）
        或 str（自然语言，如 ``"小李：不吃辣，减脂"``，自动解析+口味强度推断）。

    Returns
    -------
    dict
        ``{people_count, members_resolved, hard, soft, taste, taste_detail,
        weights, budget, targets, conflicts, needs_negotiation,
        alternatives, constraints, fairness_note}``。``constraints``
        与 ``filter_menu`` / ``optimize_plans`` 直接兼容。
    """
    resolved = [_normalize_member(m, i) for i, m in enumerate(members or [])]
    n = max(len(resolved), 1)

    # ① 硬约束：全员禁忌并集（一票否决，不可被多数推翻）
    exclude_ingredients = sorted({x for m in resolved for x in m["exclude_ingredients"]})
    exclude_tags = sorted({x for m in resolved for x in m["exclude_tags"]})

    # ② 软偏好：prefer 取多数（≥半数成员提到才进聚合偏好）；avoid 取并集（更保守）
    prefer_counts: Dict[str, int] = {}
    for m in resolved:
        for t in m["prefer_tags"]:
            prefer_counts[t] = prefer_counts.get(t, 0) + 1
    prefer_tags = sorted(t for t, cnt in prefer_counts.items() if cnt * 2 >= len(resolved))
    avoid_tags = sorted({x for m in resolved for x in m["avoid_tags"]})

    # 口味：截尾均值 + 明细（供公平性评分用）
    taste: Dict[str, float] = {}
    taste_detail: Dict[str, List[Tuple[str, int]]] = {}
    for dim in TASTE_DIMENSIONS:
        voted = [(m["name"], m["taste"][dim]) for m in resolved if dim in m["taste"]]
        if voted:
            tm = _trimmed_mean([lv for _, lv in voted])
            if tm is not None:
                taste[dim] = tm
            taste_detail[dim] = voted

    # 权重：成员均值；预算：取最严（最小）
    weights: Dict[str, float] = {}
    for key in ("cheap", "healthy", "speed", "pref"):
        vals = [float(m["weight"][key]) for m in resolved
                if isinstance(m.get("weight"), dict) and m["weight"].get(key) is not None]
        if vals:
            weights[key] = round(sum(vals) / len(vals), 4)
    if not weights:
        weights = {"cheap": 0.34, "healthy": 0.33, "speed": 0.18, "pref": 0.15}

    budgets = [float(m["budget_cny"]) for m in resolved if m.get("budget_cny")]
    per_caps = [float(m["max_per_capita_cny"]) for m in resolved if m.get("max_per_capita_cny")]
    budget_amount = min(budgets) if budgets else None
    max_per_capita = min(per_caps) if per_caps else (min(budgets) / n if budgets else None)
    targets = _merge_targets([m["targets"] for m in resolved])

    # ③ 严重冲突检测
    conflicts = _detect_taste_conflicts(resolved)

    def _base_constraints(**override: Any) -> Dict[str, Any]:
        c: Dict[str, Any] = {
            "people_count": len(resolved) or 1,
            "scene": None, "beType": None, "orderType": None,
            "budget": {"type": "total", "amount_cny": budget_amount},
            "hard": {
                "exclude_ingredients": list(exclude_ingredients),
                "exclude_tags": list(exclude_tags),
                "max_per_capita_cny": max_per_capita,
                "max_single_item_cny": None,
            },
            "soft": {"prefer_tags": list(prefer_tags), "avoid_tags": list(avoid_tags)},
            "targets": dict(targets),
            "personas": ["群体聚合"],
            "weights": dict(weights),
            "taste": dict(taste),
        }
        c.update(override)
        return c

    constraints = _base_constraints()

    # 冲突 → 两套备选（各自妥协点显式说明）
    alternatives: List[Dict[str, Any]] = []
    for cf in conflicts:
        dim = cf["dimension"]
        # 方案 A：满足「要辣」阵营 —— 口味维度不排除、计入偏好
        ca = _base_constraints()
        ca["hard"]["exclude_tags"] = [t for t in ca["hard"]["exclude_tags"] if t != dim]
        ca["soft"]["prefer_tags"] = sorted(set(ca["soft"]["prefer_tags"]) | {dim})
        ca["taste"][dim] = cf["camp_a"]["mean_level"]
        alternatives.append({
            "label": f"方案A（{cf['camp_a']['stance']}组）",
            "serves": cf["camp_a"]["members"],
            "constraints": ca,
            "compromise": (f"{cf['camp_b']['stance']}的成员（{'、'.join(cf['camp_b']['members'])}）"
                           f"需接受含{dim}菜品，可单独去掉酱料或换不辣单品"),
        })
        # 方案 B：满足「不要辣」阵营 —— 口味维度升级为硬排除
        cb = _base_constraints()
        cb["hard"]["exclude_tags"] = sorted(set(cb["hard"]["exclude_tags"]) | {dim})
        cb["taste"].pop(dim, None)
        alternatives.append({
            "label": f"方案B（{cf['camp_b']['stance']}组）",
            "serves": cf["camp_b"]["members"],
            "constraints": cb,
            "compromise": (f"{cf['camp_a']['stance']}的成员（{'、'.join(cf['camp_a']['members'])}）"
                           f"需接受全单无{dim}，可自备辣酱"),
        })

    return {
        "people_count": len(resolved),
        "members_resolved": resolved,
        "hard": constraints["hard"],
        "soft": constraints["soft"],
        "taste": taste,
        "taste_detail": taste_detail,
        "weights": weights,
        "budget": constraints["budget"],
        "targets": targets,
        "conflicts": conflicts,
        "needs_negotiation": bool(conflicts),
        "alternatives": alternatives,
        "constraints": constraints,
        "fairness_note": "整体公平性取最低成员分（木桶效应）",
    }


def _plan_taste_level(plan: Dict[str, Any], dim: str) -> int:
    """方案在口味维度上的代表强度：含该标签菜品 → 3（中辣档），否则 0。"""
    if dim == "spicy":
        has = any(dim in (ln.get("tags") or []) for ln in plan.get("items") or [])
        return 3 if has else 0
    return 3 if any(dim in (ln.get("tags") or []) for ln in plan.get("items") or []) else 0


def fairness_score(plan: Dict[str, Any], aggregated: Dict[str, Any]) -> Dict[str, Any]:
    """计算方案对每个成员的满足度（0-1）与整体公平性（取最低分，木桶效应）。

    成员满足度构成：
    * 硬约束被违反（成员个人禁忌出现在方案里）→ 直接 0；
    * 表达过口味偏好：口味接近度 ``1-|方案强度-成员强度|/5``（权重 0.5）
      + 偏好命中率（0.3）+ 避免命中率（0.2）；
    * 未表达口味：偏好命中 0.6 + 避免命中 0.4。
    """
    members = aggregated.get("members_resolved") or []
    per_member: List[Dict[str, Any]] = []
    for m in members:
        item_tags: set = set()
        item_names = []
        for ln in plan.get("items") or []:
            item_tags.update(ln.get("tags") or [])
            item_names.append(ln.get("name") or "")
        names_text = " ".join(item_names)

        # 硬约束门（成员个人禁忌）
        hard_violations = sorted(
            (set(m["exclude_tags"]) & item_tags)
            | {t for t in m["exclude_ingredients"]
               if any(k in names_text for k in INGREDIENT_SYNONYMS.get(t, (t,)))}
        )
        if hard_violations:
            per_member.append({"name": m["name"], "score": 0.0,
                                "reason": f"硬约束被违反：{hard_violations}"})
            continue

        # 口味接近度
        taste_sat: Optional[float] = None
        for dim, my_lv in (m.get("taste") or {}).items():
            plan_lv = _plan_taste_level(plan, dim)
            taste_sat = max(0.0, 1.0 - abs(plan_lv - my_lv) / TASTE_SCALE_MAX)

        # 偏好命中 / 避免命中
        prefs = set(m["prefer_tags"])
        if prefs:
            hit = sum(1 for t in prefs if t in item_tags)
            prefer_sat = hit / len(prefs)
        else:
            prefer_sat = 0.5
        avoids = set(m["avoid_tags"])
        if avoids:
            v = sum(1 for t in avoids if t in item_tags)
            avoid_sat = max(0.0, 1.0 - v / len(avoids))
        else:
            avoid_sat = 1.0

        if taste_sat is not None:
            score = 0.5 * taste_sat + 0.3 * prefer_sat + 0.2 * avoid_sat
        else:
            score = 0.6 * prefer_sat + 0.4 * avoid_sat
        per_member.append({
            "name": m["name"],
            "score": round(max(0.0, min(1.0, score)), 4),
            "taste_sat": round(taste_sat, 4) if taste_sat is not None else None,
            "prefer_sat": round(prefer_sat, 4),
            "avoid_sat": round(avoid_sat, 4),
        })

    overall = min((pm["score"] for pm in per_member), default=1.0)
    return {"overall": round(overall, 4), "per_member": per_member,
            "note": aggregated.get("fairness_note") or "整体取最低成员分（木桶效应）"}


def attach_fairness(plans: List[Dict[str, Any]], aggregated: Dict[str, Any]) -> None:
    """给每个方案就地附加 ``fairness`` 字段（fairness_score 结果）。"""
    for p in plans:
        p["fairness"] = fairness_score(p, aggregated)


def optimize_group_plans(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    members: List[Any],
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """群体决策入口：聚合偏好 → 无冲突走单方案集；有冲突输出 A/B 两套备选。

    Returns
    -------
    dict
        ``{needs_negotiation, aggregated, conflicts, plans | plan_sets}``。
        每个方案均带 ``fairness`` 字段；冲突时 ``plan_sets[i]``
        含 ``label / serves / compromise / plans``。
    """
    agg = aggregate_preferences(members)
    if agg["needs_negotiation"]:
        plan_sets: List[Dict[str, Any]] = []
        for alt in agg["alternatives"]:
            plans = optimize_plans(menu, coupons, alt["constraints"], promotions=promotions)
            attach_fairness(plans, agg)
            plan_sets.append({"label": alt["label"], "serves": alt["serves"],
                               "compromise": alt["compromise"], "plans": plans})
        return {"needs_negotiation": True, "aggregated": agg,
                "conflicts": agg["conflicts"], "plan_sets": plan_sets}
    plans = optimize_plans(menu, coupons, agg["constraints"], promotions=promotions)
    attach_fairness(plans, agg)
    return {"needs_negotiation": False, "aggregated": agg,
            "conflicts": [], "plans": plans}


def render_group_result(result: Dict[str, Any]) -> str:
    """渲染群体决策结果：无冲突 → 方案集（带公平性）；有冲突 → 冲突说明 + A/B 双方案。"""
    out: List[str] = []
    agg = result["aggregated"]
    if not result["needs_negotiation"]:
        out.append(f"【群体偏好聚合】{agg['people_count']} 人｜硬约束并集："
                   f"{'、'.join(agg['hard']['exclude_tags'] + agg['hard']['exclude_ingredients']) or '无'}"
                   f"｜口味截尾均值：{agg['taste'] or '未表达'}")
        out.append("")
        out.append(render_plans(result["plans"]))
        return "\n".join(out)

    out.append("【偏好冲突 · 需协商】以下维度存在严重对立，已给出两套备选：")
    for cf in result["conflicts"]:
        out.append(f"  ⚠️ {cf['dimension']}：{cf['camp_a']['stance']}"
                   f"（{'、'.join(cf['camp_a']['members'])}，均值 {cf['camp_a']['mean_level']}） vs "
                   f"{cf['camp_b']['stance']}"
                   f"（{'、'.join(cf['camp_b']['members'])}，均值 {cf['camp_b']['mean_level']}）")
    out.append("")
    for ps in result["plan_sets"]:
        out.append(f"━━ {ps['label']}（服务：{'、'.join(ps['serves'])}）")
        out.append(f"  妥协点：{ps['compromise']}")
        out.append("")
        out.append(render_plans(ps["plans"]))
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 2. 菜品标签
# ---------------------------------------------------------------------------

def infer_tags(item: Dict[str, Any]) -> List[str]:
    """根据名称 + 营养值推断菜品标签（写回 ``item['tags']`` 并返回）。"""
    name = str(item.get("name", ""))
    tags = set(item.get("tags") or [])

    for tag, kws in TAG_KEYWORDS.items():
        if any(k in name for k in kws):
            tags.add(tag)

    kcal = float(item.get("kcal") or 0)
    fat = float(item.get("fat") or 0)
    protein = float(item.get("protein") or 0)
    sodium = float(item.get("sodium") or 0)
    carb = float(item.get("carb") or 0)

    if 0 < kcal <= 200:
        tags.add("low_calorie")
    if protein >= 20:
        tags.add("high_protein")
    if fat >= 25:
        tags.add("high_fat")
    if sodium >= 800:
        tags.add("high_sodium")
    if carb >= 35:
        tags.add("high_carb")
    # 脂肪密度兜底：仅用于名称关键词覆盖不到的新品。
    # 阈值取 7g/100kcal 且排除蔬菜/甜品/饮品，避免把「双层吉士汉堡」「鲜蔬蛋沙拉」
    # 这类高脂但非油炸的餐品误判（实测：安格斯系列密度约 6.2~6.5，不应判为油炸）。
    if (kcal >= 200 and fat / kcal * 100 >= 7
            and not ({"vegetable", "dessert", "drink", "low_calorie"} & tags)):
        tags.add("fried")

    # 主食判定：拼单场景要求「每人至少 1 份主食」，小食/饮品/甜品不计入主食
    if any(k in name for k in MAIN_KEYWORDS):
        tags.add("main")
    else:
        tags.add("side")

    tags = sorted(tags)
    item["tags"] = tags
    return tags


def _normalize_menu(menu: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """补全字段类型并把金额统一成「分」。"""
    out: List[Dict[str, Any]] = []
    for it in menu:
        d = dict(it)
        d["price"] = int(d.get("price") or 0)          # 分
        d["kcal"] = float(d.get("kcal") or 0)
        d["protein"] = float(d.get("protein") or 0)
        d["fat"] = float(d.get("fat") or 0)
        d["sodium"] = float(d.get("sodium") or 0)
        d["carb"] = float(d.get("carb") or 0)
        infer_tags(d)
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 3. 硬约束过滤
# ---------------------------------------------------------------------------

def filter_menu(menu: List[Dict[str, Any]], constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按硬约束过滤菜单（一票否决）。

    被淘汰的商品会写入 ``_rejected`` 原因列表，便于向用户解释"为什么没有这个"。
    """
    hard = constraints.get("hard", {}) or {}
    ban_ing = set(hard.get("exclude_ingredients") or [])
    ban_tags = set(hard.get("exclude_tags") or [])
    max_single_cny = hard.get("max_single_item_cny")

    kept: List[Dict[str, Any]] = []
    for raw in menu:
        item = dict(raw)
        infer_tags(item)
        item.setdefault("price", 0)
        reasons: List[str] = []
        name = str(item.get("name", ""))

        for tag in ban_ing:
            words = INGREDIENT_SYNONYMS.get(tag, ())
            if any(w in name for w in words):
                reasons.append(f"命中忌口成分[{tag}]")
        for tag in ban_tags:
            if tag in item["tags"]:
                reasons.append(f"命中排除标签[{tag}]")
        if max_single_cny is not None:
            price_cny = float(item.get("price", 0)) / 100.0
            if price_cny > float(max_single_cny):
                reasons.append(f"单品价 {price_cny:.2f} 元超上限 {max_single_cny} 元")

        if reasons:
            item["_rejected"] = reasons
        else:
            kept.append(item)

    if not kept:
        raise ValueError(
            "硬约束过滤后无可用餐品，请放宽约束。"
            f"（忌口成分={sorted(ban_ing)}，排除标签={sorted(ban_tags)}）"
        )
    return kept


# ---------------------------------------------------------------------------
# 4. 组合优化
# ---------------------------------------------------------------------------

def _coupon_dp(
    subtotal: int,
    coupons: List[Dict[str, Any]],
    max_stack: int = 2,
    item_totals: Optional[Dict[str, int]] = None,
) -> Dict[str, Any]:
    """用动态规划挑选最优券组合。

    * 状态: ``dp[k]`` = 使用恰好 k 张券时的 (最大优惠额, 券列表, 已占分组)
    * 约束: 券的 ``minSpend`` 不得超过 subtotal；同一 ``exclusiveGroup`` 互斥；最多叠加 max_stack 张
    * 商品限定: 若券带 ``appliesToProductCodes``，则**篮子内必须包含**这些商品，
      且优惠额不得超过这些商品的小计（避免"麦乐鸡券"用在没有麦乐鸡的订单上）
    * 优惠额上限为 subtotal（不能把订单减成负数）
    """
    usable: List[Dict[str, Any]] = []
    for c in coupons or []:
        spend = int(c.get("minSpend") or 0)
        if subtotal < spend:
            continue

        cap = subtotal
        scope = c.get("appliesToProductCodes")
        if scope:
            if not item_totals:
                continue                      # 没有篮子信息，无法判断适用范围 → 不使用
            scoped_total = sum(int(item_totals.get(code, 0)) for code in scope)
            if scoped_total <= 0:
                continue                      # 篮子里没有该商品 → 该券不适用
            cap = min(cap, scoped_total)

        if c.get("type") == "percent":
            # rate 表示「折扣率 = 应付比例」：9折券 rate=0.9 → 优惠 10%
            rate = float(c.get("rate") if c.get("rate") is not None else 1.0)
            rate = max(0.0, min(1.0, rate))
            # 用 round 而非 int，避免 13000*0.1 → 1299.99 被截断成 1299
            disc = int(round(subtotal * (1.0 - rate)))
        else:
            disc = int(c.get("discount") or 0)

        disc = max(0, min(disc, cap, subtotal))
        if disc <= 0:
            continue
        usable.append({
            "coupon": c,
            "discount": disc,
            "group": c.get("exclusiveGroup") or c.get("couponId"),
        })

    dp: Dict[int, Tuple[int, List[Dict[str, Any]], set]] = {0: (0, [], set())}
    for cu in usable:
        nxt = dict(dp)
        for k, (val, chosen, groups) in dp.items():
            if k >= max_stack or cu["group"] in groups:
                continue
            cand = (val + cu["discount"], chosen + [dict(cu["coupon"], discount=cu["discount"])],
                    groups | {cu["group"]})
            if (k + 1) not in nxt or cand[0] > nxt[k + 1][0]:
                nxt[k + 1] = cand
        dp = nxt

    best = max(dp.values(), key=lambda t: t[0])
    discount = min(best[0], subtotal)
    return {
        "discount": discount,
        "coupons": best[1],
        "subtotal": subtotal,
        "final": max(0, subtotal - discount),
    }


def _build_dp(
    items: List[Dict[str, Any]],
    people: int,
    kcal_max: float,
    step: Optional[int] = None,
) -> Tuple[List[List[Optional[Tuple[int, int, int, int]]]], int, int]:
    """构建 DP 表（无界背包），供「最低成本」与「价格×热量帕累托」共用。

    状态: ``dp[主食份数][热量桶] = (cost, prev_m, prev_b, item_idx)``

    🔒 **桶语义（正确性关键）**：桶转移按**向上取整**（ceil）——桶号 ``b``
    状态下的任何篮子，其**实际热量 ≤ b×step**。这保证同桶竞争中「合规解」
    不可能被「同桶号但实际热量已超限的更便宜解」挤掉（向下取整的老实现
    存在此漏解风险，暴力穷举交叉验证已证实）。

    🔒 **step 自适应**：``step=None`` 时，状态空间 ≤ 3M 则取 ``step=1``
    ——热量先规范化为整数（向上取整），桶号 = 精确热量和，**零取整误差，
    DP 最优性可证**；菜单极大时按 ``kcal_max/2000`` 放大步长保性能
    （ceil 语义保证任何输出仍绝不超限，仅极端情况下可能轻微次优）。
    """
    # 热量规范化：向上取整为整数（麦当劳营养表本为整数；浮点输入走保守方向）
    kcals = [int(math.ceil(float(it["kcal"]))) for it in items] if items else [1]
    min_kcal = max(min(kcals), 1)
    if step is None:
        est_states = (max(people, 1) + 3) * (int(math.ceil(kcal_max)) + 2) * max(len(items), 1)
        step = 1 if est_states <= 3_000_000 else max(1, int(math.ceil(kcal_max / 2000.0)))
    step = max(int(step), 1)

    max_mains = max(people, 1) + 2          # 允许比人数多 2 份主食用于凑满减
    # 桶数：ceil(kcal_max/step) + 余量。step=1 且热量为整数时桶号即精确值，
    # 余量 0；step>1 时每件最多多算 step-1，按最坏件数（kcal_max/min_kcal）补余量，
    # 余量桶中的解由输出侧 real_kcal 校验兜底（宁可多算不可漏算）。
    slack = 0 if step == 1 else int(math.ceil(kcal_max / min_kcal)) + 1
    max_bucket = int(math.ceil(kcal_max / step)) + slack + 1

    dp: List[List[Optional[Tuple[int, int, int, int]]]] = [
        [None] * (max_bucket + 1) for _ in range(max_mains + 1)
    ]
    dp[0][0] = (0, -1, -1, -1)

    for m in range(0, max_mains + 1):
        for b in range(0, max_bucket + 1):
            cur = dp[m][b]
            if cur is None:
                continue
            cost = cur[0]
            for idx, it in enumerate(items):
                nm = m + (1 if "main" in it["tags"] else 0)
                # 向上取整进桶：保证桶号是实际热量的上界（见 docstring）
                nb = b + -(-kcals[idx] // step)
                if nm > max_mains or nb > max_bucket:
                    continue
                ncost = cost + it["price"]
                tgt = dp[nm][nb]
                if tgt is None or ncost < tgt[0]:
                    dp[nm][nb] = (ncost, m, b, idx)
    return dp, max_mains, max_bucket


def _trace_basket(
    dp: List[List[Optional[Tuple[int, int, int, int]]]], m: int, b: int
) -> List[int]:
    """从 DP 表回溯出商品下标篮子。"""
    basket: List[int] = []
    cm, cb, guard = m, b, 0
    while guard < 400:
        guard += 1
        cell = dp[cm][cb]
        if cell is None:
            break
        _, pm, pb, idx = cell
        if idx < 0:
            break
        basket.append(idx)
        cm, cb = pm, pb
    return basket


def _basket_kcal(basket: Iterable[int], items: List[Dict[str, Any]]) -> float:
    """按篮子里的商品下标求和**实际**热量（不吃热量桶取整的亏）。"""
    return float(sum(items[i]["kcal"] for i in basket))


def _dp_min_cost(
    items: List[Dict[str, Any]],
    people: int,
    kcal_min: float,
    kcal_max: float,
    step: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """动态规划求满足「主食份数 + 热量区间」的最低成本篮子（无界背包）。

    ⚠️ 拼单场景的关键约束：**主食份数 ≥ 人数**（每人至少 1 份主食），
    而不是只看"总份数 ≥ 人数"——否则会出现「2 个汉堡 + 2 份薯条供 4 人」这种分不下去的方案。
    """
    dp, max_mains, max_bucket = _build_dp(items, people, kcal_max, step=step)

    solutions: List[Dict[str, Any]] = []
    for m in range(max(people, 1), max_mains + 1):
        for b in range(0, max_bucket + 1):
            cell = dp[m][b]
            if cell is None:
                continue
            basket = _trace_basket(dp, m, b)
            if not basket:
                continue
            # ⚠️ 用实际热量校验，而不是桶号 × step（桶是向下取整的，会略微低估）
            real_kcal = _basket_kcal(basket, items)
            if real_kcal < kcal_min or real_kcal > kcal_max:
                continue
            solutions.append({"cost": int(cell[0]), "mains": m, "units": len(basket),
                              "kcal": real_kcal, "indices": basket})

    solutions.sort(key=lambda s: (s["cost"], abs(s["kcal"] - kcal_min)))
    pruned: List[Dict[str, Any]] = []
    for s in solutions:
        if any(p["cost"] <= s["cost"] and abs(p["kcal"] - kcal_min) <= abs(s["kcal"] - kcal_min)
               for p in pruned):
            continue
        pruned.append(s)
        if len(pruned) >= 40:
            break
    return pruned


def price_kcal_candidates(
    items: List[Dict[str, Any]],
    people: int,
    kcal_min: float,
    kcal_max: float,
    step: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """**成本–热量权衡曲线**：每个热量档位下的最低成本解（未做帕累托过滤）。

    用途：回答"在满足硬约束的前提下，多吃 X 千卡要多花多少钱"。
    返回按热量升序排列的候选列表。
    """
    if not items:
        return []
    dp, max_mains, max_bucket = _build_dp(items, people, kcal_max, step=step)

    cand: List[Dict[str, Any]] = []
    for b in range(0, max_bucket + 1):
        best: Optional[Tuple[int, int]] = None
        for m in range(max(people, 1), max_mains + 1):
            cell = dp[m][b]
            if cell is None:
                continue
            if best is None or cell[0] < best[0]:
                best = (int(cell[0]), m)
        if best is None:
            continue
        basket = _trace_basket(dp, best[1], b)
        if not basket:
            continue
        # ⚠️ 以实际热量为准校验上下限（热量桶向下取整会低估）
        real_kcal = _basket_kcal(basket, items)
        if real_kcal < kcal_min or real_kcal > kcal_max:
            continue
        cand.append({"kcal": real_kcal, "cost": best[0], "mains": best[1],
                     "units": len(basket), "indices": basket})

    # 同一实际热量可能对应多个篮子，只保留成本最低的那个
    by_kcal: Dict[float, Dict[str, Any]] = {}
    for s in cand:
        cur = by_kcal.get(s["kcal"])
        if cur is None or s["cost"] < cur["cost"]:
            by_kcal[s["kcal"]] = s
    return sorted(by_kcal.values(), key=lambda s: s["kcal"])


def pareto_price_kcal(
    items: List[Dict[str, Any]],
    people: int,
    kcal_min: float,
    kcal_max: float,
    step: Optional[int] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """**价格 × 热量双目标帕累托解集**（不使用加权评分）。

    两个目标同时最小化：``最终价`` 与 ``总热量``。做法：先取各热量档位的最低成本解
    （见 :func:`price_kcal_candidates`），再按「热量升序、成本严格下降」提取**非支配解**。

    ⚠️ 若菜单中"最便宜的餐品"同时也"热量最低"，则两目标不冲突，前沿会**退化为单点**。
    这属于正常的数学结果，调用方应据此说明"该菜单不存在多花钱换低热量的权衡"，
    而不是伪造多个解。此时可改用 :func:`price_kcal_candidates` 查看完整权衡曲线。
    """
    cand = price_kcal_candidates(items, people, kcal_min, kcal_max, step=step)

    # 帕累托前沿：kcal 升序，保留成本严格下降的点
    front: List[Dict[str, Any]] = []
    best_cost: Optional[int] = None
    for s in cand:                        # cand 已按 kcal 升序
        if best_cost is None or s["cost"] < best_cost:
            front.append(s)
            best_cost = s["cost"]

    # 等间隔抽样，避免输出过长（保留首尾）
    if limit and len(front) > limit:
        if limit <= 1:
            # 防御：limit=1 时仅保留首点（最低热量点），避免下方除零
            return front[:1]
        span = len(front) - 1
        idxs = sorted({round(i * span / (limit - 1)) for i in range(limit)})
        front = [front[i] for i in idxs]
    return front



def _basket_to_plan(
    basket_indices: Iterable[int],
    items: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    label: str,
) -> Dict[str, Any]:
    """把商品下标篮子 + 券组合整理成标准「方案」。"""
    counter: Dict[int, int] = {}
    for i in basket_indices:
        counter[i] = counter.get(i, 0) + 1

    lines: List[Dict[str, Any]] = []
    subtotal = 0
    item_totals: Dict[str, int] = {}
    mains = 0
    kcal = protein = fat = sodium = 0.0
    for idx, qty in sorted(counter.items()):
        it = items[idx]
        line_total = it["price"] * qty
        subtotal += line_total
        code = it.get("productCode")
        if code:
            item_totals[code] = item_totals.get(code, 0) + line_total
        if "main" in (it.get("tags") or []):
            mains += qty
        kcal += it["kcal"] * qty
        protein += it["protein"] * qty
        fat += it["fat"] * qty
        sodium += it["sodium"] * qty
        lines.append({
            "productCode": code,
            "name": it.get("name"),
            "quantity": qty,
            "unit_price_cny": round(it["price"] / 100.0, 2),
            "subtotal_cny": round(line_total / 100.0, 2),
            "kcal_per_unit": round(it["kcal"], 1),
            "tags": list(it.get("tags") or []),
            "is_main": "main" in (it.get("tags") or []),
        })

    coupon_res = _coupon_dp(subtotal, coupons, item_totals=item_totals)
    people = max(int(constraints.get("people_count") or 1), 1)
    final = coupon_res["final"]

    targets = constraints.get("targets") or {}
    flags: List[str] = []
    # 拼单底线：主食份数必须 ≥ 人数，否则分不下去
    if mains < people:
        flags.append(f"主食不足（{mains} 份 < {people} 人）")
    if targets.get("max_calories_kcal") and kcal / people > targets["max_calories_kcal"]:
        flags.append("人均热量超标")
    if targets.get("max_fat_g") and fat / people > targets["max_fat_g"]:
        flags.append("人均脂肪超标")
    if targets.get("min_protein_g") and protein / people < targets["min_protein_g"]:
        flags.append("人均蛋白不足")

    # ---- 预算约束检查（Task 1：总预算 + 每人预算）----
    budget = constraints.get("budget") or {}
    budget_total_cny = budget.get("total_cny")
    budget_pp_cny = budget.get("per_person_cny")
    if budget_total_cny is not None and final / 100.0 > float(budget_total_cny):
        flags.append(f"超预算 ¥{round(final / 100.0 - float(budget_total_cny), 2)}（总预算 ¥{budget_total_cny}）")
    if budget_pp_cny is not None and (final / 100.0) / people > float(budget_pp_cny):
        flags.append(f"人均超限 ¥{round((final / 100.0) / people - float(budget_pp_cny), 2)}（每人上限 ¥{budget_pp_cny}）")

    if not flags:
        flags.append("营养达标")

    # ---- 券可用性评估（Task 2）----
    coupon_availability = _estimate_coupons_availability(coupon_res["coupons"])

    return {
        "label": label,
        "mains": mains,
        "items": lines,
        "coupons": [
            {
                "couponId": c.get("couponId"),
                "couponCode": c.get("couponCode"),
                "name": c.get("name"),
                "discount_cny": round(int(c.get("discount") or 0) / 100.0, 2),
            }
            for c in coupon_res["coupons"]
        ],
        "coupon_availability": coupon_availability,
        "subtotal_cny": round(subtotal / 100.0, 2),
        "discount_cny": round(coupon_res["discount"] / 100.0, 2),
        "final_price_cny": round(final / 100.0, 2),
        "per_capita_cny": round(final / 100.0 / people, 2),
        "people_count": people,
        "nutrition": {
            "total_kcal": round(kcal, 1),
            "per_capita_kcal": round(kcal / people, 1),
            "total_protein_g": round(protein, 1),
            "per_capita_protein_g": round(protein / people, 1),
            "total_fat_g": round(fat, 1),
            "per_capita_fat_g": round(fat / people, 1),
            "total_sodium_mg": round(sodium, 1),
        },
        "nutrition_flags": flags,
        "units": sum(counter.values()),
    }


def _score(plan: Dict[str, Any], constraints: Dict[str, Any], weights: Dict[str, float]) -> float:
    """给方案打加权分（越高越好），权重来自画像。"""
    people = plan["people_count"]
    final = plan["final_price_cny"]

    budget = constraints.get("budget") or {}
    amount = budget.get("amount_cny")
    if budget.get("type") == "per_capita" and amount:
        ceiling = float(amount) * people
    elif amount:
        ceiling = float(amount)
    else:
        ceiling = max(final * 1.35, 1.0)
    s_cheap = max(0.0, min(1.0, 1.0 - final / ceiling))

    targets = constraints.get("targets") or {}
    hits = total = 0
    if targets.get("max_calories_kcal"):
        total += 1
        hits += 1 if plan["nutrition"]["per_capita_kcal"] <= targets["max_calories_kcal"] else 0
    if targets.get("max_fat_g"):
        total += 1
        hits += 1 if plan["nutrition"]["per_capita_fat_g"] <= targets["max_fat_g"] else 0
    if targets.get("min_protein_g"):
        total += 1
        hits += 1 if plan["nutrition"]["per_capita_protein_g"] >= targets["min_protein_g"] else 0
    s_healthy = hits / total if total else 0.6

    slow = sum(1 for ln in plan["items"] if "fried" in (ln.get("tags") or []))
    s_speed = max(0.0, 1.0 - slow / max(plan["units"], 1))

    prefer = set((constraints.get("soft") or {}).get("prefer_tags") or [])
    avoid = set((constraints.get("soft") or {}).get("avoid_tags") or [])
    names = " ".join(ln["name"] for ln in plan["items"])
    s_pref = 0.5
    if "spicy" in prefer and any(k in names for k in TAG_KEYWORDS["spicy"]):
        s_pref += 0.3
    if "fast" in prefer and slow == 0:
        s_pref += 0.2
    if "fried" in avoid and slow:
        s_pref -= 0.3
    s_pref = max(0.0, min(1.0, s_pref))

    return round(
        weights.get("cheap", 0.34) * s_cheap
        + weights.get("healthy", 0.33) * s_healthy
        + weights.get("speed", 0.18) * s_speed
        + weights.get("pref", 0.15) * s_pref,
        4,
    )


def _count_mains(picked: Iterable[int], items: List[Dict[str, Any]]) -> int:
    """统计篮子里的**主食**份数（汉堡 / 卷 / 早餐堡），用于校验「每人至少 1 份主食」。"""
    return sum(1 for i in picked if "main" in (items[i].get("tags") or []))


def _optimize_plans_weighted(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """**加权评分路径**（原 optimize_plans 实现，保留作回退与对照）。

    * **最省钱**：动态规划（无界背包）+ 券组合 DP + 帕累托剪枝
    * **最健康**：按人均热量 / 脂肪 / 蛋白达标率加权
    * **最快**：按油炸 / 现制占比估算出餐速度

    ``promotions``：来自 ``fetch_group_meal_promotions()`` 的团餐促销规则（beType=6 专用），
    会与 ``coupons`` 合并后一并参与券组合优化。

    **预算硬约束**：同时支持总预算上限和每人预算上限。
    若最优方案超出预算，自动生成降级方案（减配/分步下单），
    并明确告知预算差距。
    """
    items = _normalize_menu(menu)
    coupons = list(coupons or []) + list(promotions or [])
    weights = weights or constraints.get("weights") or {
        "cheap": 0.34, "healthy": 0.33, "speed": 0.18, "pref": 0.15,
    }

    people = max(int(constraints.get("people_count") or 1), 1)
    targets = constraints.get("targets") or {}
    kcal_per = targets.get("max_calories_kcal") or KCAL_PER_PERSON_MAX
    kcal_min = KCAL_PER_PERSON_MIN * people
    kcal_max = float(min(kcal_per, KCAL_PER_PERSON_MAX)) * people

    idx_of = {id(it): i for i, it in enumerate(items)}
    pool: List[Dict[str, Any]] = []

    # --- 最省钱：DP -------------------------------------------------------
    for sol in _dp_min_cost(items, people, kcal_min, kcal_max)[:12]:
        pool.append(_basket_to_plan(sol["indices"], items, coupons, constraints, "最省钱"))

    # --- 最健康：优先高蛋白/低脂/低热量密度 --------------------------------
    #     必须先保证「主食份数 ≥ 人数」，再补足热量下限
    healthy_sorted = sorted(
        items,
        key=lambda x: ((0 if "main" in x["tags"] else 1),
                       -(x["protein"] / max(x["kcal"], 1)), x["fat"], x["price"]),
    )
    pick: List[int] = []
    kcal_acc = 0.0
    for it in healthy_sorted:
        if kcal_acc >= kcal_min and _count_mains(pick, items) >= people:
            break
        if it["kcal"] <= 0 and kcal_acc >= kcal_min:
            continue          # 热量已达标时不必再塞零卡饮品
        pick.append(idx_of[id(it)])
        kcal_acc += it["kcal"]
    if _count_mains(pick, items) < people:      # 兜底：主食没够就补齐
        pick = list(range(len(items)))          # 极端情况下退化为全选，交由评分兜底
    if pick:
        pool.append(_basket_to_plan(pick, items, coupons, constraints, "最健康"))

    # --- 最快：优先非油炸，同样先满足主食份数 ------------------------------
    fast_sorted = sorted(items, key=lambda x: ((0 if "main" in x["tags"] else 1),
                                              ("fried" in x["tags"]), -x["kcal"]))
    pick2: List[int] = []
    kcal_acc2 = 0.0
    for it in fast_sorted:
        if kcal_acc2 >= kcal_min and _count_mains(pick2, items) >= people:
            break
        if it["kcal"] <= 0 and kcal_acc2 >= kcal_min:
            continue          # 热量已达标时不必再塞零卡饮品
        pick2.append(idx_of[id(it)])
        kcal_acc2 += it["kcal"]
    if _count_mains(pick2, items) < people:     # 兜底：主食没够就补齐
        pick2 = list(range(len(items)))
    if pick2:
        pool.append(_basket_to_plan(pick2, items, coupons, constraints, "最快"))

    # --- Task 1 & 3：预算硬约束 + 降级方案 --------------------------------
    budget = constraints.get("budget") or {}
    budget_total = budget.get("total_cny")
    budget_pp = budget.get("per_person_cny")

    effective_max_cny: Optional[float] = None
    if budget_total is not None:
        effective_max_cny = float(budget_total)
    if budget_pp is not None:
        pp_total = float(budget_pp) * people
        if effective_max_cny is None or pp_total < effective_max_cny:
            effective_max_cny = pp_total

    if effective_max_cny is not None:
        within_budget = [p for p in pool if p["final_price_cny"] <= effective_max_cny + 0.01]
        if within_budget:
            pool = within_budget
        else:
            # 无任何方案在预算内 → 生成降级方案（Task 3：减配 / 分步下单）
            degraded = _generate_degraded_plans(
                items, people, effective_max_cny, coupons, constraints
            )
            if degraded:
                return degraded

    # --- 帕累托去重 + 加权排序 --------------------------------------------
    for p in pool:
        p["score"] = _score(p, constraints, weights)

    dedup: Dict[Tuple, Dict[str, Any]] = {}
    for p in pool:
        key = tuple(sorted((ln["productCode"] or ln["name"], ln["quantity"]) for ln in p["items"]))
        if key not in dedup or p["score"] > dedup[key]["score"]:
            dedup[key] = p

    # 只保留满足「每人至少 1 份主食」的方案（这是拼单的底线）
    feasible = [p for p in dedup.values() if p.get("mains", 0) >= people]
    ranked = sorted(feasible or dedup.values(),
                    key=lambda x: (-x["score"], x["final_price_cny"]))
    # 自适应席位（与 NSGA-II 主路径同一原则）：先按标签各取一席（最优），
    # 再用次优解补足；近重复解折叠，不凑数——有几席算几席。
    picked: List[Dict[str, Any]] = []
    seen_labels: set = set()
    for p in ranked:
        if len(picked) >= 3:
            break
        if p["label"] in seen_labels:
            continue
        if any(not _plans_distinct(p, q) for q in picked):
            continue
        picked.append(p)
        seen_labels.add(p["label"])
    for p in ranked:
        if len(picked) >= 3:
            break
        if p in picked or any(not _plans_distinct(p, q) for q in picked):
            continue
        p["label"] = "均衡"
        picked.append(p)
    return picked


def _plans_distinct(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """两方案是否构成**明显不同**的取舍档位（自适应席位的判据）。

    「对比表里肉眼能看出差异」才算不同，四条判据任一命中即视为不同：
    价格差 ≥ max(¥1.00, 便宜者的 5%)，或人均热量差 ≥ 75 kcal、
    人均蛋白差 ≥ 6 g、人均脂肪差 ≥ 6 g。

    帕累托前沿上大量「换个小食」级别的近重复解会被此判据折叠——
    它们数学上互不支配，决策上是同一个方案。
    """
    pa, pb = float(a["final_price_cny"]), float(b["final_price_cny"])
    if abs(pa - pb) >= max(1.0, 0.05 * min(pa, pb)):
        return True
    na = a.get("nutrition") or {}
    nb = b.get("nutrition") or {}
    return (abs(float(na.get("per_capita_kcal") or 0)
                - float(nb.get("per_capita_kcal") or 0)) >= 75
            or abs(float(na.get("per_capita_protein_g") or 0)
                   - float(nb.get("per_capita_protein_g") or 0)) >= 6
            or abs(float(na.get("per_capita_fat_g") or 0)
                   - float(nb.get("per_capita_fat_g") or 0)) >= 6)


def optimize_plans(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
    promotions: Optional[List[Dict[str, Any]]] = None,
    method: str = "nsga2",
    top_n: int = 5,
) -> List[Dict[str, Any]]:
    """**多目标寻优入口**（默认 NSGA-II，批 2 算法升级）。

    * ``method="nsga2"``（默认）：NSGA-II 在四目标空间
      （价格↓ / 人均热量↓ / 油炸份数↓ / 人均蛋白↑）中演化，
      从**帕累托前沿**取「最省钱 / 最健康 / 最快」极值并**自适应补席**：
      席位数跟随前沿的**有效多样性**（1–``top_n``），而非固定 3——
      * 前沿丰富 → 极值之外按拥挤距离补「均衡」代表，每席与已选
        方案在 :func:`_plans_distinct` 判据下**明显不同**（价格/热量/蛋白/脂肪
        至少一项差异可见）；
      * 前沿退化 → 有几席算几席，**不凑数**（不再借加权路径补标签）；
      * 近重复解（「换个小食」级别）被判据折叠，不占席位；
      * 完整非支配解集需求请用 :func:`optimize_pareto`。
    * ``method="weighted"``：原加权评分路径（对照与回退用）。

    预算硬约束 / 主食底线 / 降级方案行为与加权路径完全一致；
    每个方案附加 ``nsga2`` 字段（front / objectives）供透明展示。
    """
    if method == "weighted":
        return _optimize_plans_weighted(menu, coupons, constraints,
                                        weights, promotions)

    items = _normalize_menu(menu)
    if not items:
        return []
    all_coupons = list(coupons or []) + list(promotions or [])
    w = weights or constraints.get("weights") or {
        "cheap": 0.34, "healthy": 0.33, "speed": 0.18, "pref": 0.15,
    }

    people = max(int(constraints.get("people_count") or 1), 1)
    targets = constraints.get("targets") or {}
    kcal_per = targets.get("max_calories_kcal") or KCAL_PER_PERSON_MAX
    kcal_min = KCAL_PER_PERSON_MIN * people
    kcal_max = float(min(kcal_per, KCAL_PER_PERSON_MAX)) * people

    baskets = _nsga2_evolve(items, people, kcal_min, kcal_max)
    if not baskets:   # 无主食等极端情况 → 加权路径兜底（其内部有降级逻辑）
        return _optimize_plans_weighted(menu, coupons, constraints,
                                        weights, promotions)

    plans = [_basket_to_plan(b, items, all_coupons, constraints, "候选")
             for b in baskets]

    # --- 预算硬约束（与加权路径同一套规则） -------------------------------
    budget = constraints.get("budget") or {}
    effective_max_cny: Optional[float] = None
    if budget.get("total_cny") is not None:
        effective_max_cny = float(budget["total_cny"])
    if budget.get("per_person_cny") is not None:
        pp_total = float(budget["per_person_cny"]) * people
        if effective_max_cny is None or pp_total < effective_max_cny:
            effective_max_cny = pp_total
    if effective_max_cny is not None:
        within = [p for p in plans
                  if p["final_price_cny"] <= effective_max_cny + 0.01]
        if within:
            plans = within
        else:
            return _generate_degraded_plans(
                items, people, effective_max_cny, all_coupons, constraints)

    # --- 主食底线（拼单不可妥协） -----------------------------------------
    feasible = [p for p in plans if p.get("mains", 0) >= people]
    if not feasible:
        return _optimize_plans_weighted(menu, coupons, constraints,
                                        weights, promotions)
    plans = feasible

    # --- 热量硬上限（targets.max_calories_kcal）---------------------------
    #     可行解存在时只保留达标者；全部超限（主餐热量天然超预算）时保留并
    #     由 nutrition_flags 如实标注「人均热量超标」——不静默放宽也不断路。
    kcal_ok = [p for p in plans
               if p["nutrition"]["total_kcal"] <= kcal_max + 1e-6]
    if kcal_ok:
        plans = kcal_ok

    # --- 券后价口径重新非支配分层，取第一前沿 -----------------------------
    objs = [_plan_objectives(p) for p in plans]
    fronts = _fast_non_dominated_sort(objs)
    front1 = [plans[i] for i in fronts[0]]

    dedup: Dict[tuple, Dict[str, Any]] = {}
    for p in front1:
        k = _basket_key(p)
        if k not in dedup:
            dedup[k] = p
    cands = list(dedup.values())
    if not cands:
        return _optimize_plans_weighted(menu, coupons, constraints,
                                        weights, promotions)
    c_objs = [_plan_objectives(p) for p in cands]

    # --- 三个命名极值：min 价 / max 蛋白 / min 油炸 ------------------------
    #     极值对应可预期的用户意图（省钱/营养/速度），无论席位多少都保留。
    #     「最健康」额外尊重显式营养目标：用户给了 targets.max_fat_g 时，
    #     若前沿中存在脂肪达标者，则只在达标子集里选蛋白最高者——避免
    #     「蛋白冠军但脂肪超标」的方案顶着「最健康」标签（2026-10 实测缺陷：
    #     脂肪 30.5g > 目标 25g 曾被选为最健康）。全部超标则回退全局极值，
    #     由 nutrition_flags 如实标注，不静默放宽。
    #     极值撞篮（如「最省钱」与「最快」同为 0 油炸最低价）时，在未选
    #     池中找**同目标值**的并列极值顶替标签；没有并列者该标签空缺，
    #     不用更差的方案冒名。
    fat_cap = targets.get("max_fat_g")
    health_pool: List[int] = list(range(len(cands)))
    if fat_cap is not None:
        fat_ok = [i for i in range(len(cands))
                  if (cands[i]["nutrition"].get("per_capita_fat_g") or 0)
                  <= float(fat_cap) + 1e-6]
        if fat_ok:
            health_pool = fat_ok
    _extreme_key = {
        "最省钱": lambda i: (c_objs[i][0], c_objs[i][1]),   # 价格 min
        "最健康": lambda i: (c_objs[i][3], c_objs[i][0]),   # 蛋白 max（达标池内）
        "最快":   lambda i: (c_objs[i][2], c_objs[i][0]),   # 油炸 min
    }
    _extreme_obj = {"最省钱": 0, "最健康": 3, "最快": 2}
    _extreme_pool = {"最省钱": range(len(cands)),
                     "最健康": health_pool,
                     "最快": range(len(cands))}

    result: List[Dict[str, Any]] = []
    used: set = set()
    for label in ("最省钱", "最健康", "最快"):
        pool = sorted(_extreme_pool[label], key=_extreme_key[label])
        best_v = c_objs[pool[0]][_extreme_obj[label]]
        for i in pool:
            if _basket_key(cands[i]) in used:
                continue
            if c_objs[i][_extreme_obj[label]] <= best_v + 1e-9:
                cands[i]["label"] = label
                result.append(cands[i])
                used.add(_basket_key(cands[i]))
                break

    # --- 自适应补席：拥挤距离降序，只收「明显不同」的均衡档位 --------------
    #     席位跟随前沿有效多样性（1–top_n），两个方向都不硬凑：
    #     * 向上：前沿丰富时极值之外补「均衡」代表，每席与已选方案在
    #       _plans_distinct 下至少一项差异肉眼可见（近重复解不占席位）；
    #     * 向下：前沿退化（极值重合/菜单极小）时输出 1–2 套即止，
    #       不再借加权路径补齐「Top-3」格式——有几席算几席。
    #     补席与「最健康」同一达标口径：脂肪目标存在且有达标者时，超标
    #     方案不占席位——其蛋白优势以违反群体健康目标为代价，且会反超
    #     「最健康」的蛋白、破坏命名极值的标签语义。
    extra_pool = health_pool
    avail = [i for i in extra_pool if _basket_key(cands[i]) not in used]
    if len(result) < top_n and avail:
        dist = _crowding_distance(list(range(len(cands))), c_objs)
        order = sorted(avail, key=lambda i: -dist[i])
        for i in order:
            if len(result) >= top_n:
                break
            cand = cands[i]
            if any(not _plans_distinct(cand, sel) for sel in result):
                continue
            cand["label"] = "均衡"
            result.append(cand)
            used.add(_basket_key(cand))

    # --- 附加透明信息（front / objectives）与兼容 score --------------------
    for p in result:
        o = _plan_objectives(p)
        p.setdefault("score", _score(p, constraints, w))
        p["nsga2"] = {
            "front": 1,
            "objectives": {
                "final_price_cny": o[0],
                "per_capita_kcal": o[1],
                "fried_count": o[2],
                "per_capita_protein_g": -o[3],
            },
        }
    label_order = ("最省钱", "最健康", "最快", "均衡")
    result.sort(key=lambda p: label_order.index(p["label"])
                if p["label"] in label_order else 9)
    return result[:top_n]


def optimize_cost_curve(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    promotions: Optional[List[Dict[str, Any]]] = None,
    tiers: int = 7,
) -> List[Dict[str, Any]]:
    """**成本–热量权衡曲线**：从"刚好达标"到"吃得更好"等间隔取样，给出每档的最低价方案。

    与 :func:`optimize_pareto` 互补：
      - 帕累托前沿 = 互不支配的解（可能因菜单特性退化为单点）；
      - 成本曲线   = 每个热量档位的实际最低价，直接回答"多吃 X 千卡要多花多少钱"。

    Returns
    -------
    list[dict]  按总热量升序的方案列表，额外带 ``delta_price_cny``（相比上一档多花多少）。
    """
    items = _normalize_menu(menu)
    all_coupons = list(coupons or []) + list(promotions or [])

    people = max(int(constraints.get("people_count") or 1), 1)
    targets = constraints.get("targets") or {}
    kcal_per = targets.get("max_calories_kcal") or KCAL_PER_PERSON_MAX
    kcal_min = KCAL_PER_PERSON_MIN * people
    kcal_max = float(min(kcal_per, KCAL_PER_PERSON_MAX)) * people

    cand = price_kcal_candidates(items, people, kcal_min, kcal_max)
    if not cand:
        return []

    # 按 kcal 等间隔取样（cand 已按 kcal 升序）
    if tiers and len(cand) > tiers:
        span = len(cand) - 1
        idxs = sorted({round(i * span / (tiers - 1)) for i in range(tiers)})
        picked = [cand[i] for i in idxs]
    else:
        picked = cand

    plans: List[Dict[str, Any]] = []
    prev_price: Optional[float] = None
    for pt in picked:
        plan = _basket_to_plan(pt["indices"], items, all_coupons, constraints, "成本曲线")
        price = plan["final_price_cny"]
        plan["delta_price_cny"] = None if prev_price is None else round(price - prev_price, 2)
        prev_price = price
        plans.append(plan)
    return plans


def optimize_pareto(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    promotions: Optional[List[Dict[str, Any]]] = None,
    limit: int = 12,
) -> List[Dict[str, Any]]:
    """**价格 × 热量双目标帕累托解集**（不走加权评分）。

    与 :func:`optimize_plans` 的区别：
      - ``optimize_plans`` 给出「最省钱 / 最健康 / 最快」极值 + 均衡补席的**自适应方案集**，适合直接推荐；
      - ``optimize_pareto`` 给出**完整的非支配解集**，适合让用户自己看到"多花 X 元能少吃 Y 千卡"
        的完整权衡曲线，不做任何主观加权。

    Returns
    -------
    list[dict]
        按总热量升序排列的方案列表，每项含 ``items / coupons / final_price_cny /
        per_capita_cny / nutrition`` 等标准字段，另附：
        ``price_vs_next_cny``（相比下一档多花多少）与 ``kcal_saved_vs_next``（少吃多少千卡）。
    """
    items = _normalize_menu(menu)
    all_coupons = list(coupons or []) + list(promotions or [])

    people = max(int(constraints.get("people_count") or 1), 1)
    targets = constraints.get("targets") or {}
    kcal_per = targets.get("max_calories_kcal") or KCAL_PER_PERSON_MAX
    kcal_min = KCAL_PER_PERSON_MIN * people
    kcal_max = float(min(kcal_per, KCAL_PER_PERSON_MAX)) * people

    front = pareto_price_kcal(items, people, kcal_min, kcal_max, limit=limit)
    plans: List[Dict[str, Any]] = []
    for pt in front:
        plan = _basket_to_plan(pt["indices"], items, all_coupons, constraints, "帕累托")
        plan["objective"] = {
            "total_kcal": plan["nutrition"]["total_kcal"],
            "final_price_cny": plan["final_price_cny"],
        }
        plans.append(plan)

    # 相邻档位的边际权衡（front 按热量升序 ⇒ 热量越低、价格越高）
    for i, p in enumerate(plans):
        if i + 1 < len(plans):
            nxt = plans[i + 1]
            p["price_vs_next_cny"] = round(p["final_price_cny"] - nxt["final_price_cny"], 2)
            p["kcal_saved_vs_next"] = round(
                nxt["nutrition"]["total_kcal"] - p["nutrition"]["total_kcal"], 1
            )
        else:
            p["price_vs_next_cny"] = None
            p["kcal_saved_vs_next"] = None
    return plans


# ---------------------------------------------------------------------------
# 4.2 按热量预算配餐（扩展分支 D）：主餐 + 小食 + 饮品，人均热量 ≤ 预算
#
#     与 optimize_plans 的区别：
#     * optimize_plans 在「篮子总量」层面优化（总热量上限 = 人均上限 × 人数），
#       允许成员之间热量分布不均；
#     * optimize_calorie_budget 在「每人组合」层面构造：1 主餐 + 0–1 小食 + 0–1 饮品，
#       人均热量 ≤ 预算由构造保证——直接回答「4 个人每人 500 大卡以内怎么点」。
# ---------------------------------------------------------------------------

#: 热量预算配餐的三个方案定位
CALORIE_BUDGET_LABELS = ("热量预算·最省", "热量预算·高蛋白", "热量预算·均衡混搭")


def _meal_role(item: Dict[str, Any]) -> str:
    """主餐 / 小食 / 饮品 三分法（标签来自 infer_tags，与 strategy.md 字典同步）。"""
    tags = item.get("tags") or []
    if "main" in tags:
        return "main"
    if "drink" in tags:
        return "drink"
    return "side"


def _calorie_budget_combos(items: List[Dict[str, Any]],
                           cap_kcal: float) -> List[Tuple[int, ...]]:
    """枚举「1 主餐 + 0–1 小食 + 0–1 饮品」且组合热量 ≤ cap 的单人组合。

    主餐是硬结构（每人至少 1 份）；小食/饮品可缺省（预算紧张时只点主餐 + 零卡饮）。
    """
    mains: List[int] = []
    sides: List[int] = []
    drinks: List[int] = []
    for i, it in enumerate(items):
        role = _meal_role(it)
        if role == "main":
            mains.append(i)
        elif role == "side":
            sides.append(i)
        else:
            drinks.append(i)

    combos: List[Tuple[int, ...]] = []
    for m in mains:
        m_kcal = items[m]["kcal"]
        if m_kcal > cap_kcal:
            continue                     # 主餐本身超预算 → 该主餐无解
        rem = cap_kcal - m_kcal
        combos.append((m,))              # 仅主餐
        for s in sides:
            s_kcal = items[s]["kcal"]
            if s_kcal > rem:
                continue
            combos.append((m, s))        # 主餐 + 小食
            for d in drinks:
                if s_kcal + items[d]["kcal"] <= rem:
                    combos.append((m, s, d))   # 主餐 + 小食 + 饮品（完整结构）
        for d in drinks:
            if items[d]["kcal"] <= rem:
                combos.append((m, d))    # 主餐 + 饮品
    return combos


def _combo_stats(combo: Tuple[int, ...],
                 items: List[Dict[str, Any]]) -> Tuple[float, int, float]:
    """单人组合的 (热量, 成本/分, 蛋白质)。"""
    return (sum(items[i]["kcal"] for i in combo),
            sum(items[i]["price"] for i in combo),
            sum(items[i]["protein"] for i in combo))


def optimize_calorie_budget(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    top_n: int = 3,
) -> List[Dict[str, Any]]:
    """**按热量预算配餐**（扩展分支 D）：输出人均热量 ≤ 预算的主餐+小食+饮品方案。

    * 热量预算：``targets.max_calories_kcal``（**人均硬上限**，由「每人 X 大卡」解析；
      未显式给出时按减脂档位 tier 推断，默认 standard=600 kcal）；
    * 方案 1「最省」：预算内成本最低的单人组合 × 人数；
    * 方案 2「高蛋白」：蛋白质最高的单人组合 × 人数（与「最省」同篮子时**不重复输出**）；
    * 方案 3「均衡混搭」：不同主餐的最省组合轮流分配（多样性最好）；
    * 席位自适应：可行组合少（如仅 1 套）时输出 1–2 套即止，不凑数；
      ``top_n`` 为上限（角色固定三类，默认 3）；
    * 无解（热量最低的主餐也超预算）：**不静默放宽**，输出差距说明 + 调整建议。

    每套方案附 ``calorie_budget`` 字段：
    ``{"cap_per_person_kcal", "per_capita_kcal", "headroom_kcal", "mode"}``。
    """
    items = _normalize_menu(menu)
    people = max(int(constraints.get("people_count") or 1), 1)
    targets = constraints.get("targets") or {}
    tier = targets.get("tier") or "standard"
    cap = targets.get("max_calories_kcal") or DIET_TIERS.get(tier, DIET_TIERS["standard"])["kcal"]
    cap = float(cap)

    combos = _calorie_budget_combos(items, cap)
    if not combos:
        mains = [it for it in items if _meal_role(it) == "main"]
        if mains:
            best = min(mains, key=lambda x: (x["kcal"], x["price"]))
            gap = best["kcal"] - cap
            reason = (
                f"热量预算 {cap:.0f} kcal/人 内无可行组合：热量最低的主餐「{best['name']}」"
                f"已有 {best['kcal']:.0f} kcal，超出预算 {gap:.0f} kcal。"
                f"建议：1) 将热量预算上调至 ≥ {best['kcal']:.0f} kcal/人；"
                f"2) 或接受不含主餐的轻食组合（小食+饮品）。"
            )
        else:
            reason = "菜单中没有任何主餐，无法按「每人 1 份主餐」的底线配餐。"
        return [{
            "label": "热量预算无解", "degraded": True, "degrade_reason": reason,
            "items": [], "coupons": [], "subtotal_cny": 0, "discount_cny": 0,
            "final_price_cny": 0, "per_capita_cny": 0, "people_count": people,
            "mains": 0, "units": 0, "nutrition": {},
            "nutrition_flags": ["热量预算内无解，详见说明"],
            "coupon_availability": None,
        }]

    cheapest = min(combos, key=lambda cb: (_combo_stats(cb, items)[1],
                                           _combo_stats(cb, items)[0]))
    ranked_protein = sorted(combos, key=lambda cb: (-_combo_stats(cb, items)[2],
                                                    _combo_stats(cb, items)[1]))
    protein_best = ranked_protein[0]
    if protein_best == cheapest:
        protein_best = next((cb for cb in ranked_protein if cb != cheapest), cheapest)

    # 席位不凑数：可行组合唯一时「高蛋白」与「最省」是同一篮子，
    # 只输出一套（不再复制一份换个标签充数）。
    plans: List[Dict[str, Any]] = [
        _basket_to_plan(list(cheapest) * people, items, coupons,
                        constraints, CALORIE_BUDGET_LABELS[0])]
    if protein_best != cheapest:
        plans.append(_basket_to_plan(list(protein_best) * people, items,
                                     coupons, constraints,
                                     CALORIE_BUDGET_LABELS[1]))

    # 方案 3：均衡混搭——每个主餐保留其最省组合，取 ≤3 个不同主餐轮流分配到人。
    # 优先「主餐+小食+饮品」完整结构（更贴合配餐语义），无完整结构时退化为任意组合。
    full_struct = [cb for cb in combos if len(cb) == 3]
    mix_pool = full_struct if full_struct else combos
    best_per_main: Dict[int, Tuple[int, ...]] = {}
    for cb in sorted(mix_pool, key=lambda cb: _combo_stats(cb, items)[1]):
        best_per_main.setdefault(cb[0], cb)
    distinct = list(best_per_main.values())[:min(3, people)]
    if len(distinct) > 1:
        base, extra = divmod(people, len(distinct))
        basket: List[int] = []
        for j, cb in enumerate(distinct):
            basket.extend(list(cb) * (base + (1 if j < extra else 0)))
        plans.append(_basket_to_plan(basket, items, coupons, constraints,
                                     CALORIE_BUDGET_LABELS[2]))

    for p in plans:
        per_kcal = float((p.get("nutrition") or {}).get("per_capita_kcal") or 0.0)
        p["calorie_budget"] = {
            "cap_per_person_kcal": cap,
            "per_capita_kcal": per_kcal,
            "headroom_kcal": round(cap - per_kcal, 1),
            "mode": "per_person",
        }
    return plans[:top_n]


# ---------------------------------------------------------------------------
# 4.3 NSGA-II 多目标优化（批 2 算法升级）
#
#     替换原「加权评分」排序：以**帕累托支配关系**为主、拥挤距离为辅，
#     直接在四目标空间（价格↓ / 人均热量↓ / 油炸份数↓ / 人均蛋白↑）中演化，
#     产出**多样化**的帕累托最优解集，再从前沿上取命名极值并自适应补席标注为
#     「最省钱 / 最健康 / 最快」——三套方案的取舍关系由前沿结构自然给出，
#     不再依赖主观权重。
#
#     * 演化期目标用「未扣券成本」快速评估；最终前沿用 _basket_to_plan
#       （含券组合 DP）重建后**重新分层**，保证标签对应的极值点与券后价一致；
#     * 个体可行性修复：主食份数 ≥ 人数（硬底线）、人均热量上限、单位上限；
#     * 固定随机种子（NSGA2_SEED），结果可复现；
#     * 原加权路径保留为 method="weighted"（回退与对照用）。
# ---------------------------------------------------------------------------

#: NSGA-II 参数（经验值：样例菜单 <1s，160 品真实菜单 ~2s）
NSGA2_GENERATIONS = 20
NSGA2_POP_SIZE = 40
NSGA2_SEED = 20261010
#: 早停窗口：第一前沿目标签名连续 N 代不变视为收敛，提前终止演化
#: （固定种子下早停路径确定，可复现性不受影响）
NSGA2_EARLY_STOP = 5
#: 单位上限：单人 ≤6 份、多人 ≤ 2×人数（防爆篮子）
NSGA2_UNITS_CAP = lambda people: max(6, 2 * people)  # noqa: E731


def _plan_objectives(plan: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """方案四目标向量（**全部最小化**）：价格 / 人均热量 / 油炸份数 / 负人均蛋白。"""
    n = plan.get("nutrition") or {}
    fried = sum(1 for ln in plan.get("items") or []
                if "fried" in (ln.get("tags") or []))
    return (float(plan.get("final_price_cny") or 0.0),
            float(n.get("per_capita_kcal") or 0.0),
            float(fried),
            -float(n.get("per_capita_protein_g") or 0.0))


def _dominates(oa: Sequence[float], ob: Sequence[float]) -> bool:
    """帕累托支配：oa 在所有目标上 ≤ ob，且至少一个严格 <（浮点容差 1e-9）。"""
    return (all(a <= b + 1e-9 for a, b in zip(oa, ob))
            and any(a < b - 1e-9 for a, b in zip(oa, ob)))


def _fast_non_dominated_sort(objs: List[Sequence[float]]) -> List[List[int]]:
    """NSGA-II 快速非支配排序：返回按层排列的索引分层 [[F1], [F2], ...]。"""
    n = len(objs)
    dominated_by: List[List[int]] = [[] for _ in range(n)]
    dom_count = [0] * n
    fronts: List[List[int]] = [[]]
    for i in range(n):
        for j in range(i + 1, n):
            if _dominates(objs[i], objs[j]):
                dominated_by[i].append(j)
                dom_count[j] += 1
            elif _dominates(objs[j], objs[i]):
                dominated_by[j].append(i)
                dom_count[i] += 1
    for i in range(n):
        if dom_count[i] == 0:
            fronts[0].append(i)
    k = 0
    while fronts[k]:
        nxt: List[int] = []
        for i in fronts[k]:
            for j in dominated_by[i]:
                dom_count[j] -= 1
                if dom_count[j] == 0:
                    nxt.append(j)
        k += 1
        fronts.append(nxt)
    return [f for f in fronts if f]


def _crowding_distance(front: List[int],
                       objs: List[Sequence[float]]) -> Dict[int, float]:
    """NSGA-II 拥挤距离：边界点 inf，中间点为相邻解在各目标上的归一化间距之和。"""
    dist = {i: 0.0 for i in front}
    if len(front) <= 2:
        return {i: float("inf") for i in front}
    for obj_i in range(len(objs[front[0]])):
        ordered = sorted(front, key=lambda i: objs[i][obj_i])
        dist[ordered[0]] = float("inf")
        dist[ordered[-1]] = float("inf")
        span = objs[ordered[-1]][obj_i] - objs[ordered[0]][obj_i]
        if span <= 1e-12:
            continue
        for a, b, c in zip(ordered, ordered[1:], ordered[2:]):
            dist[b] = dist[b] + (objs[c][obj_i] - objs[a][obj_i]) / span
    return dist


def _basket_cost_cents(basket: List[int],
                       items: List[Dict[str, Any]]) -> int:
    """篮子未扣券成本（分）——演化期快速评估用。"""
    return sum(items[i]["price"] for i in basket)


def _repair_individual(basket: List[int], items: List[Dict[str, Any]],
                       people: int, kcal_max: float) -> List[int]:
    """可行性修复：主食 ≥ 人数 → 单位 ≤ 上限 → 热量 ≤ 上限（顺序执行）。

    热量超限时：先删非主食；再删多余主食；只剩「每人 1 份主食」仍超时，
    把热量最高的主食换成热量更低的主食（主食底线不可破，只能换）。
    """
    b = list(basket)
    mains_idx = [i for i in range(len(items)) if "main" in items[i]["tags"]]
    if not mains_idx:
        return b
    cheapest_main = min(mains_idx, key=lambda i: items[i]["price"])
    while _count_mains(b, items) < people:
        b.append(cheapest_main)
    units_cap = NSGA2_UNITS_CAP(people)
    while len(b) > units_cap:
        non_mains = [i for i in b if "main" not in items[i]["tags"]]
        if non_mains:
            b.remove(max(non_mains, key=lambda i: items[i]["kcal"]))
        else:
            b.pop()
    while _basket_kcal(b, items) > kcal_max:
        non_mains = [i for i in b if "main" not in items[i]["tags"]]
        if non_mains:
            b.remove(max(non_mains, key=lambda i: items[i]["kcal"]))
            continue
        if len(b) > people:
            b.pop()                       # 多余的主食先删
            continue
        # 只剩每人 1 份主食仍超 → 换成热量更低的主食
        mains_in = [i for i in b if "main" in items[i]["tags"]]
        worst = max(mains_in, key=lambda i: items[i]["kcal"])
        best = min(mains_idx, key=lambda i: items[i]["kcal"])
        if items[best]["kcal"] < items[worst]["kcal"]:
            b[b.index(worst)] = best
        else:
            break    # 已是最低热量主食：约束本身不可满足，交给支配关系与 flags
    return b


def _random_individual(items: List[Dict[str, Any]], people: int,
                       kcal_min: float, kcal_max: float,
                       rng: random.Random) -> List[int]:
    """随机可行个体：每人 1 份随机主食 + 随机补充至热量下限，再修复。"""
    mains_idx = [i for i in range(len(items)) if "main" in items[i]["tags"]]
    if not mains_idx:
        return []
    basket = [rng.choice(mains_idx) for _ in range(people)]
    guard = 0
    while _basket_kcal(basket, items) < kcal_min and guard < 50:
        basket.append(rng.randrange(len(items)))
        guard += 1
    return _repair_individual(basket, items, people, kcal_max)


def _nsga2_seed_population(items: List[Dict[str, Any]], people: int,
                           kcal_min: float, kcal_max: float,
                           pop_size: int, rng: random.Random) -> List[List[int]]:
    """初始种群：DP 最省钱解 + 成本-热量曲线抽样 + 贪心健康/快 + 随机个体。"""
    seeds: List[List[int]] = []
    try:
        for sol in _dp_min_cost(items, people, kcal_min, kcal_max)[:8]:
            seeds.append(list(sol["indices"]))
    except Exception:  # noqa: BLE001 —— DP 对极端区间可能无解，不影响 GA
        pass
    try:
        for pt in price_kcal_candidates(items, people, kcal_min, kcal_max)[:8]:
            seeds.append(list(pt["indices"]))
    except Exception:  # noqa: BLE001
        pass
    # 贪心：最健康（蛋白密度优先）/ 最快（非油炸优先），与加权路径同思路
    idx_of = {id(it): i for i, it in enumerate(items)}
    for keyfn in (lambda x: (-(x["protein"] / max(x["kcal"], 1)), x["fat"], x["price"]),
                  lambda x: (bool("fried" in x["tags"]), x["price"])):
        ordered = sorted(items,
                         key=lambda x: ((0 if "main" in x["tags"] else 1),) + keyfn(x))
        pick: List[int] = []
        acc = 0.0
        for it in ordered:
            if acc >= kcal_min and _count_mains(pick, items) >= people:
                break
            pick.append(idx_of[id(it)])
            acc += it["kcal"]
        if pick:
            seeds.append(pick)
    # 去重 + 修复
    seen: set = set()
    unique: List[List[int]] = []
    for b in seeds:
        rb = _repair_individual(b, items, people, kcal_max)
        key = tuple(sorted(rb))
        if key not in seen and rb:
            seen.add(key)
            unique.append(rb)
    # 随机个体补齐
    guard = 0
    while len(unique) < pop_size and guard < pop_size * 10:
        rb = _random_individual(items, people, kcal_min, kcal_max, rng)
        guard += 1
        if not rb:
            break
        key = tuple(sorted(rb))
        if key not in seen:
            seen.add(key)
            unique.append(rb)
    return unique[:pop_size]


def _nsga2_evolve(items: List[Dict[str, Any]], people: int,
                  kcal_min: float, kcal_max: float,
                  generations: int = NSGA2_GENERATIONS,
                  pop_size: int = NSGA2_POP_SIZE,
                  seed: int = NSGA2_SEED) -> List[List[int]]:
    """NSGA-II 主循环：锦标赛选择 + 均匀交叉 + 变异 + 环境选择（rank + 拥挤距离）。"""
    rng = random.Random(seed)
    pop = _nsga2_seed_population(items, people, kcal_min, kcal_max, pop_size, rng)
    if not pop:
        return []

    def raw_objs(b: List[int]) -> Tuple[float, float, float, float]:
        return (_basket_cost_cents(b, items) / 100.0,
                _basket_kcal(b, items) / max(people, 1),
                float(sum(1 for i in b if "fried" in items[i]["tags"])),
                -sum(items[i]["protein"] for i in b) / max(people, 1))

    def select(population: List[List[int]],
               rank: Dict[int, int],
               crowd: Dict[int, float]) -> List[int]:
        """二元锦标赛：rank 小者胜，同 rank 拥挤距离大者胜。"""
        a, b = rng.randrange(len(population)), rng.randrange(len(population))
        if rank.get(a, 99) != rank.get(b, 99):
            return population[a if rank.get(a, 99) < rank.get(b, 99) else b]
        return population[a if crowd.get(a, 0.0) >= crowd.get(b, 0.0) else b]

    front_sig: Optional[tuple] = None
    stable = 0
    for _ in range(max(generations, 1)):
        objs = [raw_objs(b) for b in pop]
        fronts = _fast_non_dominated_sort(objs)
        # 早停：前沿四目标极值点（价格min/热量min/油炸min/蛋白max，即核心方案
        # 极值的来源）连续 N 代不变 → 输出已收敛，剩余代数只填充前沿中部
        fo = [objs[i] for i in fronts[0]]
        sig = (min(o[0] for o in fo), min(o[1] for o in fo),
               min(o[2] for o in fo), min(o[3] for o in fo))
        if sig == front_sig:
            stable += 1
            if stable >= NSGA2_EARLY_STOP:
                break
        else:
            front_sig, stable = sig, 0
        rank: Dict[int, int] = {}
        crowd: Dict[int, float] = {}
        for r, front in enumerate(fronts):
            for i in front:
                rank[i] = r
            for i, d in _crowding_distance(front, objs).items():
                crowd[i] = d
        # 产生子代
        offspring: List[List[int]] = []
        while len(offspring) < len(pop):
            p1 = select(pop, rank, crowd)
            p2 = select(pop, rank, crowd)
            merged = list(p1) + list(p2)
            rng.shuffle(merged)
            child = merged[:max(len(p1), len(p2))]
            child = _repair_individual(child, items, people, kcal_max)
            # 变异（35% 替换 / 25% 添加 / 40% 删除非主食）
            r = rng.random()
            if r < 0.35 and child:
                child[rng.randrange(len(child))] = rng.randrange(len(items))
            elif r < 0.60:
                child = child + [rng.randrange(len(items))]
            else:
                non_mains = [k for k, v in enumerate(child)
                             if "main" not in items[v]["tags"]]
                if non_mains:
                    del child[rng.choice(non_mains)]
                elif len(child) > people:
                    child.pop()
            child = _repair_individual(child, items, people, kcal_max)
            if child:
                offspring.append(child)
        # 环境选择：父代+子代合并，按 (rank, 拥挤距离) 截断
        combined = pop + offspring
        c_objs = [raw_objs(b) for b in combined]
        c_fronts = _fast_non_dominated_sort(c_objs)
        new_pop: List[List[int]] = []
        for front in c_fronts:
            if len(new_pop) + len(front) <= len(pop):
                new_pop.extend(combined[i] for i in front)
            else:
                dist = _crowding_distance(front, c_objs)
                rest = sorted(front, key=lambda i: -dist[i])
                new_pop.extend(combined[i] for i in rest[: len(pop) - len(new_pop)])
                break
        pop = new_pop if new_pop else pop
    # 去重返回
    seen: set = set()
    out: List[List[int]] = []
    for b in pop:
        key = tuple(sorted(b))
        if key not in seen:
            seen.add(key)
            out.append(b)
    return out


def _basket_key(plan: Dict[str, Any]) -> tuple:
    """方案的内容键（用于去重，与加权路径同口径）。"""
    return tuple(sorted((ln["productCode"] or ln["name"], ln["quantity"])
                        for ln in plan["items"]))


# ---------------------------------------------------------------------------
# 4.5 健壮性基础设施：脱敏、JSONL 日志、指数退避重试、超时、幂等键
#
#     * 日志：每次 MCP 调用记录 工具名/耗时/状态(成功|失败|降级)/重试次数，
#       JSON Lines 落盘 logs/mcp_calls.log；敏感信息一律占位符替代。
#     * 重试：仅对 429 / 网络超时 / 5xx 重试，间隔 1s/2s/4s，最多 3 次，
#       每次重试前检查总预算（MCP_TOTAL_BUDGET_S）；401/400/业务错误**不重试**。
#     * 超时：单次调用 MCP_TIMEOUT_S=10s，超时抛 TimeoutError 由调用方走降级。
#     * 幂等：create-order 前生成幂等键（user+store+订单内容哈希+5 分钟时间窗口），
#       命中已有成功订单则直接复用，不重复下单；记录存 logs/idempotency.json。
# ---------------------------------------------------------------------------

def redact_text(text: Any) -> str:
    """敏感信息脱敏：手机号→[PHONE]、地址→[ADDRESS]、Bearer Token→[TOKEN]。"""
    s = str(text or "")
    s = _PHONE_RE.sub(REDACT_PHONE, s)
    s = re.sub(r"Bearer\s+\S+", f"Bearer {REDACT_TOKEN}", s)
    s = re.sub(r"(?i)(token|authorization)[\"':= ]+\S{8,}",
               lambda m: m.group(0).split(m.group(1))[0] + m.group(1) + "=" + REDACT_TOKEN,
               s)
    for kw in ("addressDetail", "fullAddress", "address"):
        s = re.sub(rf'"{kw}"\s*:\s*"[^"]*"', f'"{kw}": "{REDACT_ADDRESS}"', s)
    return s


def _redact_obj(obj: Any) -> Any:
    """递归脱敏 dict/list/str（用于日志入参快照）。"""
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if lk in ("token", "authorization"):
                out[k] = REDACT_TOKEN
            elif lk in ("address", "addressdetail", "fulladdress"):
                out[k] = REDACT_ADDRESS
            elif lk in ("phone", "contactphone", "mobile", "tel"):
                out[k] = REDACT_PHONE
            else:
                out[k] = _redact_obj(v)
        return out
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, str):
        return redact_text(obj)
    return obj


def log_mcp_call(
    tool: str,
    duration_ms: int,
    status: str,                 # success | error | degraded | retry
    retries: int = 0,
    arguments: Optional[Dict[str, Any]] = None,
    detail: str = "",
    trace_id: Optional[str] = None,
) -> None:
    """追加一条 JSON Lines 日志（失败时静默跳过，不影响主流程）。

    ``trace_id`` 为服务端返回的 traceId（成功路径才有），用于证明数据实时性。
    """
    rec = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "tool": tool,
        "duration_ms": int(duration_ms),
        "status": status,
        "retries": int(retries),
        "args": _redact_obj(arguments or {}),
        "detail": redact_text(detail)[:500],
    }
    if trace_id:
        rec["trace_id"] = trace_id
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # 日志失败不阻断业务


def _is_retryable_error(exc: Exception) -> bool:
    """可重试：429 限流 / 网络超时 / 5xx 服务端错误；其余（401/400/业务错）不重试。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError)):
        # URLError 包一层时看内因是否超时
        reason = getattr(exc, "reason", None)
        if isinstance(reason, Exception) and not isinstance(reason, (TimeoutError, ConnectionError)):
            return isinstance(reason, OSError)
        return True
    return False


# --- ReflectCache：参数级 MCP 结果缓存（批 2 算法升级） --------------------
#
#     精确匹配「工具名 + 参数」复用只读工具的响应，减少 MCP 重复调用：
#     * 键 = sha256(工具名 + 规范化参数 JSON)——参数级**精确匹配**，不做模糊匹配、
#       不做语义归并（{"a":1} 与 {"a":1.0}、{"a":"1"} 视为不同参数）；
#     * 只缓存**只读工具**（CACHEABLE_TOOLS 白名单），写操作一律穿透；
#     * 只缓存**成功响应**（isError=false 且 structuredContent.success ≠ false）；
#     * `now-time-info` 永不缓存（时间敏感）；
#     * TTL 默认 300s（`MCD_CACHE_TTL_S` 可调）；`MCD_REFLECT_CACHE=0` 整体关闭；
#     * 这也是规则 5「429 优先读本地缓存」的落地实现——限流时同参数调用直接命中缓存。
# ---------------------------------------------------------------------------

#: 默认缓存 TTL（秒）：菜单/券/营养数据的变化频率远低于该窗口
REFLECT_CACHE_TTL_S = 300.0

#: 只读工具白名单（可缓存）；写操作（create-order / cancel-order / mall-create-order /
#: party-order-create / draw-lottery / auto-bind-coupons / delivery-create-address）一律不缓存
CACHEABLE_TOOLS = frozenset({
    "query-nearby-stores", "delivery-query-stores", "delivery-query-addresses",
    "query-meals", "query-meal-detail", "calculate-price", "list-nutrition-foods",
    "available-coupons", "query-my-coupons", "query-store-coupons", "query-promotions",
    "query-meal-assistance", "query-survey-coupon", "order-list", "query-order",
    "query-my-account", "mall-points-products", "mall-product-detail",
    "mall-order-list", "mall-order-detail", "query-lottery-info", "query-my-prizes",
    "campaign-calendar", "query-party-city", "query-party-store",
    "query-party-store-date", "query-party-store-session",
})

#: 时间敏感工具：永不缓存
NEVER_CACHE_TOOLS = frozenset({"now-time-info"})


def _reflect_cache_enabled() -> bool:
    """环境开关：MCD_REFLECT_CACHE=0 关闭缓存（默认开启）。"""
    return os.environ.get("MCD_REFLECT_CACHE", "1") != "0"


def _reflect_cache_ttl() -> float:
    """TTL 读取：MCD_CACHE_TTL_S（秒），非法值回退默认。"""
    raw = os.environ.get("MCD_CACHE_TTL_S")
    try:
        v = float(raw) if raw is not None else REFLECT_CACHE_TTL_S
        return v if v >= 0 else REFLECT_CACHE_TTL_S
    except (TypeError, ValueError):
        return REFLECT_CACHE_TTL_S


class ReflectCache:
    """参数级 MCP 响应缓存。

    键 = ``sha256(tool + "\\x00" + canonical_json(arguments))``；
    值 = ``{"resp", "ts", "tool"}``。命中条件：键存在 **且** 未过 TTL。
    统计：hits / misses / entries / hit_rate（供输出层透明展示）。
    """

    def __init__(self, ttl_s: Optional[float] = None, max_entries: int = 256):
        self._ttl = ttl_s          # None → 每次读取环境变量（运行期可调）
        self.max_entries = max_entries
        self._store: Dict[str, Dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0

    @staticmethod
    def make_key(tool: str, arguments: Dict[str, Any]) -> str:
        """参数级精确键：工具名 + 规范化 JSON（键排序、无空白、UTF-8）。"""
        canon = json.dumps(arguments or {}, ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(f"{tool}\x00{canon}".encode("utf-8")).hexdigest()

    def _ttl_now(self) -> Tuple[float, float]:
        return (self._ttl if self._ttl is not None else _reflect_cache_ttl(),
                time.time())

    def lookup(self, tool: str, arguments: Dict[str, Any],
               ) -> Optional[Dict[str, Any]]:
        """命中返回响应的深拷贝（调用方可安全修改），未命中/过期返回 None。"""
        key = self.make_key(tool, arguments)
        ent = self._store.get(key)
        ttl, now = self._ttl_now()
        if ent is None or now - ent["ts"] > ttl:
            if ent is not None and now - ent["ts"] > ttl:
                del self._store[key]          # 惰性清理过期项
            self.misses += 1
            return None
        self.hits += 1
        return copy.deepcopy(ent["resp"])

    def store(self, tool: str, arguments: Dict[str, Any],
              resp: Dict[str, Any]) -> None:
        """写入缓存；超过容量时按最旧淘汰（FIFO by ts）。"""
        key = self.make_key(tool, arguments)
        if len(self._store) >= self.max_entries and key not in self._store:
            oldest = min(self._store, key=lambda k: self._store[k]["ts"])
            del self._store[oldest]
        self._store[key] = {"resp": copy.deepcopy(resp), "ts": time.time(),
                            "tool": tool}

    def stats(self) -> Dict[str, Any]:
        total = self.hits + self.misses
        return {"hits": self.hits, "misses": self.misses,
                "entries": len(self._store),
                "hit_rate": round(self.hits / total, 4) if total else 0.0}

    def clear(self) -> None:
        self._store.clear()
        self.hits = 0
        self.misses = 0


#: 进程级单例（所有 _mcp_call 调用方自动共享）
REFLECT_CACHE = ReflectCache()


def _reflect_cache_lookup(tool: str, arguments: Dict[str, Any],
                          ) -> Optional[Dict[str, Any]]:
    """统一查询入口：非白名单 / 环境关闭 / 永不缓存工具 直接返回 None。"""
    if (tool not in CACHEABLE_TOOLS or tool in NEVER_CACHE_TOOLS
            or not _reflect_cache_enabled()):
        return None
    return REFLECT_CACHE.lookup(tool, arguments)


def _reflect_cache_store(tool: str, arguments: Dict[str, Any],
                         resp: Dict[str, Any]) -> None:
    """统一写入入口：仅白名单工具且业务成功（success ≠ false 且 isError ≠ true）。"""
    if tool not in CACHEABLE_TOOLS or tool in NEVER_CACHE_TOOLS:
        return
    if not _reflect_cache_enabled():
        return
    result = (resp or {}).get("result") or {}
    if result.get("isError"):
        return
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and sc.get("success") is False:
        return                      # 业务错误不缓存（重试语义不受影响）
    REFLECT_CACHE.store(tool, arguments, resp)


def _mcp_call_once(tool: str, arguments: Dict[str, Any], token: str,
                   timeout: int) -> Dict[str, Any]:
    """单次 MCP HTTP 调用（无重试），超时抛 TimeoutError。"""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            exc_body = exc.read().decode("utf-8", errors="replace")[:300]
        except Exception:  # noqa: BLE001 —— 读错误体失败无所谓
            exc_body = ""
        raise urllib.error.HTTPError(exc.url, exc.code,
                                     f"{exc.reason}|{exc_body}", exc.headers, None)  # type: ignore[arg-type]
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if isinstance(reason, TimeoutError) or (
                isinstance(reason, Exception) and "timed out" in str(reason).lower()):
            raise TimeoutError(f"MCP 调用超时（{timeout}s）: {exc}") from exc
        raise
    except TimeoutError:
        raise
    if body.lstrip().startswith("event:") or "\ndata:" in body:
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
    return json.loads(body)


def _mcp_call(tool: str, arguments: Dict[str, Any], token: Optional[str] = None,
              timeout: int = MCP_TIMEOUT_S,
              retries: Optional[int] = None,
              budget_s: float = MCP_TOTAL_BUDGET_S,
              log: bool = True,
              use_cache: bool = True) -> Dict[str, Any]:
    """带**指数退避重试 + 超时 + 日志 + 参数级缓存**的 MCP 调用（统一入口）。

    * 重试条件：429 / 网络超时 / 5xx；间隔 1s→2s→4s，最多 3 次；
      每次重试前检查总预算 ``budget_s``，超预算立即放弃。
    * 不重试：401 / 400 / 其他 4xx / 业务错误（success=false，由调用方判读）。
    * ReflectCache（``use_cache=True``）：只读工具按「工具名 + 参数」精确匹配
      复用近期成功响应——同参数重复调用不再触网（规则 5「429 优先读本地缓存」
      的落地实现）。写操作与 now-time-info 永不缓存。
    * 全程记录 JSONL 日志（工具名/耗时/状态(含 cache)/重试次数，入参脱敏快照）。
    """
    # --- ReflectCache：参数级缓存优先于网络（含重试） -----------------------
    if use_cache:
        cached = _reflect_cache_lookup(tool, arguments)
        if cached is not None:
            if log:
                log_mcp_call(tool, 0, "cache", retries=0, arguments=arguments,
                             detail="ReflectCache 命中（工具名+参数 精确匹配，未触网）")
            return cached

    token = token or os.environ.get("MCD_MCP_TOKEN") or ""
    max_retry = MCP_RETRY_MAX if retries is None else int(retries)
    attempt = 0
    started = time.monotonic()
    last_exc: Optional[Exception] = None
    while attempt <= max_retry:
        attempt += 1
        t0 = time.monotonic()
        try:
            resp = _mcp_call_once(tool, arguments, token, timeout)
            if log:
                _sc = (resp.get("result") or {}).get("structuredContent")
                _tid = _sc.get("traceId") if isinstance(_sc, dict) else None
                log_mcp_call(tool, int((time.monotonic() - t0) * 1000), "success",
                             retries=attempt - 1, arguments=arguments,
                             trace_id=_tid if isinstance(_tid, str) else None)
            if use_cache:
                _reflect_cache_store(tool, arguments, resp)
            return resp
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            elapsed = time.monotonic() - started
            retryable = _is_retryable_error(exc)
            if log:
                log_mcp_call(tool, int((time.monotonic() - t0) * 1000),
                             "retry" if retryable else "error",
                             retries=attempt - 1, arguments=arguments,
                             detail=f"{type(exc).__name__}: {exc}")
            if not retryable:
                break
            if attempt > max_retry:
                break
            backoff = MCP_BACKOFF_S[min(attempt - 1, len(MCP_BACKOFF_S) - 1)]
            if elapsed + backoff > budget_s:
                if log:
                    log_mcp_call(tool, 0, "error", retries=attempt - 1,
                                 arguments=arguments, detail="总预算耗尽，放弃重试")
                break
            time.sleep(backoff)
    raise last_exc if last_exc else RuntimeError(f"MCP 调用失败: {tool}")


# --- 降级策略 -------------------------------------------------------------

#: 各 MCP 工具超时/失败后的降级行为（供上层决策与用户提示）
DEGRADE_RULES: Dict[str, Dict[str, Any]] = {
    "query-meals": {"fallback": "cache", "note": "菜单查询失败，已回退到本地缓存菜单"},
    "list-nutrition-foods": {"fallback": "cache", "note": "营养查询失败，已回退到本地缓存营养表"},
    "query-store-coupons": {"fallback": "skip_coupons", "note": "券查询失败，本次仅按满减促销计算，结果可能偏高"},
    "query-promotions": {"fallback": "skip_promotions", "note": "促销查询失败，本次跳过满减规则"},
    "calculate-price": {"fallback": "mark_unverified", "note": "核价失败，价格以实际下单为准"},
    "query-nearby-stores": {"fallback": "ask_user", "note": "门店查询失败，请手动提供门店编号或名称"},
    "delivery-query-stores": {"fallback": "ask_user", "note": "配送门店查询失败，请确认地址后重试"},
    "delivery-query-addresses": {"fallback": "ask_user", "note": "地址查询失败，请手动提供配送地址"},
    "create-order": {"fallback": "none", "note": "下单失败，不降级（写操作必须真实成功）"},
}

#: 降级提示统一话术
DEGRADED_BANNER = "该结果基于缓存/降级逻辑，可能与实际有偏差"


def degrade_for(tool: str) -> Dict[str, Any]:
    """返回工具的降级策略与提示文案；未知工具默认「如实报告失败」。"""
    rule = DEGRADE_RULES.get(tool)
    if rule:
        return {"tool": tool, **rule, "banner": DEGRADED_BANNER,
                "degraded": True}
    return {"tool": tool, "fallback": "none", "note": f"{tool} 调用失败",
            "banner": DEGRADED_BANNER, "degraded": True}


def call_with_degrade(
    tool: str,
    arguments: Dict[str, Any],
    token: Optional[str] = None,
    cache_loader: Optional[Any] = None,
) -> Dict[str, Any]:
    """MCP 调用 + 超时降级包装：失败时按 DEGRADE_RULES 返回降级结果。

    Returns
    -------
    dict
        ``{"ok": bool, "data": …, "degraded": bool, "banner": str, "note": str,
        "error": str|None, "fallback": str}``。``ok=False`` 且 ``degraded=True``
        表示走降级路径，调用方须把 ``banner`` 展示给用户。
    """
    t0 = time.monotonic()
    try:
        resp = _mcp_call(tool, arguments, token=token)
        sc = (resp.get("result") or {}).get("structuredContent") or {}
        if sc.get("success") is False:
            # 业务错误（门店打烊等）：不重试、不降级编数据，如实返回
            return {"ok": False, "data": None, "degraded": False, "banner": None,
                    "note": f"业务错误 {sc.get('code')}: {sc.get('message')}",
                    "error": f"业务错误 {sc.get('code')}: {sc.get('message')}",
                    "fallback": "none"}
        return {"ok": True, "data": sc.get("data"), "degraded": False,
                "banner": None, "note": "", "error": None, "fallback": "none"}
    except Exception as exc:  # noqa: BLE001 —— 超时/网络/5xx 全部走降级
        dg = degrade_for(tool)
        data = None
        if dg["fallback"] == "cache" and cache_loader is not None:
            try:
                data = cache_loader()
                dg = {**dg, "note": dg["note"] + "（已命中本地缓存）"}
            except Exception:  # noqa: BLE001 —— 缓存也没有就纯降级
                dg = {**dg, "note": dg["note"] + "（本地缓存不可用）"}
        log_mcp_call(tool, int((time.monotonic() - t0) * 1000), "degraded",
                     arguments=arguments, detail=f"降级: {type(exc).__name__}: {exc}")
        return {"ok": False, "data": data, "degraded": True,
                "banner": dg["banner"], "note": dg["note"],
                "error": f"{type(exc).__name__}: {exc}", "fallback": dg["fallback"]}


# --- 幂等键（防重复下单） --------------------------------------------------

_IDEMPOTENCY_FILE = os.path.join(LOG_DIR, "idempotency.json")


def make_idempotency_key(
    user_id: str,
    store_code: str,
    items: Sequence[Tuple[str, int]],
    now: Optional[float] = None,
) -> Tuple[str, int]:
    """生成 create-order 幂等键：user+store+订单内容哈希+5 分钟时间窗口。

    同一 5 分钟窗口内，同一用户在同一门店提交**完全相同**的订单内容，
    幂等键相同 → 视为重复提交，复用已有订单。窗口序号随时间前进，
    正常的「隔几分钟再点一单」不受影响。
    """
    ts = float(now if now is not None else time.time())
    window = int(ts // IDEMPOTENCY_WINDOW_S)
    normalized = json.dumps(
        [[str(c), int(q)] for c, q in sorted(items, key=lambda x: str(x[0]))],
        ensure_ascii=False, sort_keys=True)
    raw = f"{user_id}|{store_code}|{normalized}|{window}"
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return key, window


def _load_idempotency_store() -> Dict[str, Dict[str, Any]]:
    try:
        with open(_IDEMPOTENCY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_idempotency_store(store: Dict[str, Dict[str, Any]]) -> None:
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(_IDEMPOTENCY_FILE, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass  # 持久化失败不阻断（下次可能重复下单，但比崩溃好）


def check_idempotency(
    user_id: str,
    store_code: str,
    items: Sequence[Tuple[str, int]],
    now: Optional[float] = None,
    store: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """create-order 前置检查：命中已有成功订单 → 直接复用，不重复创建。

    Returns
    -------
    dict
        ``{"duplicate": bool, "key": str, "existing_order": dict|None,
        "window": int}``。``duplicate=True`` 时 ``existing_order``
        含订单号与时间戳，调用方应直接返回该订单。
    """
    key, window = make_idempotency_key(user_id, store_code, items, now=now)
    db = store if store is not None else _load_idempotency_store()
    hit = db.get(key)
    if hit and hit.get("status") == "success":
        return {"duplicate": True, "key": key, "existing_order": hit, "window": window}
    return {"duplicate": False, "key": key, "existing_order": None, "window": window}


def record_idempotency(
    key: str,
    order_id: str,
    status: str = "success",
    now: Optional[float] = None,
    store: Optional[Dict[str, Dict[str, Any]]] = None,
) -> None:
    """记录幂等键 → 订单号映射（create-order 成功后必须调用）。

    ``store`` 注入时仅更新内存（测试用），不落盘。
    """
    ts_now = float(now if now is not None else time.time())
    if store is not None:
        store[key] = {
            "order_id": str(order_id),
            "status": status,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts_now)),
        }
        return
    db = _load_idempotency_store()
    db[key] = {
        "order_id": str(order_id),
        "status": status,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts_now)),
        "_epoch": ts_now,
    }
    # 清理 >1 天的过期条目，防文件无限膨胀
    cutoff = ts_now - 86400
    db = {k: v for k, v in db.items()
          if not isinstance(v, dict) or float(v.get("_epoch", 0) or 0) >= cutoff}
    _save_idempotency_store(db)


def create_order_guarded(
    user_id: str,
    store_code: str,
    items: Sequence[Tuple[str, int]],
    order_arguments: Dict[str, Any],
    token: Optional[str] = None,
    mcp_call: Optional[Any] = None,
    now: Optional[float] = None,
    idem_store: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """**幂等下单入口**：查重 → 调 create-order → 记录幂等键。

    * 幂等键命中已有成功订单 → **不调用 MCP**，直接返回已有订单
      （``duplicate=True``）；
    * 调用失败 / 业务错误 → 不记录幂等键（下次可重试），如实返回错误；
    * 成功 → 记录 ``key → orderId``，窗口内重复提交会被拦截。

    ``mcp_call`` 可注入自定义函数用于测试（默认用 :func:`_mcp_call`）。
    """
    chk = check_idempotency(user_id, store_code, items, now=now, store=idem_store)
    if chk["duplicate"]:
        log_mcp_call("create-order", 0, "success", arguments={"storeCode": store_code},
                     detail=f"幂等命中，复用订单 {chk['existing_order'].get('order_id')}")
        return {"ok": True, "duplicate": True,
                "order": chk["existing_order"],
                "key": chk["key"], "error": None}

    caller = mcp_call or _mcp_call
    try:
        resp = caller("create-order", order_arguments, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "duplicate": False, "order": None,
                "key": chk["key"], "error": f"{type(exc).__name__}: {exc}"}

    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "duplicate": False, "order": None, "key": chk["key"],
                "error": f"业务错误 {sc.get('code')}: {sc.get('message')}"}
    data = sc.get("data") or {}
    order_id = (data.get("orderId") or data.get("orderNo")
                or data.get("order_id") or "")
    if not order_id:
        # 没拿到订单号视为未确认成功：不记录幂等键（宁可重复检查，不可漏单）
        return {"ok": False, "duplicate": False, "order": data, "key": chk["key"],
                "error": "响应中未找到订单号，未记录幂等键"}
    record_idempotency(chk["key"], str(order_id), "success",
                       now=now, store=idem_store)
    return {"ok": True, "duplicate": False, "order": data,
            "key": chk["key"], "error": None}


# ---------------------------------------------------------------------------
# 5. 核价校验（MCP calculate-price）
#     ⚠️ _mcp_call 已升级为带 重试/超时/日志 的统一入口（见第 4.5 节），
#        verify_price 等全部调用方自动获得健壮性能力。
# ---------------------------------------------------------------------------

def verify_price(
    plan: Dict[str, Any],
    store_code: str,
    token: Optional[str] = None,
    be_type: Optional[int] = None,
    order_type: Optional[int] = None,
    be_code: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 ``calculate-price`` 核价，并与优化器估算值比对。

    ⚠️ ``calculate-price`` 返回的价格单位是「分」，本函数统一换算成「元」。

    Returns
    -------
    dict
        ``{"ok", "official_final_cny", "estimated_cny", "diff_cny", "diff_pct",
        "trust", "business_code", "raw", "error"}``
    """
    be_type = be_type if be_type is not None else plan.get("beType")
    order_type = order_type if order_type is not None else plan.get("orderType")
    be_code = be_code if be_code is not None else plan.get("beCode")

    first_coupon = next((c for c in plan.get("coupons", []) if c.get("couponId")), None)
    mcp_items: List[Dict[str, Any]] = []
    for ln in plan.get("items", []):
        entry: Dict[str, Any] = {
            "productCode": ln["productCode"],
            "quantity": int(ln["quantity"]),
        }
        if first_coupon:
            entry["couponId"] = first_coupon["couponId"]
        mcp_items.append(entry)

    arguments: Dict[str, Any] = {
        "storeCode": str(store_code),
        "orderType": int(order_type if order_type is not None else 1),
        "beType": int(be_type if be_type is not None else 1),
        "items": mcp_items,
    }
    if be_code:
        arguments["beCode"] = str(be_code)

    estimated = float(plan.get("final_price_cny") or 0.0)
    try:
        resp = _mcp_call("calculate-price", arguments, token=token)
    except Exception as exc:  # noqa: BLE001 —— 网络/鉴权异常统一兜住
        return {"ok": False, "official_final_cny": None, "estimated_cny": estimated,
                "diff_cny": None, "diff_pct": None, "trust": "estimated",
                "business_code": None, "raw": None, "error": f"{type(exc).__name__}: {exc}"}

    result = resp.get("result") or {}
    if result.get("isError"):
        return {"ok": False, "official_final_cny": None, "estimated_cny": estimated,
                "diff_cny": None, "diff_pct": None, "trust": "estimated",
                "business_code": None, "raw": result, "error": "MCP 返回 isError=True"}

    sc = result.get("structuredContent") or {}
    # ⚠️ 实测偏差：本 MCP 的业务错误**不会**置 isError=True，
    #    而是把 success=false + code + message 放在 structuredContent 里。
    if sc.get("success") is False:
        code = sc.get("code")
        msg = sc.get("message")
        return {"ok": False, "official_final_cny": None, "estimated_cny": estimated,
                "diff_cny": None, "diff_pct": None, "trust": "estimated",
                "business_code": code, "raw": sc,
                "error": f"服务端业务错误 {code}: {msg}"}

    data = sc.get("data") or {}
    cents = None
    for key in ("finalPrice", "payPrice", "totalPrice", "actualPrice", "price"):
        if isinstance(data.get(key), (int, float)):
            cents = float(data[key])
            break
    if cents is None:
        return {"ok": False, "official_final_cny": None, "estimated_cny": estimated,
                "diff_cny": None, "diff_pct": None, "trust": "estimated",
                "business_code": sc.get("code"), "raw": data,
                "error": "未在响应中找到价格字段，请核对 schema 偏差"}   # noqa: E501

    official = round(cents / 100.0, 2)          # 分 → 元
    diff = round(official - estimated, 2)
    diff_pct = round(abs(diff) / official * 100, 2) if official else 0.0
    return {
        "ok": True,
        "official_final_cny": official,
        "estimated_cny": estimated,
        "diff_cny": diff,
        "diff_pct": diff_pct,
        # 偏差 > 5% 时以官方价为准（见 SKILL.md 第八节）
        "trust": "official",
        "raw": data,
        "error": None,
    }


# ---------------------------------------------------------------------------
# 5.5 企业团餐专用链路（beType=6）
#     团餐是唯一可用 query-promotions / query-meal-assistance 的场景，
#     这两条链路必须在 calculate-price 之前接入，否则算出的价格会偏高。
# ---------------------------------------------------------------------------

def _promotion_to_coupon(promo: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 ``query-promotions`` 返回的促销规则转成内部券结构。

    规则字段（实测 schema）：``promotionId`` / ``promotionType`` / ``ruleCategory`` /
    ``ruleDetail`` / ``products``。含义：
      - ``promotionType``：31 = 满额减，33 = 订单折扣
      - ``ruleCategory``：30 = 满折/折扣，40 = 满减
      - ``products.type``：1 包含 / 2 不包含 / 3 全部

    ⚠️ ``ruleDetail`` 内部字段名官方未在 schema 中展开（仅描述为"规则明细"），
    因此本函数对常见命名做**容错取值**；若全部取不到则返回 ``None`` 并记录告警，
    不会静默产生错误的优惠额。
    """
    def pick(*keys: str) -> Optional[float]:
        for src in (promo.get("ruleDetail") or {}, promo):
            if isinstance(src, dict):
                for k in keys:
                    v = src.get(k)
                    if isinstance(v, (int, float)) and v > 0:
                        return float(v)
                    if isinstance(v, str) and v.replace(".", "", 1).isdigit():
                        return float(v)
        return None

    promo_type = promo.get("promotionType")
    rule_cat = promo.get("ruleCategory")
    cid = str(promo.get("promotionId") or f"promo-{promo_type}-{rule_cat}")

    # 满减：31 / 40
    if promo_type == 31 or rule_cat == 40:
        threshold = pick("thresholdAmount", "threshold", "conditionAmount", "fullAmount", "amountCondition")
        discount = pick("discountAmount", "reduceAmount", "offAmount", "cutAmount", "amount")
        if threshold is None or discount is None:
            return None
        return {
            "couponId": cid, "couponCode": None,
            "name": f"团餐满减(满{threshold / 100:.0f}减{discount / 100:.0f})",
            "type": "threshold", "minSpend": int(threshold), "discount": int(discount),
            "exclusiveGroup": "group_order", "source": "query-promotions",
        }

    # 折扣：33 / 30（rate 表示"应付比例"，如 0.9 = 9 折）
    if promo_type == 33 or rule_cat == 30:
        rate = pick("discountRate", "rate", "discount", "zhe", "discountValue")
        if rate is None:
            return None
        if rate > 1:                      # 兼容"9"表示 9 折的写法
            rate = rate / 10.0
        if not (0 < rate < 1):
            return None
        return {
            "couponId": cid, "couponCode": None,
            "name": f"团餐折扣({rate * 10:.1f}折)",
            "type": "percent", "minSpend": 0, "rate": rate,
            "exclusiveGroup": "group_order", "source": "query-promotions",
        }
    return None


def fetch_group_meal_promotions(
    store_code: str,
    be_code: str,
    token: Optional[str] = None,
    reservation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 ``query-promotions`` 取团餐满减/满折规则，转为内部券结构。

    ⚠️ 该工具**仅企业团餐(beType=6)可用**，其他场景调用会失败。
    """
    args: Dict[str, Any] = {
        "storeCode": str(store_code), "beCode": str(be_code),
        "orderType": 2, "beType": 6,          # 团餐固定：外送 + beType=6
    }
    if reservation_date:
        args["reservationDate"] = reservation_date
    try:
        resp = _mcp_call("query-promotions", args, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "coupons": [], "error": f"{type(exc).__name__}: {exc}", "raw": None}

    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "coupons": [], "raw": sc,
                "error": f"服务端业务错误 {sc.get('code')}: {sc.get('message')}"}

    rules = sc.get("data") or []
    if isinstance(rules, dict):
        rules = rules.get("promotions") or rules.get("list") or []
    coupons, skipped = [], []
    for r in rules if isinstance(rules, list) else []:
        c = _promotion_to_coupon(r) if isinstance(r, dict) else None
        (coupons if c else skipped).append(c or r)
    return {"ok": True, "coupons": coupons, "unparsed": skipped, "raw": rules, "error": None}


def fetch_meal_assistance(
    store_code: str,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
    reservation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """调用 ``query-meal-assistance`` 取团餐可用助餐服务（拿 ``gmServiceCode``）。

    ⚠️ 该工具**仅企业团餐(beType=6)可用**；选中的 ``gmServiceCode`` 必须传给
    ``calculate-price`` 与 ``create-order``。
    """
    args: Dict[str, Any] = {"storeCode": str(store_code), "orderType": 2, "beType": 6}
    if be_code:
        args["beCode"] = str(be_code)
    if reservation_date:
        args["reservationDate"] = reservation_date
    try:
        resp = _mcp_call("query-meal-assistance", args, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "options": [], "error": f"{type(exc).__name__}: {exc}", "raw": None}

    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "options": [], "raw": sc,
                "error": f"服务端业务错误 {sc.get('code')}: {sc.get('message')}"}

    data = sc.get("data") or []
    if isinstance(data, dict):
        data = data.get("services") or data.get("list") or [data]
    options = []
    for s in data if isinstance(data, list) else []:
        if isinstance(s, dict):
            options.append({
                "gmServiceCode": s.get("gmServiceCode") or s.get("serviceCode") or s.get("code"),
                "name": s.get("serviceName") or s.get("name") or s.get("gmServiceName"),
                "raw": s,
            })
    return {"ok": True, "options": options, "raw": data, "error": None}


def fetch_group_meal_context(
    store_code: str,
    be_code: str,
    token: Optional[str] = None,
    reservation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """一次取齐团餐上下文：促销规则 + 助餐服务。

    典型用法::

        ctx = fetch_group_meal_context(store_code, be_code)
        plans = optimize_plans(menu, store_coupons + ctx["promotions"], constraints)
        # 下单时把选中的 gmServiceCode 传给 calculate-price / create-order
    """
    promo = fetch_group_meal_promotions(store_code, be_code, token, reservation_date)
    assist = fetch_meal_assistance(store_code, be_code, token, reservation_date)
    return {
        "promotions": promo.get("coupons") or [],
        "promotions_ok": promo.get("ok"),
        "promotions_error": promo.get("error"),
        "promotions_unparsed": promo.get("unparsed") or [],
        "assistance": assist.get("options") or [],
        "assistance_ok": assist.get("ok"),
        "assistance_error": assist.get("error"),
    }


# ---------------------------------------------------------------------------
# 5.7 全链路真实化（方向 5.5）：任意输入 → 真实 MCP 数据
#
#     主链路数据来源契约（评审输入任意场景时，以下数据必须来自实时 MCP）：
#     * 营养表 ← list-nutrition-foods（实测 data 为 CSV 文本，160 品）
#     * 菜单   ← query-meals（两级结构：categories[].meals[].code + meals 映射表；
#                价格为「元」字符串（偏差 4）；discountType 含「随单购」按
#                originalPrice 计价、其余按 currentPrice —— 与 docs/pricing-verification.md
#                口径一致；营养按「名称归一化精确匹配」关联，未命中置 0、不估算）
#     * 券     ← query-store-coupons（唯一权威；券池可能为空 —— 空即无券，
#                属真实结果，不造券；最终价以 calculate-price 核价为准）
#     * 门店   ← MCD_STORE_CODE / --store 参数 > query-nearby-stores 自动选址
#                > 默认演示门店（仅此步允许默认，且如实标注来源）
#
#     降级契约（不偷偷用假数据）：
#     * MCP 调用失败（网络/超时/Token 失效/非营业时段业务错误）→ 明确输出
#       失败原因与营业时段提示；
#     * 兜底数据（SAMPLE_MENU / SAMPLE_COUPONS）仅在用户未加 ``--strict`` 时
#       使用，且输出中**强制标注**「兜底数据，非实时」横幅；
#     * ``--strict`` 模式下 MCP 不可用直接报错退出，绝不降级。
# ---------------------------------------------------------------------------

#: 数据来源与降级横幅（输出中必须原样展示，不得省略）
LIVE_SOURCE_NOTE = "数据来源：实时 MCP"
FALLBACK_BANNER = ("⚠️ 兜底数据，非实时：MCP 获取失败，以下结果基于内置样例菜单/券，"
                   "可能与实际有偏差（营业时段一般为 07:00–22:00，请稍后用真实数据重跑）")
DEMO_BANNER = "⚠️ 演示模式：以下结果基于内置样例数据（非实时 MCP），仅用于展示算法能力"

#: 自动选址兜底门店（北京国贸附近，营业 07:00–22:00，实测可下单）
DEFAULT_STORE_CODE = "1950936"

#: 名称归一化：去空格/™/®/引号；【】[] 去括号保留内容（品牌前缀）；（）() 去括号及内容（包装规格）
_NAME_BRACKETS_KEEP_RE = re.compile(r"[【\[]([^（(【\[]*?)[】\]]")
_NAME_BRACKETS_STRIP_RE = re.compile(r"[（(]([^（(【\[]*?)[）)]")


def normalize_food_name(name: Any) -> str:
    """餐品名称归一化（用于 query-meals ↔ list-nutrition-foods 精确匹配）。

    例：``纯牛奶(盒装)`` → ``纯牛奶``；``【美汁源】"黄金橙橙"`` → ``美汁源黄金橙橙``。
    规则：圆括号（包装/规格描述）连同内容一并去除；方括号【】（品牌前缀）去
    括号保留内容，避免品牌信息丢失导致跨来源匹配退化（两侧对称归一）。
    """
    s = str(name or "")
    s = s.replace(" ", "").replace("™", "").replace("®", "")
    s = _NAME_BRACKETS_KEEP_RE.sub(r"\1", s)
    s = _NAME_BRACKETS_STRIP_RE.sub("", s)
    for q in ("\"", "'", "“", "”", "‘", "’"):
        s = s.replace(q, "")
    return s


def parse_nutrition_csv(text: str) -> List[Dict[str, Any]]:
    """解析 ``list-nutrition-foods`` 的 data 字段（实测为 CSV 文本，非 JSON 数组）。

    格式（首行表头 + 每行一条）::

        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213

    返回 ``[{name, kcal, protein, fat, carb, sodium}]``；无法解析的行跳过，不编造。
    """
    columns: Optional[List[str]] = None
    items: List[Dict[str, Any]] = []
    for ln in str(text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^\[\d+\]\{([^}]*)\}:?$", ln)
        if m:
            columns = [c.strip() for c in m.group(1).split(",")]
            continue
        if not columns or "productName" not in columns:
            continue
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < len(columns):
            continue
        row = dict(zip(columns, parts))

        def _num(key: str) -> float:
            try:
                return float(row.get(key) or 0)
            except (TypeError, ValueError):
                return 0.0

        items.append({
            "name": row.get("productName") or "",
            "kcal": _num("energyKcal"),
            "protein": _num("protein"),
            "fat": _num("fat"),
            "carb": _num("carbohydrate"),
            "sodium": _num("sodium"),
        })
    return items


def _yuan_str_to_cents(v: Any) -> Optional[int]:
    """query-meals 价格字段（「元」字符串，偏差 4）→ 内部「分」。失败返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v) * 100))
    if isinstance(v, str):
        try:
            return int(round(float(v.strip()) * 100))
        except ValueError:
            return None
    return None


def fetch_nutrition_table(token: Optional[str] = None) -> Dict[str, Any]:
    """调 ``list-nutrition-foods`` 拉取全量营养表（真实数据）。

    Returns: ``{"ok", "items", "by_name"(归一化名称→营养), "count", "error", "trace_id"}``
    """
    try:
        resp = _mcp_call("list-nutrition-foods", {}, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "items": [], "by_name": {}, "count": 0,
                "error": f"{type(exc).__name__}: {exc}", "trace_id": None}
    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "items": [], "by_name": {}, "count": 0,
                "error": f"业务错误 {sc.get('code')}: {sc.get('message')}",
                "trace_id": sc.get("traceId")}
    data = sc.get("data")
    items: List[Dict[str, Any]] = []
    if isinstance(data, str):
        items = parse_nutrition_csv(data)
    elif isinstance(data, list):        # schema 若改为 JSON 数组的兼容路径
        for d in data:
            if isinstance(d, dict):
                items.append({
                    "name": str(d.get("productName") or d.get("name") or ""),
                    "kcal": float(d.get("energyKcal") or 0),
                    "protein": float(d.get("protein") or 0),
                    "fat": float(d.get("fat") or 0),
                    "carb": float(d.get("carbohydrate") or 0),
                    "sodium": float(d.get("sodium") or 0),
                })
    by_name = {normalize_food_name(i["name"]): i for i in items if i["name"]}
    return {"ok": True, "items": items, "by_name": by_name, "count": len(items),
            "error": None, "trace_id": sc.get("traceId")}


def fetch_real_menu(
    store_code: str,
    be_type: int = 1,
    order_type: int = 1,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
    nutrition_by_name: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """调 ``query-meals`` 拉取**实时菜单**并转成内部结构（价格为分）。

    * 两级结构（偏差 8）：``data.categories[].meals[].code`` + ``data.meals`` 映射表；
    * 计价口径：``discountType`` 含「随单购」→ ``originalPrice``，否则 ``currentPrice``；
    * 营养：与营养表按归一化名称**精确匹配**，未命中置 0（不估算、不编造）。

    Returns: ``{"ok", "menu", "count", "nutrition_matched", "nutrition_unmatched",
    "error", "business_error", "trace_id"}``
    """
    args: Dict[str, Any] = {
        "storeCode": str(store_code),
        "orderType": int(order_type or 1),
        "beType": int(be_type or 1),
    }
    if be_code:
        args["beCode"] = str(be_code)
    try:
        resp = _mcp_call("query-meals", args, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "menu": [], "count": 0, "nutrition_matched": 0,
                "nutrition_unmatched": 0, "error": f"{type(exc).__name__}: {exc}",
                "business_error": False, "trace_id": None}
    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "menu": [], "count": 0, "nutrition_matched": 0,
                "nutrition_unmatched": 0,
                "error": f"业务错误 {sc.get('code')}: {sc.get('message')}",
                "business_error": True, "trace_id": sc.get("traceId")}
    data = sc.get("data")
    meals_map: Dict[str, Any] = {}
    if isinstance(data, dict) and isinstance(data.get("meals"), dict):
        meals_map = data["meals"]
    elif isinstance(data, list):       # 扁平结构兼容
        for i, m in enumerate(data):
            if isinstance(m, dict):
                meals_map[str(m.get("code") or m.get("productCode") or i)] = m
    menu: List[Dict[str, Any]] = []
    matched = 0
    for code, m in meals_map.items():
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        if not name:
            continue
        current = _yuan_str_to_cents(m.get("currentPrice"))
        original = _yuan_str_to_cents(m.get("originalPrice"))
        discount_type = str(m.get("discountType") or "")
        price = original if "随单购" in discount_type else current
        if price is None:
            price = original if original is not None else current
        if price is None:
            continue                   # 无价格信息：不入菜单，不编造
        nut = (nutrition_by_name or {}).get(normalize_food_name(name))
        if nut:
            matched += 1
        menu.append({
            "productCode": str(code),
            "name": name,
            "price": price,           # 分
            "kcal": float(nut["kcal"]) if nut else 0.0,
            "protein": float(nut["protein"]) if nut else 0.0,
            "fat": float(nut["fat"]) if nut else 0.0,
            "sodium": float(nut["sodium"]) if nut else 0.0,
            "carb": float(nut["carb"]) if nut else 0.0,
            "discountType": discount_type,
            "nutrition_matched": bool(nut),
        })
    return {"ok": True, "menu": menu, "count": len(menu),
            "nutrition_matched": matched,
            "nutrition_unmatched": len(menu) - matched,
            "error": None, "business_error": False, "trace_id": sc.get("traceId")}


def _normalize_mcp_coupon(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把 ``query-store-coupons`` 返回的券转成内部券结构；字段缺失返回 None（不猜）。

    ⚠️ 实测该账户券池为空，以下字段映射按官方文档命名做**容错取值**；
    金额单位（元/分）歧义时按「优惠额 < 100 视为元」启发式处理并在
    ``note`` 中标注，**最终优惠价以 calculate-price 核价为准**。
    """
    name = c.get("name") or c.get("title") or c.get("couponName") or ""
    cid = c.get("couponId") or c.get("id") or c.get("couponCode") or ""
    if not str(name).strip() or not str(cid).strip():
        return None

    def _cents(v: Any, *, is_discount: bool = False) -> Optional[int]:
        """元/分歧义消解：优惠额 < 100 几乎不可能是「分」（<1 元券不现实）→ 视为元。

        注意：≥100 一律按「分」处理；100~199 区间存在理论歧义（如 ¥150 门槛
        会被读作 150 分），启发式无法完全消解——最终金额以 calculate-price
        官方核价为准（见 SKILL.md 核价规则）。``is_discount`` 参数保留以兼容
        调用方，两类字段的阈值行为一致。
        """
        if isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            if v >= 100:
                return int(v)
            return int(round(v * 100))
        if isinstance(v, str):
            try:
                return _cents(float(v.strip()), is_discount=is_discount)
            except ValueError:
                return None
        return None

    def _pick(*keys: str) -> Any:
        for k in keys:
            if c.get(k) is not None:
                return c.get(k)
        return None

    min_spend = _cents(_pick("minSpend", "thresholdAmount", "useThreshold",
                              "minPrice", "threshold"))
    discount = _cents(_pick("discount", "discountAmount", "reduceAmount",
                            "amount", "value"), is_discount=True)
    rate = _pick("rate", "discountRate")
    try:
        rate = float(rate) if rate is not None else None
        if rate is not None and rate > 1:
            rate = rate / 10.0
    except (TypeError, ValueError):
        rate = None

    out: Dict[str, Any] = {
        "couponId": str(cid),
        "couponCode": str(c.get("couponCode") or "") or None,
        "name": str(name),
        "source": "query-store-coupons",
        "raw": c,
    }
    if rate is not None and 0 < rate < 1:
        out.update({"type": "percent", "minSpend": min_spend or 0, "rate": rate,
                    "exclusiveGroup": "order"})
    elif min_spend is not None and discount is not None:
        out.update({"type": "threshold", "minSpend": min_spend,
                    "discount": discount, "exclusiveGroup": "order"})
    else:
        return None               # 结构不认识：宁可不加这张券，也不算错优惠
    return out


def fetch_real_coupons(
    store_code: str,
    be_type: int = 1,
    order_type: int = 1,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """调 ``query-store-coupons`` 拉取**门店真实可用券**。

    券池为空是**真实结果**（该门店/渠道当前无券），``ok=True, coupons=[]``。

    Returns: ``{"ok", "coupons", "skipped"(无法解析的原始券), "error",
    "business_error", "trace_id"}``
    """
    args: Dict[str, Any] = {
        "storeCode": str(store_code),
        "orderType": int(order_type or 1),
        "beType": int(be_type or 1),
    }
    if be_code:
        args["beCode"] = str(be_code)
    try:
        resp = _mcp_call("query-store-coupons", args, token=token)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "coupons": [], "skipped": [],
                "error": f"{type(exc).__name__}: {exc}",
                "business_error": False, "trace_id": None}
    sc = (resp.get("result") or {}).get("structuredContent") or {}
    if sc.get("success") is False:
        return {"ok": False, "coupons": [], "skipped": [],
                "error": f"业务错误 {sc.get('code')}: {sc.get('message')}",
                "business_error": True, "trace_id": sc.get("traceId")}
    data = sc.get("data")
    raw: List[Any] = []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = data.get("coupons") or data.get("list") or []
    coupons: List[Dict[str, Any]] = []
    skipped: List[Any] = []
    for c in raw:
        if isinstance(c, dict):
            nc = _normalize_mcp_coupon(c)
            (coupons if nc else skipped).append(nc if nc else c)
    return {"ok": True, "coupons": coupons, "skipped": skipped,
            "error": None, "business_error": False, "trace_id": sc.get("traceId")}


def resolve_store(
    token: Optional[str] = None,
    be_type: int = 1,
    city: Optional[str] = None,
    keyword: Optional[str] = None,
) -> Dict[str, Any]:
    """选址：``MCD_STORE_CODE`` 环境变量 > ``query-nearby-stores`` 自动选址 >
    默认演示门店（仅此步允许默认值，且 ``source`` 如实标注）。

    Returns: ``{"ok", "store_code", "store_name", "be_code", "source", "error"}``
    """
    env = os.environ.get("MCD_STORE_CODE")
    if env:
        return {"ok": True, "store_code": str(env), "store_name": None,
                "be_code": None, "source": "MCD_STORE_CODE 环境变量", "error": None}
    city = city or os.environ.get("MCD_STORE_CITY", "北京市")
    keyword = keyword or os.environ.get("MCD_STORE_KEYWORD", "国贸")
    # query-nearby-stores 合法 beType 仅 [1, 5]（得来速场景才有 beCode 可取）
    # （2026-10-10 13:41 复验：beType=6 被 MCP 参数校验拒绝，enum 仅 [1,5]；
    #   团餐门店请走 delivery-query-stores，见 references/tools.md）
    search_be_type = 5 if int(be_type or 1) == 5 else 1
    try:
        resp = _mcp_call("query-nearby-stores",
                         {"beType": search_be_type, "searchType": 2,
                          "city": city, "keyword": keyword},
                         token=token)
        sc = (resp.get("result") or {}).get("structuredContent") or {}
        if sc.get("success") is False:
            raise RuntimeError(f"业务错误 {sc.get('code')}: {sc.get('message')}")
        data = sc.get("data")
        stores = data if isinstance(data, list) else (
            (data or {}).get("stores") or (data or {}).get("list") or [])
        for s in stores:
            if not isinstance(s, dict):
                continue
            if s.get("businessStatus") is False:
                continue               # 打烊门店跳过
            code = s.get("storeCode") or s.get("code")
            if not code:
                continue
            return {"ok": True, "store_code": str(code),
                    "store_name": s.get("storeName") or s.get("name"),
                    "be_code": (str(s["beCode"]) if s.get("beCode") else None),
                    "source": f"query-nearby-stores（{city}·{keyword} 自动选址）",
                    "error": None}
        raise RuntimeError("返回门店列表为空或无营业中门店")
    except Exception as exc:  # noqa: BLE001 —— 自动选址失败 → 默认门店 + 如实标注
        return {"ok": False, "store_code": DEFAULT_STORE_CODE, "store_name": None,
                "be_code": None, "source": "默认演示门店（自动选址失败）",
                "error": f"{type(exc).__name__}: {exc}"}


def fetch_live_context(
    store_code: str,
    constraints: Optional[Dict[str, Any]] = None,
    be_type: Optional[int] = None,
    order_type: Optional[int] = None,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """一次取齐规划所需**真实数据**：营养表 + 菜单 + 门店券（+ 团餐促销）。

    ``ok=False`` 表示菜单拿不到（真实链路无法进行，调用方须走降级/报错）；
    营养/券单项失败不阻断，但会写入 ``notes`` 供输出层显式提示。

    Returns: ``{"ok", "menu", "coupons", "store_code", "be_type", "order_type",
    "traces"[(tool, trace_id)], "notes"[str], "errors"{tool: err}, "fetched_at"}``
    """
    notes: List[str] = []
    errors: Dict[str, str] = {}
    traces: List[Dict[str, Any]] = []

    # 场景参数：优先显式入参 > constraints 推断 > 到店自取默认
    c = constraints or {}
    bt = int(be_type if be_type is not None else (c.get("beType") or 1) or 1)
    ot = int(order_type if order_type is not None
             else (c.get("orderType") or SCENE_ORDERTYPE.get(bt, 1)) or 1)
    if bt in (5, 6) and not be_code:
        # 得来速/团餐链路必须 beCode；缺失时按到店自取口径取数并如实标注
        notes.append(f"场景 beType={bt} 需要门店 beCode（得来速经 query-nearby-stores"
                     f"(beType=5)、团餐经 delivery-query-stores 获取），本次未提供，"
                     f"菜单按到店自取（beType=1）口径获取，价格与可用品类可能存在差异。")
        bt, ot = 1, 1

    # --- 并行取数：营养表与门店券互不依赖，并行拉取（3 RTT → 2 RTT）--------
    #     菜单依赖营养表（nutrition_by_name 做精确匹配），串行在两者之后。
    #     ReflectCache 为参数级缓存（GIL 下 dict 原子操作），线程安全；
    #     日志按行 append，乱序无害（每行带时间戳）。
    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_nutrition = pool.submit(fetch_nutrition_table, token)
        fut_coupons = pool.submit(
            fetch_real_coupons, store_code,
            be_type=bt, order_type=ot, be_code=be_code, token=token)
        nutrition = fut_nutrition.result()
        coupons = fut_coupons.result()

    if nutrition["ok"]:
        traces.append({"tool": "list-nutrition-foods",
                       "trace_id": nutrition.get("trace_id")})
    else:
        errors["list-nutrition-foods"] = nutrition["error"] or ""
        notes.append("营养表获取失败：本次营养值按 0 计（不估算、不编造），"
                     "「最健康」方案的营养评分可能失真。")

    menu = fetch_real_menu(store_code, be_type=bt, order_type=ot,
                           be_code=be_code, token=token,
                           nutrition_by_name=nutrition.get("by_name"))
    if menu["ok"]:
        traces.append({"tool": "query-meals", "trace_id": menu.get("trace_id")})
        notes.append(
            f"菜单 {menu['count']} 品（实时）；营养精确匹配 {menu['nutrition_matched']} 品，"
            f"未匹配 {menu['nutrition_unmatched']} 品按 0 计（多为套餐/组合/周边，"
            f"list-nutrition-foods 无套餐口径，不估算）。")
    else:
        errors["query-meals"] = menu["error"] or ""

    coupons_out: List[Dict[str, Any]] = []
    if coupons["ok"]:
        traces.append({"tool": "query-store-coupons",
                       "trace_id": coupons.get("trace_id")})
        coupons_out = coupons["coupons"]
        if coupons.get("skipped"):
            notes.append(f"门店券中 {len(coupons['skipped'])} 张结构无法解析，已跳过"
                         f"（宁可不计入优惠，不算错价）。")
        if not coupons_out:
            notes.append("门店券池为空（query-store-coupons 实时返回）→ 本次按无券计算。")
    else:
        errors["query-store-coupons"] = coupons["error"] or ""
        notes.append("门店券查询失败：本次按无券计算，结果可能偏高。")

    # 团餐场景：促销规则（需 beCode，前面缺 beCode 时已退化为 beType=1，不会走到）
    if bt == 6 and be_code:
        promo = fetch_group_meal_promotions(store_code, be_code, token)
        if promo.get("ok"):
            coupons_out = coupons_out + (promo.get("coupons") or [])
            notes.append(f"团餐促销规则已并入券池（query-promotions，"
                         f"{len(promo.get('coupons') or [])} 条可解析）。")
        else:
            errors["query-promotions"] = promo.get("error") or ""
            notes.append("团餐促销查询失败：未并入满减/满折规则，结果可能偏高。")

    return {
        "ok": bool(menu["ok"]),
        "menu": menu.get("menu") or [],
        "coupons": coupons_out,
        "store_code": str(store_code),
        "be_type": bt,
        "order_type": ot,
        "traces": traces,
        "notes": notes,
        "errors": errors,
        "menu_error": menu.get("error"),
        "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def run_live_optimization(
    user_input: str,
    store_code: Optional[str] = None,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """**全链路真实化入口**：解析约束 → 实时取数 → 过滤 → 优化。

    Returns: ``{"ok", "constraints", "kept", "plans", "context", "error"}``。
    ``ok=False`` 时 ``error`` 为 MCP 获取失败原因（调用方须显式提示，不得静默降级）。
    """
    constraints = parse_constraints(user_input)
    store = str(store_code) if store_code else resolve_store(token=token)["store_code"]
    ctx = fetch_live_context(store, constraints, be_code=be_code, token=token)
    if not ctx["ok"]:
        return {"ok": False, "constraints": constraints, "kept": [],
                "plans": [], "context": ctx, "error": ctx.get("menu_error")}
    try:
        kept = filter_menu(_normalize_menu(ctx["menu"]), constraints)
    except ValueError as exc:
        # 与 main 的过滤异常处理同模式：硬约束无解时返回结构化错误，不裸抛
        return {"ok": False, "constraints": constraints, "kept": [],
                "plans": [], "context": ctx, "error": str(exc)}
    plans = optimize_plans(kept, ctx["coupons"], constraints)
    return {"ok": True, "constraints": constraints, "kept": kept,
            "plans": plans, "context": ctx, "error": None}


def acquire_planning_data(
    constraints: Dict[str, Any],
    demo: bool = False,
    strict: bool = False,
    store_code: Optional[str] = None,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
) -> Dict[str, Any]:
    """获取规划用菜单与券：**默认真实 MCP**；失败时按降级策略兜底（显式标注）。

    Returns: ``{"source"(live|fallback|demo|error), "menu", "coupons", "banner",
    "notes", "traces", "store_code", "be_type", "order_type", "error", "fetched_at"}``
    """
    if demo:
        return {"source": "demo", "menu": _normalize_menu(SAMPLE_MENU),
                "coupons": SAMPLE_COUPONS, "banner": DEMO_BANNER,
                "notes": [], "traces": [], "store_code": None,
                "be_type": None, "order_type": None, "error": None, "fetched_at": None}

    tok = token or os.environ.get("MCD_MCP_TOKEN") or ""
    if not tok:
        err = "未设置 MCD_MCP_TOKEN 环境变量（Token 从 https://open.mcd.cn/mcp 获取）"
    else:
        if store_code:
            store = str(store_code)
        else:
            rs = resolve_store(token=tok,
                               be_type=int(constraints.get("beType") or 1) or 1)
            store = rs["store_code"]
        ctx = fetch_live_context(store, constraints, be_code=be_code, token=tok)
        if ctx["ok"]:
            return {"source": "live", "menu": _normalize_menu(ctx["menu"]),
                    "coupons": ctx["coupons"], "banner": None,
                    "notes": list(ctx["notes"]), "traces": ctx["traces"],
                    "store_code": store, "be_type": ctx["be_type"],
                    "order_type": ctx["order_type"], "error": None,
                    "fetched_at": ctx["fetched_at"]}
        err = ctx.get("menu_error") or "未知错误"

    # ---- MCP 不可用：如实报告；仅非 strict 模式才允许兜底，且强制标注 ----
    if strict:
        return {"source": "error", "menu": [], "coupons": [], "banner": None,
                "notes": [], "traces": [], "store_code": None, "be_type": None,
                "order_type": None, "error": err, "fetched_at": None}
    return {"source": "fallback", "menu": _normalize_menu(SAMPLE_MENU),
            "coupons": SAMPLE_COUPONS, "banner": FALLBACK_BANNER,
            "notes": [f"MCP 获取失败原因：{err}"], "traces": [],
            "store_code": None, "be_type": None, "order_type": None,
            "error": err, "fetched_at": None}


# ---------------------------------------------------------------------------
# 5.6 渠道比价（到手成本：到店自取 vs 麦乐送 vs 得来速）
#
#     ⚠️ 能力边界（2026-10-10 实测，详见 MCP_INTEGRATION.md）：
#     * MCP 的 35 个工具**不提供**美团 / 饿了么等第三方平台比价接口，
#       本模块只对比麦当劳内部渠道，外部平台价格不做、不估、不编造。
#     * beType 枚举仅 [1, 2, 5, 6]，**不存在 beType=3**（参数校验直接拒绝）；
#       得来速的正确编码是 **beType=5**，orderType=1，beCode 必传。
#     * calculate-price 返回完整到手成本结构（单位均为「分」）：
#       productPrice(商品) + deliveryPrice(运费) + packingPrice(打包费)
#       + tablewarePrice(餐具费) − discount(优惠) = price(现价)。
#     * 实测同一门店 beType=1 与 beType=5 菜单与核价完全一致（¥47.00=¥47.00），
#       差异在 takeWayList：beType=1 返回 堂食/外带/得来速 三种取餐方式，
#       beType=5 锁定「得来速-车道自提」（code=take-dt-quick）。
#     * 麦乐送(beType=2)的 beCode 必须来自 delivery-query-stores（前置：
#       账户存在配送地址）。无地址 → 无法核价，如实标注，不编造运费。
# ---------------------------------------------------------------------------

#: 渠道规格：名称 → beType / orderType / beCode 要求 / beCode 来源
CHANNEL_SPECS: Dict[str, Dict[str, Any]] = {
    "到店自取": {"beType": 1, "orderType": 1, "needs_be_code": False,
                 "be_code_source": "无需 beCode（传了会报错）"},
    "麦乐送": {"beType": 2, "orderType": 2, "needs_be_code": True,
               "be_code_source": "delivery-query-stores（前置：账户需有配送地址，"
                                 "经 delivery-query-addresses 获取 addressId）"},
    "得来速": {"beType": 5, "orderType": 1, "needs_be_code": True,
               "be_code_source": "query-nearby-stores(beType=5)"},
}

#: 到店类渠道实测取餐方式 code（来自 calculate-price 的 takeWayList）
TAKE_WAY_CODES: Dict[str, str] = {
    "eat-in": "堂食",
    "locker-in": "外带（店内取餐柜）",
    "take-dt-quick": "得来速（车道自提）",
}


def fetch_channel_quote(
    items: Sequence[Tuple[str, int]],
    store_code: str,
    channel: str,
    be_code: Optional[str] = None,
    token: Optional[str] = None,
    need_tableware: Optional[bool] = None,
    reservation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """对单一渠道调 ``calculate-price``，提取完整「到手成本」结构。

    到手成本（内部单位：分；返回值已转「元」）::

        商品 productPrice + 运费 deliveryPrice + 打包费 packingPrice
        + 餐具费 tablewarePrice − 优惠 discount = 现价 price

    * ``items``: ``(productCode, quantity)`` 序列 —— **同一篮子**在各渠道间对比才公平。
    * 得来速 / 麦乐送必须传 ``be_code``；缺失时如实返回「无法核价」，不猜测、不编造。
    * 分项之和若与官方现价 ``price`` 不一致，**以现价为准**并置 ``sum_consistent=False``。

    Returns
    -------
    dict
        ``{channel, ok, error, beType, orderType, product_cny, delivery_cny,
        packing_cny, tableware_cny, discount_cny, original_cny, final_cny,
        sum_consistent, take_ways, enjoyed, enjoyable, trace_id, raw}``
    """
    spec = CHANNEL_SPECS.get(channel)
    if spec is None:
        return {"channel": channel, "ok": False,
                "error": f"未知渠道 {channel!r}（支持：{'、'.join(CHANNEL_SPECS)}；"
                         f"美团/饿了么等外部平台不在 MCP 能力范围内）"}

    if spec["needs_be_code"] and not be_code:
        return {"channel": channel, "ok": False,
                "beType": spec["beType"], "orderType": spec["orderType"],
                "error": f"无法核价：缺少 beCode（来源：{spec['be_code_source']}）"}

    arguments: Dict[str, Any] = {
        "storeCode": str(store_code),
        "orderType": spec["orderType"],
        "beType": spec["beType"],
        "items": [{"productCode": str(c), "quantity": int(q)} for c, q in items],
    }
    if be_code:
        arguments["beCode"] = str(be_code)
    if need_tableware is not None:
        arguments["needTableware"] = bool(need_tableware)
    if reservation_date:
        arguments["reservationDate"] = reservation_date

    try:
        resp = _mcp_call("calculate-price", arguments, token=token)
    except Exception as exc:  # noqa: BLE001 —— 网络/鉴权异常统一兜住
        return {"channel": channel, "ok": False,
                "beType": spec["beType"], "orderType": spec["orderType"],
                "error": f"{type(exc).__name__}: {exc}"}

    result = resp.get("result") or {}
    if result.get("isError"):
        return {"channel": channel, "ok": False,
                "error": "MCP 返回 isError=True", "raw": result}
    sc = result.get("structuredContent") or {}
    # 与 verify_price 同源的偏差 1：业务错误不置 isError，藏在 structuredContent
    if sc.get("success") is False:
        return {"channel": channel, "ok": False,
                "business_code": sc.get("code"),
                "error": f"服务端业务错误 {sc.get('code')}: {sc.get('message')}",
                "raw": sc}

    data = sc.get("data") or {}

    def _cents(key: str) -> int:
        v = data.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    product = _cents("productPrice")
    delivery = _cents("deliveryPrice")
    packing = _cents("packingPrice")
    tableware = _cents("tablewarePrice")
    discount = _cents("discount")
    final = _cents("price")
    original = _cents("originalPrice")
    sum_consistent = (product + delivery + packing + tableware - discount) == final

    take_ways = [
        {"code": t.get("code"), "title": t.get("title"), "subtitle": t.get("subtitle")}
        for t in (data.get("takeWayList") or []) if isinstance(t, dict)
    ]

    def _yuan(c: int) -> float:
        return round(c / 100.0, 2)

    return {
        "channel": channel, "ok": True, "error": None,
        "beType": spec["beType"], "orderType": spec["orderType"],
        "product_cny": _yuan(product),
        "delivery_cny": _yuan(delivery),
        "packing_cny": _yuan(packing),
        "tableware_cny": _yuan(tableware),
        "discount_cny": _yuan(discount),
        "original_cny": _yuan(original),
        "final_cny": _yuan(final),
        "sum_consistent": sum_consistent,
        "take_ways": take_ways,
        "enjoyed": data.get("enjoyed") or None,
        "enjoyable": data.get("enjoyable") or None,
        "trace_id": sc.get("traceId"),
        "raw": data,
    }


def compare_channels(
    items: Sequence[Tuple[str, int]],
    store_code: str,
    be_codes: Optional[Dict[str, str]] = None,
    token: Optional[str] = None,
    need_tableware: Optional[bool] = None,
    channels: Optional[Sequence[str]] = None,
    reservation_date: Optional[str] = None,
) -> Dict[str, Any]:
    """**同一篮子 × 多渠道「到手成本」对比**（仅麦当劳 MCP 内部渠道）。

    ⚠️ MCP 的 35 个工具**不提供**美团 / 饿了么等第三方平台比价接口，
       本函数只对比：到店自取(beType=1) / 麦乐送(beType=2) / 得来速(beType=5)。
       外部平台价格一律不做、不估、不编造。

    Parameters
    ----------
    items : (productCode, quantity) 序列
        待对比的**同一篮子**。productCode 必须来自 ``query-meals`` 真实返回。
    be_codes : dict, 可选
        渠道名 → beCode。得来速 / 麦乐送必填（缺失的渠道会如实标「无法核价」）。
    need_tableware : bool, 可选
        是否需要餐具（各渠道统一传同一值，保证对比公平）。

    Returns
    -------
    dict
        ``{store_code, items, quotes, priced_count, cheapest, scope_note}``，
        其中 ``cheapest`` 为到手成本最低的渠道列表（可能并列，如到店自取与得来速同价）。
    """
    be_codes = be_codes or {}
    names = [c for c in (channels or CHANNEL_SPECS) if c in CHANNEL_SPECS]
    quotes = [
        fetch_channel_quote(items, store_code, ch, be_code=be_codes.get(ch),
                            token=token, need_tableware=need_tableware,
                            reservation_date=reservation_date)
        for ch in names
    ]
    priced = [q for q in quotes if q.get("ok")]
    cheapest: List[str] = []
    if priced:
        low = min(q["final_cny"] for q in priced)
        cheapest = [q["channel"] for q in priced if q["final_cny"] == low]
    return {
        "store_code": str(store_code),
        "items": [{"productCode": c, "quantity": q} for c, q in items],
        "quotes": quotes,
        "priced_count": len(priced),
        "cheapest": cheapest,
        "scope_note": "仅对比麦当劳 MCP 内部渠道（到店自取/麦乐送/得来速）；"
                      "MCP 不提供美团、饿了么等第三方平台比价数据",
    }


# ---------------------------------------------------------------------------
# 6. 输出
# ---------------------------------------------------------------------------

def _disp_width(s: str) -> int:
    """字符串终端显示宽度（CJK/全角字符按 2 列计）。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in str(s))


def _pad(s: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格对齐（中文与数字混排列对齐用）。"""
    s = str(s)
    gap = max(width - _disp_width(s), 0)
    return s + " " * gap if align == "left" else " " * gap + s


def _budget_brief(constraints: Dict[str, Any]) -> str:
    """预算的一句话摘要（总预算 / 人均预算，无预算 → 不限）。"""
    b = constraints.get("budget") or {}
    if b.get("total_cny") is not None:
        return f"总预算 ≤¥{float(b['total_cny']):.0f}"
    if b.get("per_person_cny") is not None:
        return f"人均 ≤¥{float(b['per_person_cny']):.0f}"
    return "预算不限"


def constraints_brief(constraints: Dict[str, Any]) -> str:
    """约束解析结果的单行摘要（替代整份 JSON，供人类快速核对）。"""
    people = constraints.get("people_count") or 1
    scene = constraints.get("scene") or "到店自取"
    parts = [f"{people} 人", scene, _budget_brief(constraints)]
    hard = constraints.get("hard") or {}
    excl = [t for t in (hard.get("exclude_tags") or [])]
    ing = [i for i in (hard.get("exclude_ingredients") or [])]
    if ing or excl:
        parts.append("忌：" + "、".join(ing + excl))
    tgt = constraints.get("targets") or {}
    tgt_bits = []
    if tgt.get("max_calories_kcal"):
        tgt_bits.append(f"≤{tgt['max_calories_kcal']:.0f}kcal")
    if tgt.get("max_fat_g"):
        tgt_bits.append(f"脂肪≤{tgt['max_fat_g']:.0f}g")
    if tgt.get("min_protein_g"):
        tgt_bits.append(f"蛋白≥{tgt['min_protein_g']:.0f}g")
    if tgt_bits:
        parts.append("目标：" + "·".join(tgt_bits))
    personas = constraints.get("personas") or []
    if personas:
        parts.append("画像：" + "/".join(personas))
    return "｜".join(parts)


def recommend_plan(plans: List[Dict[str, Any]],
                   constraints: Dict[str, Any]) -> str:
    """N 方案对比总表 + 一句话推荐（含取舍理由，不把选择权甩给用户）。

    席位少于 3 时附一行说明：前沿只有这么多**明显不同**的取舍档位，
    其余候选与已选方案差异可忽略——诚实少于凑数。
    """
    if not plans:
        return ""
    w = constraints.get("weights") or {"cheap": 0.34, "healthy": 0.33,
                                       "speed": 0.18, "pref": 0.15}
    out: List[str] = [f"【方案对比（{len(plans)} 套）】"]
    out.append(_pad("方案", 10) + _pad("最终价", 10) + _pad("人均", 9)
               + _pad("人均kcal", 9) + _pad("蛋白g", 7) + _pad("脂肪g", 7)
               + _pad("券优惠", 9) + "体检")
    for i, p in enumerate(plans, 1):
        n = p.get("nutrition") or {}
        disc = sum(float(c.get("discount_cny") or 0) for c in (p.get("coupons") or []))
        flags = "；".join(p.get("nutrition_flags") or []) or "达标"
        out.append(_pad(f"{chr(64 + i)} {p.get('label', '')}", 10)
                   + _pad(f"¥{p['final_price_cny']:.2f}", 10)
                   + _pad(f"¥{p['per_capita_cny']:.2f}", 9)
                   + _pad(f"{(n.get('per_capita_kcal') or 0):.0f}", 9)
                   + _pad(f"{(n.get('per_capita_protein_g') or 0):.1f}", 7)
                   + _pad(f"{(n.get('per_capita_fat_g') or 0):.1f}", 7)
                   + _pad(f"−¥{disc:.2f}", 9) + flags)
    # 推荐：按约束权重综合得分最高者（与 _score 同口径），理由取其最强项
    try:
        best = max(plans, key=lambda p: _score(p, constraints, w))
    except Exception:  # noqa: BLE001 —— 评分异常时退化为最省钱
        best = plans[0]
    bi = plans.index(best) + 1
    others = [p for p in plans if p is not best]
    reason = {
        "最省钱": f"在「{_budget_brief(constraints)}」下综合得分最高，价格最稳",
        "最健康": "营养目标满足度最好（蛋白/脂肪/kcal 均衡）",
        "最快": "主食结构最简单、非油炸，出餐最快",
        "均衡": "价格/营养/速度折中，无明显短板",
    }.get(best.get("label"), "与你们的人数/预算/画像约束最匹配")
    _focus = {"最省钱": "省钱", "最健康": "营养", "最快": "速度", "均衡": "均衡"}
    alt = ""
    if others:
        o = others[0]
        oi = plans.index(o) + 1
        alt = (f"；若更在意{_focus.get(o.get('label'), '别的取舍')}"
               f"可看方案 {chr(64 + oi)}")
    out.append(f"💡 推荐方案 {chr(64 + bi)}（{best.get('label')}）：{reason}{alt}")
    if len(plans) < 3:
        out.append(f"📌 仅 {len(plans)} 套：当前菜单/约束下只有这么多明显不同的"
                   "取舍档位，其余候选差异可忽略（席位自适应，不凑数展示）")
    return "\n".join(out)


def render_plans(plans: List[Dict[str, Any]]) -> str:
    """把方案集渲染成可读文本（金额一律「元」，列按显示宽度对齐）。"""
    icon = {"最省钱": "🍔", "最健康": "🥗", "最快": "⚡",
            "均衡": "🍱", "减配方案": "🥶", "分步下单方案": "📝",
            "热量预算·最省": "🔥", "热量预算·高蛋白": "💪",
            "热量预算·均衡混搭": "🥗", "热量预算无解": "🚫"}
    out: List[str] = []
    for i, p in enumerate(plans, 1):
        out.append(f"{icon.get(p['label'], '🍟')} 方案 {chr(64 + i)}｜{p['label']}")
        if p.get("degraded"):
            out.append(f"├ ⚠️ {p.get('degrade_reason', '该方案为降级方案')}")
        out.append(f"├ 人数：{p['people_count']} 人｜主食 {p.get('mains', 0)} 份（每人至少 1 份 ✅）")
        out.append(f"├ 总份数：{p.get('units', 0)} 份")
        # 商品明细：名称左对齐、金额右对齐（显示宽度，中文 2 列）
        lines = p.get("items") or []
        name_w = max((_disp_width(ln["name"]) for ln in lines), default=0)
        out.append("├ 商品明细：")
        for ln in lines:
            amt = f"¥{ln['subtotal_cny']:.2f}"
            out.append(f"│   · {_pad(ln['name'], name_w)}  ×{ln['quantity']}  "
                       f"{_pad(amt, 9, 'right')}")
        # 券组合：合并为一行（多券用「｜」分隔，省行数省 token）
        if p.get("coupons"):
            segs = [f"{c['name']}（{c['couponId']}）−¥{c['discount_cny']:.2f}"
                    for c in p["coupons"]]
            out.append(f"├ 券组合：{'｜'.join(segs)}")
        else:
            out.append("├ 券组合：无可用券")
        out.append(f"├ 原价：¥{p['subtotal_cny']:.2f}  →  最终价：¥{p['final_price_cny']:.2f}")
        out.append(f"├ 人均：¥{p['per_capita_cny']:.2f}")
        if p.get("coupon_availability"):
            out.append(f"├ 券可用性：{p['coupon_availability']['overall']}")
        n = p.get("nutrition")
        if n:
            out.append(
                f"├ 营养标签：总热量 {n['total_kcal']:.0f} kcal｜人均 {n['per_capita_kcal']:.0f} kcal"
                f"｜人均蛋白 {n['per_capita_protein_g']:.1f} g｜人均脂肪 {n['per_capita_fat_g']:.1f} g"
            )
        cb = p.get("calorie_budget")
        if cb:
            out.append(
                f"├ 热量预算：人均 {cb['per_capita_kcal']:.0f} / {cb['cap_per_person_kcal']:.0f} kcal"
                f"（余 {cb['headroom_kcal']:.0f} kcal）"
            )
        if p.get("fairness"):
            fr = p["fairness"]
            parts = []
            for pm in fr["per_member"]:
                s = f"{pm['name']} {pm['score']:.2f}"
                if pm.get("reason"):
                    s += f"（{pm['reason']}）"
                parts.append(s)
            out.append(f"├ 公平性：整体 {fr['overall']:.2f}（木桶效应）｜成员满足度：{'｜'.join(parts)}")
        if "营养达标" in p.get("nutrition_flags", []):
            out.append(f"└ 体检：营养达标")
        else:
            flags = p.get("nutrition_flags", [])
            if flags:
                out.append(f"└ 体检：{'；'.join(flags)}")
            else:
                out.append("└ 体检：—")
        out.append("")
    return "\n".join(out)


def render_pareto(plans: List[Dict[str, Any]],
                  curve: Optional[List[Dict[str, Any]]] = None) -> str:
    """渲染「价格 × 热量」双目标结果：帕累托前沿 + （可选）成本–热量权衡曲线。"""
    out: List[str] = []
    out.append("【价格 × 热量 帕累托前沿】不做加权，全部为互不支配的解")
    if not plans:
        out.append("（无可行解，请放宽约束）")
    else:
        out.append(_pad("总热量(kcal)", 12, "right") + " " + _pad("总价(元)", 10, "right")
                   + " " + _pad("人均(元)", 10, "right") + " " + _pad("主食", 4, "right")
                   + "  边际权衡")
        out.append("-" * 78)
        for p in plans:
            n = p["nutrition"]
            edge = ""
            if p.get("price_vs_next_cny") is not None:
                edge = (f"↓换下一档省 ¥{p['price_vs_next_cny']:.2f}"
                        f"，多吃 {p['kcal_saved_vs_next']:.0f} kcal")
            out.append(_pad(f"{n['total_kcal']:.0f}", 12, "right") + " "
                       + _pad(f"{p['final_price_cny']:.2f}", 10, "right") + " "
                       + _pad(f"{p['per_capita_cny']:.2f}", 10, "right") + " "
                       + _pad(f"{p.get('mains', 0)}", 4, "right") + "  " + edge)
        if len(plans) == 1:
            out.append("")
            out.append("⚠️ 前沿退化为单点：本菜单下「最便宜」与「热量最低」是同一组解，")
            out.append("   即两个目标不冲突，不存在「多花钱换低热量」的权衡。这不是算法错误。")
            out.append("   需要看热量增加要花多少钱，请看下方成本–热量曲线。")

    if curve:
        out.append("")
        out.append("【成本–热量权衡曲线】各热量档位下的最低价方案")
        out.append(_pad("总热量(kcal)", 12, "right") + " " + _pad("最终价(元)", 11, "right")
                   + " " + _pad("人均(元)", 10, "right") + " " + _pad("主食", 4, "right")
                   + "  较上一档")
        out.append("-" * 78)
        for p in curve:
            n = p["nutrition"]
            d = p.get("delta_price_cny")
            delta = "—" if d is None else f"{'+' if d >= 0 else ''}¥{d:.2f}"
            out.append(_pad(f"{n['total_kcal']:.0f}", 12, "right") + " "
                       + _pad(f"{p['final_price_cny']:.2f}", 11, "right") + " "
                       + _pad(f"{p['per_capita_cny']:.2f}", 10, "right") + " "
                       + _pad(f"{p.get('mains', 0)}", 4, "right") + "  " + delta)
        out.append("")
        out.append("说明：本表为「给定热量下的最低价」，可直接回答「多吃 X 千卡要多花多少钱」。")
    return "\n".join(out)


def render_channel_comparison(result: Dict[str, Any]) -> str:
    """渲染「渠道比价 · 到手成本」对比表（金额一律「元」，2 位小数）。"""
    out: List[str] = []
    out.append("【渠道比价 · 到手成本】同一篮子 × 麦当劳内部渠道")
    out.append("范围说明：MCP 不提供美团/饿了么等第三方平台比价接口，"
               "外部渠道不做对比、不编造数据。")
    out.append("")
    out.append(f"{'渠道':<6} {'商品':>8} {'运费':>8} {'打包费':>8} {'餐具费':>8} "
               f"{'优惠':>8} {'到手成本':>10}  取餐方式 / 状态")
    out.append("-" * 92)

    cheapest = set(result.get("cheapest") or [])
    priced = [q for q in result.get("quotes", []) if q.get("ok")]
    low = min((q["final_cny"] for q in priced), default=None)

    for q in result.get("quotes", []):
        mark = "  💰最低" if q["channel"] in cheapest else ""
        if not q.get("ok"):
            out.append(f"{q['channel']:<6} {'—':>8} {'—':>8} {'—':>8} {'—':>8} "
                       f"{'—':>8} {'无法核价':>10}  ❌ {q.get('error')}")
            continue
        ways = "、".join((t.get("title") or t.get("code") or "?") for t in q["take_ways"]) or "—"
        flag = "" if q["sum_consistent"] else "  ⚠️分项和≠现价，以官方现价为准"
        out.append(f"{q['channel']:<6} {q['product_cny']:>8.2f} {q['delivery_cny']:>8.2f} "
                   f"{q['packing_cny']:>8.2f} {q['tableware_cny']:>8.2f} "
                   f"{q['discount_cny']:>8.2f} {q['final_cny']:>10.2f}  {ways}{flag}{mark}")

    out.append("-" * 92)
    if cheapest:
        out.append(f"结论：{'、'.join(cheapest)} 到手成本最低（¥{low:.2f}）。")
        if len(cheapest) > 1:
            out.append("同价时的取舍：开车/赶时间选得来速（免下车），顺路/堂食选到店自取"
                       "（取餐方式更灵活）；价格本身无差异。")
    elif priced:
        out.append("结论：仅一个渠道可核价，无横向对比意义。")
    else:
        out.append("结论：所有渠道均无法核价（详见各行原因），不编造任何数字。")

    # 凑单提示：enjoyable.balance = 距离下一档优惠的门槛差额（分）
    for q in priced:
        en = q.get("enjoyable") or {}
        balance = en.get("balance")
        if isinstance(balance, (int, float)) and balance > 0:
            out.append(f"凑单提示（{q['channel']}）：再买 ¥{balance / 100:.2f} "
                       f"可享下一档优惠（{en.get('realDiscount')}）。")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 6.5 智能分账（split_bill）
#
#     拼单的真实痛点不是「总价多少」而是「谁该付多少」：
#     * 按成员画像把每个商品归属到人（减脂→低热量、预算控→低价、要赠品→甜品）；
#     * 主食优先保证每人 1 份；非主食在「画像匹配 + 金额均衡」间取平衡；
#     * 券优惠按各成员小计占总价的比例分摊（整数分，尾差给小计最大者）；
#     * 内部一律以「分」计算，输出前转元，校验 Σ应付 == 方案最终价。
# ---------------------------------------------------------------------------

_PERSONA_MEMBER_NAMES = {
    "减脂": "减脂党",
    "忌口": "忌口党",
    "要赠品": "赠品党",
    "预算控": "省钱党",
}


def _persona_fit_score(line: Dict[str, Any], persona: Optional[str]) -> float:
    """成员画像对单个商品（单位份）的匹配分，越高越应归属给该成员。"""
    score = 0.0
    tags = set(line.get("tags") or [])
    kcal = float(line.get("kcal_per_unit") or 0)
    price_cents = int(round(float(line.get("unit_price_cny") or 0) * 100))
    if persona == "减脂":
        if "low_calorie" in tags or "vegetable" in tags:
            score += 2.0
        if "fried" in tags or "high_fat" in tags:
            score -= 2.0
        score -= kcal / 400.0          # 同类里热量越低越好
    elif persona == "预算控":
        # 量级刻意压低（≤1.25），不得压过「要赠品→甜品 +2」等强标签偏好
        score += max(0.0, (2500 - price_cents) / 2000.0)   # 越便宜越加分
    elif persona == "要赠品":
        if "dessert" in tags or "toy" in tags:
            score += 2.0
    elif persona == "忌口":
        # 方案已过忌口过滤（全员可吃），忌口成员优先拿「最保险」的非重口味项
        if not ({"spicy", "fried"} & tags):
            score += 1.0
    return score


def split_bill(plan: Dict[str, Any], personas: Optional[List[str]] = None) -> Dict[str, Any]:
    """把一个方案按成员归属精确分账。

    * ``plan``     : ``_basket_to_plan`` 产出的标准方案（items / coupons / final_price_cny）
    * ``personas`` : 成员画像列表（如 ["减脂", "忌口", "要赠品", "预算控"]），
                     缺省或数量不等于人数时退化为「均衡轮转」模式（每人物理均摊感更强）。
    """
    people = max(int(plan.get("people_count") or 1), 1)
    personas = list(personas or [])
    # 画像数量须与人数一致才启用画像匹配，否则退化为均衡模式
    persona_mode = len(personas) == people and people > 1

    members: List[Dict[str, Any]] = []
    for i in range(people):
        persona = personas[i] if persona_mode else None
        members.append({
            "name": _PERSONA_MEMBER_NAMES.get(persona, f"成员{i + 1}") if persona
            else f"成员{i + 1}",
            "persona": persona,
            "units": [],
            "subtotal_cents": 0,
        })

    # ---- 展开单位商品（每行 × 数量），分主食/非主食两批 --------------------
    units: List[Dict[str, Any]] = []
    for line in plan.get("items") or []:
        for _ in range(int(line.get("quantity") or 1)):
            units.append({
                "name": line.get("name"),
                "price_cents": int(round(float(line.get("unit_price_cny") or 0) * 100)),
                "is_main": bool(line.get("is_main")),
                "kcal": float(line.get("kcal_per_unit") or 0),
                "tags": list(line.get("tags") or []),
            })
    mains = [u for u in units if u["is_main"]]
    others = [u for u in units if not u["is_main"]]

    def _fit(unit: Dict[str, Any], member: Dict[str, Any]) -> float:
        if persona_mode:
            # 适配 _persona_fit_score 的字段约定（unit 为展开后的单位商品）
            return _persona_fit_score(
                {"tags": unit["tags"],
                 "kcal_per_unit": unit["kcal"],
                 "unit_price_cny": unit["price_cents"] / 100.0},
                member["persona"])
        return 0.0

    def _take(member: Dict[str, Any], unit: Dict[str, Any]) -> None:
        member["units"].append(unit)
        member["subtotal_cents"] += unit["price_cents"]

    # ---- 阶段 1：主食轮转保障 —— 每人先分到 1 份「TA 画像最匹配」的主食 ----
    # 纯贪心会让单一画像（如预算控）包揽全部主食，必须先保障人人有主食，
    # 多余主食才进入阶段 2 参与画像匹配竞争。
    remaining_mains = list(mains)
    while remaining_mains:
        needy = [m for m in members
                 if not any(u["is_main"] for u in m["units"])]
        if not needy:
            break
        for m in needy:
            if not remaining_mains:
                break
            best_i = max(range(len(remaining_mains)),
                         key=lambda i: _fit(remaining_mains[i], m))
            _take(m, remaining_mains.pop(best_i))

    # ---- 阶段 2：其余商品按「画像匹配优先，其次金额均衡」竞争 --------------
    for u in others + remaining_mains:
        best, best_key = None, None
        for m in members:
            key = (-_fit(u, m), m["subtotal_cents"])
            if best_key is None or key < best_key:
                best, best_key = m, key
        assert best is not None
        _take(best, u)

    # ---- 券优惠按小计比例分摊（整分，尾差给小计最大者） --------------------
    total_sub = sum(m["subtotal_cents"] for m in members)
    discount_cents = int(round(float(plan.get("discount_cny") or 0) * 100))
    final_cents = int(round(float(plan.get("final_price_cny") or 0) * 100))
    # 🔒 边界防护：券面额异常大于商品小计时（数据异常场景），
    # 可分摊优惠以小计为上限，超出部分记入 unallocated（不硬摊、绝不产生负应付）。
    allocatable = min(discount_cents, total_sub)
    unallocated_cents = discount_cents - allocatable
    allocated = 0
    if total_sub > 0 and allocatable > 0:
        shares: List[int] = []
        for m in members:
            share = int(allocatable * m["subtotal_cents"] / total_sub)
            share = min(share, m["subtotal_cents"])   # 逐人不得超出其小计
            shares.append(share)
            allocated += share
        remainder = allocatable - allocated
        # 尾差逐分回填给「小计最大且仍有余量」者：杜绝任何成员负应付
        while remainder > 0:
            cands = [i for i in range(people) if shares[i] < members[i]["subtotal_cents"]]
            if not cands:
                break
            idx = max(cands, key=lambda i: members[i]["subtotal_cents"])
            shares[idx] += 1
            remainder -= 1
        for m, s in zip(members, shares):
            m["discount_share_cents"] = s
    else:
        for m in members:
            m["discount_share_cents"] = 0

    for m in members:
        m["due_cents"] = m["subtotal_cents"] - m["discount_share_cents"]

    # ---- 校验：Σ应付必须等于方案最终价（分级别） --------------------------
    sum_due = sum(m["due_cents"] for m in members)

    # 汇总输出（行明细按商品名合并）
    out_members: List[Dict[str, Any]] = []
    for m in members:
        agg: Dict[str, Dict[str, Any]] = {}
        for u in m["units"]:
            e = agg.setdefault(u["name"], {"name": u["name"], "quantity": 0,
                                           "subtotal_cny": 0.0})
            e["quantity"] += 1
            e["subtotal_cny"] = round(e["subtotal_cny"] + u["price_cents"] / 100.0, 2)
        out_members.append({
            "name": m["name"],
            "persona": m["persona"],
            "items": [agg[k] for k in sorted(agg)],
            "subtotal_cny": round(m["subtotal_cents"] / 100.0, 2),
            "discount_share_cny": round(m["discount_share_cents"] / 100.0, 2),
            "due_cny": round(m["due_cents"] / 100.0, 2),
        })

    return {
        "plan_label": plan.get("label"),
        "people_count": people,
        "mode": "persona" if persona_mode else "balanced",
        "members": out_members,
        "total": {
            "subtotal_cny": round(total_sub / 100.0, 2),
            "discount_cny": round(discount_cents / 100.0, 2),
            "final_price_cny": round(final_cents / 100.0, 2),
            "sum_due_cny": round(sum_due / 100.0, 2),
            "balanced": sum_due == final_cents,
            "unallocated_discount_cny": round(unallocated_cents / 100.0, 2),
        },
    }


def render_split_bill(split: Dict[str, Any]) -> str:
    """渲染智能分账结果（文本表格）。"""
    out: List[str] = []
    out.append("=" * 68)
    out.append(f"💰 智能分账 · {split['plan_label']}（{split['people_count']} 人 · "
               f"{'画像匹配' if split['mode'] == 'persona' else '均衡分摊'}模式）")
    out.append("=" * 68)
    for m in split["members"]:
        items_desc = "、".join(
            f"{it['name']}×{it['quantity']}" for it in m["items"]) or "（无）"
        out.append(f"👤 {m['name']}" + (f"（{m['persona']}）" if m["persona"] else ""))
        out.append(f"   商品：{items_desc}")
        out.append(f"   小计 ¥{m['subtotal_cny']:.2f} − 优惠分摊 "
                   f"¥{m['discount_share_cny']:.2f} = 应付 ¥{m['due_cny']:.2f}")
    t = split["total"]
    out.append("-" * 68)
    out.append(f"合计：应付 Σ¥{t['sum_due_cny']:.2f} vs 方案最终价 "
               f"¥{t['final_price_cny']:.2f}    "
               f"{'✅ 分账平衡' if t['balanced'] else '❌ 不平，请检查'}")
    out.append("（优惠按各人小计占总价比例分摊；主食优先保证每人 1 份）")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 7. 样例与自测
#
#     ⚠️ 以下 SAMPLE_MENU / SAMPLE_COUPONS 为【兜底/演示数据，非实时】：
#     * 仅在 --demo（显式演示）或 MCP 获取失败的非 --strict 模式下进入主链路，
#       且输出中强制标注 FALLBACK_BANNER / DEMO_BANNER；
#     * 默认主链路一律使用 query-meals / query-store-coupons 实时数据（见 5.7 节）；
#     * _selftest 作为单元测试固件使用本样例（不触网，断言算法行为）。
# ---------------------------------------------------------------------------

#: 兜底/演示样例菜单（数据取自 list-nutrition-foods 真实营养值，2026-10-09 快照）
SAMPLE_MENU: List[Dict[str, Any]] = [
    {"productCode": "P001", "name": "板烧鸡腿堡", "price": 2150, "kcal": 391, "protein": 23, "fat": 17, "sodium": 1041, "carb": 35},
    {"productCode": "P002", "name": "巨无霸", "price": 2450, "kcal": 513, "protein": 27, "fat": 26, "sodium": 961, "carb": 42},
    {"productCode": "P003", "name": "麦辣鸡腿汉堡", "price": 2250, "kcal": 485, "protein": 24, "fat": 24, "sodium": 1208, "carb": 42},
    {"productCode": "P004", "name": "麦香鱼", "price": 1750, "kcal": 325, "protein": 16, "fat": 13, "sodium": 556, "carb": 35},
    {"productCode": "P005", "name": "双层吉士汉堡", "price": 1950, "kcal": 429, "protein": 27, "fat": 22, "sodium": 1017, "carb": 31},
    {"productCode": "P006", "name": "中薯条", "price": 1200, "kcal": 289, "protein": 4, "fat": 12, "sodium": 165, "carb": 38},
    {"productCode": "P007", "name": "小杯玉米杯", "price": 900, "kcal": 53, "protein": 2, "fat": 1, "sodium": 1, "carb": 7},
    {"productCode": "P008", "name": "苹果片", "price": 800, "kcal": 32, "protein": 0, "fat": 0, "sodium": 0, "carb": 7},
    {"productCode": "P009", "name": "无糖可口可乐中杯", "price": 1000, "kcal": 0, "protein": 0, "fat": 0, "sodium": 35, "carb": 1},
    {"productCode": "P010", "name": "纯牛奶（盒装）", "price": 1100, "kcal": 129, "protein": 7, "fat": 7, "sodium": 73, "carb": 10},
    {"productCode": "P011", "name": "麦乐鸡5块", "price": 1450, "kcal": 213, "protein": 12, "fat": 12, "sodium": 422, "carb": 13},
    {"productCode": "P012", "name": "麦辣鸡翅-2块", "price": 1350, "kcal": 224, "protein": 13, "fat": 15, "sodium": 537, "carb": 9},
    {"productCode": "P013", "name": "培根安格斯厚牛堡", "price": 3350, "kcal": 707, "protein": 34, "fat": 44, "sodium": 1226, "carb": 44},
    {"productCode": "P014", "name": "圆筒冰淇淋", "price": 600, "kcal": 93, "protein": 2, "fat": 3, "sodium": 36, "carb": 14},
    {"productCode": "P015", "name": "鲜蔬蛋沙拉叠叠卷", "price": 1850, "kcal": 266, "protein": 11, "fat": 14, "sodium": 518, "carb": 24},
]

#: 兜底/演示样例券（结构对齐 query-store-coupons 的常见字段；非实时数据）
SAMPLE_COUPONS: List[Dict[str, Any]] = [
    {"couponId": "C001", "couponCode": "CC001", "name": "满99减15", "type": "threshold",
     "minSpend": 9900, "discount": 1500, "exclusiveGroup": "order"},
    {"couponId": "C002", "couponCode": "CC002", "name": "满129减25", "type": "threshold",
     "minSpend": 12900, "discount": 2500, "exclusiveGroup": "order"},
    {"couponId": "C003", "couponCode": "CC003", "name": "麦乐鸡立减5元", "type": "threshold",
     "minSpend": 1450, "discount": 500, "exclusiveGroup": "item_mcnuggets",
     "appliesToProductCodes": ["P011"]},
    {"couponId": "C004", "couponCode": "CC004", "name": "9折券", "type": "percent",
     "minSpend": 5000, "rate": 0.9, "exclusiveGroup": "order"},
]

DEMO_INPUT = "我们4个人拼单，人均40以内，有人减脂，有人不吃牛肉，忌辣，帮我把券用上凑个满减"


# ---------------------------------------------------------------------------
# 7.1 输出契约（JSON Schema）
#
#     optimizer.py 的结构化输出（--json）遵循本契约；镜像文件 references/schema.json
#     由 ``--dump-schema`` 生成，``--selftest`` 会校验两者同步。
#
#     ⚠️ 命名澄清：这里的 ``genui`` 是**本 Skill 自定义的展示提示字段**（渲染层的
#     卡片建议），与 MCP 响应中不存在的 genui 字段是两回事
#     （见 MCP_INTEGRATION.md 偏差 3：35 个工具 schema 实测命中 0 次）。
#
#     校验器 ``validate_plans_doc`` 为**纯标准库**实现（本脚本不依赖第三方），
#     支持 draft-07 的子集：type / required / properties / items / enum / const /
#     minimum / maximum / minItems / maxItems / minLength。
# ---------------------------------------------------------------------------

#: 结构化输出契约（JSON Schema draft-07；镜像文件 references/schema.json）
PLAN_OUTPUT_SCHEMA: Dict[str, Any] = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "mcd-meal-brain 输出契约（plans v1.0）",
    "description": (
        "optimizer.py 结构化输出的 JSON Schema。金额单位一律「元」（2 位小数）。"
        "genui 为本 Skill 自定义的展示提示字段，与 MCP 响应中不存在的 genui 无关。"
    ),
    "type": "object",
    "required": ["schema_version", "scene", "people_count", "plans"],
    "properties": {
        "schema_version": {"type": "string", "const": "1.0"},
        "generated_by": {"type": "string"},
        "scene": {"type": "string", "description": "场景（到店自取/得来速/麦乐送/企业团餐）"},
        "people_count": {"type": "integer", "minimum": 1},
        "price_verified": {"type": "boolean",
                            "description": "方案价格是否已过 calculate-price 官方核价"},
        "plans": {
            "type": "array", "minItems": 1, "maxItems": 5,
            "description": (
                "方案席位自适应：命名极值（最省钱/最健康/最快）+ 按前沿有效"
                "多样性补的「均衡」席；前沿退化时可以只有 1–2 套，不凑数"),
            "items": {
                "type": "object",
                "required": ["name", "items", "coupons", "original_total",
                             "final_total", "per_person", "nutrition", "genui"],
                "properties": {
                    "name": {"type": "string",
                              "description": "方案定位：最省钱/最健康/最快/均衡"},
                    "degraded": {"type": "boolean",
                                 "description": "降级建议型方案标记（如「预算不足/分步下单」）；为 true 时 items 允许为空数组"},
                    "degrade_reason": {"type": "string",
                                       "description": "降级原因说明（degraded=true 时携带）"},
                    "items": {
                        "type": "array", "minItems": 0,
                        "description": "商品明细；正常方案必非空（validate_plans_doc 业务规则兜底），仅降级建议型方案允许为空",
                        "items": {
                            "type": "object",
                            "required": ["productCode", "name", "quantity",
                                         "unit_price_cny", "subtotal_cny"],
                            "properties": {
                                "productCode": {"type": ["string", "null"],
                                                 "description": "query-meals 真实编码；内置演示数据可为 null"},
                                "name": {"type": "string"},
                                "quantity": {"type": "integer", "minimum": 1},
                                "unit_price_cny": {"type": "number", "minimum": 0},
                                "subtotal_cny": {"type": "number", "minimum": 0},
                                "is_main": {"type": "boolean"},
                                "kcal_per_unit": {"type": "number", "minimum": 0},
                            },
                        },
                    },
                    "coupons": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["couponId", "name", "discount_cny"],
                            "properties": {
                                "couponId": {"type": ["string", "null"]},
                                "couponCode": {"type": ["string", "null"]},
                                "name": {"type": "string"},
                                "discount_cny": {"type": "number", "minimum": 0},
                                "source": {"type": "string",
                                            "description": "券出处：query-store-coupons / query-promotions / builtin-demo"},
                            },
                        },
                    },
                    "original_total": {"type": "number", "minimum": 0,
                                        "description": "原价（元）"},
                    "discount_cny": {"type": "number", "minimum": 0,
                                      "description": "总优惠额（元）"},
                    "final_total": {"type": "number", "minimum": 0,
                                     "description": "最终价（元）；未核价时为估算值"},
                    "per_person": {"type": "number", "minimum": 0,
                                    "description": "人均（元）"},
                    "people_count": {"type": "integer", "minimum": 1},
                    "mains": {"type": "integer", "minimum": 0,
                               "description": "主食份数；拼单底线：必须 ≥ people_count"},
                    "nutrition": {
                        "type": "object",
                        "required": ["total_kcal", "per_capita_kcal",
                                      "total_protein_g", "per_capita_protein_g",
                                      "total_fat_g", "per_capita_fat_g",
                                      "total_sodium_mg"],
                        "properties": {
                            "total_kcal": {"type": "number", "minimum": 0},
                            "per_capita_kcal": {"type": "number", "minimum": 0},
                            "total_protein_g": {"type": "number", "minimum": 0},
                            "per_capita_protein_g": {"type": "number", "minimum": 0},
                            "total_fat_g": {"type": "number", "minimum": 0},
                            "per_capita_fat_g": {"type": "number", "minimum": 0},
                            "total_sodium_mg": {"type": "number", "minimum": 0},
                        },
                    },
                    "nutrition_flags": {"type": "array",
                                         "items": {"type": "string"},
                                         "description": "体检结论：营养达标/热量超标/脂肪超标/蛋白不足/主食不足"},
                    "fairness": {
                        "type": "object",
                        "description": "公平性评分（群体决策时由 attach_fairness 附加）",
                        "required": ["overall", "per_member"],
                        "properties": {
                            "overall": {"type": "number", "minimum": 0, "maximum": 1,
                                         "description": "整体公平性 = 最低成员分（木桶效应）"},
                            "per_member": {
                                "type": "array", "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "required": ["name", "score"],
                                    "properties": {
                                        "name": {"type": "string"},
                                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                                        "reason": {"type": "string"},
                                        "taste_sat": {"type": ["number", "null"]},
                                        "prefer_sat": {"type": "number"},
                                        "avoid_sat": {"type": "number"},
                                    },
                                },
                            },
                            "note": {"type": "string"},
                        },
                    },
                    "tradeoffs": {"type": "string",
                                   "description": "取舍说明：牺牲了什么、换来了什么"},
                    "price_note": {"type": "string",
                                    "description": "如「价格为估算，未核价」"},
                    "genui": {
                        "type": "object",
                        "required": ["card_title", "emoji", "tagline"],
                        "description": "展示提示（本 Skill 自定义），供渲染层生成方案卡片",
                        "properties": {
                            "card_title": {"type": "string"},
                            "emoji": {"type": "string"},
                            "tagline": {"type": "string"},
                            "highlights": {"type": "array",
                                            "items": {"type": "string"}},
                            "accent": {"type": "string",
                                        "enum": ["green", "orange", "blue"]},
                        },
                    },
                },
            },
        },
    },
}

#: 方案定位 → 展示提示（genui 渲染素材）
_GENUI_META: Dict[str, Dict[str, str]] = {
    "最省钱": {"emoji": "🍔", "accent": "green", "tagline": "预算内的最大满足"},
    "最健康": {"emoji": "🥗", "accent": "blue", "tagline": "营养达标优先"},
    "最快": {"emoji": "⚡", "accent": "orange", "tagline": "出餐最快组合"},
}


def _plan_genui(plan: Dict[str, Any]) -> Dict[str, Any]:
    """由方案数据推导展示提示（genui）。纯确定性派生，不含编造信息。"""
    meta = _GENUI_META.get(plan.get("label") or "",
                           {"emoji": "🍟", "accent": "green", "tagline": "综合方案"})
    highlights: List[str] = []
    if plan.get("final_price_cny") is not None:
        highlights.append(f"最终价 ¥{plan['final_price_cny']:.2f}")
    n = plan.get("nutrition") or {}
    if n.get("per_capita_kcal") is not None:
        highlights.append(f"人均 {n['per_capita_kcal']:.0f} kcal")
    flags = [f for f in (plan.get("nutrition_flags") or []) if f]
    if flags:
        highlights.append("；".join(flags))
    return {
        "card_title": f"{meta['emoji']} {plan.get('label') or '方案'}",
        "emoji": meta["emoji"],
        "tagline": meta["tagline"],
        "highlights": highlights,
        "accent": meta["accent"],
    }


def _plan_tradeoffs(plan: Dict[str, Any]) -> str:
    """由方案定位与体检结论推导取舍说明（确定性文案，不编造数值）。"""
    label = plan.get("label") or ""
    parts: List[str] = []
    if label == "最省钱":
        parts.append("以最低总价优先，牺牲口味多样性与凑券深度")
    elif label == "最健康":
        parts.append("多花预算换营养达标（蛋白/脂肪/热量）")
    elif label == "最快":
        parts.append("以经典单品为主，牺牲部分优惠换出餐速度")
    bad = [f for f in (plan.get("nutrition_flags") or []) if f != "营养达标"]
    if bad:
        parts.append("注意：" + "；".join(bad))
    return "；".join(parts) if parts else "无额外取舍"


def plans_to_doc(
    plans: List[Dict[str, Any]],
    scene: str = "未指定",
    people_count: Optional[int] = None,
    price_verified: bool = False,
) -> Dict[str, Any]:
    """把内部方案列表转成**符合 PLAN_OUTPUT_SCHEMA 契约**的输出文档。

    字段映射：label→name，subtotal_cny→original_total，final_price_cny→final_total，
    per_capita_cny→per_person；genui / tradeoffs 为确定性派生。
    """
    people = int(people_count or (plans[0]["people_count"] if plans else 1) or 1)
    doc_plans: List[Dict[str, Any]] = []
    for p in plans:
        _nutrition = dict(p.get("nutrition") or {})
        if p.get("degraded") and not _nutrition:
            # 降级建议型方案（无商品明细）：营养按 0 填充以满足契约必填
            # （空篮筐 = 零营养，与 items 允许为空的语义一致）
            _nutrition = {"total_kcal": 0.0, "per_capita_kcal": 0.0,
                          "total_protein_g": 0.0, "per_capita_protein_g": 0.0,
                          "total_fat_g": 0.0, "per_capita_fat_g": 0.0,
                          "total_sodium_mg": 0.0}
        doc_plan: Dict[str, Any] = {
            "name": p.get("label") or "方案",
            "items": [
                {
                    "productCode": ln.get("productCode"),
                    "name": ln.get("name"),
                    "quantity": int(ln.get("quantity") or 1),
                    "unit_price_cny": ln.get("unit_price_cny"),
                    "subtotal_cny": ln.get("subtotal_cny"),
                    "is_main": bool(ln.get("is_main")),
                }
                for ln in (p.get("items") or [])
            ],
            "coupons": [
                {
                    "couponId": c.get("couponId"),
                    "couponCode": c.get("couponCode"),
                    "name": c.get("name"),
                    "discount_cny": c.get("discount_cny"),
                    "source": c.get("source") or "builtin-demo",
                }
                for c in (p.get("coupons") or [])
            ],
            "original_total": p.get("subtotal_cny"),
            "discount_cny": p.get("discount_cny"),
            "final_total": p.get("final_price_cny"),
            "per_person": p.get("per_capita_cny"),
            "people_count": int(p.get("people_count") or people),
            "mains": int(p.get("mains") or 0),
            "nutrition": _nutrition,
            "nutrition_flags": list(p.get("nutrition_flags") or []),
            "tradeoffs": _plan_tradeoffs(p),
            "genui": _plan_genui(p),
        }
        if p.get("fairness"):
            fr = p["fairness"]
            doc_plan["fairness"] = {
                "overall": fr["overall"],
                "per_member": [
                    {"name": pm["name"], "score": pm["score"],
                     **({"reason": pm["reason"]} if pm.get("reason") else {}),
                     **({"taste_sat": pm.get("taste_sat")} if pm.get("taste_sat") is not None else {}),
                     "prefer_sat": pm.get("prefer_sat", 1.0),
                     "avoid_sat": pm.get("avoid_sat", 1.0)}
                    for pm in fr["per_member"]
                ],
                "note": fr.get("note"),
            }
        if p.get("degraded"):
            # 降级建议型方案：透传标记与原因（items 允许为空，见 PLAN_OUTPUT_SCHEMA）
            doc_plan["degraded"] = True
            doc_plan["degrade_reason"] = p.get("degrade_reason")
        if not price_verified:
            # 契约：price_note 可选；仅在未核价时携带提示
            doc_plan["price_note"] = "价格为估算，未核价"
        doc_plans.append(doc_plan)
    return {
        "schema_version": "1.0",
        "generated_by": "mcd-meal-brain optimizer",
        "scene": scene,
        "people_count": people,
        "price_verified": bool(price_verified),
        "plans": doc_plans,
    }


def _type_ok(value: Any, expected: Any) -> bool:
    """JSON Schema type 校验（注意：Python 的 bool 是 int 子类，须先排除）。"""
    if isinstance(expected, list):
        return any(_type_ok(value, t) for t in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return True  # 未知类型关键字：放行（向前兼容）


def _validate_schema(
    node: Any,
    schema: Dict[str, Any],
    path: str,
    errors: List[str],
) -> None:
    """递归校验（draft-07 子集：type/required/properties/items/enum/const/
    minimum/maximum/minItems/maxItems/minLength）。"""
    t = schema.get("type")
    if t is not None and not _type_ok(node, t):
        errors.append(f"{path}: 期望 type={t}，实际 {type(node).__name__}")
        return
    if "const" in schema and node != schema["const"]:
        errors.append(f"{path}: 必须等于 {schema['const']!r}")
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: 必须是 {schema['enum']!r} 之一")
    if isinstance(node, (int, float)) and not isinstance(node, bool):
        if "minimum" in schema and node < schema["minimum"]:
            errors.append(f"{path}: {node} < minimum {schema['minimum']}")
        if "maximum" in schema and node > schema["maximum"]:
            errors.append(f"{path}: {node} > maximum {schema['maximum']}")
    if isinstance(node, str) and "minLength" in schema and len(node) < schema["minLength"]:
        errors.append(f"{path}: 长度 {len(node)} < minLength {schema['minLength']}")
    if isinstance(node, list):
        if "minItems" in schema and len(node) < schema["minItems"]:
            errors.append(f"{path}: 元素数 {len(node)} < minItems {schema['minItems']}")
        if "maxItems" in schema and len(node) > schema["maxItems"]:
            errors.append(f"{path}: 元素数 {len(node)} > maxItems {schema['maxItems']}")
        sub = schema.get("items")
        if isinstance(sub, dict):
            for i, el in enumerate(node):
                _validate_schema(el, sub, f"{path}[{i}]", errors)
    if isinstance(node, dict):
        for key in schema.get("required") or []:
            if key not in node:
                errors.append(f"{path}: 缺少必填字段 {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in node:
                _validate_schema(node[key], sub, f"{path}.{key}", errors)


def validate_plans_doc(doc: Any) -> List[str]:
    """校验输出文档是否符合 PLAN_OUTPUT_SCHEMA；返回错误列表（空 = 通过）。"""
    errors: List[str] = []
    _validate_schema(doc, PLAN_OUTPUT_SCHEMA, "$", errors)
    # 业务规则（契约强度兜底）：非降级方案 items 必非空；
    # 仅 degraded 建议型方案（如「预算不足/分步下单」）允许空明细。
    if isinstance(doc, dict):
        for i, plan in enumerate(doc.get("plans") or []):
            if isinstance(plan, dict) and not plan.get("degraded") and not plan.get("items"):
                errors.append(f"$.plans[{i}]: 非降级方案 items 不能为空")
    return errors


def _selftest() -> None:
    # 1) 约束解析
    c = parse_constraints(DEMO_INPUT)
    assert c["people_count"] == 4, c["people_count"]
    assert "beef" in c["hard"]["exclude_ingredients"], c["hard"]
    assert "spicy" in c["hard"]["exclude_tags"], c["hard"]
    assert c["hard"]["max_per_capita_cny"] == 40.0, c["hard"]
    assert c["targets"].get("tier") == "standard", c["targets"]
    assert "减脂" in c["personas"] and "预算控" in c["personas"], c["personas"]

    # 2) 标签推断
    t = infer_tags({"name": "中薯条", "kcal": 289, "fat": 12, "protein": 4, "sodium": 165, "carb": 38})
    assert "fried" in t and "high_carb" in t, t
    t2 = infer_tags({"name": "板烧鸡腿堡", "kcal": 391, "fat": 17, "protein": 23, "sodium": 1041, "carb": 35})
    assert "high_protein" in t2 and "high_sodium" in t2, t2
    # 回归：高脂但非油炸的餐品不得被误标为 fried
    for bad in (
        {"name": "双层吉士汉堡", "kcal": 429, "fat": 22, "protein": 27, "sodium": 1017, "carb": 31},
        {"name": "鲜蔬蛋沙拉叠叠卷", "kcal": 266, "fat": 14, "protein": 11, "sodium": 518, "carb": 24},
        {"name": "培根安格斯厚牛堡", "kcal": 707, "fat": 44, "protein": 34, "sodium": 1226, "carb": 44},
    ):
        assert "fried" not in infer_tags(bad), (bad["name"], infer_tags(bad))

    # 3) 硬约束过滤
    kept = filter_menu(_normalize_menu(SAMPLE_MENU), c)
    names = [m["name"] for m in kept]
    assert not any("巨无霸" in n for n in names), names        # 牛肉
    assert not any("安格斯" in n for n in names), names        # 牛肉
    assert not any("辣" in n for n in names), names            # 忌辣
    assert "板烧鸡腿堡" in names, names

    # 4) 券 DP
    #    不带篮子信息时：商品限定券(C003)不可用 → order 组最优是「满129减25」
    assert _coupon_dp(13000, SAMPLE_COUPONS)["discount"] == 2500, _coupon_dp(13000, SAMPLE_COUPONS)
    #    篮子含麦乐鸡(P011)时：C002(2500) + C003(500) 可叠加
    with_nuggets = _coupon_dp(13000, SAMPLE_COUPONS, item_totals={"P011": 2900})
    assert with_nuggets["discount"] == 3000, with_nuggets
    #    篮子不含麦乐鸡时：C003 必须被排除
    without_nuggets = _coupon_dp(13000, SAMPLE_COUPONS, item_totals={"P001": 13000})
    assert without_nuggets["discount"] == 2500, without_nuggets
    #    折扣券语义：9折 → 优惠 10%（rate 是应付比例，不是优惠比例）
    only_percent = [c for c in SAMPLE_COUPONS if c["type"] == "percent"]
    assert only_percent and _coupon_dp(13000, only_percent)["discount"] == 1300, \
        _coupon_dp(13000, only_percent)
    # 未达门槛的券不应被选中
    assert _coupon_dp(1000, SAMPLE_COUPONS)["discount"] == 0, _coupon_dp(1000, SAMPLE_COUPONS)

    # 5) 组合优化（席位自适应：1–5，篮子唯一）
    plans = optimize_plans(kept, SAMPLE_COUPONS, c)
    assert 1 <= len(plans) <= 5, len(plans)
    assert len({_basket_key(p) for p in plans}) == len(plans), "篮子必须唯一"
    for p in plans:
        assert p["people_count"] == 4
        assert p["final_price_cny"] <= p["subtotal_cny"] + 1e-9
        assert p["per_capita_cny"] > 0
        # 回归：拼单底线 —— 主食份数必须 ≥ 人数，否则 4 个人分不下去
        assert p["mains"] >= p["people_count"], (p["label"], p["mains"], p["items"])

    # 5b) 主食识别回归：汉堡/卷 记为 main，薯条/饮品/甜品记为 side
    assert "main" in infer_tags({"name": "巨无霸", "kcal": 513, "fat": 26, "protein": 27, "sodium": 961, "carb": 42})
    assert "main" in infer_tags({"name": "麦香鱼", "kcal": 325, "fat": 13, "protein": 16, "sodium": 556, "carb": 35})
    assert "main" in infer_tags({"name": "培根安格斯厚牛堡", "kcal": 707, "fat": 44, "protein": 34, "sodium": 1226, "carb": 44})
    assert "main" in infer_tags({"name": "鲜蔬蛋沙拉叠叠卷", "kcal": 266, "fat": 14, "protein": 11, "sodium": 518, "carb": 24})
    assert "main" not in infer_tags({"name": "中薯条", "kcal": 289, "fat": 12, "protein": 4, "sodium": 165, "carb": 38})
    assert "main" not in infer_tags({"name": "麦乐鸡5块", "kcal": 213, "fat": 12, "protein": 12, "sodium": 422, "carb": 13})
    assert "main" not in infer_tags({"name": "无糖可口可乐中杯", "kcal": 0, "fat": 0, "protein": 0, "sodium": 35, "carb": 1})

    # 7) 价格 × 热量 帕累托前沿
    norm = _normalize_menu(SAMPLE_MENU)
    front = pareto_price_kcal(norm, 4, 1400.0, 3600.0)
    assert front, "帕累托前沿不应为空"
    for a, bb in zip(front, front[1:]):
        assert a["kcal"] < bb["kcal"], ("热量应严格升序", a["kcal"], bb["kcal"])
        assert a["cost"] > bb["cost"], ("成本应严格下降（否则被支配）", a["cost"], bb["cost"])
    assert all(pt["mains"] >= 4 for pt in front), "前沿解必须满足主食 >= 人数"
    pp = optimize_pareto(kept, SAMPLE_COUPONS, c)
    assert pp, "帕累托方案集不应为空"
    for p in pp:
        assert p["mains"] >= 4
        assert p["final_price_cny"] > 0
        assert "objective" in p and "total_kcal" in p["objective"]

    # 8) 团餐促销规则解析（query-promotions → 内部券结构）
    c1 = _promotion_to_coupon({"promotionId": "P1", "promotionType": 31, "ruleCategory": 40,
                               "ruleDetail": {"thresholdAmount": 20000, "discountAmount": 3000}})
    assert c1 and c1["type"] == "threshold" and c1["minSpend"] == 20000 and c1["discount"] == 3000, c1
    c2 = _promotion_to_coupon({"promotionId": "P2", "promotionType": 33, "ruleCategory": 30,
                               "ruleDetail": {"discountRate": 0.9}})
    assert c2 and c2["type"] == "percent" and abs(c2["rate"] - 0.9) < 1e-9, c2
    # 字段缺失时必须返回 None，绝不凭空造优惠
    assert _promotion_to_coupon({"promotionId": "P3", "promotionType": 31, "ruleDetail": {}}) is None
    assert _promotion_to_coupon({"promotionId": "P4", "promotionType": 99}) is None

    # 9) 团餐促销必须真正参与券组合优化
    demo_promo = {"couponId": "G1", "name": "团餐满减", "type": "threshold",
                  "minSpend": 5000, "discount": 800, "exclusiveGroup": "group_order"}
    with_promo = optimize_plans(kept, SAMPLE_COUPONS, c, promotions=[demo_promo])
    assert any(any(cp.get("couponId") == "G1" for cp in p["coupons"]) for p in with_promo), \
        "团餐促销未参与券组合优化"

    # 6) 核价：业务错误识别（离线桩，不联网）
    stub_plan = {"items": [{"productCode": "P001", "quantity": 1}], "coupons": [],
                 "final_price_cny": 21.5, "people_count": 1, "beType": 1, "orderType": 1}
    orig_call = globals()["_mcp_call"]

    def _stub_business_error(*_a, **_k):
        return {"result": {"isError": False, "structuredContent": {
            "success": False, "code": 60003, "message": "门店暂停营业，请重新选择门店"}}}

    def _stub_ok(*_a, **_k):
        return {"result": {"isError": False, "structuredContent": {
            "success": True, "code": 200, "data": {"finalPrice": 2050}}}}

    try:
        globals()["_mcp_call"] = _stub_business_error
        r = verify_price(stub_plan, "1950564")
        assert r["ok"] is False and r["business_code"] == 60003, r

        globals()["_mcp_call"] = _stub_ok
        r2 = verify_price(stub_plan, "1950564")
        # 2050 分 → 20.50 元；参数单位为分，输出必须转元
        assert r2["ok"] is True and r2["official_final_cny"] == 20.50, r2
        assert r2["trust"] == "official", r2
    finally:
        globals()["_mcp_call"] = orig_call

    # 10) 渠道比价（离线桩，不联网）
    #     a) 得来速缺 beCode → 必须如实失败，不得编造价格
    q = fetch_channel_quote([("5022", 1)], "1950936", "得来速", be_code=None)
    assert q["ok"] is False and "beCode" in (q.get("error") or ""), q

    #     b) 美团等外部平台不在 MCP 能力范围 → 直接拒绝
    q_ext = fetch_channel_quote([("5022", 1)], "1950936", "美团")
    assert q_ext["ok"] is False and "美团" in q_ext["error"], q_ext

    def _qstub(data: Dict[str, Any]) -> Dict[str, Any]:
        return {"result": {"isError": False,
                           "structuredContent": {"success": True, "code": 200,
                                                 "traceId": "stub", "data": data}}}

    try:
        #     c) beCode 传递约定：得来速必传 beCode；到店自取绝不携带 beCode
        captured: Dict[str, Any] = {}

        def _capture(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
            captured.clear()
            captured.update(args)
            return _qstub({"productPrice": 4700, "price": 4700})

        globals()["_mcp_call"] = _capture
        fetch_channel_quote([("5022", 1)], "1950936", "得来速", be_code="195093641")
        assert captured.get("beCode") == "195093641", captured
        assert captured.get("beType") == 5 and captured.get("orderType") == 1, captured
        fetch_channel_quote([("5022", 1)], "1950936", "到店自取")
        assert "beCode" not in captured, captured
        assert captured.get("beType") == 1 and captured.get("orderType") == 1, captured

        #     d) 实测回放：同门店 beType=1 与 beType=5 同价，takeWayList 不同；
        #        be_codes 缺麦乐送 → 它必须如实标「无法核价」
        def _replay(_tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
            if args.get("beType") == 1:
                ways = [{"code": "eat-in", "title": "堂食"},
                        {"code": "locker-in", "title": "外带"},
                        {"code": "take-dt-quick", "title": "得来速"}]
            else:
                ways = [{"code": "take-dt-quick", "title": "得来速"}]
            return _qstub({"productPrice": 4700, "price": 4700, "discount": 0,
                           "takeWayList": ways})

        globals()["_mcp_call"] = _replay
        r = compare_channels([("5022", 2), ("4825", 1)], "1950936",
                             be_codes={"得来速": "195093641"})
        ml = next(x for x in r["quotes"] if x["channel"] == "麦乐送")
        assert ml["ok"] is False and "beCode" in ml["error"], ml
        dn = next(x for x in r["quotes"] if x["channel"] == "到店自取")
        dt = next(x for x in r["quotes"] if x["channel"] == "得来速")
        assert dn["ok"] and dt["ok"]
        assert dn["final_cny"] == 47.00 and dt["final_cny"] == 47.00
        assert len(dn["take_ways"]) == 3 and len(dt["take_ways"]) == 1
        assert dt["take_ways"][0]["code"] == "take-dt-quick"
        assert set(r["cheapest"]) == {"到店自取", "得来速"}, r["cheapest"]

        #     e) 外送渠道含运费/打包费：4700+900+200+0-500 = 5300 分 = ¥53.00
        globals()["_mcp_call"] = lambda _t, _a, **_k: _qstub(
            {"productPrice": 4700, "deliveryPrice": 900, "packingPrice": 200,
             "tablewarePrice": 0, "discount": 500, "price": 5300})
        q2 = fetch_channel_quote([("5022", 1)], "1950936", "麦乐送", be_code="X1")
        assert q2["ok"] and q2["final_cny"] == 53.00, q2
        assert q2["sum_consistent"] is True, q2
        assert q2["delivery_cny"] == 9.00 and q2["packing_cny"] == 2.00, q2

        #     f) 分项和与现价不一致 → 以官方现价为准并显式标注
        globals()["_mcp_call"] = lambda _t, _a, **_k: _qstub(
            {"productPrice": 4700, "deliveryPrice": 900, "price": 5000})
        q3 = fetch_channel_quote([("5022", 1)], "1950936", "麦乐送", be_code="X1")
        assert q3["ok"] and q3["final_cny"] == 50.00 and q3["sum_consistent"] is False, q3

        #     g) 渲染不炸且包含关键信息
        txt = render_channel_comparison(r)
        assert "到手成本" in txt and "无法核价" in txt and "得来速" in txt, txt[:200]
    finally:
        globals()["_mcp_call"] = orig_call

    # 11) 输出契约（JSON Schema）
    #     a) 真实优化结果 → 契约文档必须零错误通过校验
    _c11 = parse_constraints(DEMO_INPUT)
    _kept11 = filter_menu(_normalize_menu(SAMPLE_MENU), _c11)
    _plans11 = optimize_plans(_kept11, SAMPLE_COUPONS, _c11)
    doc = plans_to_doc(_plans11, scene="到店自取（演示）",
                       people_count=_c11.get("people_count"))
    errs = validate_plans_doc(doc)
    assert not errs, errs
    assert doc["schema_version"] == "1.0" and doc["price_verified"] is False
    p0 = doc["plans"][0]
    for key in ("name", "items", "coupons", "original_total", "final_total",
                "per_person", "nutrition", "genui"):
        assert key in p0, key
    assert p0["price_note"] == "价格为估算，未核价"
    assert p0["genui"]["accent"] in ("green", "orange", "blue")
    assert p0["genui"]["card_title"] and p0["genui"]["tagline"]
    # 拼单底线在契约层同样成立：主食份数 ≥ 人数
    assert p0["mains"] >= p0["people_count"], (p0["mains"], p0["people_count"])

    #     b) 负向：删除必填字段 → 必须报错
    bad = json.loads(json.dumps(doc))
    del bad["plans"][0]["genui"]
    assert validate_plans_doc(bad), "缺 genui 未被检出"

    #     c) 负向：类型错误（final_total 传字符串）→ 必须报错
    bad2 = json.loads(json.dumps(doc))
    bad2["plans"][0]["final_total"] = "47.00"
    assert validate_plans_doc(bad2), "final_total 类型错误未被检出"

    #     d) 负向：const 破坏（schema_version 改版本号）→ 必须报错
    bad3 = json.loads(json.dumps(doc))
    bad3["schema_version"] = "9.9"
    assert validate_plans_doc(bad3), "schema_version 破坏未被检出"

    #     e) 负向：枚举越界（genui.accent 传非法色）→ 必须报错
    bad4 = json.loads(json.dumps(doc))
    bad4["plans"][0]["genui"]["accent"] = "purple"
    assert validate_plans_doc(bad4), "accent 枚举越界未被检出"

    #     f) 负向：people_count=0 违反 minimum → 必须报错
    bad5 = json.loads(json.dumps(doc))
    bad5["people_count"] = 0
    assert validate_plans_doc(bad5), "people_count minimum 未生效"

    #     g) 镜像同步：references/schema.json 与 PLAN_OUTPUT_SCHEMA 必须一致
    _schema_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "references", "schema.json")
    if os.path.exists(_schema_path):
        with open(_schema_path, "r", encoding="utf-8") as fh:
            disk = json.load(fh)
        assert disk == PLAN_OUTPUT_SCHEMA, (
            "references/schema.json 与 PLAN_OUTPUT_SCHEMA 不同步，"
            "请运行 python scripts/optimizer.py --dump-schema > references/schema.json 重新生成")

    # 12) 模糊输入处理（三层询问机制）
    #     a) 完整输入 → 无追问，ready，无 banner
    r_full = clarify_input(DEMO_INPUT)
    assert r_full["ready"] is True and not r_full["questions"], r_full
    assert r_full["banner"] is None and r_full["layer"] == 1, r_full
    assert r_full["constraints"]["people_count"] == 4

    #     b) 「随便」→ 第三层：不追问、全默认、带 banner、口味默认生效
    r_any = clarify_input("随便")
    assert r_any["you_decide"] is True and r_any["layer"] == 3, r_any
    assert r_any["questions"] == [] and r_any["ready"] is True, r_any
    assert r_any["banner"] == "基于默认偏好：不辣、不油炸", r_any["banner"]
    assert set(DEFAULT_TASTE_EXCLUDE_TAGS) <= set(
        r_any["constraints"]["hard"]["exclude_tags"]), r_any["constraints"]["hard"]
    assert r_any["constraints"]["scene"] == "到店自取"  # 第一层静默默认
    assert any("人数：默认 1 人" in d for d in r_any["defaults_applied"])

    #     c) 部分输入 → 第二层：只追问缺失项（≤3），口味默认已预填
    r_part = clarify_input("我们3个人吃点好的")
    assert r_part["layer"] == 2 and r_part["ready"] is False, r_part
    assert len(r_part["questions"]) == 2, r_part["questions"]  # 预算 + 口味确认
    assert not any("几位" in q for q in r_part["questions"])  # 人数已知，不问
    assert set(DEFAULT_TASTE_EXCLUDE_TAGS) <= set(
        r_part["constraints"]["hard"]["exclude_tags"])  # 口味默认预填
    assert any("待确认" in d for d in r_part["defaults_applied"])

    #     d) 几乎空白 → 第二层满 3 问
    r_bare = clarify_input("来份汉堡")
    assert r_bare["ready"] is False and len(r_bare["questions"]) == 3, r_bare
    assert r_bare["constraints"]["scene"] == "到店自取"  # 场景永不追问

    #     e) answers 合并 → ready 翻转，口味回答走同义词归一并推翻默认
    r_ans = clarify_input("来份汉堡", answers={
        "people_count": 2, "budget": 50, "taste": "不吃辣，无辣不欢的反面"})
    assert r_ans["ready"] is True and r_ans["questions"] == [], r_ans
    assert r_ans["constraints"]["people_count"] == 2
    assert r_ans["constraints"]["budget"]["amount_cny"] == 50.0
    assert "spicy" in r_ans["constraints"]["hard"]["exclude_tags"]

    #     f) answers.taste=「都行」→ 接受默认口味（不再追问）
    r_acc = clarify_input("来份汉堡", answers={"people_count": 1, "budget": "不限", "taste": "都行"})
    assert r_acc["ready"] is True, r_acc
    assert set(DEFAULT_TASTE_EXCLUDE_TAGS) <= set(
        r_acc["constraints"]["hard"]["exclude_tags"])

    #     g) 默认口味下菜单仍可解（ SAMPLE_MENU 排除 spicy/fried 后非空且含主食）
    _kept12 = filter_menu(_normalize_menu(SAMPLE_MENU), r_any["constraints"])
    assert _kept12, "默认口味过滤后候选为空"
    assert any("main" in (m.get("tags") or []) for m in _kept12)

    # 13) 群体决策公平性
    #     a) 硬约束交集（禁忌并集排除）：A 不吃辣 + B 不吃牛肉 → 同时排除
    agg = aggregate_preferences([
        "小李：不吃辣",
        "小王：不吃牛肉",
    ])
    assert set(agg["hard"]["exclude_tags"]) >= {"spicy"}, agg["hard"]
    assert "beef" in agg["hard"]["exclude_ingredients"], agg["hard"]
    assert agg["people_count"] == 2 and not agg["needs_negotiation"]

    #     b) 截尾均值：很辣4/中辣2/不辣0/不辣0 → 去两端 [0,2] → 1.0，不判冲突
    agg2 = aggregate_preferences([
        {"name": "A", "taste": {"spicy": 4}},
        {"name": "B", "taste": {"spicy": 2}},
        {"name": "C", "taste": {"spicy": 0}},
        {"name": "D", "taste": {"spicy": 0}},
    ])
    assert agg2["taste"]["spicy"] == 1.0, agg2["taste"]
    assert not agg2["needs_negotiation"], agg2["conflicts"]

    #     c) 严重冲突：一半要辣(4,5)一半不要(0,0) → 需协商 + 两套备选约束
    agg3 = aggregate_preferences([
        "小李：无辣不欢",
        "小王：很辣",
        "小张：不吃辣",
        "小陈：不要辣",
    ])
    assert agg3["needs_negotiation"], agg3["conflicts"]
    cf = agg3["conflicts"][0]
    assert cf["dimension"] == "spicy" and cf["gap"] >= 3
    assert set(cf["camp_a"]["members"]) == {"小李", "小王"}
    assert set(cf["camp_b"]["members"]) == {"小张", "小陈"}
    assert len(agg3["alternatives"]) == 2
    ca, cb = agg3["alternatives"]
    assert "spicy" not in ca["constraints"]["hard"]["exclude_tags"]       # A：不排除辣
    assert "spicy" in ca["constraints"]["soft"]["prefer_tags"]
    assert "spicy" in cb["constraints"]["hard"]["exclude_tags"]           # B：硬排除辣
    assert ca["compromise"] and cb["compromise"]                          # 妥协点必须说明

    #     d) 公平性评分：木桶效应（整体=最低分）；含辣方案对不吃辣成员扣口味分
    _grp = optimize_group_plans(
        _normalize_menu(SAMPLE_MENU), SAMPLE_COUPONS,
        [{"name": "甲", "taste": {"spicy": 0}, "avoid_tags": ["spicy"]},
         {"name": "乙", "taste": {"spicy": 5}}])
    # 无硬排除辣 → 优化器可能选辣品 → 冲突检测命中（乙=5 要辣 vs 甲=0）→ 双方案
    assert _grp["needs_negotiation"]
    plans_b = next(ps for ps in _grp["plan_sets"] if "B" in ps["label"])["plans"]
    fr_b = plans_b[0]["fairness"]
    assert 0 <= fr_b["overall"] <= 1
    assert fr_b["overall"] == min(pm["score"] for pm in fr_b["per_member"])
    scores_b = {pm["name"]: pm["score"] for pm in fr_b["per_member"]}
    assert scores_b["甲"] > scores_b["乙"]  # 无辣方案：不吃辣的甲更满足

    #     e) 硬约束违反 → 该成员 0 分（构造一个违反个人禁忌的方案直接评）
    _fake_plan = {"items": [{"name": "麦辣鸡腿汉堡", "tags": ["spicy", "main"],
                              "quantity": 1, "unit_price_cny": 22.5, "subtotal_cny": 22.5}],
                   "people_count": 1}
    fr_v = fairness_score(_fake_plan, aggregate_preferences(["小李：不吃辣"]))
    assert fr_v["per_member"][0]["score"] == 0.0
    assert "硬约束被违反" in fr_v["per_member"][0]["reason"]

    #     f) 群体结果走契约文档仍校验通过（fairness 为可选字段）
    _doc13 = plans_to_doc(_grp["plan_sets"][0]["plans"], scene="群体冲突演示", people_count=2)
    assert not validate_plans_doc(_doc13), validate_plans_doc(_doc13)
    assert "fairness" in _doc13["plans"][0]

    #     g) 截尾均值细节：n<4 不截尾（[0,5] → 2.5）
    agg4 = aggregate_preferences([
        {"name": "A", "taste": {"spicy": 0}},
        {"name": "B", "taste": {"spicy": 5}},
    ])
    assert agg4["taste"]["spicy"] == 2.5, agg4["taste"]
    # 两人 0 vs 5 也构成严重对立（双方阵营各 1 人 ≥ ⌈2/3⌉=1，gap=5）
    assert agg4["needs_negotiation"]

    # 14) 系统健壮性：幂等键 / 指数退避重试 / 超时降级 / 日志脱敏
    #     ⚠️ 整组重定向 LOG_FILE 到临时目录，避免测试数据污染真实日志
    import urllib.error as _ue
    import tempfile as _tf

    _saved_logfile14 = globals()["LOG_FILE"]
    _tf14 = _tf.TemporaryDirectory()
    globals()["LOG_FILE"] = os.path.join(_tf14.name, "mcp_calls.log")

    #     a) 幂等键生成：同一订单内容+同窗口 → 同键；内容或窗口变化 → 不同键
    k1, w1 = make_idempotency_key("u1", "1950936",
                                  [("5022", 2), ("4825", 1)], now=1000000.0)
    k2, w2 = make_idempotency_key("u1", "1950936",
                                  [("4825", 1), ("5022", 2)], now=1000000.0)
    assert k1 == k2 and w1 == w2, "内容排序不同但本质相同的订单应生成相同幂等键"
    k3, _ = make_idempotency_key("u1", "1950936",
                                 [("5022", 2), ("4825", 2)], now=1000000.0)
    k4, _ = make_idempotency_key("u1", "1950936",
                                 [("5022", 2), ("4825", 1)],
                                 now=1000000.0 + IDEMPOTENCY_WINDOW_S + 1)
    k5, _ = make_idempotency_key("u2", "1950936",
                                 [("5022", 2), ("4825", 1)], now=1000000.0)
    assert k1 != k3, "订单内容不同必须得到不同幂等键"
    assert k1 != k4, "超出 5 分钟窗口后应生成新键（允许再点一单）"
    assert k1 != k5, "不同用户必须得到不同幂等键"

    #     b) 幂等去重：注入内存 store，第二次同键下单不触达 MCP
    _idem_db: Dict[str, Dict[str, Any]] = {}
    calls: List[str] = []

    def _fake_order_ok(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
        calls.append(tool)
        return {"result": {"isError": False, "structuredContent": {
            "success": True, "code": 200,
            "data": {"orderId": "ORD-10001"}}}}

    items14 = [("5022", 2), ("4825", 1)]
    r1 = create_order_guarded("u1", "1950936", items14, {"storeCode": "1950936"},
                              mcp_call=_fake_order_ok, now=1000000.0,
                              idem_store=_idem_db)
    assert r1["ok"] and not r1["duplicate"] and r1["order"]["orderId"] == "ORD-10001"
    assert calls == ["create-order"]
    r2 = create_order_guarded("u1", "1950936", items14, {"storeCode": "1950936"},
                              mcp_call=_fake_order_ok, now=1000000.0 + 60,
                              idem_store=_idem_db)      # 同窗口重复提交
    assert r2["ok"] and r2["duplicate"], "同窗口重复提交必须命中幂等键"
    assert r2["order"]["order_id"] == "ORD-10001"
    assert calls == ["create-order"], "幂等命中时不得再次调用 create-order"

    #     c) 下单失败不记录幂等键（可重试）
    def _fake_order_fail(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
        return {"result": {"isError": False, "structuredContent": {
            "success": False, "code": 60003, "message": "门店暂停营业"}}}

    _idem_db2: Dict[str, Dict[str, Any]] = {}
    r3 = create_order_guarded("u1", "1950936", items14, {"storeCode": "1950936"},
                              mcp_call=_fake_order_fail, now=1000000.0,
                              idem_store=_idem_db2)
    assert r3["ok"] is False and "60003" in r3["error"]
    assert not _idem_db2, "失败的订单不得记录幂等键"

    #     d) 指数退避重试：429 重试至成功；次数正确；401 不重试
    attempts14: List[int] = []

    def _flaky_429(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
        attempts14.append(1)
        if len(attempts14) < 3:
            raise _ue.HTTPError(MCP_URL, 429, "Too Many Requests", None, None)
        return {"result": {"isError": False, "structuredContent": {"success": True}}}

    _saved_call14 = globals()["_mcp_call_once"]

    def _dispatch(tool: str, args: Dict[str, Any], token: str = "", timeout: int = 0):
        # _mcp_call 内部走 _mcp_call_once；这里拦截转给可变函数
        return _dispatch.fn(tool, args)  # type: ignore[attr-defined]

    _dispatch.fn = _flaky_429  # type: ignore[attr-defined]
    globals()["_mcp_call_once"] = _dispatch
    try:
        resp14 = _mcp_call("query-meals", {"storeCode": "1950936"},
                           retries=3, budget_s=60.0, log=False, use_cache=False)
        assert (resp14.get("result") or {}).get(
            "structuredContent", {}).get("success") is True
        assert len(attempts14) == 3, f"429 应重试到成功，实际尝试 {len(attempts14)} 次"

        # 401 不重试：立即抛出
        attempts14.clear()

        def _unauthorized(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
            attempts14.append(1)
            raise _ue.HTTPError(MCP_URL, 401, "Unauthorized", None, None)

        _dispatch.fn = _unauthorized  # type: ignore[attr-defined]
        try:
            _mcp_call("query-meals", {"storeCode": "1950936"},
                      retries=3, budget_s=60.0, log=False, use_cache=False)
            raise AssertionError("401 应当抛出异常")
        except _ue.HTTPError as e14:
            assert e14.code == 401
        assert len(attempts14) == 1, f"401 不得重试，实际调用 {len(attempts14)} 次"

        # 5xx 重试耗尽：抛最后一个异常
        attempts14.clear()

        def _always_500(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
            attempts14.append(1)
            raise _ue.HTTPError(MCP_URL, 500, "Internal Server Error", None, None)

        _dispatch.fn = _always_500  # type: ignore[attr-defined]
        try:
            _mcp_call("query-meals", {"storeCode": "1950936"},
                      retries=2, budget_s=60.0, log=False, use_cache=False)
            raise AssertionError("5xx 重试耗尽应抛异常")
        except _ue.HTTPError as e14b:
            assert e14b.code == 500
        assert len(attempts14) == 3, f"初始+2次重试应共 3 次调用，实际 {len(attempts14)}"
    finally:
        globals()["_mcp_call_once"] = _saved_call14

    #     e) 超时降级：call_with_degrade 走 DEGRADE_RULES，带 banner
    def _timeout_call(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
        raise TimeoutError("MCP 调用超时（10s）")

    _saved_mcp14 = globals()["_mcp_call"]
    globals()["_mcp_call"] = _timeout_call
    try:
        dg14 = call_with_degrade("calculate-price", {"storeCode": "1950936"})
        assert dg14["ok"] is False and dg14["degraded"] is True
        assert dg14["fallback"] == "mark_unverified"
        assert dg14["banner"] == DEGRADED_BANNER
        dg14b = call_with_degrade("query-nearby-stores", {"beType": 1})
        assert dg14b["fallback"] == "ask_user" and "手动" in dg14b["note"]
        # 菜单查询带缓存加载器 → 降级取缓存
        dg14c = call_with_degrade("query-meals", {"storeCode": "1950936"},
                                  cache_loader=lambda: [{"name": "缓存菜单"}])
        assert dg14c["fallback"] == "cache" and dg14c["data"] == [{"name": "缓存菜单"}]
        assert "已命中本地缓存" in dg14c["note"]
        # 业务错误：不降级、如实返回
        def _biz_err(tool: str, args: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
            return {"result": {"isError": False, "structuredContent": {
                "success": False, "code": 600057, "message": "门店可能已关闭"}}}
        globals()["_mcp_call"] = _biz_err
        dg14d = call_with_degrade("query-meals", {"storeCode": "1950936"})
        assert dg14d["ok"] is False and dg14d["degraded"] is False
        assert "600057" in dg14d["error"]
    finally:
        globals()["_mcp_call"] = _saved_mcp14

    #     f) 日志脱敏：手机号/Token/地址不落盘
    log_mcp_call("create-order", 123, "success", retries=1,
                 arguments={"storeCode": "1950936",
                            "phone": "13812345678",
                            "token": "secret-token-abc",
                            "addressDetail": "北京市朝阳区某街道1号"},
                 detail="Bearer abc123 13812345678")
    with open(globals()["LOG_FILE"], "r", encoding="utf-8") as _fh14:
        line = _fh14.readlines()[-1]
    assert "13812345678" not in line, "真实手机号不得写入日志"
    assert "[PHONE]" in line
    assert "secret-token-abc" not in line and "[TOKEN]" in line
    assert "某街道" not in line and "[ADDRESS]" in line
    assert "abc123" not in line, "Bearer 凭证不得出现在日志 detail 中"
    rec14 = json.loads(line)
    assert rec14["tool"] == "create-order" and rec14["status"] == "success"
    assert rec14["duration_ms"] == 123 and rec14["retries"] == 1

    globals()["LOG_FILE"] = _saved_logfile14
    _tf14.cleanup()

    #     g) _is_retryable_error 分类正确
    assert _is_retryable_error(_ue.HTTPError(MCP_URL, 429, "Too Many", None, None)) is True
    assert _is_retryable_error(_ue.HTTPError(MCP_URL, 503, "Service Unavailable", None, None)) is True
    assert _is_retryable_error(_ue.HTTPError(MCP_URL, 401, "Unauthorized", None, None)) is False
    assert _is_retryable_error(_ue.HTTPError(MCP_URL, 400, "Bad Request", None, None)) is False
    assert _is_retryable_error(TimeoutError("timeout")) is True

    # 15) 热量预算配餐（扩展分支 D）
    cb_c = parse_constraints("4个人，每人500大卡以内")
    assert cb_c["targets"]["max_calories_kcal"] == 500, cb_c["targets"]
    assert cb_c["targets"]["kcal_scope"] == "per_person"
    assert cb_c["people_count"] == 4
    # 总热量口径换算：4 人总热量 2000 大卡 → 人均 500
    cb_t = parse_constraints("4个人，总热量2000大卡")
    assert cb_t["targets"]["max_calories_kcal"] == 500, cb_t["targets"]
    # 无「每人/总」前缀的既有语义不回归：仍按人均上限
    cb_plain = parse_constraints("500大卡以内")
    assert cb_plain["targets"]["max_calories_kcal"] == 500
    # 样例菜单 + 500 kcal/人：可行组合存在且人均不超预算
    cb_kept = filter_menu(_normalize_menu(SAMPLE_MENU), cb_c)
    cb_plans = optimize_calorie_budget(cb_kept, SAMPLE_COUPONS, cb_c)
    assert 2 <= len(cb_plans) <= 3, len(cb_plans)
    for p in cb_plans:
        assert p["mains"] >= 4, "每人 1 份主餐的底线必须守住"
        assert p["nutrition"]["per_capita_kcal"] <= 500 + 1e-6, (
            p["label"], p["nutrition"]["per_capita_kcal"])
        assert "calorie_budget" in p and p["calorie_budget"]["headroom_kcal"] >= -1e-6
    # 高蛋白方案蛋白 ≥ 最省方案（同一可行域内）
    prot = {p["label"]: p["nutrition"]["per_capita_protein_g"] for p in cb_plans}
    assert prot["热量预算·高蛋白"] >= prot["热量预算·最省"] - 1e-6, prot
    # 预算过低（连热量最低的主餐都装不下）→ 显式无解说明，不崩溃、不静默放宽
    cb_hard = parse_constraints("4个人，每人50大卡以内")
    cb_kept2 = filter_menu(_normalize_menu(SAMPLE_MENU), cb_hard)
    cb_plans2 = optimize_calorie_budget(cb_kept2, SAMPLE_COUPONS, cb_hard)
    assert len(cb_plans2) == 1 and cb_plans2[0]["degraded"] is True
    assert "超出预算" in cb_plans2[0]["degrade_reason"]

    # 16) NSGA-II 多目标优化（批 2 算法升级）
    #     a) 支配关系与分层基础语义
    assert _dominates((1, 1), (2, 2))
    assert not _dominates((2, 2), (1, 1))
    assert not _dominates((1, 2), (2, 1))          # 互不支配
    _fronts = _fast_non_dominated_sort([(1, 1), (2, 2), (1.5, 1.5), (0, 2)])
    assert _fronts == [[0, 3], [2], [1]]           # (1,1)与(0,2)互不支配；(2,2)被链式支配
    _dist = _crowding_distance([0, 1, 2], [(0, 0), (1, 1), (2, 2)])
    assert _dist[0] == float("inf") and _dist[2] == float("inf")
    assert abs(_dist[1] - 2.0) < 1e-9              # 两目标各贡献 1.0
    #     b) 默认路径输出：命名极值齐全 + 第一前沿 + 互不支配 + 极值方向
    #        席位自适应（1–5）：近重复解折叠、篮子唯一、补席不反超命名极值
    n_plans = optimize_plans(kept, SAMPLE_COUPONS, c)
    assert 1 <= len(n_plans) <= 5, len(n_plans)
    assert len({_basket_key(p) for p in n_plans}) == len(n_plans), "篮子必须唯一"
    _labels16 = {p["label"] for p in n_plans}
    assert _labels16 <= {"最省钱", "最健康", "最快", "均衡"}, _labels16
    for p in n_plans:
        assert p["nsga2"]["front"] == 1
        assert p["mains"] >= 4
    _objs16 = [_plan_objectives(p) for p in n_plans]
    for _i16 in range(len(_objs16)):
        for _j16 in range(len(_objs16)):
            if _i16 != _j16:
                assert not _dominates(_objs16[_i16], _objs16[_j16]), "Top 方案必须互不支配"
    _by16 = {p["label"]: p for p in n_plans}
    if "最省钱" in _by16:
        assert _by16["最省钱"]["final_price_cny"] == min(p["final_price_cny"] for p in n_plans)
    if "最健康" in _by16:
        assert _by16["最健康"]["nutrition"]["per_capita_protein_g"] == max(
            p["nutrition"]["per_capita_protein_g"] for p in n_plans)
    #     c) 确定性：固定种子，两次运行结果完全一致
    _sig16 = [(p["label"], _basket_key(p)) for p in optimize_plans(kept, SAMPLE_COUPONS, c)]
    _sig16b = [(p["label"], _basket_key(p)) for p in optimize_plans(kept, SAMPLE_COUPONS, c)]
    assert _sig16 == _sig16b, "NSGA-II 必须可复现（固定随机种子）"
    #     d) 加权路径保留可用（method="weighted"，同样不凑数）
    _w16 = optimize_plans(kept, SAMPLE_COUPONS, c, method="weighted")
    assert _w16 and {p["label"] for p in _w16} <= {"最省钱", "最健康", "最快", "均衡"}
    #     e) 显式热量预算作为硬上限（可行解存在时必须全达标）
    _c16 = parse_constraints("4个人，预算300，每人500大卡以内")
    _kept16 = filter_menu(_normalize_menu(SAMPLE_MENU), _c16)
    for p in optimize_plans(_kept16, SAMPLE_COUPONS, _c16):
        assert p["nutrition"]["per_capita_kcal"] <= 500 + 1e-6, (
            p["label"], p["nutrition"]["per_capita_kcal"])

    # 17) ReflectCache 参数级缓存（批 2 算法升级）
    #     a) 键的参数级精确性：参数顺序无关；值/类型不同即不同键；工具不同即不同键
    _k1 = ReflectCache.make_key("query-meals", {"storeCode": "S1", "orderType": 1})
    _k2 = ReflectCache.make_key("query-meals", {"orderType": 1, "storeCode": "S1"})
    _k3 = ReflectCache.make_key("query-meals", {"storeCode": "S1", "orderType": 2})
    _k4 = ReflectCache.make_key("query-store-coupons", {"storeCode": "S1", "orderType": 1})
    _k5 = ReflectCache.make_key("query-meals", {"storeCode": "S1", "orderType": 1.0})
    assert _k1 == _k2, "参数顺序不得影响键（规范化 JSON 键排序）"
    assert _k1 != _k3 and _k1 != _k4, "参数值不同 / 工具不同 → 不同键"
    assert _k1 != _k5, "类型不同（1 vs 1.0）视为不同参数（精确匹配语义）"

    #     b) 集成：同参数只触网一次；参数不同再触网；深拷贝防污染
    _net17: list = []

    def _fake_once17(tool, arguments, token, timeout):
        _net17.append((tool, json.dumps(arguments, sort_keys=True)))
        return {"result": {"isError": False, "structuredContent": {
            "success": True, "code": 200, "data": {"calls": len(_net17)}}}}

    _orig_once17 = globals()["_mcp_call_once"]
    globals()["_mcp_call_once"] = _fake_once17
    REFLECT_CACHE.clear()
    try:
        _r1 = _mcp_call("query-meals", {"storeCode": "S1", "orderType": 1},
                        token="t", log=False)
        _r2 = _mcp_call("query-meals", {"orderType": 1, "storeCode": "S1"},
                        token="t", log=False)
        assert len(_net17) == 1, "同参数（含键顺序不同）必须命中缓存不触网"
        assert _r1 == _r2
        _mcp_call("query-meals", {"storeCode": "S2", "orderType": 1},
                  token="t", log=False)
        assert len(_net17) == 2, "参数不同必须重新触网"
        _st17 = REFLECT_CACHE.stats()
        assert _st17["hits"] == 1 and _st17["misses"] == 2, _st17
        # 深拷贝：调用方修改响应不得污染缓存
        _r1["result"]["structuredContent"]["data"]["calls"] = 999
        _r5 = _mcp_call("query-meals", {"storeCode": "S1", "orderType": 1},
                        token="t", log=False)
        assert _r5["result"]["structuredContent"]["data"]["calls"] == 1, "缓存必须返回深拷贝"

        #     c) 写操作 / now-time-info 一律穿透
        _net17.clear()
        REFLECT_CACHE.clear()
        _mcp_call("create-order", {"storeCode": "S1"}, token="t", log=False)
        _mcp_call("create-order", {"storeCode": "S1"}, token="t", log=False)
        assert len(_net17) == 2, "写操作必须穿透缓存"
        _net17.clear()
        _mcp_call("now-time-info", {}, token="t", log=False)
        _mcp_call("now-time-info", {}, token="t", log=False)
        assert len(_net17) == 2, "时间敏感工具永不缓存"

        #     d) 业务错误不缓存（重试语义不变）
        def _fake_biz_err17(*_a, **_k):
            _net17.append("biz")
            return {"result": {"isError": False, "structuredContent": {
                "success": False, "code": 60003, "message": "门店打烊"}}}

        _net17.clear()
        REFLECT_CACHE.clear()
        globals()["_mcp_call_once"] = _fake_biz_err17
        _mcp_call("query-meals", {"storeCode": "S9"}, token="t", log=False)
        _mcp_call("query-meals", {"storeCode": "S9"}, token="t", log=False)
        assert len(_net17) == 2, "业务错误不得缓存"

        #     e) TTL 过期 → 重新触网
        globals()["_mcp_call_once"] = _fake_once17
        _net17.clear()
        _ttl17 = ReflectCache(ttl_s=0)
        _saved_rc17 = globals()["REFLECT_CACHE"]
        globals()["REFLECT_CACHE"] = _ttl17
        try:
            _mcp_call("query-meals", {"storeCode": "S3"}, token="t", log=False)
            time.sleep(0.01)
            _mcp_call("query-meals", {"storeCode": "S3"}, token="t", log=False)
            assert len(_net17) == 2, "TTL 过期后必须重新触网"
        finally:
            globals()["REFLECT_CACHE"] = _saved_rc17
    finally:
        globals()["_mcp_call_once"] = _orig_once17
        REFLECT_CACHE.clear()

    # ------------------------------------------------------------------
    # 第 18 组：智能分账（split_bill）
    #     a) 分账平衡：Σ应付 == 最终价（分级别）
    #     b) 主食保障：每人至少 1 份主食
    #     c) 画像匹配：减脂→最低热量主食；要赠品→甜品
    #     d) 优惠按小计比例分摊，无优惠时全部为 0
    #     e) 均衡模式（无画像）：金额均衡且主食有保障
    # ------------------------------------------------------------------
    _sb_plan = {
        "label": "分账自测", "people_count": 4,
        "items": [
            {"name": "板烧鸡腿堡", "quantity": 1, "unit_price_cny": 21.5,
             "subtotal_cny": 21.5, "kcal_per_unit": 440, "tags": ["main"], "is_main": True},
            {"name": "麦香鱼", "quantity": 1, "unit_price_cny": 17.5,
             "subtotal_cny": 17.5, "kcal_per_unit": 330, "tags": ["main"], "is_main": True},
            {"name": "麦辣鸡腿堡", "quantity": 1, "unit_price_cny": 22.0,
             "subtotal_cny": 22.0, "kcal_per_unit": 520, "tags": ["main", "fried"], "is_main": True},
            {"name": "双层吉士汉堡", "quantity": 1, "unit_price_cny": 19.5,
             "subtotal_cny": 19.5, "kcal_per_unit": 450, "tags": ["main"], "is_main": True},
            {"name": "圆筒冰淇淋", "quantity": 2, "unit_price_cny": 5.0,
             "subtotal_cny": 10.0, "kcal_per_unit": 200, "tags": ["dessert"], "is_main": False},
            {"name": "小杯玉米杯", "quantity": 2, "unit_price_cny": 8.0,
             "subtotal_cny": 16.0, "kcal_per_unit": 100, "tags": ["low_calorie"], "is_main": False},
        ],
        "coupons": [{"name": "9折券", "discount_cny": 10.0}],
        "subtotal_cny": 106.5, "discount_cny": 10.0, "final_price_cny": 96.5,
    }
    # a+b+c) 画像模式
    _sb = split_bill(_sb_plan, ["减脂", "忌口", "要赠品", "预算控"])
    assert _sb["mode"] == "persona"
    assert _sb["total"]["balanced"] is True, "分账必须平衡（Σ应付==最终价）"
    for _m in _sb["members"]:
        _main_names = {"板烧鸡腿堡", "麦香鱼", "麦辣鸡腿堡", "双层吉士汉堡"}
        assert any(it["name"] in _main_names for it in _m["items"]), \
            f"{_m['name']} 未分到主食"
    _jiani = next(m for m in _sb["members"] if m["persona"] == "减脂")
    _jiani_items = {it["name"] for it in _jiani["items"]}
    assert "麦香鱼" in _jiani_items, "减脂成员应分到最低热量主食（麦香鱼 330kcal）"
    _zengpin = next(m for m in _sb["members"] if m["persona"] == "要赠品")
    assert any("冰淇淋" in it["name"] for it in _zengpin["items"]), \
        "要赠品成员应分到甜品（圆筒冰淇淋）"
    # 优惠分摊：Σ分摊 == 优惠总额
    _sum_share = round(sum(m["discount_share_cny"] for m in _sb["members"]), 2)
    assert _sum_share == _sb_plan["discount_cny"], f"优惠分摊合计 {_sum_share} != 10.0"
    # d) 无优惠
    _sb_noc = split_bill({**_sb_plan, "discount_cny": 0.0, "final_price_cny": 106.5,
                          "coupons": []}, ["减脂", "忌口", "要赠品", "预算控"])
    assert all(m["discount_share_cny"] == 0 for m in _sb_noc["members"])
    assert _sb_noc["total"]["balanced"] is True
    # e) 均衡模式
    _sb_bal = split_bill(_sb_plan)
    assert _sb_bal["mode"] == "balanced" and _sb_bal["total"]["balanced"] is True
    for _m in _sb_bal["members"]:
        _main_names = {"板烧鸡腿堡", "麦香鱼", "麦辣鸡腿堡", "双层吉士汉堡"}
        assert any(it["name"] in _main_names for it in _m["items"])
    # 渲染不崩
    _render18 = render_split_bill(_sb)
    assert "分账平衡" in _render18 and "智能分账" in _render18

    print("[selftest] 全部断言通过 ✅")


def _demo_channel_comparison() -> None:
    """渠道比价演示：**回放 2026-10-10 真实实测数据**（不联网、不编造）。

    实测环境：门店 1950936（北京朝阳公园南路得来速餐厅），营业时段内。
    篮子：猪柳蛋麦满分(5022) ×2 + 脆薯饼(4825) ×1。

    * 到店自取(beType=1)：核价 ¥47.00，traceId=03e3b4…，取餐方式 堂食/外带/得来速；
    * 得来速(beType=5, beCode=195093641)：核价 ¥47.00，traceId=5afb5f…，
      取餐方式锁定「得来速-车道自提」；
    * 麦乐送(beType=2)：当前账户无配送地址（delivery-query-addresses 返回空），
      无法取得 beCode → 如实标注「无法核价」，**不编造运费**。
    """
    real_replay: Dict[Tuple[int, int], Dict[str, Any]] = {
        (1, 1): {
            "success": True, "code": 200, "traceId": "03e3b4b361c04b5c2c1c952a78790c7",
            "data": {
                "productOriginalPrice": 4700, "productPrice": 4700,
                "originalPrice": 4700, "discount": 0, "price": 4700,
                "productList": [
                    {"productCode": "5022", "productName": "猪柳蛋麦满分",
                     "quantity": 2, "originalSubtotal": 3800, "subtotal": 3800},
                    {"productCode": "4825", "productName": "脆薯饼",
                     "quantity": 1, "originalSubtotal": 900, "subtotal": 900},
                ],
                "takeWayList": [
                    {"code": "eat-in", "title": "堂食", "subtitle": "店内用餐"},
                    {"code": "locker-in", "title": "外带", "subtitle": "店内取餐柜"},
                    {"code": "take-dt-quick", "title": "得来速", "subtitle": "车道自提"},
                ],
            },
        },
        (1, 5): {
            "success": True, "code": 200, "traceId": "5afb5ff94fd59262ccd36e47a4ec98da",
            "data": {
                "productOriginalPrice": 4700, "productPrice": 4700,
                "originalPrice": 4700, "discount": 0, "price": 4700,
                "productList": [
                    {"productCode": "5022", "productName": "猪柳蛋麦满分",
                     "quantity": 2, "originalSubtotal": 3800, "subtotal": 3800},
                    {"productCode": "4825", "productName": "脆薯饼",
                     "quantity": 1, "originalSubtotal": 900, "subtotal": 900},
                ],
                "takeWayList": [
                    {"code": "take-dt-quick", "title": "得来速", "subtitle": "车道自提"},
                ],
            },
        },
    }

    orig_call = globals()["_mcp_call"]

    def _replay(tool: str, arguments: Dict[str, Any], **_k: Any) -> Dict[str, Any]:
        key = (int(arguments.get("orderType") or 0), int(arguments.get("beType") or 0))
        sc = real_replay.get(key)
        if sc is None:            # 麦乐送无实测数据 → 也不会走到这里（缺 beCode 提前返回）
            return {"result": {"isError": False, "structuredContent": {
                "success": False, "code": -1, "message": f"无回放数据: {key}"}}}
        return {"result": {"isError": False, "structuredContent": sc}}

    print("=" * 68)
    print("渠道比价演示（回放 2026-10-10 实测数据，门店 1950936）")
    print("篮子：猪柳蛋麦满分 ×2 + 脆薯饼 ×1")
    print("=" * 68)
    try:
        globals()["_mcp_call"] = _replay
        result = compare_channels(
            [("5022", 2), ("4825", 1)], "1950936",
            be_codes={"得来速": "195093641"},   # 麦乐送无 beCode → 如实标「无法核价」
        )
    finally:
        globals()["_mcp_call"] = orig_call
    print()
    print(render_channel_comparison(result))
    print()
    print("注：到店自取/得来速为官方核价实测值；麦乐送因当前账户无配送地址无法核价，")
    print("    实际使用时先经 delivery-query-addresses → delivery-query-stores 取得")
    print("    beCode 后即可补齐该渠道（到手成本将包含运费与打包费）。")


def _arg_value(argv: Sequence[str], flag: str) -> Optional[str]:
    """取 ``--flag value`` 形式的命令行参数值；不存在或无值返回 None。"""
    if flag in argv:
        i = argv.index(flag)
        if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            return argv[i + 1]
    return None


def _print_data_source(data: Dict[str, Any]) -> None:
    """统一输出数据来源说明（live / fallback / demo 必须显式区分，不得静默）。"""
    print("\n[数据来源]")
    if data["source"] == "live":
        print(f"✅ {LIVE_SOURCE_NOTE}｜门店 {data['store_code']}｜"
              f"菜单 {len(data['menu'])} 品｜券 {len(data['coupons'])} 张｜"
              f"获取时间 {data['fetched_at']}")
        for t in data.get("traces") or []:
            print(f"   · {t['tool']}  traceId={t['trace_id']}")
    elif data["source"] == "fallback":
        print(f"❌ 当前无法连接 MCP，原因：{data['error']}")
        print("   请检查网络 / MCD_MCP_TOKEN，或在门店营业时段（一般为 07:00–22:00）重试。")
        print(f"   {FALLBACK_BANNER}")
    else:
        print(f"ℹ️ {DEMO_BANNER}")
    for note in data.get("notes") or []:
        print(f"   ⚠️ {note}")


def _mcp_unavailable_hint(error: str) -> List[str]:
    return [
        f"当前无法连接 MCP，原因：{error}",
        "请检查网络 / MCD_MCP_TOKEN，或在门店营业时段（一般为 07:00–22:00）重试。",
        "如仅需查看算法演示，可运行：python scripts/optimizer.py --demo",
    ]


def _safe_stdio() -> None:
    """GBK 控制台/管道下 emoji 输出防崩：errors=replace（--json 仍输出合法 JSON）。

    Windows 交互式控制台走 PEP 528（UTF-8）不受影响；但输出重定向/管道时
    编码退回 cp936(GBK)，emoji（🍔✅ 等）会触发 UnicodeEncodeError。
    ``errors="replace"`` 把不可编码字符替换为 U+FFFD（GBK 落盘为 ?），
    不会产生非法 JSON 转义（不可用 backslashreplace，会破坏 --json payload）。
    """
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except AttributeError:
            pass  # 非 TextIO（如已替换的缓冲区）时静默跳过


def main(argv: Sequence[str]) -> int:
    _safe_stdio()
    if argv and argv[0] == "--selftest":
        _selftest()
        return 0

    if argv and argv[0] == "--compare-channels":
        _demo_channel_comparison()
        return 0

    if argv and argv[0] == "--clarify":
        # 模糊输入分析演示：--clarify "随便" [--answers '{"people_count":4}']
        user_input = next((a for a in argv[1:] if not a.startswith("-")), "随便")
        answers = None
        if "--answers" in argv:
            try:
                answers = json.loads(argv[argv.index("--answers") + 1])
            except (IndexError, json.JSONDecodeError):
                print("⚠️ --answers 参数格式错误，应为 JSON 字符串", file=sys.stderr)
                return 1
        r = clarify_input(user_input, answers=answers)
        print("=" * 68)
        print(f"输入：{r['input']!r}    →  层级 L{r['layer']}"
              f"（ready={r['ready']}，you_decide={r['you_decide']}）")
        print("=" * 68)
        if r["defaults_applied"]:
            print("已应用默认值：", "；".join(r["defaults_applied"]))
        if r["questions"]:
            print("\n需要追问（一次性，≤3 个）：")
            for i, q in enumerate(r["questions"], 1):
                print(f"  {i}. {q}")
            print("\n拿到回答后重跑：clarify_input(输入, answers={'people_count':…, "
                  "'budget':…, 'taste':…})")
        if r["banner"]:
            print(f"\n输出方案时必须标注：{r['banner']}")
        print("\n[模糊点明细]")
        for key, a in r["ambiguities"].items():
            state = "缺失" if a["missing"] else "已明确"
            extra = f"，默认={a['default']}" if a["missing"] and a["default"] else ""
            ask = "，需追问" if a["ask"] else ""
            print(f"  {key:<13} {state}{extra}{ask}")
        return 0

    if argv and argv[0] == "--group":
        # 群体决策演示：--group '[...members...]'（JSON，元素为 dict 或自然语言字符串）
        # 不传参数则跑内置「一半要辣一半不要辣」冲突场景
        demo_members = [
            "小李：无辣不欢，要辣",
            "小王：很辣，无辣不欢",
            "小张：不吃辣，怕辣",
            "小陈：不要辣，清淡",
        ]
        if len(argv) > 1 and not argv[1].startswith("-"):
            try:
                members = json.loads(argv[1])
            except json.JSONDecodeError:
                print("⚠️ --group 参数应为 JSON 数组", file=sys.stderr)
                return 1
        else:
            members = demo_members
        data = acquire_planning_data(
            {}, demo="--demo" in argv, strict="--strict" in argv,
            store_code=_arg_value(argv, "--store"), be_code=_arg_value(argv, "--becode"))
        if data["source"] == "error":
            for line in _mcp_unavailable_hint(data["error"]):
                print(line, file=sys.stderr)
            return 1
        _print_data_source(data)
        result = optimize_group_plans(data["menu"], data["coupons"], members)
        print(render_group_result(result))
        return 0

    if argv and argv[0] == "--calorie-budget":
        # 热量预算配餐（扩展分支 D）：--calorie-budget "4个人，每人500大卡以内" [--demo|--strict]
        user_input = next((a for a in argv[1:] if not a.startswith("-")),
                          "4个人，每人500大卡以内")
        print("=" * 68)
        print("用户输入：", user_input)
        print("=" * 68)
        constraints = parse_constraints(user_input)
        cap = (constraints.get("targets") or {}).get("max_calories_kcal")
        if not cap:
            print("❌ 未识别到热量预算。示例输入：「4个人，每人500大卡以内」", file=sys.stderr)
            return 1
        print(f"[解析结果] 人数 {constraints['people_count']}"
              f"｜人均热量上限 {cap:.0f} kcal（tier="
              f"{(constraints.get('targets') or {}).get('tier', 'custom')}）")
        data = acquire_planning_data(
            constraints, demo="--demo" in argv, strict="--strict" in argv,
            store_code=_arg_value(argv, "--store"), be_code=_arg_value(argv, "--becode"))
        if data["source"] == "error":
            for line in _mcp_unavailable_hint(data["error"]):
                print(line, file=sys.stderr)
            return 1
        _print_data_source(data)
        try:
            kept = filter_menu(data["menu"], constraints)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        plans = optimize_calorie_budget(kept, data["coupons"], constraints)
        # banner 已由 _print_data_source 统一输出，此处不重复
        print(f"\n[热量预算配餐 · 主餐+小食+饮品（{len(plans)} 套，席位自适应）]")
        print(render_plans(plans))
        return 0

    if argv and argv[0] == "--dump-schema":
        # 输出契约镜像（重定向到 references/schema.json 以保持同步）
        print(json.dumps(PLAN_OUTPUT_SCHEMA, ensure_ascii=False, indent=2))
        return 0

    if argv and argv[0] == "--json":
        # 结构化输出：解析 → 【默认实时 MCP 取数】→ 过滤 → 优化 → 契约文档
        # （--demo 用内置样例；MCP 失败非 strict 模式兜底并强制标注；--strict 报错退出）
        user_input = next((a for a in argv[1:] if not a.startswith("-")), DEMO_INPUT)
        constraints = parse_constraints(user_input)
        data = acquire_planning_data(
            constraints, demo="--demo" in argv, strict="--strict" in argv,
            store_code=_arg_value(argv, "--store"), be_code=_arg_value(argv, "--becode"))
        if data["source"] == "error":
            for line in _mcp_unavailable_hint(data["error"]):
                print(line, file=sys.stderr)
            return 1
        try:
            kept = filter_menu(data["menu"], constraints)
        except ValueError as exc:
            print(f"❌ {exc}", file=sys.stderr)
            return 1
        plans = optimize_plans(kept, data["coupons"], constraints)
        scene_name = constraints.get("scene") or "到店自取"
        if data["source"] == "live":
            scene = f"{scene_name}（门店 {data['store_code']}，实时 MCP 数据）"
        elif data["source"] == "fallback":
            scene = f"{scene_name}（兜底数据，非实时）"
        else:
            scene = f"{scene_name}（演示数据）"
        doc = plans_to_doc(plans, scene=scene,
                           people_count=constraints.get("people_count"))
        # 数据来源与降级信息写入契约文档（额外字段，不破坏 schema 校验）
        doc["data_source"] = {
            "menu": "query-meals（实时）" if data["source"] == "live"
                    else f"内置样例（{'兜底' if data['source'] == 'fallback' else '演示'}，非实时）",
            "coupons": "query-store-coupons（实时）" if data["source"] == "live"
                       else f"内置样例（{'兜底' if data['source'] == 'fallback' else '演示'}，非实时）",
            "nutrition": "list-nutrition-foods（实时，名称精确匹配）" if data["source"] == "live"
                         else "内置样例（非实时）",
            "store_code": data["store_code"],
            "fetched_at": data["fetched_at"],
            "trace_ids": {t["tool"]: t["trace_id"] for t in (data.get("traces") or [])},
            "banner": data["banner"],
            "notes": data["notes"],
        }
        errs = validate_plans_doc(doc)
        if errs:  # 防御：结构化输出必须自证契约
            print("Schema 校验失败：", errs, file=sys.stderr)
            return 1
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    # 支持命令行传入自定义需求；不传则用内置样例
    # ⚠️ 默认走【实时 MCP 数据】（方向 5.5 全链路真实化）：
    #    * 任意用户输入 → query-meals / query-store-coupons / list-nutrition-foods
    #    * --demo           强制使用内置样例（显式标注「演示数据」）
    #    * --strict         MCP 不可用时直接报错退出，绝不兜底
    #    * --store CODE     指定门店（缺省经 query-nearby-stores 自动选址）
    #    * --becode CODE    得来速/团餐链路的门店 beCode
    #    * --verify         对最省钱方案调 calculate-price 官方核价
    user_input = next((a for a in argv if not a.startswith("-")), DEMO_INPUT)

    print("=" * 68)
    print("用户输入：", user_input)
    print("=" * 68)

    constraints = parse_constraints(user_input)
    # 默认单行摘要（省 40+ 行输出/token）；--debug 展开完整约束 JSON
    if "--debug" in argv:
        print("\n[解析结果]")
        print(json.dumps({k: v for k, v in constraints.items() if k != "raw"},
                         ensure_ascii=False, indent=2))
    else:
        print(f"\n[解析] {constraints_brief(constraints)}")

    data = acquire_planning_data(
        constraints, demo="--demo" in argv, strict="--strict" in argv,
        store_code=_arg_value(argv, "--store"), be_code=_arg_value(argv, "--becode"))
    if data["source"] == "error":
        for line in _mcp_unavailable_hint(data["error"]):
            print(line, file=sys.stderr)
        return 1
    _print_data_source(data)

    try:
        kept = filter_menu(data["menu"], constraints)
    except ValueError as exc:
        print(f"\n❌ {exc}", file=sys.stderr)
        return 1
    print(f"\n[硬约束过滤] 保留 {len(kept)} 项：", "、".join(m["name"] for m in kept))

    plans = optimize_plans(kept, data["coupons"], constraints)
    # banner 已由 _print_data_source 统一输出（demo/fallback 必显式标注），此处不重复
    print(f"\n[方案（NSGA-II 帕累托前沿，{len(plans)} 套自适应席位）]")
    print(render_plans(plans))
    if plans:
        print(recommend_plan(plans, constraints))

    if "--verify" in argv and data["source"] == "live":
        cheap = next((p for p in plans if p["label"] == "最省钱"),
                     plans[0] if plans else None)
        if cheap:
            vr = verify_price(cheap, data["store_code"],
                              be_type=data["be_type"], order_type=data["order_type"])
            print("\n[官方核价 calculate-price]")
            if vr["ok"]:
                print(f"  官方价 ¥{vr['official_final_cny']:.2f}"
                      f"｜本地估算 ¥{vr['estimated_cny']:.2f}"
                      f"｜偏差 ¥{vr['diff_cny']:+.2f}（{vr['diff_pct']:.2f}%）→ "
                      + ("一致 ✅" if vr["diff_pct"] <= 5 else "偏差>5%，以官方价为准"))
            else:
                print(f"  核价失败：{vr['error']}（价格以实际下单为准）")

    if "--split" in argv:
        # 智能分账：对最省钱方案按成员归属精确分账（画像来自约束解析）
        cheap = next((p for p in plans if p["label"] == "最省钱"),
                     plans[0] if plans else None)
        if cheap:
            split = split_bill(cheap, constraints.get("personas"))
            print("\n" + render_split_bill(split))

    print("\n[价格 × 热量 双目标]")
    print(render_pareto(optimize_pareto(kept, data["coupons"], constraints),
                        curve=optimize_cost_curve(kept, data["coupons"], constraints)))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
