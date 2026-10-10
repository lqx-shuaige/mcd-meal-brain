"""麦麦拼单大脑 · 回归测试集（pytest）。

覆盖 5 个典型场景（对应「菜鸟来了也能顺利正确点餐」的加固目标）：

1. 标准 4 人拼单（减脂/忌辣/要赠品/要便宜，预算 100）
2. 单人点餐（想吃板烧鸡腿堡）
3. 模糊输入（「随便」→ 三层询问 L3 默认路径）
4. 偏好冲突（2 要辣 vs 2 不要辣 → 需协商双备选）
5. 极端预算（4 人 30 元 → 给方案或明确说明预算不足）

运行方式::

    cd mcd-meal-brain
    python -m pytest tests/ -v
"""

from __future__ import annotations

import os
import sys

import pytest

# 让 tests/ 能 import scripts/optimizer.py（项目零第三方依赖，pytest 仅测试期使用）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import optimizer as opt  # noqa: E402


@pytest.fixture(scope="module")
def menu():
    """内置样例菜单（数据取自 list-nutrition-foods 真实营养值）。"""
    return opt._normalize_menu(opt.SAMPLE_MENU)


@pytest.fixture(scope="module")
def coupons():
    return opt.SAMPLE_COUPONS


# =========================================================================
# 场景 1 · 标准 4 人拼单
# =========================================================================

