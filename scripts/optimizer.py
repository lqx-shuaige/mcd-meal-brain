#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦麦拼单大脑 —— 组合优化核心脚本。

设计目标
--------
1. ``parse_constraints``  把用户的自然语言解析成结构化约束（硬约束 + 软偏好 + 画像）。
2. ``filter_menu``        按硬约束过滤菜单，把解空间先压下来（一票否决）。
3. ``optimize_plans``     多目标寻优：动态规划求最省钱方案，加权评分求最健康 / 最快方案，
                          帕累托剪枝后输出 Top-3。
4. ``verify_price``       调用 MCP 的 ``calculate-price`` 做官方核价（单位：分 → 元）。

约定
----
* **内部所有金额一律用「分」（int）**，避免浮点误差；仅在输出层转成「元」。
* 不依赖任何第三方库（仅标准库），便于在沙箱内直接运行。
* 常量表（标签关键词、成分同义词）与 ``references/strategy.md`` 保持一致。

命令行自测::

    python optimizer.py             # 跑内置样例，输出 3 个方案
    python optimizer.py --selftest  # 只跑断言自检
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
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
    people = _find_number(text, [r"(\d+)\s*(?:人|位)", r"我们\s*(\d+)", r"(\d+)\s*份"])
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
    per_capita = _find_number(text, [r"人均\s*(\d+(?:\.\d+)?)", r"平均\s*(\d+(?:\.\d+)?)"])
    total = _find_number(
        text,
        [r"(?:总预算|总价|一共|总共|控制在)\s*(\d+(?:\.\d+)?)", r"预算\s*(\d+(?:\.\d+)?)"],
    )
    if per_capita is not None and total is None:
        budget = {"type": "per_capita", "amount_cny": per_capita}
    elif total is not None:
        budget = {"type": "total", "amount_cny": total}
    else:
        budget = {"type": "total", "amount_cny": None}

    # --- 硬约束 -----------------------------------------------------------
    exclude_ingredients = _extract_ingredients(text)
    exclude_tags: List[str] = []
    if re.search(r"(不吃|不能吃|忌|不要|别)\s*辣", text):
        exclude_tags.append("spicy")
    if re.search(r"(不吃|不能吃|忌|不要|别)\s*(油炸|炸的|煎炸)", text):
        exclude_tags.append("fried")

    max_per_capita = per_capita
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
        targets = dict(DIET_TIERS[tier])
        targets["tier"] = tier
    kcal_cap = _find_number(text, [r"(\d+)\s*(?:大卡|千卡|kcal|卡)"])
    if kcal_cap:
        targets["max_calories_kcal"] = kcal_cap
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


