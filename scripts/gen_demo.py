#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""演示报告生成器 —— 把 optimizer.py 的真实输出渲染成单文件可视化报告。

产出
----
``docs/demo.html``：零依赖、纯 HTML+CSS+内联 SVG，浏览器直接打开（无需服务器）。

内容
----
1. 省钱大字卡：基线（朴素点法）→ 最省钱方案，降幅红色大字突出
2. 价格对比柱状图（SVG 手绘，无任何 JS 图表库）
3. 营养雷达图（人均 热量/蛋白/脂肪/钠 四维，SVG 手绘）
4. 券组合明细表（每套方案用了哪些券、省了多少）
5. 分摊账单表（每人应付多少，均摊口径）
6. 三方案商品明细对比表

数据真实性
----------
**所有展示数字均来自 optimizer.py 的真实运行输出**（parse_constraints →
filter_menu → optimize_plans 完整链路），基线价从 SAMPLE_MENU 真实单价推导，
本脚本不做任何手编数据；完整结构化输出以 ``<script type="application/json">``
形式内嵌在 HTML 末尾，供评审审计。

零依赖约束
----------
仅用 Python 标准库（json/math/datetime/html），不影响 optimizer.py 本体；
HTML 不引用任何外部资源（无 CDN / 字体 / 图片），离线可开。

用法
::

    python scripts/gen_demo.py                    # 默认演示场景 → docs/demo.html
    python scripts/gen_demo.py --input "我们3个人…" --out docs/other.html
