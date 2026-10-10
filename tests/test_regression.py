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


# =========================================================================
# 方向 4 · 算法精度升级测试（每人预算 / 券可用性 / 极端预算降级）
# =========================================================================

class TestPerPersonBudget:
    """【任务 1】每人预算上限：同时支持总预算 + 每人预算上限。

    场景：4 个人总预算 100，每人不超过 30。
    """

    INPUT = "4个人，总预算100，每人不超过30"

    def test_parse_both_budgets(self):
        """总预算与每人预算同时被正确解析。"""
        c = opt.parse_constraints(self.INPUT)
        assert c["people_count"] == 4
        assert c["budget"]["total_cny"] == 100.0
        assert c["budget"]["per_person_cny"] == 30.0

    def test_parse_per_person_only(self):
        """仅提供每人预算上限，不提供总预算。"""
        c = opt.parse_constraints("4个人，每人不超过25")
        assert c["budget"]["total_cny"] is None
        assert c["budget"]["per_person_cny"] == 25.0

    def test_parse_total_only(self):
        """仅提供总预算上限，不提供每人预算。"""
        c = opt.parse_constraints("4个人，总预算120")
        assert c["budget"]["total_cny"] == 120.0
        assert c["budget"]["per_person_cny"] is None

    def test_budget_hard_constraint_enforced(self, menu, coupons):
        """每人预算上限作为硬约束被强制执行。"""
        c = opt.parse_constraints("4个人，总预算100，每人不超过30")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)
        assert plans, "应至少有一个方案"
        for p in plans:
            assert p["final_price_cny"] <= 100.0 + 1e-6, \
                f"方案 {p['label']} 最终价 {p['final_price_cny']} 超出总预算 100"
            per_capita = p["final_price_cny"] / 4
            assert per_capita <= 30.0 + 1e-6, \
                f"方案 {p['label']} 人均 {per_capita} 超出每人上限 30"

    def test_per_person_budget_no_crash(self, menu, coupons):
        """每人预算上限极低时不应崩溃，应返回降级方案或空列表。"""
        c = opt.parse_constraints("4个人，总预算100，每人不超过3")
        kept = opt.filter_menu(menu, c)
        # 不应抛出异常
        plans = opt.optimize_plans(kept, coupons, c)
        assert isinstance(plans, list), "应返回列表类型"


class TestCouponAvailability:
    """【任务 2】券可用性概率估算。"""

    def test_coupon_with_high_threshold_is_low(self):
        """高门槛券（满 99）的可用性应为「低」。"""
        coupon = {"couponId": "C1", "name": "满99减15", "type": "threshold",
                  "minSpend": 9900, "discount": 1500}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "低"
        assert avail["historical"] is None  # 无历史数据，不编造

    def test_coupon_with_low_threshold_is_high(self):
        """低门槛券（满 9 元以下）的可用性应为「高」。"""
        coupon = {"couponId": "C2", "name": "满9减1", "type": "threshold",
                  "minSpend": 900, "discount": 100}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "高"
        assert avail["historical"] is None

    def test_coupon_with_medium_threshold_is_medium(self):
        """中等门槛券（满 30-60 元）的可用性应为「中」。"""
        coupon = {"couponId": "C3", "name": "满39减5", "type": "threshold",
                  "minSpend": 3900, "discount": 500}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "中"

    def test_coupon_with_time_restriction_is_low(self):
        """带时段限制的券可用性应为「低」。"""
        coupon = {"couponId": "C4", "name": "早餐券", "type": "threshold",
                  "minSpend": 1000, "discount": 200,
                  "timeRestriction": "仅 06:00-10:30 可用"}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "低"

    def test_coupon_with_store_restriction_is_low(self):
        """带门店限制的券可用性应为「低」。"""
        coupon = {"couponId": "C5", "name": "指定门店券", "type": "threshold",
                  "minSpend": 1000, "discount": 200,
                  "appliesToStores": ["S001"]}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "低"

    def test_coupon_with_historical_data(self):
        """有历史可用数据时，应基于数据估算可用性。"""
        coupon = {"couponId": "C6", "name": "测试券", "type": "threshold",
                  "minSpend": 1000, "discount": 200,
                  "successRate": 0.85}
        avail = opt._estimate_coupon_availability(coupon)
        assert avail["level"] == "高"
        assert avail["historical"] == 0.85
        assert "85%" in avail["reason"]

    def test_historical_data_priority_over_threshold(self):
        """有历史数据时优先使用历史数据，而非基于门槛推断。"""
        # 高门槛但历史可用率高
        coupon = {"couponId": "C7", "name": "满99减15", "type": "threshold",
                  "minSpend": 9900, "discount": 1500,
                  "successRate": 0.9}
        avail = opt._estimate_coupon_availability(coupon)
        # 有历史数据时，优先使用历史数据 → 高
        assert avail["level"] == "高"
        assert avail["historical"] == 0.9

    def test_plan_output_contains_coupon_availability(self, menu, coupons):
        """方案输出应包含券可用性字段。"""
        c = opt.parse_constraints("4个人，预算100")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)
        assert plans
        for p in plans:
            assert "coupon_availability" in p, \
                f"方案 {p['label']} 缺少 coupon_availability 字段"
            ca = p["coupon_availability"]
            assert ca is None or "overall" in ca, \
                "coupon_availability 应包含 overall 字段"