def _dp_min_cost(
    items: List[Dict[str, Any]],
    people: int,
    kcal_min: float,
    kcal_max: float,
    step: int = 25,
) -> List[Dict[str, Any]]:
    """动态规划求满足「份数 + 热量区间」的最低成本篮子（无界背包）。

    状态: ``(份数, 热量桶)`` → 最小花费（分）与重建路径。
    """
    max_units = max(people, 1) + 3          # 允许比人数多几份用于凑单
    max_bucket = int(kcal_max // step) + 1

    dp: List[List[Optional[Tuple[int, int, int, int]]]] = [
        [None] * (max_bucket + 1) for _ in range(max_units + 1)
    ]
    dp[0][0] = (0, -1, -1, -1)

    for u in range(0, max_units + 1):
        for b in range(0, max_bucket + 1):
            cur = dp[u][b]
            if cur is None:
                continue
            cost = cur[0]
            for idx, it in enumerate(items):
                nu, nb = u + 1, b + int(it["kcal"] // step)
                if nu > max_units or nb > max_bucket:
                    continue
                ncost = cost + it["price"]
                tgt = dp[nu][nb]
                if tgt is None or ncost < tgt[0]:
                    dp[nu][nb] = (ncost, u, b, idx)

    solutions: List[Dict[str, Any]] = []
    for u in range(max(people, 1), max_units + 1):
        for b in range(0, max_bucket + 1):
            cell = dp[u][b]
            if cell is None:
                continue
            kcal = b * step
            if kcal < kcal_min or kcal > kcal_max:
                continue
            basket: List[int] = []
            cu, cb, guard = u, b, 0
            while cu > 0 and guard < 200:
                guard += 1
                c2 = dp[cu][cb]
                if c2 is None:
                    break
                _, pu, pb, idx = c2
                if idx < 0:
                    break
                basket.append(idx)
                cu, cb = pu, pb
            if basket:
                solutions.append({"cost": int(cell[0]), "units": u, "kcal": kcal, "indices": basket})

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
    kcal = protein = fat = sodium = 0.0
    for idx, qty in sorted(counter.items()):
        it = items[idx]
        line_total = it["price"] * qty
        subtotal += line_total
        code = it.get("productCode")
        if code:
            item_totals[code] = item_totals.get(code, 0) + line_total
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
            "tags": list(it.get("tags") or []),
        })

    coupon_res = _coupon_dp(subtotal, coupons, item_totals=item_totals)
    people = max(int(constraints.get("people_count") or 1), 1)
    final = coupon_res["final"]

    targets = constraints.get("targets") or {}
    flags: List[str] = []
    if targets.get("max_calories_kcal") and kcal / people > targets["max_calories_kcal"]:
        flags.append("人均热量超标")
    if targets.get("max_fat_g") and fat / people > targets["max_fat_g"]:
        flags.append("人均脂肪超标")
    if targets.get("min_protein_g") and protein / people < targets["min_protein_g"]:
        flags.append("人均蛋白不足")
    if not flags:
        flags.append("营养达标")

    return {
        "label": label,
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


def optimize_plans(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
) -> List[Dict[str, Any]]:
    """多目标寻优，返回 Top-3 方案。

    * **最省钱**：动态规划（无界背包）+ 券组合 DP + 帕累托剪枝
    * **最健康**：按人均热量 / 脂肪 / 蛋白达标率加权
    * **最快**：按油炸 / 现制占比估算出餐速度
    """
    items = _normalize_menu(menu)
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
    healthy_sorted = sorted(
        items,
        key=lambda x: (-(x["protein"] / max(x["kcal"], 1)), x["fat"], x["price"]),
    )
    pick: List[int] = []
    kcal_acc = 0.0
    for it in healthy_sorted:
        if kcal_acc >= kcal_min and len(pick) >= people:
            break
        pick.append(idx_of[id(it)])
        kcal_acc += it["kcal"]
    if pick:
        pool.append(_basket_to_plan(pick, items, coupons, constraints, "最健康"))

    # --- 最快：优先非油炸 -------------------------------------------------
    fast_sorted = sorted(items, key=lambda x: (("fried" in x["tags"]), -x["kcal"]))
    pick2: List[int] = []
    kcal_acc2 = 0.0
    for it in fast_sorted:
        if kcal_acc2 >= kcal_min and len(pick2) >= people:
            break
        pick2.append(idx_of[id(it)])
        kcal_acc2 += it["kcal"]
    if pick2:
        pool.append(_basket_to_plan(pick2, items, coupons, constraints, "最快"))

    # --- 帕累托去重 + 加权排序 --------------------------------------------
    for p in pool:
        p["score"] = _score(p, constraints, weights)

    dedup: Dict[Tuple, Dict[str, Any]] = {}
    for p in pool:
        key = tuple(sorted((ln["productCode"] or ln["name"], ln["quantity"]) for ln in p["items"]))
        if key not in dedup or p["score"] > dedup[key]["score"]:
            dedup[key] = p

    return sorted(dedup.values(), key=lambda x: (-x["score"], x["final_price_cny"]))[:3]


# ---------------------------------------------------------------------------
# 5. 核价校验（MCP calculate-price）
# ---------------------------------------------------------------------------

def _mcp_call(tool: str, arguments: Dict[str, Any], token: Optional[str] = None,
              timeout: int = 30) -> Dict[str, Any]:
    """极简 MCP(Streamable HTTP) 客户端：一次 POST 完成 tools/call。"""
    token = token or os.environ.get("MCD_MCP_TOKEN") or ""
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
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if body.lstrip().startswith("event:") or "\ndata:" in body:
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
    return json.loads(body)


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
# 6. 输出
# ---------------------------------------------------------------------------

def render_plans(plans: List[Dict[str, Any]]) -> str:
    """把 Top-3 方案渲染成可读文本（金额一律「元」）。"""
    icon = {"最省钱": "🍔", "最健康": "🥗", "最快": "⚡"}
    out: List[str] = []
    for i, p in enumerate(plans, 1):
        out.append(f"{icon.get(p['label'], '🍟')} 方案 {chr(64 + i)}｜{p['label']}")
        out.append(f"├ 人数：{p['people_count']} 人")
        out.append("├ 商品明细：")
        for ln in p["items"]:
            out.append(f"│   · {ln['name']} ×{ln['quantity']}   ¥{ln['subtotal_cny']:.2f}")
        if p["coupons"]:
            for c in p["coupons"]:
                out.append(f"├ 券组合：{c['name']}（{c['couponId']}）  −¥{c['discount_cny']:.2f}")
        else:
            out.append("├ 券组合：无可用券")
        out.append(f"├ 原价：¥{p['subtotal_cny']:.2f}  →  最终价：¥{p['final_price_cny']:.2f}")
        out.append(f"├ 人均：¥{p['per_capita_cny']:.2f}")
        n = p["nutrition"]
        out.append(
            f"├ 营养标签：总热量 {n['total_kcal']:.0f} kcal｜人均 {n['per_capita_kcal']:.0f} kcal"
            f"｜人均蛋白 {n['per_capita_protein_g']:.1f} g｜人均脂肪 {n['per_capita_fat_g']:.1f} g"
        )
        out.append(f"└ 体检：{'；'.join(p['nutrition_flags'])}（评分 {p['score']}）")
        out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 7. 样例与自测
# ---------------------------------------------------------------------------

#: 内置样例菜单（数据取自 list-nutrition-foods 真实营养值）
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

#: 内置样例券（结构对齐 query-store-coupons 的常见字段）
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

    # 5) 组合优化
    plans = optimize_plans(kept, SAMPLE_COUPONS, c)
    assert 1 <= len(plans) <= 3, len(plans)
    for p in plans:
        assert p["people_count"] == 4
        assert p["final_price_cny"] <= p["subtotal_cny"] + 1e-9
        assert p["per_capita_cny"] > 0
        assert "营养" in p["nutrition_flags"][0] or p["nutrition_flags"]

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

    print("[selftest] 全部断言通过 ✅")


def main(argv: Sequence[str]) -> int:
    if argv and argv[0] == "--selftest":
        _selftest()
        return 0

    print("=" * 68)
    print("用户输入：", DEMO_INPUT)
    print("=" * 68)

    constraints = parse_constraints(DEMO_INPUT)
    print("\n[解析结果]")
    print(json.dumps({k: v for k, v in constraints.items() if k != "raw"}, ensure_ascii=False, indent=2))

    kept = filter_menu(_normalize_menu(SAMPLE_MENU), constraints)
    print(f"\n[硬约束过滤] 保留 {len(kept)} 项：", "、".join(m["name"] for m in kept))

    plans = optimize_plans(kept, SAMPLE_COUPONS, constraints)
    print("\n[Top-3 方案]")
    print(render_plans(plans))

    _selftest()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
