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
5. ``compare_channels``   渠道比价：同一篮子在 到店自取 / 麦乐送 / 得来速 三个**内部渠道**的
                          「到手成本」对比（商品 + 运费 + 打包费 + 餐具费 − 优惠）。
                          ⚠️ MCP 不提供美团/饿了么等第三方平台接口，外部渠道不做、不编造。

约定
----
* **内部所有金额一律用「分」（int）**，避免浮点误差；仅在输出层转成「元」。
* 不依赖任何第三方库（仅标准库），便于在沙箱内直接运行。
* 常量表（标签关键词、成分同义词）与 ``references/strategy.md`` 保持一致。

命令行自测::

    python optimizer.py                            # 跑内置样例，输出 3 个方案
    python optimizer.py "我们4个人，预算100，一个减脂，一个不吃辣"   # 自定义输入
    python optimizer.py --selftest                 # 只跑断言自检
    python optimizer.py --compare-channels         # 渠道比价演示（回放 2026-10-10 实测数据）
    python optimizer.py --json [需求]              # 结构化输出（references/schema.json 契约）
    python optimizer.py --dump-schema              # 输出契约镜像（重定向到 references/schema.json）
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
        ``people_count``（int）/ ``budget``（数值或 None=不限）/ ``taste``（自然语言，
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
            try:
                c["budget"] = {"type": "total", "amount_cny": float(answers["budget"])}
                c["hard"]["max_per_capita_cny"] = float(answers["budget"])
            except (TypeError, ValueError):
                pass  # 非数值（如「不限」）→ 维持不限预算
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
    step: int = 25,
) -> Tuple[List[List[Optional[Tuple[int, int, int, int]]]], int, int]:
    """构建 DP 表（无界背包），供「最低成本」与「价格×热量帕累托」共用。

    状态: ``dp[主食份数][热量桶] = (cost, prev_m, prev_b, item_idx)``
    """
    max_mains = max(people, 1) + 2          # 允许比人数多 2 份主食用于凑满减
    max_bucket = int(kcal_max // step) + 1

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
                nb = b + int(it["kcal"] // step)
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
    step: int = 25,
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
    step: int = 25,
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
    step: int = 25,
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
    if not flags:
        flags.append("营养达标")

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


def optimize_plans(
    menu: List[Dict[str, Any]],
    coupons: List[Dict[str, Any]],
    constraints: Dict[str, Any],
    weights: Optional[Dict[str, float]] = None,
    promotions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """多目标寻优，返回 Top-3 方案。

    * **最省钱**：动态规划（无界背包）+ 券组合 DP + 帕累托剪枝
    * **最健康**：按人均热量 / 脂肪 / 蛋白达标率加权
    * **最快**：按油炸 / 现制占比估算出餐速度

    ``promotions``：来自 ``fetch_group_meal_promotions()`` 的团餐促销规则（beType=6 专用），
    会与 ``coupons`` 合并后一并参与券组合优化。
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
    return ranked[:3]


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
      - ``optimize_plans`` 用加权评分给出「最省钱 / 最健康 / 最快」三个代表解，适合直接推荐；
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

def render_plans(plans: List[Dict[str, Any]]) -> str:
    """把 Top-3 方案渲染成可读文本（金额一律「元」）。"""
    icon = {"最省钱": "🍔", "最健康": "🥗", "最快": "⚡"}
    out: List[str] = []
    for i, p in enumerate(plans, 1):
        out.append(f"{icon.get(p['label'], '🍟')} 方案 {chr(64 + i)}｜{p['label']}")
        out.append(f"├ 人数：{p['people_count']} 人｜主食 {p.get('mains', 0)} 份（每人至少 1 份 ✅）")
        out.append(f"├ 总份数：{p['units']} 份")
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


def render_pareto(plans: List[Dict[str, Any]],
                  curve: Optional[List[Dict[str, Any]]] = None) -> str:
    """渲染「价格 × 热量」双目标结果：帕累托前沿 + （可选）成本–热量权衡曲线。"""
    out: List[str] = []
    out.append("【价格 × 热量 帕累托前沿】不做加权，全部为互不支配的解")
    if not plans:
        out.append("（无可行解，请放宽约束）")
    else:
        out.append(f"{'总热量(kcal)':>12} {'总价(元)':>10} {'人均(元)':>10} {'主食':>5}  边际权衡")
        out.append("-" * 78)
        for p in plans:
            n = p["nutrition"]
            edge = ""
            if p.get("price_vs_next_cny") is not None:
                edge = (f"↓换下一档省 ¥{p['price_vs_next_cny']:.2f}"
                        f"，多吃 {p['kcal_saved_vs_next']:.0f} kcal")
            out.append(f"{n['total_kcal']:>12.0f} {p['final_price_cny']:>10.2f} "
                       f"{p['per_capita_cny']:>10.2f} {p.get('mains', 0):>5}  {edge}")
        if len(plans) == 1:
            out.append("")
            out.append("⚠️ 前沿退化为单点：本菜单下「最便宜」与「热量最低」是同一组解，")
            out.append("   即两个目标不冲突，不存在「多花钱换低热量」的权衡。这不是算法错误。")
            out.append("   需要看热量增加要花多少钱，请看下方成本–热量曲线。")

    if curve:
        out.append("")
        out.append("【成本–热量权衡曲线】各热量档位下的最低价方案")
        out.append(f"{'总热量(kcal)':>12} {'最终价(元)':>11} {'人均(元)':>10} {'主食':>5}  较上一档")
        out.append("-" * 78)
        for p in curve:
            n = p["nutrition"]
            d = p.get("delta_price_cny")
            delta = "—" if d is None else f"{'+' if d >= 0 else ''}¥{d:.2f}"
            out.append(f"{n['total_kcal']:>12.0f} {p['final_price_cny']:>11.2f} "
                       f"{p['per_capita_cny']:>10.2f} {p.get('mains', 0):>5}  {delta}")
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
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {
                "type": "object",
                "required": ["name", "items", "coupons", "original_total",
                             "final_total", "per_person", "nutrition", "genui"],
                "properties": {
                    "name": {"type": "string",
                              "description": "方案定位：最省钱/最健康/最快"},
                    "items": {
                        "type": "array", "minItems": 1,
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
            "nutrition": p.get("nutrition") or {},
            "nutrition_flags": list(p.get("nutrition_flags") or []),
            "tradeoffs": _plan_tradeoffs(p),
            "genui": _plan_genui(p),
        }
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

    # 5) 组合优化
    plans = optimize_plans(kept, SAMPLE_COUPONS, c)
    assert 1 <= len(plans) <= 3, len(plans)
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


def main(argv: Sequence[str]) -> int:
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

    if argv and argv[0] == "--dump-schema":
        # 输出契约镜像（重定向到 references/schema.json 以保持同步）
        print(json.dumps(PLAN_OUTPUT_SCHEMA, ensure_ascii=False, indent=2))
        return 0

    if argv and argv[0] == "--json":
        # 结构化输出：走完整解析→过滤→优化流水线，输出契约文档（schema.json 校验通过）
        user_input = next((a for a in argv[1:] if not a.startswith("-")), DEMO_INPUT)
        constraints = parse_constraints(user_input)
        kept = filter_menu(_normalize_menu(SAMPLE_MENU), constraints)
        plans = optimize_plans(kept, SAMPLE_COUPONS, constraints)
        doc = plans_to_doc(plans, scene="到店自取（演示）",
                           people_count=constraints.get("people_count"))
        errs = validate_plans_doc(doc)
        if errs:  # 防御：结构化输出必须自证契约
            print("Schema 校验失败：", errs, file=sys.stderr)
            return 1
        print(json.dumps(doc, ensure_ascii=False, indent=2))
        return 0

    # 支持命令行传入自定义需求；不传则用内置样例
    user_input = next((a for a in argv if not a.startswith("-")), DEMO_INPUT)

    print("=" * 68)
    print("用户输入：", user_input)
    print("=" * 68)

    constraints = parse_constraints(user_input)
    print("\n[解析结果]")
    print(json.dumps({k: v for k, v in constraints.items() if k != "raw"}, ensure_ascii=False, indent=2))

    kept = filter_menu(_normalize_menu(SAMPLE_MENU), constraints)
    print(f"\n[硬约束过滤] 保留 {len(kept)} 项：", "、".join(m["name"] for m in kept))

    plans = optimize_plans(kept, SAMPLE_COUPONS, constraints)
    print("\n[Top-3 方案（加权评分）]")
    print(render_plans(plans))

    print("\n[价格 × 热量 双目标]")
    print(render_pareto(optimize_pareto(kept, SAMPLE_COUPONS, constraints),
                        curve=optimize_cost_curve(kept, SAMPLE_COUPONS, constraints)))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