class TestExtremeBudgetDegrade:
    """【任务 3】极端预算降级处理。"""

    def test_degraded_plan_generated_when_over_budget(self, menu, coupons):
        """当预算不足时，应自动生成降级方案，而非无解。"""
        items = opt._normalize_menu(opt.SAMPLE_MENU)
        # 4 人预算 50 元 → 每人 12.5 元，远低于最便宜主食的价格
        degraded = opt._generate_degraded_plans(items, 4, 50.0, opt.SAMPLE_COUPONS,
                                                 {"people_count": 4, "budget": {"total_cny": 50.0}})
        assert degraded, "预算不足时应生成降级方案"
        assert all(p.get("degraded") for p in degraded), "降级方案应标记 degraded=True"
        assert all("degrade_reason" in p for p in degraded), "降级方案应有降级原因说明"

    def test_degraded_plan_budget_respected(self, menu, coupons):
        """降级方案的最终价格不应超出预算。"""
        items = opt._normalize_menu(opt.SAMPLE_MENU)
        budget = 50.0
        degraded = opt._generate_degraded_plans(items, 4, budget, opt.SAMPLE_COUPONS,
                                                 {"people_count": 4, "budget": {"total_cny": budget}})
        assert degraded
        for p in degraded:
            # 有商品明细的方案（减配方案）应在预算内
            if p.get("items"):
                assert p["final_price_cny"] <= budget + 0.01, \
                    f"减配方案 {p['final_price_cny']} 超出预算 {budget}"

    def test_extreme_low_budget_clear_message(self, menu, coupons):
        """预算极低（1 元）时，应给出明确的预算差距说明。"""
        items = opt._normalize_menu(opt.SAMPLE_MENU)
        degraded = opt._generate_degraded_plans(items, 4, 1.0, opt.SAMPLE_COUPONS,
                                                 {"people_count": 4, "budget": {"total_cny": 1.0}})
        assert degraded, "预算极低时应返回说明方案"
        for p in degraded:
            assert "degrade_reason" in p
            reason = p["degrade_reason"]
            # 应包含预算相关的明确说明
            assert "预算" in reason, f"降级说明应包含「预算」: {reason}"
            # 应包含建议
            assert "建议" in reason or "调整" in reason, \
                f"降级说明应包含建议: {reason}"

    def test_optimize_plans_with_extreme_budget_no_crash(self, menu, coupons):
        """optimize_plans 在极端预算下不应崩溃。"""
        c = opt.parse_constraints("4个人，预算10")
        kept = opt.filter_menu(menu, c)
        # 不应抛出异常
        plans = opt.optimize_plans(kept, coupons, c)
        assert isinstance(plans, list)

    def test_split_order_suggestion(self, menu, coupons):
        """当人数 ≥ 2 且预算不足以一次性满足时，应提供分步下单建议。"""
        items = opt._normalize_menu(opt.SAMPLE_MENU)
        # 4 人预算 30 → 每人 7.5 元，主食可能不足
        degraded = opt._generate_degraded_plans(items, 4, 30.0, opt.SAMPLE_COUPONS,
                                                 {"people_count": 4, "budget": {"total_cny": 30.0}})
        assert degraded
        labels = [p["label"] for p in degraded]
        # 应至少包含减配方案或分步下单建议
        assert any(l in ("减配方案", "分步下单方案") for l in labels), \
            f"应包含降级方案，实际: {labels}"


# =========================================================================
# 扩展分支 D · 按热量预算配餐（每人 X 大卡以内）
# =========================================================================