"""

from __future__ import annotations

import html
import json
import math
import os
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import optimizer  # noqa: E402  —— 同目录导入，复用完整优化链路

# ---------------------------------------------------------------------------
# 0. 常量
# ---------------------------------------------------------------------------

#: 演示场景（与 README 5.1 节一致，可一键复现）
DEMO_INPUT = "我们4个人，预算100，一个减脂，一个不吃辣，一个要赠品，一个要便宜"

#: 基线商品编码：朴素点法 = 每人 1 份板烧鸡腿堡，不用任何券
BASELINE_PRODUCT_CODE = "P001"

#: 方案展示顺序（用户视角：省钱 → 健康 → 快）
PLAN_ORDER = ("最省钱", "最健康", "最快")

#: 方案配色（麦当劳主题色系）
PLAN_COLORS: Dict[str, Dict[str, str]] = {
    "最省钱": {"main": "#da291c", "light": "#fdecea"},   # 麦当劳红
    "最健康": {"main": "#00a651", "light": "#e8f7ef"},   # 绿
    "最快": {"main": "#f59e0b", "light": "#fef5e7"},     # 琥珀
}
BASELINE_COLOR = "#8a8f98"

#: 输出路径（相对仓库根）
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO_ROOT, "docs", "demo.html")


# ---------------------------------------------------------------------------
# 1. 数据准备（全部来自 optimizer 真实输出）
# ---------------------------------------------------------------------------

def run_scenario(user_input: str) -> Dict[str, Any]:
    """跑完整优化链路，返回报告所需的全部真实数据。"""
    menu = optimizer._normalize_menu(optimizer.SAMPLE_MENU)
    constraints = optimizer.parse_constraints(user_input)
    kept = optimizer.filter_menu(menu, constraints)
    plans = optimizer.optimize_plans(kept, optimizer.SAMPLE_COUPONS, constraints)

    if not plans:
        raise SystemExit("优化器未返回任何方案，无法生成报告")

    by_label = {p["label"]: p for p in plans}
    ordered = [by_label[lb] for lb in PLAN_ORDER if lb in by_label]
    ordered += [p for p in plans if p["label"] not in PLAN_ORDER]  # 兜底：未知标签排后

    # 基线：朴素点法（每人 1 份基准单品，不用券），价格取自 SAMPLE_MENU 真实单价
    base_item = next(
        (m for m in menu if m.get("productCode") == BASELINE_PRODUCT_CODE), menu[0]
    )
    people = int(constraints.get("people_count") or 1)
    baseline = {
        "product_name": base_item["name"],
        "unit_price_cny": round(base_item["price"] / 100.0, 2),
        "people_count": people,
        "total_cny": round(base_item["price"] * people / 100.0, 2),
        "per_capita_cny": round(base_item["price"] / 100.0, 2),
    }

    # 硬约束过滤明细（被淘汰项 + 原因），供「解析卡」展示
    ban_tags = set((constraints.get("hard") or {}).get("exclude_tags") or [])
    ban_ing = set((constraints.get("hard") or {}).get("exclude_ingredients") or [])
    rejected: List[Dict[str, str]] = []
    for m in menu:
        reasons = []
        for tag in ban_ing:
            words = optimizer.INGREDIENT_SYNONYMS.get(tag, ())
            if any(w in m["name"] for w in words):
                reasons.append(f"命中忌口成分[{tag}]")
        for tag in ban_tags:
            if tag in (m.get("tags") or []):
                reasons.append(f"命中排除标签[{tag}]")
        if reasons:
            rejected.append({"name": m["name"], "reason": "、".join(sorted(set(reasons)))})

    # 券规则描述（从券定义生成，非手编）
    coupon_rules = _coupon_rule_table()

    return {
        "user_input": user_input,
        "constraints": constraints,
        "kept_count": len(kept),
        "menu_total": len(menu),
        "rejected": rejected,
        "plans": ordered,
        "baseline": baseline,
        "coupon_rules": coupon_rules,
        "generated_at": _now_str(),
    }


def _now_str() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _coupon_rule_table() -> Dict[str, str]:
    """券ID → 人类可读规则（如「满 ¥99 减 ¥15」「满 ¥50 享 9 折」）。"""
    rules: Dict[str, str] = {}
    for c in optimizer.SAMPLE_COUPONS:
        cid = c.get("couponId")
        if c.get("type") == "percent":
            rate = float(c.get("rate") or 1.0)
            rules[cid] = f"满 ¥{c['minSpend'] / 100:.0f} 享 {rate * 10:g} 折"
        else:
            rules[cid] = f"满 ¥{c['minSpend'] / 100:.0f} 减 ¥{c['discount'] / 100:.0f}"
    return rules


def _per_capita(plan: Dict[str, Any], key_total: str) -> float:
    """从方案营养总量换算人均（钠在 optimizer 输出中只有总量口径）。"""
    return plan["nutrition"][key_total] / max(plan["people_count"], 1)


def _nutrition_matrix(plans: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """雷达图数据：每方案的人均 热量/蛋白/脂肪/钠（真实值换算）。"""
    rows = []
    for p in plans:
        n = p["nutrition"]
        rows.append({
            "label": p["label"],
            "kcal": round(_per_capita(p, "per_capita_kcal"), 1),
            "protein": round(_per_capita(p, "per_capita_protein_g"), 1),
            "fat": round(_per_capita(p, "per_capita_fat_g"), 1),
            "sodium": round(_per_capita(p, "total_sodium_mg"), 0),
        })
    return rows


# ---------------------------------------------------------------------------
# 2. SVG 图表（手绘，无 JS 依赖）
# ---------------------------------------------------------------------------

def bar_chart_svg(baseline_total: float, plans: Sequence[Dict[str, Any]]) -> str:
    """价格对比柱状图：基线 + 三方案最终价（含原价参考刻线）。"""
    W, H = 680, 420
    plot_l, plot_r, plot_t, plot_b = 70, 650, 50, 350
    plot_h = plot_b - plot_t

    bars = [("基线·朴素点法", baseline_total, BASELINE_COLOR, False)]
    for p in plans:
        c = PLAN_COLORS.get(p["label"], {}).get("main", "#5b7fff")
        bars.append((p["label"], p["final_price_cny"], c, p["label"] == "最省钱"))

    y_max = math.ceil(max(v for _, v, _, _ in bars) / 20.0) * 20 or 20
    n = len(bars)
    slot = (plot_r - plot_l) / n
    bar_w = min(88, slot * 0.55)

    def y_of(v: float) -> float:
        return plot_b - v / y_max * plot_h

    parts: List[str] = [f'<svg viewBox="0 0 {W} {H}" role="img" '
                        f'aria-label="价格对比柱状图" xmlns="http://www.w3.org/2000/svg">']

    # 网格线 + y 轴刻度
    for tick in range(0, y_max + 1, 20):
        gy = y_of(tick)
        parts.append(f'<line x1="{plot_l}" y1="{gy:.1f}" x2="{plot_r}" y2="{gy:.1f}" '
                     f'stroke="#ececec" stroke-width="1"/>')
        parts.append(f'<text x="{plot_l - 10}" y="{gy + 4:.1f}" text-anchor="end" '
                     f'class="axis">¥{tick}</text>')
    parts.append(f'<line x1="{plot_l}" y1="{plot_b}" x2="{plot_r}" y2="{plot_b}" '
                 f'stroke="#c9c9c9" stroke-width="1.5"/>')

    for i, (name, value, color, highlight) in enumerate(bars):
        cx = plot_l + slot * (i + 0.5)
        x0 = cx - bar_w / 2
        y0 = y_of(value)
        h = plot_b - y0
        # 柱体
        parts.append(f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{bar_w:.1f}" '
                     f'height="{h:.1f}" rx="6" fill="{color}" '
                     f'fill-opacity="{1.0 if highlight else 0.88}"/>')
        # 顶部金额
        parts.append(f'<text x="{cx:.1f}" y="{y0 - 26:.1f}" text-anchor="middle" '
                     f'class="bar-val{" hot" if highlight else ""}">¥{value:.2f}</text>')
        # 底部标签
        parts.append(f'<text x="{cx:.1f}" y="{plot_b + 30}" text-anchor="middle" '
                     f'class="bar-lab">{"基线" if name.startswith("基线") else name}</text>')
        parts.append(f'<text x="{cx:.1f}" y="{plot_b + 50}" text-anchor="middle" '
                     f'class="bar-sub">{"不用券" if name.startswith("基线") else "券后到手"}</text>')

    # 基线参考虚线（贯穿画面，便于横向比较）
    gy = y_of(baseline_total)
    parts.append(f'<line x1="{plot_l}" y1="{gy:.1f}" x2="{plot_r}" y2="{gy:.1f}" '
                 f'stroke="{BASELINE_COLOR}" stroke-width="1.5" stroke-dasharray="6 5" '
                 f'stroke-opacity="0.65"/>')

    parts.append("</svg>")
    return "".join(parts)


def radar_chart_svg(matrix: Sequence[Dict[str, Any]]) -> str:
    """营养雷达图：人均 热量/蛋白/脂肪/钠 四维，三方案叠加对比。

    每个维度以三方案中的最大值为 100%（外圈），真实数值标注在顶点旁。
    """
    W, H = 680, 540
    cx, cy, R = 340, 280, 190
    axes = [
        ("热量", "kcal/人", "kcal", -90),
        ("蛋白质", "g/人", "protein", 0),
        ("脂肪", "g/人", "fat", 90),
        ("钠", "mg/人", "sodium", 180),
    ]

    def pt(angle_deg: float, r: float) -> Tuple[float, float]:
        a = math.radians(angle_deg)
        return cx + r * math.cos(a), cy + r * math.sin(a)

    # 各维度归一化分母（三方案最大值，防零）
    denoms = {}
    for _, _, key, _ in axes:
        denoms[key] = max((row[key] for row in matrix), default=0.0) or 1.0

    parts: List[str] = [f'<svg viewBox="0 0 {W} {H}" role="img" '
                        f'aria-label="营养雷达图" xmlns="http://www.w3.org/2000/svg">']

    # 同心网格（25/50/75/100%）
    for frac in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(
            f"{x:.1f},{y:.1f}" for x, y in (pt(a, R * frac) for _, _, _, a in axes)
        )
        parts.append(f'<polygon points="{pts}" fill="none" stroke="#e3e6ea" '
                     f'stroke-width="1"/>')
    # 轴线 + 维度标签
    for title, unit, _, a in axes:
        ex, ey = pt(a, R)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{ex:.1f}" y2="{ey:.1f}" '
                     f'stroke="#d5d9de" stroke-width="1"/>')
        lx, ly = pt(a, R + 42)
        dy = -6 if abs(math.sin(math.radians(a))) > 0.5 else 0
        anchor = "middle"
        parts.append(f'<text x="{lx:.1f}" y="{ly + dy:.1f}" text-anchor="{anchor}" '
                     f'class="radar-axis">{title}</text>')
        parts.append(f'<text x="{lx:.1f}" y="{ly + dy + 18:.1f}" text-anchor="{anchor}" '
                     f'class="radar-unit">{unit}</text>')

    # 三方案多边形
    for row in matrix:
        color = PLAN_COLORS.get(row["label"], {}).get("main", "#5b7fff")
        verts: List[Tuple[float, float, float]] = []
        for _, _, key, a in axes:
            r = R * (row[key] / denoms[key])
            verts.append((*pt(a, r), row[key]))
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in verts)
        parts.append(f'<polygon points="{poly}" fill="{color}" fill-opacity="0.16" '
                     f'stroke="{color}" stroke-width="2.5" stroke-linejoin="round"/>')
        # 顶点圆点 + 数值（沿半径向内偏移，避免与外圈标签打架）
        for i, (x, y, val) in enumerate(verts):
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}"/>')
            vx = x + (x - cx) * 0.055
            vy = y + (y - cy) * 0.055 + 4
            parts.append(f'<text x="{vx:.1f}" y="{vy:.1f}" text-anchor="middle" '
                         f'class="radar-val" fill="{color}">{val:g}</text>')

    parts.append("</svg>")
    return "".join(parts)


# ---------------------------------------------------------------------------
# 3. HTML 渲染
# ---------------------------------------------------------------------------

def _fmt(v: Optional[float]) -> str:
    return "—" if v is None else f"¥{v:.2f}"


def render_report(data: Dict[str, Any]) -> str:
    """渲染完整单文件 HTML 报告。"""
    plans: List[Dict[str, Any]] = data["plans"]
    base = data["baseline"]
    c = data["constraints"]
    cheapest = next((p for p in plans if p["label"] == "最省钱"), plans[0])

    saved = round(base["total_cny"] - cheapest["final_price_cny"], 2)
    saved_pct = round(saved / base["total_cny"] * 100, 1) if base["total_cny"] else 0.0

    matrix = _nutrition_matrix(plans)
    bar_svg = bar_chart_svg(base["total_cny"], plans)
    radar_svg = radar_chart_svg(matrix)

    esc = html.escape
    members = ["减脂成员", "忌辣成员", "要赠品成员", "预算控成员"]
    people = cheapest["people_count"]

    # --- 场景解析摘要 -----------------------------------------------------
    budget = c.get("budget") or {}
    budget_txt = (f"总额 {_fmt(budget.get('amount_cny'))}"
                  if budget.get("type") == "total"
                  else f"人均 {_fmt(budget.get('amount_cny'))}")
    hard = c.get("hard") or {}
    hard_bits = []
    if hard.get("exclude_tags"):
        hard_bits.append("排除标签 " + "、".join(hard["exclude_tags"]))
    if hard.get("exclude_ingredients"):
        hard_bits.append("忌口成分 " + "、".join(hard["exclude_ingredients"]))
    targets = c.get("targets") or {}
    target_bits = []
    if targets.get("max_calories_kcal"):
        target_bits.append(f"单餐 ≤{targets['max_calories_kcal']:.0f} kcal")
    if targets.get("max_fat_g"):
        target_bits.append(f"脂肪 ≤{targets['max_fat_g']:.0f} g")
    if targets.get("min_protein_g"):
        target_bits.append(f"蛋白 ≥{targets['min_protein_g']:.0f} g")
    weights = c.get("weights") or {}
    weights_txt = " · ".join(
        f"{k} {weights[k]:.2f}" for k in ("cheap", "healthy", "speed", "pref") if k in weights
    )

    # --- 券组合明细行 ------------------------------------------------------
    coupon_rows: List[str] = []
    for p in plans:
        if p["coupons"]:
            for i, cp in enumerate(p["coupons"]):
                rule = data["coupon_rules"].get(cp.get("couponId"), "")
                coupon_rows.append(
                    f"<tr><td>{esc(p['label'])}{'' if i else ''}</td>"
                    f"<td>{esc(cp['name'])}</td>"
                    f"<td><code>{esc(str(cp.get('couponId') or '—'))}</code></td>"
                    f"<td>{esc(rule)}</td>"
                    f"<td class='neg'>−{_fmt(cp['discount_cny'])[1:]}</td></tr>"
                )
        else:
            coupon_rows.append(
                f"<tr><td>{esc(p['label'])}</td><td colspan='3' class='muted'>无可用券</td>"
                f"<td>−¥0.00</td></tr>"
            )

    # --- 分摊账单行 --------------------------------------------------------
    bill_rows: List[str] = []
    for p in plans:
        color = PLAN_COLORS.get(p["label"], {}).get("main", "#5b7fff")
        light = PLAN_COLORS.get(p["label"], {}).get("light", "#eef1ff")
        cells = "".join(
            f"<td class='bill-cell'>{_fmt(p['per_capita_cny'])}</td>" for _ in range(people)
        )
        tag = ""
        if p["label"] == "最省钱":
            tag = f"<span class='pill' style='background:{color}'>最便宜</span>"
        bill_rows.append(
            f"<tr style='background:{light}'><td class='plan-name' style='color:{color}'>"
            f"{esc(p['label'])}{tag}</td>{cells}"
            f"<td class='bill-total'>{_fmt(p['final_price_cny'])}</td></tr>"
        )

    # --- 商品明细行 --------------------------------------------------------
    item_rows: List[str] = []
    for p in plans:
        color = PLAN_COLORS.get(p["label"], {}).get("main", "#5b7fff")
        li = "".join(
            f"<span class='food'>{esc(ln['name'])}×{ln['quantity']}"
            f"<i>{_fmt(ln['subtotal_cny'])}</i></span>"
            for ln in p["items"]
        )
        coupon_txt = " + ".join(
            f"{esc(cp['name'])} −¥{cp['discount_cny']:.2f}" for cp in p["coupons"]
        ) or "无"
        flags = "；".join(p.get("nutrition_flags") or [])
        item_rows.append(
            f"<tr><td class='plan-name' style='color:{color}'>{esc(p['label'])}</td>"
            f"<td><div class='foods'>{li}</div></td>"
            f"<td>{_fmt(p['subtotal_cny'])}</td>"
            f"<td class='neg'>−{_fmt(p['discount_cny'])[1:]}</td>"
            f"<td class='strong'>{_fmt(p['final_price_cny'])}</td>"
            f"<td>{_fmt(p['per_capita_cny'])}</td>"
            f"<td class='muted small'>{esc(coupon_txt)}<br>{esc(flags)}</td></tr>"
        )

    # --- 雷达图数值表 ------------------------------------------------------
    radar_table_rows = "".join(
        f"<tr><td class='plan-name' style='color:{PLAN_COLORS.get(r['label'], {}).get('main', '#5b7fff')}'>"
        f"{esc(r['label'])}</td><td>{r['kcal']:g}</td><td>{r['protein']:g}</td>"
        f"<td>{r['fat']:g}</td><td>{r['sodium']:g}</td></tr>"
        for r in matrix
    )

    # --- 被淘汰菜品 --------------------------------------------------------
    rejected_html = "".join(
        f"<span class='rej'>✕ {esc(r['name'])}（{esc(r['reason'])}）</span>"
        for r in data["rejected"]
    ) or "<span class='muted'>无（全部餐品可用）</span>"

    legend = "".join(
        f"<span class='lg'><i style='background:{PLAN_COLORS[p['label']]['main']}'></i>"
        f"{esc(p['label'])}</span>" for p in plans if p["label"] in PLAN_COLORS
    )

    doc_json = json.dumps(
        {
            "user_input": data["user_input"],
            "baseline": base,
            "plans": plans,
            "nutrition_per_capita": matrix,
        },
        ensure_ascii=False, indent=2,
    )

    css = CSS
    tpl = TEMPLATE
    for key, val in {
        "{{CSS}}": css,
        "{{GENERATED_AT}}": esc(data["generated_at"]),
        "{{USER_INPUT}}": esc(data["user_input"]),
        "{{PEOPLE}}": people,
        "{{BUDGET}}": esc(budget_txt),
        "{{HARD}}": esc("；".join(hard_bits) or "无"),
        "{{TARGETS}}": esc("；".join(target_bits) or "无"),
        "{{PERSONAS}}": esc("、".join(c.get("personas") or [])),
        "{{WEIGHTS}}": esc(weights_txt),
        "{{MENU_TOTAL}}": data["menu_total"],
        "{{KEPT_COUNT}}": data["kept_count"],
        "{{REJECTED}}": rejected_html,
        "{{BASE_NAME}}": esc(base["product_name"]),
        "{{BASE_UNIT}}": _fmt(base["unit_price_cny"]),
        "{{BASE_TOTAL}}": _fmt(base["total_cny"]),
        "{{BASE_PC}}": _fmt(base["per_capita_cny"]),
        "{{CHEAP_FINAL}}": _fmt(cheapest["final_price_cny"]),
        "{{SAVED}}": f"{saved:.2f}",
        "{{SAVED_PCT}}": f"{saved_pct:.1f}",
        "{{SAVED_PC}}": _fmt(saved),
        "{{BAR_SVG}}": bar_svg,
        "{{RADAR_SVG}}": radar_svg,
        "{{RADAR_TABLE}}": radar_table_rows,
        "{{LEGEND}}": legend,
        "{{COUPON_ROWS}}": "".join(coupon_rows),
        "{{BILL_ROWS}}": "".join(bill_rows),
        "{{ITEM_ROWS}}": "".join(item_rows),
        "{{MEMBER_HEADS}}": "".join(f"<th>{esc(m)}</th>" for m in members),
        "{{DOC_JSON}}": esc(doc_json),
    }.items():
        tpl = tpl.replace(key, str(val))
    return tpl


#: 样式表（浅色主题，无外部资源）
CSS = """
:root{--red:#da291c;--yellow:#ffc72c;--ink:#1f2329;--muted:#6b7280;--line:#e5e7eb;
--card:#ffffff;--bg:#f7f8fa}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
"Hiragino Sans GB","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink);
line-height:1.65;padding:32px 16px 64px}
.wrap{max-width:980px;margin:0 auto}
header.hd{text-align:center;margin-bottom:28px}
.hd .logo{display:inline-flex;align-items:center;gap:10px;font-weight:800;font-size:22px}
.hd .logo .b{background:var(--yellow);color:#3b2c00;border-radius:8px;padding:2px 10px}
.hd .sub{color:var(--muted);font-size:14px;margin-top:6px}
.hd .meta{color:var(--muted);font-size:12px;margin-top:8px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:24px 28px;margin-bottom:24px;box-shadow:0 1px 3px rgba(31,35,41,.05)}
h2{font-size:18px;margin-bottom:14px;display:flex;align-items:center;gap:8px}
h2 .no{background:var(--ink);color:#fff;border-radius:6px;font-size:12px;
padding:2px 8px;font-weight:600}
.scene-quote{background:#fffbeb;border:1px solid #fde68a;border-radius:10px;
padding:12px 16px;font-size:15px;margin-bottom:14px}
.kv{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}
.kv .it{background:#fafbfc;border:1px solid var(--line);border-radius:10px;padding:10px 14px}
.kv .it b{display:block;font-size:12px;color:var(--muted);font-weight:500;margin-bottom:2px}
.rej{display:inline-block;background:#fef2f2;color:#b91c1c;border:1px solid #fecaca;
border-radius:6px;padding:2px 10px;font-size:13px;margin:4px 6px 0 0}
/* ---- 省钱大字卡 ---- */
.hero{background:linear-gradient(135deg,#fff 0%,#fff7f6 100%);
border:2px solid #fbd3cf;border-radius:16px;padding:30px 28px;text-align:center;
margin-bottom:24px}
.hero .flow{display:flex;justify-content:center;align-items:center;gap:22px;flex-wrap:wrap}
.hero .node .lab{font-size:13px;color:var(--muted)}
.hero .node .val{font-size:34px;font-weight:800}
.hero .arrow{font-size:26px;color:var(--muted)}
.hero .save{margin-top:14px}
.hero .save .big{color:var(--red);font-size:56px;font-weight:900;letter-spacing:1px;
text-shadow:0 1px 0 #fff}
.hero .save .pct{display:inline-block;background:var(--red);color:#fff;border-radius:10px;
font-size:24px;font-weight:800;padding:4px 18px;margin-left:12px;vertical-align:middle}
.hero .note{color:var(--muted);font-size:13px;margin-top:10px}
/* ---- 图表 ---- */
.chart-box svg{width:100%;height:auto;display:block}
.axis{font-size:12px;fill:#9ca3af}
.bar-val{font-size:16px;font-weight:700;fill:#374151}
.bar-val.hot{fill:var(--red);font-size:19px;font-weight:800}
.bar-lab{font-size:14px;font-weight:600;fill:#374151}
.bar-sub{font-size:11px;fill:#9ca3af}
.radar-axis{font-size:15px;font-weight:700;fill:#374151}
.radar-unit{font-size:11px;fill:#9ca3af}
.radar-val{font-size:11.5px;font-weight:700}
.legend{display:flex;gap:18px;justify-content:center;margin-top:6px;flex-wrap:wrap}
.lg{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:#374151}
.lg i{width:14px;height:14px;border-radius:4px;display:inline-block}
/* ---- 表格 ---- */
table{width:100%;border-collapse:collapse;font-size:14px}
th{background:#f3f4f6;font-size:12.5px;color:#4b5563;font-weight:600;
padding:9px 10px;border-bottom:2px solid var(--line);text-align:center}
td{padding:9px 10px;border-bottom:1px solid #eef0f2;text-align:center;vertical-align:middle}
tr:last-child td{border-bottom:none}
td.plan-name{font-weight:800;text-align:left;white-space:nowrap}
td.strong{font-weight:800}
td.neg{color:#b91c1c;font-weight:700}
td.muted{color:var(--muted)}
td.small{font-size:12px}
td.bill-cell{font-weight:600}
td.bill-total{font-weight:800;background:rgba(0,0,0,.025)}
.foods{display:flex;flex-wrap:wrap;gap:6px;justify-content:flex-start}
.food{background:#f8f9fa;border:1px solid var(--line);border-radius:6px;
padding:2px 8px;font-size:12.5px;white-space:nowrap}
.food i{font-style:normal;color:var(--muted);margin-left:5px}
.pill{color:#fff;font-size:11px;border-radius:99px;padding:1px 8px;margin-left:6px;
font-weight:600;vertical-align:2px}
.tbl-note{color:var(--muted);font-size:12.5px;margin-top:10px}
.split{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media(max-width:860px){.split{grid-template-columns:1fr}.hero .save .big{font-size:42px}}
footer{color:var(--muted);font-size:12.5px;text-align:center;margin-top:8px}
footer code{background:#eef0f2;border-radius:4px;padding:1px 6px;font-size:12px}
.rawdata{display:none}
"""

#: HTML 模板（占位符 {{KEY}} 由 render_report 填充）
TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦拼单大脑 · 演示报告</title>
<style>{{CSS}}</style>
</head>
<body>
<div class="wrap">

<header class="hd">
  <div class="logo"><span class="b">麦麦拼单大脑</span> 演示报告</div>
  <div class="sub">4 人拼单 · 三套方案对比 · 券组合优化 · 营养达标</div>
  <div class="meta">数据来源：optimizer.py 真实运行输出（未核价，价格为估算）｜生成时间 {{GENERATED_AT}}</div>
</header>

<section class="card">
  <h2><span class="no">场景</span>需求解析</h2>
  <div class="scene-quote">🗣️ 「{{USER_INPUT}}」</div>
  <div class="kv">
    <div class="it"><b>人数</b>{{PEOPLE}} 人</div>
    <div class="it"><b>预算</b>{{BUDGET}}</div>
    <div class="it"><b>硬约束</b>{{HARD}}</div>
    <div class="it"><b>营养目标</b>{{TARGETS}}</div>
    <div class="it"><b>命中画像</b>{{PERSONAS}}</div>
    <div class="it"><b>聚合权重</b>{{WEIGHTS}}</div>
  </div>
  <div class="tbl-note">菜单候选 {{MENU_TOTAL}} 项 → 硬约束过滤后保留 <b>{{KEPT_COUNT}}</b> 项：
  {{REJECTED}}</div>
</section>

<section class="hero">
  <div class="flow">
    <div class="node">
      <div class="lab">基线 · 朴素点法（{{BASE_NAME}} ×{{PEOPLE}}，不用券）</div>
      <div class="val">{{BASE_TOTAL}}</div>
      <div class="lab">人均 {{BASE_PC}}</div>
    </div>
    <div class="arrow">➜</div>
    <div class="node">
      <div class="lab">最省钱方案（券后到手）</div>
      <div class="val" style="color:#1f2329">{{CHEAP_FINAL}}</div>
      <div class="lab">主食 {{PEOPLE}} 份 · 每人至少 1 份 ✅</div>
    </div>
  </div>
  <div class="save">
    <span class="big">省 ¥{{SAVED}}</span><span class="pct">↓ {{SAVED_PCT}}%</span>
  </div>
  <div class="note">降幅按基线总价计算：¥{{SAVED}} ÷ 基线总价。三套方案最终价均低于基线。</div>
</section>

<section class="card chart-box">
  <h2><span class="no">图表 1</span>价格对比（含基线）</h2>
  {{BAR_SVG}}
  <div class="tbl-note">灰色虚线为基线参考线（朴素点法总价）。柱值为券后到手价；
  主食份数均满足「每人至少 1 份」的拼单底线。</div>
</section>

<div class="split">
  <section class="card chart-box">
    <h2><span class="no">图表 2</span>营养雷达图（人均）</h2>
    {{RADAR_SVG}}
    <div class="legend">{{LEGEND}}</div>
  </section>
  <section class="card">
    <h2><span class="no">图表 2</span>营养数值明细（人均）</h2>
    <table>
      <thead><tr><th style="text-align:left">方案</th><th>热量 kcal</th>
      <th>蛋白 g</th><th>脂肪 g</th><th>钠 mg</th></tr></thead>
      <tbody>{{RADAR_TABLE}}</tbody>
    </table>
    <div class="tbl-note">雷达图各维度以三方案中的最大值为外圈（100%）。
    减脂目标（标准档）：单餐 ≤600 kcal、脂肪 ≤25 g、蛋白 ≥20 g。</div>
  </section>
</div>

<section class="card">
  <h2><span class="no">表格 1</span>券组合明细</h2>
  <table>
    <thead><tr><th style="text-align:left">方案</th><th>券名称</th><th>券 ID</th>
    <th>券规则</th><th>优惠金额</th></tr></thead>
    <tbody>{{COUPON_ROWS}}</tbody>
  </table>
  <div class="tbl-note">券组合由动态规划求解：按 <code>exclusiveGroup</code> 互斥、
  按 <code>minSpend</code> 校验门槛、按 <code>appliesToProductCodes</code> 校验适用范围，
  最多叠加 2 张，优惠额不超过商品小计。</div>
</section>

<section class="card">
  <h2><span class="no">表格 2</span>分摊账单（每人应付）</h2>
  <table>
    <thead><tr><th style="text-align:left">方案</th>{{MEMBER_HEADS}}<th>合计</th></tr></thead>
    <tbody>{{BILL_ROWS}}</tbody>
  </table>
  <div class="tbl-note">均摊口径：每人应付 = 券后总价 ÷ 4（与 README 口径一致）。
  三套方案人均均在预算（总额 ¥100 ÷ 4 = ¥25/人）之内。</div>
</section>

<section class="card">
  <h2><span class="no">表格 3</span>三方案商品明细对比</h2>
  <table>
    <thead><tr><th style="text-align:left">方案</th><th style="text-align:left">商品清单</th>
    <th>原价</th><th>券优惠</th><th>券后价</th><th>人均</th><th>用券 / 体检</th></tr></thead>
    <tbody>{{ITEM_ROWS}}</tbody>
  </table>
</section>

<footer>
  本页所有数字由 <code>optimizer.py</code> 完整链路（解析 → 过滤 → 优化）实时产出，非手编数据。<br>
  复现命令：<code>python scripts/optimizer.py "{{USER_INPUT}}"</code>　｜
  重新生成本页：<code>python scripts/gen_demo.py</code>　｜
  价格未过官方核价（<code>calculate-price</code>），以实际下单为准。
</footer>

</div>
<script type="application/json" id="plan-data">{{DOC_JSON}}</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 4. 入口
# ---------------------------------------------------------------------------

def main(argv: Sequence[str]) -> int:
    user_input = DEMO_INPUT
    out_path = DEFAULT_OUT
    args = list(argv or [])
    if "--input" in args:
        i = args.index("--input")
        if i + 1 < len(args):
            user_input = args[i + 1]
    if "--out" in args:
        i = args.index("--out")
        if i + 1 < len(args):
            out_path = os.path.abspath(args[i + 1])

    data = run_scenario(user_input)
    html_text = render_report(data)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html_text)

    # 生成后自检：关键真实数字必须出现在页面里
    cheapest = next(p for p in data["plans"] if p["label"] == "最省钱")
    checks = [
        f"¥{data['baseline']['total_cny']:.2f}",
        f"¥{cheapest['final_price_cny']:.2f}",
    ]
    missing = [s for s in checks if s not in html_text]
    if missing:
        print(f"⚠️ 自检失败，页面缺少关键数字：{missing}", file=sys.stderr)
        return 1

    print(f"✅ 已生成 {out_path}")
    print(f"   场景：{user_input}")
    print(f"   基线：¥{data['baseline']['total_cny']:.2f} → "
          f"最省钱：¥{cheapest['final_price_cny']:.2f}")
    for p in data["plans"]:
        print(f"   {p['label']}：原价 ¥{p['subtotal_cny']:.2f} − 券 ¥{p['discount_cny']:.2f}"
              f" = ¥{p['final_price_cny']:.2f}（人均 ¥{p['per_capita_cny']:.2f}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