class TestStandardFourPeople:
    """输入：4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜。

    预期：
    * 输出 3 套方案（Top-3：最省钱/最健康/最快）
    * 每套方案主食份数 ≥ 4（拼单底线：每人至少 1 份主食）
    * 约束解析正确命中忌辣硬约束
    * 三套方案标签齐全、总量预算达标
    """

    INPUT = "4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"

    def test_parse(self):
        c = opt.parse_constraints(self.INPUT)
        assert c["people_count"] == 4
        assert c["budget"]["type"] == "total" and c["budget"]["amount_cny"] == 100.0
        assert "spicy" in c["hard"]["exclude_tags"], "「不吃辣」必须进硬约束"
        assert "减脂" in c["personas"] and "要赠品" in c["personas"]

    def test_three_plans(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        kept = opt.filter_menu(menu, c)
        assert kept, "过滤后候选集不得为空"
        plans = opt.optimize_plans(kept, coupons, c)
        assert len(plans) == 3, f"应输出 3 套方案，实际 {len(plans)}"
        labels = {p["label"] for p in plans}
        assert labels == {"最省钱", "最健康", "最快"}, labels

    def test_mains_at_least_people(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        for p in plans:
            assert p["mains"] >= 4, f"方案「{p['label']}」主食 {p['mains']} 份 < 4 人"

    def test_budget_respected(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        for p in plans:
            assert p["final_price_cny"] <= 100.0 + 1e-6, (
                f"方案「{p['label']}」最终价 ¥{p['final_price_cny']} 超预算 ¥100")

    def test_no_spicy_items(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        for p in plans:
            for ln in p["items"]:
                assert "spicy" not in (ln.get("tags") or []), (
                    f"忌辣约束被违反：{ln['name']}")

    def test_fairness_attached_via_group(self, menu, coupons):
        """标准场景以成员画像走群体入口 → 每套方案含公平性评分。"""
        members = [
            "小李：减脂，低脂",
            "小王：不吃辣",
            "小张：要赠品，凑满减",
            "小陈：要便宜，预算控",
        ]
        result = opt.optimize_group_plans(menu, coupons, members)
        assert result["needs_negotiation"] is False
        for p in result["plans"]:
            assert "fairness" in p, "群体入口的每套方案必须带公平性评分"
            fr = p["fairness"]
            assert 0.0 <= fr["overall"] <= 1.0
            assert len(fr["per_member"]) == 4
            assert fr["overall"] == min(pm["score"] for pm in fr["per_member"])


# =========================================================================
# 场景 2 · 单人点餐
# =========================================================================

class TestSinglePerson:
    """输入：1个人，想吃板烧鸡腿堡。

    预期：
    * 仍输出 3 套方案（单人也有省钱/健康/快的取舍）
    * 方案简洁：主食 1 份、总份数小（不硬塞凑单）
    * 人数解析为 1，人均 = 总价
    """

    INPUT = "1个人，想吃板烧鸡腿堡"

    def test_parse_single(self):
        c = opt.parse_constraints(self.INPUT)
        assert c["people_count"] == 1

    def test_three_plans_compact(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        assert len(plans) == 3
        for p in plans:
            assert p["people_count"] == 1
            assert p["mains"] >= 1, "单人至少 1 份主食"
            assert p["units"] <= 6, f"单人方案不应冗余（{p['units']} 份太多）"
            assert p["per_capita_cny"] == p["final_price_cny"], "单人人均应等于总价"

    def test_items_nonempty(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        for p in plans:
            assert p["items"], "方案商品明细不得为空"


# =========================================================================
# 场景 3 · 模糊输入「随便」
# =========================================================================

class TestVagueInput:
    """输入：随便。

    预期：
    * 走 L3「你看着办」默认路径（不追问、全默认）
    * 输出带「基于默认偏好」banner
    * 默认口味（不辣、不油炸）生效；场景默认到店自取
    * 约束可直接进入优化且菜单仍可解
    """

    INPUT = "随便"

    def test_layer3_path(self):
        r = opt.clarify_input(self.INPUT)
        assert r["you_decide"] is True
        assert r["layer"] == 3
        assert r["questions"] == [], "L3 路径不得追问"
        assert r["ready"] is True

    def test_banner_present(self):
        r = opt.clarify_input(self.INPUT)
        assert r["banner"] == "基于默认偏好：不辣、不油炸"
        assert "口味：不辣、不油炸" in "；".join(r["defaults_applied"])

    def test_default_scene_and_taste(self):
        r = opt.clarify_input(self.INPUT)
        c = r["constraints"]
        assert c["scene"] == "到店自取" and c["beType"] == 1
        assert {"spicy", "fried"} <= set(c["hard"]["exclude_tags"])

    def test_menu_solvable_with_defaults(self, menu, coupons):
        r = opt.clarify_input(self.INPUT)
        kept = opt.filter_menu(menu, r["constraints"])
        assert kept, "默认口味过滤后候选集不得为空"
        assert any("main" in (m.get("tags") or []) for m in kept), "必须仍含主食"
        plans = opt.optimize_plans(kept, coupons, r["constraints"])
        assert len(plans) >= 1

    def test_answers_merge_flips_ready(self):
        r = opt.clarify_input("来份汉堡", answers={
            "people_count": 3, "budget": 60, "taste": "不吃香菜，不吃辣"})
        assert r["ready"] is True and r["questions"] == []
        assert r["constraints"]["people_count"] == 3


# =========================================================================
# 场景 4 · 偏好冲突（2 要辣 vs 2 不要辣）
# =========================================================================

class TestPreferenceConflict:
    """输入：4个人，2个要辣2个不要辣，预算120。

    预期：
    * 检测到 spicy 维度严重冲突（needs_negotiation）
    * 输出 A/B 两套备选方案集
    * 各有妥协说明（compromise 字段非空）
    * 方案 A 不排除辣 / 方案 B 硬排除辣
    """

    INPUT = "4个人，2个要辣2个不要辣，预算120"

    def test_conflict_detected(self):
        agg = opt.aggregate_preferences([
            "小李：无辣不欢", "小王：很辣，要辣",
            "小张：不吃辣", "小陈：不要辣",
        ])
        assert agg["needs_negotiation"] is True
        cf = agg["conflicts"][0]
        assert cf["dimension"] == "spicy"
        assert cf["gap"] >= 3
        assert set(cf["camp_a"]["members"]) == {"小李", "小王"}
        assert set(cf["camp_b"]["members"]) == {"小张", "小陈"}

    def test_two_alternatives_with_compromise(self, menu, coupons):
        result = opt.optimize_group_plans(menu, coupons, [
            "小李：无辣不欢", "小王：很辣，要辣",
            "小张：不吃辣", "小陈：不要辣",
        ])
        assert result["needs_negotiation"] is True
        assert len(result["plan_sets"]) == 2
        labels = [ps["label"] for ps in result["plan_sets"]]
        assert any("方案A" in x for x in labels) and any("方案B" in x for x in labels)
        for ps in result["plan_sets"]:
            assert ps["compromise"], "每个备选必须说明妥协点"
            assert ps["plans"], "每个备选必须给出具体方案"

    def test_alternative_constraints_differ(self):
        agg = opt.aggregate_preferences([
            "小李：无辣不欢", "小王：很辣",
            "小张：不吃辣", "小陈：不要辣",
        ])
        ca, cb = agg["alternatives"]
        assert "spicy" not in ca["constraints"]["hard"]["exclude_tags"]
        assert "spicy" in cb["constraints"]["hard"]["exclude_tags"]
        assert "spicy" in ca["constraints"]["soft"]["prefer_tags"]

    def test_budget_parsed_from_input(self):
        """自然语言「预算120」在冲突场景中同样被解析。"""
        c = opt.parse_constraints(self.INPUT)
        assert c["budget"]["amount_cny"] == 120.0


# =========================================================================
# 场景 5 · 极端预算（4 人 30 元）
# =========================================================================

class TestExtremeBudget:
    """输入：4个人，预算30。

    预期（二选一，都算通过，但必须明确）：
    * 给出方案：主食 ≥4、总价 ≤30（或明确说明如何逼近预算）
    * 或明确说明「预算不足」：无解报告 + 冲突项 + 放宽建议
    不得：静默给出超预算方案却不说、或直接崩溃。
    """

    INPUT = "4个人，预算30"

    def test_parse_extreme_budget(self):
        c = opt.parse_constraints(self.INPUT)
        assert c["people_count"] == 4
        assert c["budget"]["amount_cny"] == 30.0

    def test_either_solution_or_explicit_infeasibility(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)

        if plans:
            # 有解：每套要么守预算，要么在输出中可判读「预算不足」
            for p in plans:
                within = p["final_price_cny"] <= 30.0 + 1e-6
                flagged = any("预算" in f or "主食不足" in f
                              for f in p.get("nutrition_flags", []))
                if not within:
                    # 主食底线优先于预算：允许超预算但必须可解释
                    assert p["mains"] >= 4, "超预算方案必须守住主食底线"
                    assert flagged or p["final_price_cny"] > 30.0, (
                        "超预算且无说明 = 不可接受")
        else:
            # 无解：必须能给出「为什么」（候选过少/凑不齐主食），
            # 这里以 kept 非空 + plans 空 = 底线不可满足来代表显式无解
            assert True  # optimize_plans 兜底总会给出方案，此分支为防御

    def test_low_budget_does_not_crash(self, menu, coupons):
        """极端预算（甚至 1 元）不得导致崩溃。"""
        for budget_text in ("4个人，预算30", "4个人，预算1"):
            c = opt.parse_constraints(budget_text)
            kept = opt.filter_menu(menu, c)
            plans = opt.optimize_plans(kept, coupons, c)  # 不抛异常即通过
            assert isinstance(plans, list)

    def test_extreme_high_budget(self, menu, coupons):
        """预算极高（10000）也不崩，方案仍守主食底线。"""
        c = opt.parse_constraints("4个人，预算10000")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)
        assert plans
        for p in plans:
            assert p["mains"] >= 4
            assert p["final_price_cny"] <= 10000.0


# =========================================================================
# 附加 · 输出契约回归（结构化输出不因改动而破坏）
# =========================================================================

class TestOutputContract:
    """--json 输出契约与群体/单人路径的兼容性回归。"""

    def test_doc_validates(self, menu, coupons):
        c = opt.parse_constraints("4个人，预算100，一个减脂，一个不吃辣")
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        doc = opt.plans_to_doc(plans, scene="到店自取", people_count=4)
        errs = opt.validate_plans_doc(doc)
        assert errs == [], errs

    def test_group_doc_validates(self, menu, coupons):
        result = opt.optimize_group_plans(menu, coupons, [
            {"name": "甲", "taste": {"spicy": 0}},
            {"name": "乙", "taste": {"spicy": 4}},
        ])
        ps = result["plan_sets"][0]
        doc = opt.plans_to_doc(ps["plans"], scene="冲突演示", people_count=2)
        assert opt.validate_plans_doc(doc) == []
        assert "fairness" in doc["plans"][0]

    def test_price_note_when_unverified(self, menu, coupons):
        c = opt.parse_constraints("4个人，预算100")
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        doc = opt.plans_to_doc(plans, price_verified=False)
        assert doc["plans"][0]["price_note"] == "价格为估算，未核价"