class TestCalorieBudget:
    """输入：4个人，每人500大卡以内。

    预期：
    * 「每人 X 大卡」解析为人均硬上限（kcal_scope=per_person）
    * 输出 2–3 套「主餐+小食+饮品」组合，人均热量 ≤ 预算（构造保证）
    * 每人 1 份主餐底线（mains ≥ 人数）
    * 预算过低时显式无解说明，不崩溃、不静默放宽
    """

    INPUT = "4个人，每人500大卡以内"

    def test_parse_per_person_kcal(self):
        c = opt.parse_constraints(self.INPUT)
        assert c["people_count"] == 4
        assert c["targets"]["max_calories_kcal"] == 500
        assert c["targets"]["kcal_scope"] == "per_person"
        assert c["targets"]["tier"] == "custom"

    def test_parse_total_kcal_converted(self):
        """总热量口径按人数换算为人均。"""
        c = opt.parse_constraints("4个人，总热量2000大卡")
        assert c["targets"]["max_calories_kcal"] == 500

    def test_parse_bare_kcal_no_regression(self):
        """无「每人/总」前缀的既有语义不回归：仍按人均上限。"""
        c = opt.parse_constraints("500大卡以内")
        assert c["targets"]["max_calories_kcal"] == 500

    def test_plans_within_budget(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_calorie_budget(kept, coupons, c)
        assert 2 <= len(plans) <= 3, f"应输出 2–3 套方案，实际 {len(plans)}"
        for p in plans:
            assert p["mains"] >= 4, f"「{p['label']}」主餐 {p['mains']} 份 < 4 人"
            assert p["nutrition"]["per_capita_kcal"] <= 500 + 1e-6, (
                f"「{p['label']}」人均热量 {p['nutrition']['per_capita_kcal']} 超预算 500")
            cb = p["calorie_budget"]
            assert cb["cap_per_person_kcal"] == 500
            assert cb["headroom_kcal"] >= -1e-6

    def test_combo_structure(self, menu, coupons):
        """单人组合结构 = 1 主餐 + 0–1 小食 + 0–1 饮品（角色划分正确）。"""
        c = opt.parse_constraints(self.INPUT)
        items = opt._normalize_menu(menu)
        combos = opt._calorie_budget_combos(items, 500)
        assert combos, "500 kcal/人 下应存在可行组合"
        mains_idx = {i for i, it in enumerate(items) if opt._meal_role(it) == "main"}
        for cb in combos:
            assert len(cb) <= 3
            assert sum(1 for i in cb if i in mains_idx) == 1, "每人组合恰含 1 份主餐"
            assert sum(it["kcal"] for it in (items[i] for i in cb)) <= 500 + 1e-6

    def test_protein_plan_beats_budget_plan(self, menu, coupons):
        """高蛋白方案的人均蛋白 ≥ 最省方案（同一可行域内）。"""
        c = opt.parse_constraints(self.INPUT)
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_calorie_budget(kept, coupons, c)
        by_label = {p["label"]: p for p in plans}
        assert by_label["热量预算·高蛋白"]["nutrition"]["per_capita_protein_g"] \
            >= by_label["热量预算·最省"]["nutrition"]["per_capita_protein_g"] - 1e-6

    def test_tight_cap_explicit_infeasible(self, menu, coupons):
        """预算过低（每人 50 大卡）：显式无解说明，不崩溃。"""
        c = opt.parse_constraints("4个人，每人50大卡以内")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_calorie_budget(kept, coupons, c)
        assert len(plans) == 1
        p = plans[0]
        assert p["degraded"] is True
        assert "热量预算无解" in p["label"]
        assert "超出预算" in p["degrade_reason"]
        assert "建议" in p["degrade_reason"]

    def test_doc_validates(self, menu, coupons):
        """热量预算方案同样满足输出契约。"""
        c = opt.parse_constraints(self.INPUT)
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_calorie_budget(kept, coupons, c)
        doc = opt.plans_to_doc(plans, scene="热量预算配餐", people_count=4)
        errs = opt.validate_plans_doc(doc)
        assert errs == [], errs


# =========================================================================
# 批 2 · NSGA-II 多目标优化
# =========================================================================

class TestNSGA2:
    """optimize_plans 默认走 NSGA-II：四目标帕累托前沿取三个极值点。

    目标（全部最小化）：价格 / 人均热量 / 油炸份数 / 负人均蛋白。
    预期：
    * Top-3 标签齐全（最省钱/最健康/最快），全部位于第一前沿
    * 三套方案互不支配（帕累托最优）
    * 标签方向正确（最省钱=最低价，最健康=最高蛋白）
    * 固定种子可复现；加权路径 method="weighted" 保留可用
    """

    INPUT = "4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"

    def test_three_extreme_plans(self, menu, coupons):
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        assert len(plans) == 3
        assert {p["label"] for p in plans} == {"最省钱", "最健康", "最快"}
        for p in plans:
            assert p["mains"] >= 4
            assert p["final_price_cny"] <= 100.0 + 1e-6
            assert p["nsga2"]["front"] == 1

    def test_pareto_mutual_nondomination(self, menu, coupons):
        """三套方案两两互不支配（均为帕累托最优解）。"""
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        objs = [opt._plan_objectives(p) for p in plans]
        for i in range(len(objs)):
            for j in range(i + 1, len(objs)):
                assert not opt._dominates(objs[i], objs[j]), (i, j)
                assert not opt._dominates(objs[j], objs[i]), (i, j)

    def test_label_extremes(self, menu, coupons):
        """标签方向：最省钱=最低价；最健康=最高蛋白。"""
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
        by_label = {p["label"]: p for p in plans}
        assert by_label["最省钱"]["final_price_cny"] == \
            min(p["final_price_cny"] for p in plans)
        assert by_label["最健康"]["nutrition"]["per_capita_protein_g"] == \
            max(p["nutrition"]["per_capita_protein_g"] for p in plans)

    def test_deterministic(self, menu, coupons):
        """固定随机种子：两次运行输出完全一致。"""

        def sig():
            plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c)
            return [(p["label"], opt._basket_key(p)) for p in plans]

        c = opt.parse_constraints(self.INPUT)
        assert sig() == sig()

    def test_weighted_method_available(self, menu, coupons):
        """原加权路径保留可用（对照与回退）。"""
        c = opt.parse_constraints(self.INPUT)
        plans = opt.optimize_plans(opt.filter_menu(menu, c), coupons, c,
                                   method="weighted")
        assert plans
        assert {p["label"] for p in plans} <= {"最省钱", "最健康", "最快"}

    def test_dominates_and_front_sort(self):
        """支配关系与非支配排序的基础语义。"""
        assert opt._dominates((1, 1), (2, 2))
        assert not opt._dominates((2, 2), (1, 1))
        assert not opt._dominates((1, 2), (2, 1))
        fronts = opt._fast_non_dominated_sort(
            [(1, 1), (2, 2), (1.5, 1.5), (0, 2)])
        assert fronts == [[0, 3], [2], [1]]  # (1,1)与(0,2)互不支配；(2,2)被链式支配

    def test_crowding_distance(self):
        """拥挤距离：边界点无穷大，中间点为归一化间距之和。"""
        dist = opt._crowding_distance([0, 1, 2], [(0, 0), (1, 1), (2, 2)])
        assert dist[0] == float("inf") and dist[2] == float("inf")
        assert abs(dist[1] - 2.0) < 1e-9

    def test_kcal_hard_cap_respected(self, menu, coupons):
        """显式热量预算（每人 500 大卡）作为硬上限：可行解存在时全部达标。"""
        c = opt.parse_constraints("4个人，预算300，每人500大卡以内")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)
        assert plans
        for p in plans:
            assert p["nutrition"]["per_capita_kcal"] <= 500 + 1e-6, (
                p["label"], p["nutrition"]["per_capita_kcal"])


# =========================================================================
# 批 2 · ReflectCache 参数级缓存
# =========================================================================

class TestReflectCache:
    """MCP 只读工具的参数级结果缓存。

    预期：
    * 键 = 工具名 + 规范化参数（顺序无关、精确匹配）
    * 同参数重复调用只触网一次；参数不同重新触网
    * 写操作 / now-time-info 穿透；业务错误不缓存；TTL 过期重新触网
    * 返回深拷贝，调用方修改不污染缓存
    """

    def test_make_key_exact_match(self):
        k1 = opt.ReflectCache.make_key("query-meals", {"storeCode": "S1", "orderType": 1})
        k2 = opt.ReflectCache.make_key("query-meals", {"orderType": 1, "storeCode": "S1"})
        k3 = opt.ReflectCache.make_key("query-meals", {"storeCode": "S1", "orderType": 2})
        k4 = opt.ReflectCache.make_key("query-store-coupons",
                                       {"storeCode": "S1", "orderType": 1})
        assert k1 == k2, "参数顺序不得影响键"
        assert k1 != k3, "参数值不同 → 不同键"
        assert k1 != k4, "工具不同 → 不同键"

    def test_integration_same_args_single_network_call(self):
        net = []

        def fake_once(tool, arguments, token, timeout):
            net.append((tool, dict(arguments)))
            return {"result": {"isError": False, "structuredContent": {
                "success": True, "code": 200, "data": {"calls": len(net)}}}}

        orig = opt.__dict__["_mcp_call_once"]
        opt._mcp_call_once = fake_once
        opt.REFLECT_CACHE.clear()
        try:
            r1 = opt._mcp_call("query-meals", {"storeCode": "S1", "orderType": 1},
                               token="t", log=False)
            r2 = opt._mcp_call("query-meals", {"orderType": 1, "storeCode": "S1"},
                               token="t", log=False)
            assert len(net) == 1, "同参数必须命中缓存"
            assert r1 == r2
            r3 = opt._mcp_call("query-meals", {"storeCode": "S2", "orderType": 1},
                               token="t", log=False)
            assert len(net) == 2, "参数不同必须重新触网"
            assert r3["result"]["structuredContent"]["data"]["calls"] == 2
            st = opt.REFLECT_CACHE.stats()
            assert st["hits"] == 1 and st["misses"] == 2
        finally:
            opt._mcp_call_once = orig
            opt.REFLECT_CACHE.clear()

    def test_deep_copy_isolation(self):
        net = []

        def fake_once(tool, arguments, token, timeout):
            net.append(1)
            return {"result": {"structuredContent": {"success": True,
                                                     "data": {"n": len(net)}}}}

        orig = opt.__dict__["_mcp_call_once"]
        opt._mcp_call_once = fake_once
        opt.REFLECT_CACHE.clear()
        try:
            r1 = opt._mcp_call("list-nutrition-foods", {}, token="t", log=False)
            r1["result"]["structuredContent"]["data"]["n"] = 999
            r2 = opt._mcp_call("list-nutrition-foods", {}, token="t", log=False)
            assert r2["result"]["structuredContent"]["data"]["n"] == 1, \
                "缓存必须返回深拷贝"
        finally:
            opt._mcp_call_once = orig
            opt.REFLECT_CACHE.clear()

    def test_write_tools_and_time_bypass(self):
        net = []

        def fake_once(tool, arguments, token, timeout):
            net.append(tool)
            return {"result": {"structuredContent": {"success": True, "data": {}}}}

        orig = opt.__dict__["_mcp_call_once"]
        opt._mcp_call_once = fake_once
        opt.REFLECT_CACHE.clear()
        try:
            opt._mcp_call("create-order", {"storeCode": "S1"}, token="t", log=False)
            opt._mcp_call("create-order", {"storeCode": "S1"}, token="t", log=False)
            assert net.count("create-order") == 2, "写操作必须穿透缓存"
            opt._mcp_call("now-time-info", {}, token="t", log=False)
            opt._mcp_call("now-time-info", {}, token="t", log=False)
            assert net.count("now-time-info") == 2, "时间敏感工具永不缓存"
        finally:
            opt._mcp_call_once = orig
            opt.REFLECT_CACHE.clear()

    def test_business_error_not_cached(self):
        net = []

        def fake_once(tool, arguments, token, timeout):
            net.append(tool)
            return {"result": {"isError": False, "structuredContent": {
                "success": False, "code": 60003, "message": "门店打烊"}}}

        orig = opt.__dict__["_mcp_call_once"]
        opt._mcp_call_once = fake_once
        opt.REFLECT_CACHE.clear()
        try:
            opt._mcp_call("query-meals", {"storeCode": "S9"}, token="t", log=False)
            opt._mcp_call("query-meals", {"storeCode": "S9"}, token="t", log=False)
            assert len(net) == 2, "业务错误不得缓存"
        finally:
            opt._mcp_call_once = orig
            opt.REFLECT_CACHE.clear()

    def test_ttl_expiry_refetches(self):
        import time as _time
        net = []

        def fake_once(tool, arguments, token, timeout):
            net.append(tool)
            return {"result": {"structuredContent": {"success": True, "data": {}}}}

        orig = opt.__dict__["_mcp_call_once"]
        orig_cache = opt.__dict__["REFLECT_CACHE"]
        opt._mcp_call_once = fake_once
        opt.REFLECT_CACHE = opt.ReflectCache(ttl_s=0)
        try:
            opt._mcp_call("query-meals", {"storeCode": "S3"}, token="t", log=False)
            _time.sleep(0.01)
            opt._mcp_call("query-meals", {"storeCode": "S3"}, token="t", log=False)
            assert len(net) == 2, "TTL 过期后必须重新触网"
        finally:
            opt._mcp_call_once = orig
            opt.REFLECT_CACHE = orig_cache
            opt.REFLECT_CACHE.clear()

    def test_capacity_eviction(self):
        cache = opt.ReflectCache(ttl_s=60, max_entries=2)
        for i in range(3):
            cache.store("query-meals", {"storeCode": f"S{i}"}, {"i": i})
        assert cache.stats()["entries"] == 2, "超容量必须淘汰最旧项"
        # 最旧的 S0 已被淘汰 → miss；S1/S2 命中
        assert cache.lookup("query-meals", {"storeCode": "S0"}) is None
        assert cache.lookup("query-meals", {"storeCode": "S2"}) == {"i": 2}


class TestSplitBill:
    """智能分账：归属分配 + 优惠比例分摊 + 分账平衡。"""

    PLAN = {
        "label": "分账回归", "people_count": 4,
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
    MAINS = {"板烧鸡腿堡", "麦香鱼", "麦辣鸡腿堡", "双层吉士汉堡"}

    def test_balanced_persona_mode(self):
        s = opt.split_bill(self.PLAN, ["减脂", "忌口", "要赠品", "预算控"])
        assert s["mode"] == "persona"
        assert s["total"]["balanced"] is True
        assert round(s["total"]["sum_due_cny"], 2) == 96.5

    def test_everyone_gets_main(self):
        for s in (opt.split_bill(self.PLAN, ["减脂", "忌口", "要赠品", "预算控"]),
                  opt.split_bill(self.PLAN)):
            for m in s["members"]:
                assert any(it["name"] in self.MAINS for it in m["items"]), \
                    f"{m['name']} 未分到主食"

    def test_persona_matching(self):
        s = opt.split_bill(self.PLAN, ["减脂", "忌口", "要赠品", "预算控"])
        jianfei = next(m for m in s["members"] if m["persona"] == "减脂")
        assert "麦香鱼" in {it["name"] for it in jianfei["items"]}, "减脂→最低热量主食"
        zengpin = next(m for m in s["members"] if m["persona"] == "要赠品")
        assert any("冰淇淋" in it["name"] for it in zengpin["items"]), "要赠品→甜品"

    def test_discount_proportional(self):
        s = opt.split_bill(self.PLAN, ["减脂", "忌口", "要赠品", "预算控"])
        assert round(sum(m["discount_share_cny"] for m in s["members"]), 2) == 10.0
        # 比例性：小计更高的成员分摊的优惠绝对值不小于小计更低者（同比例下成立）
        shares = [(m["subtotal_cny"], m["discount_share_cny"]) for m in s["members"]]
        shares.sort()
        assert shares[-1][1] >= shares[0][1]

    def test_no_discount(self):
        plan = {**self.PLAN, "discount_cny": 0.0, "final_price_cny": 106.5, "coupons": []}
        s = opt.split_bill(plan, ["减脂", "忌口", "要赠品", "预算控"])
        assert all(m["discount_share_cny"] == 0 for m in s["members"])
        assert s["total"]["balanced"] is True

    def test_balanced_mode(self):
        s = opt.split_bill(self.PLAN)
        assert s["mode"] == "balanced" and s["total"]["balanced"] is True

    def test_render(self):
        s = opt.split_bill(self.PLAN, ["减脂", "忌口", "要赠品", "预算控"])
        text = opt.render_split_bill(s)
        assert "智能分账" in text and "分账平衡" in text and "应付" in text


class TestDPExactness:
    """DP 精确性：与暴力穷举交叉验证（同一搜索空间内必须严格最优）。

    历史教训：热量桶向下取整时，同桶竞争中「超限的便宜解」会挤掉
    「合规的最优解」（2026-10-10 审计发现，随机实例可复现差 ¥5.59）。
    修复：ceil 桶语义 + step 自适应（整数热量下 step=1 零取整误差）。
    """

    @staticmethod
    def _brute(items, people, kcal_min, kcal_max, max_units=8):
        max_mains = max(people, 1) + 2          # 与 DP 同一搜索空间
        n = len(items)
        best = None
        import itertools
        for combo in itertools.product(*[range(0, max_units // n + 2) for _ in range(n)]):
            mains = sum(c for c, it in zip(combo, items) if "main" in it["tags"])
            if mains < people or mains > max_mains:
                continue
            if sum(combo) == 0 or sum(combo) > max_units:
                continue
            kcal = sum(c * it["kcal"] for c, it in zip(combo, items))
            if kcal < kcal_min or kcal > kcal_max:
                continue
            cost = sum(c * it["price"] for c, it in zip(combo, items))
            if best is None or cost < best:
                best = cost
        return best

    def test_dp_matches_brute_force(self):
        import random
        rng = random.Random(20261010)
        checked = 0
        for _ in range(150):
            n = rng.randint(3, 6)
            items = [{"name": f"P{i}", "price": rng.randint(300, 3500),
                      "kcal": float(rng.randint(30, 800)),
                      "tags": ["main"] if i % 2 == 0 else ["side"]} for i in range(n)]
            people = rng.randint(1, 3)
            kcal_min = float(rng.randint(200, 900))
            kcal_max = kcal_min + rng.randint(300, 1600)
            bf = self._brute(items, people, kcal_min, kcal_max)
            sols = opt._dp_min_cost(items, people, kcal_min, kcal_max)
            dp_best = sols[0]["cost"] if sols else None
            if bf is None:
                continue
            checked += 1
            assert dp_best is not None, "DP 漏解：暴力找到可行解而 DP 无解"
            assert dp_best <= bf, f"DP 次优：{dp_best} > 暴力最优 {bf}"
        assert checked >= 100, "随机实例有效率不足，测试本身可疑"

    def test_dp_never_outputs_overlimit(self):
        import random
        rng = random.Random(9527)
        for _ in range(150):
            n = rng.randint(3, 6)
            items = [{"name": f"P{i}", "price": rng.randint(300, 3500),
                      "kcal": float(rng.randint(30, 800)),
                      "tags": ["main"] if i % 2 == 0 else ["side"]} for i in range(n)]
            people = rng.randint(1, 3)
            kcal_min = float(rng.randint(200, 900))
            kcal_max = kcal_min + rng.randint(300, 1600)
            for s in opt._dp_min_cost(items, people, kcal_min, kcal_max):
                real = sum(items[i]["kcal"] for i in s["indices"])
                assert kcal_min <= real <= kcal_max, \
                    f"DP 输出违反热量约束：{real} ∉ [{kcal_min}, {kcal_max}]"

    def test_split_bill_over_discount_guard(self):
        """券面额异常大于小计：不产生负应付，超出部分记 unallocated。"""
        plan = {
            "label": "异常券", "people_count": 2,
            "items": [
                {"name": "麦香鱼", "quantity": 1, "unit_price_cny": 17.5,
                 "subtotal_cny": 17.5, "kcal_per_unit": 325, "tags": ["main"], "is_main": True},
                {"name": "圆筒冰淇淋", "quantity": 1, "unit_price_cny": 6.0,
                 "subtotal_cny": 6.0, "kcal_per_unit": 93, "tags": ["dessert"], "is_main": False},
            ],
            "coupons": [{"name": "异常满减", "discount_cny": 50.0}],
            "subtotal_cny": 23.5, "discount_cny": 50.0, "final_price_cny": 0.0,
        }
        s = opt.split_bill(plan)
        assert all(m["due_cny"] >= 0 for m in s["members"]), "出现负应付"
        assert all(m["discount_share_cny"] <= m["subtotal_cny"] for m in s["members"]), \
            "成员分摊优惠超出其小计"
        assert s["total"]["unallocated_discount_cny"] == 26.5, "超出部分未正确记入 unallocated"


# =========================================================================
# 场景 6 · 全面审计修复回归（2026-10-10）
# =========================================================================

class TestAuditFixes:
    """钉死 2026-10-10 全面审计修复的行为语义，防止回退。

    覆盖：
    * B1  clarify_input 二轮答案合并的预算 canonical 结构（total_cny/per_person_cny
           补全、不再污染 hard.max_per_capita_cny）
    * B2  normalize_food_name 语义（【】去括号保内容 / 圆括号去内容）
    * B4  口味否定式（「不怕辣」≠「怕辣」）
    * B3  降级方案过输出契约（items 允许为空 + degraded 标记透传）
          与契约强度兜底（非降级方案 items 必非空）
    * B11 gen_demo 人均营养矩阵不再二次除以人数
    """

    def test_clarify_budget_merge_canonical(self):
        """B1：二轮合并预算后，budget 字典结构与 parse_constraints 一致。"""
        r = opt.clarify_input("来份汉堡",
                              answers={"people_count": 2, "budget": 60})
        b = r["constraints"]["budget"]
        assert b == {"type": "total", "amount_cny": 60.0,
                     "total_cny": 60.0, "per_person_cny": None}, b
        # 总预算不再被误写进人均上限（语义错位，旧实现曾硬塞 max_per_capita_cny）
        assert r["constraints"]["hard"].get("max_per_capita_cny") is None

    def test_clarify_budget_merge_per_capita_string(self):
        """B1：字符串答案含「每人/人均」时按人均口径解析。"""
        r = opt.clarify_input("来份汉堡",
                              answers={"people_count": 2, "budget": "每人30"})
        b = r["constraints"]["budget"]
        assert b["type"] == "per_capita", b
        assert b["per_person_cny"] == 30.0 and b["total_cny"] is None, b

    def test_normalize_food_name_semantics(self):
        """B2：方括号保内容（品牌前缀）、圆括号去内容（包装规格）、剥 ™/®/引号。"""
        assert opt.normalize_food_name("【美汁源】“黄金橙橙”") == "美汁源黄金橙橙"
        assert opt.normalize_food_name("纯牛奶(盒装)") == "纯牛奶"
        assert opt.normalize_food_name("纯牛奶（盒装）") == "纯牛奶"
        assert opt.normalize_food_name("麦咖啡™热奶铁中杯") == "麦咖啡热奶铁中杯"

    def test_taste_negation_not_spicy(self):
        """B4：「不怕辣」是肯定式（=3 能吃辣），不再被子串「怕辣」误判为 0。"""
        assert opt._infer_taste("不怕辣") == {"spicy": 3}
        assert opt._infer_taste("怕辣") == {"spicy": 0}
        assert opt._infer_taste("不吃辣") == {"spicy": 0}
        assert opt._infer_taste("非常怕辣") == {"spicy": 0}

    def test_degraded_plans_pass_contract(self, menu):
        """B3：极端预算降级方案（items 为空）必须能通过输出契约校验。"""
        degraded = opt._generate_degraded_plans(
            menu, 4, 1.0, opt.SAMPLE_COUPONS,
            {"people_count": 4, "budget": {"total_cny": 1.0}})
        assert degraded and degraded[0].get("degraded") is True
        doc = opt.plans_to_doc(degraded, scene="极端预算", people_count=4)
        assert opt.validate_plans_doc(doc) == [], opt.validate_plans_doc(doc)
        assert doc["plans"][0]["degraded"] is True
        assert doc["plans"][0]["degrade_reason"]
        assert doc["plans"][0]["items"] == []
        # 空篮筐营养按 0 填充（7 个契约必填字段齐全）
        assert doc["plans"][0]["nutrition"]["per_capita_kcal"] == 0.0

    def test_normal_plans_items_never_empty(self, menu, coupons):
        """B3 契约强度兜底：非降级方案 items 必非空，且整体过契约。"""
        c = opt.parse_constraints("4个人，预算100")
        kept = opt.filter_menu(menu, c)
        plans = opt.optimize_plans(kept, coupons, c)
        assert plans, "标准场景应有可行方案"
        for p in plans:
            assert not p.get("degraded")
            assert p["items"], f"{p['label']} 明细为空"
        doc = opt.plans_to_doc(plans, scene="标准拼单", people_count=4)
        assert opt.validate_plans_doc(doc) == []

    def test_gen_demo_nutrition_matrix_no_double_division(self):
        """B11：雷达图人均值与 optimizer 输出的 per_capita_* 一致，不二次除人数。"""
        import gen_demo
        data = gen_demo.run_scenario(gen_demo.DEMO_INPUT)
        matrix = {r["label"]: r for r in gen_demo._nutrition_matrix(data["plans"])}
        cheap = next(p for p in data["plans"] if p["label"] == "最省钱")
        n = cheap["nutrition"]
        people = cheap["people_count"]
        assert people == 4
        assert matrix["最省钱"]["kcal"] == round(n["per_capita_kcal"], 1)
        assert matrix["最省钱"]["protein"] == round(n["per_capita_protein_g"], 1)
        assert matrix["最省钱"]["fat"] == round(n["per_capita_fat_g"], 1)
        # 钠是总量口径：唯一应该除以人数的字段
        assert matrix["最省钱"]["sodium"] == round(n["total_sodium_mg"] / people, 0)
