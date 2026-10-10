#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent Skills 规范符合性检查器（方向3 · Skill 可移植性验证）。

依据 Agent Skills 开放规范（agentskills.io / Anthropic skills spec）逐项检查：

1. Frontmatter
   - 必须存在 YAML frontmatter（``---`` 包裹）
   - 必填字段 ``name``：小写字母/数字/连字符，1–64 字符，建议与目录名一致
   - 必填字段 ``description``：非空，≤1024 字符，说明 what + when
2. 渐进式披露（progressive disclosure）
   - SKILL.md 本体 ≤500 行（规范建议，元数据→正文→引用文件三层加载）
   - 正文引用的 ``references/`` 文件必须真实存在
3. 零依赖
   - scripts/optimizer.py 的 import 全部来自 Python 标准库
4. 安全
   - SKILL.md / references / 配置示例中不得出现疑似 Token、手机号等敏感信息

用法::

    python scripts/validate_skill.py            # 检查当前目录的 Skill
    python scripts/validate_skill.py --json     # 机器可读输出
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Python < 3.10 回退集合（sys.stdlib_module_names 自 3.10 起提供；
#: 覆盖 optimizer.py 实际 import + 常用标准库，避免 3.9- 环境 AttributeError）
_STDLIB_FALLBACK = frozenset({
    "__future__", "argparse", "base64", "collections", "copy", "csv", "datetime",
    "decimal", "enum", "functools", "glob", "hashlib", "html", "io", "itertools",
    "json", "logging", "math", "os", "pathlib", "platform", "random", "re",
    "shutil", "socket", "sqlite3", "ssl", "statistics", "string", "struct",
    "subprocess", "sys", "textwrap", "threading", "time", "typing", "unittest",
    "urllib", "uuid",
})

#: 检查的文件范围（secrets 扫描）
SECRET_SCAN_FILES = [
    "SKILL.md",
    "references/workflow.md",
    "references/tools.md",
    "references/strategy.md",
    "mcp-config.example.json",
    "README.md",
]

#: 疑似敏感信息模式（Token 前缀 / Bearer 凭据 / 大陆手机号）
SECRET_PATTERNS = [
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "疑似 API Token（sk-…）"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}"), "疑似 Bearer 凭据"),
    (re.compile(r"\b1[3-9]\d{9}\b"), "疑似手机号"),
]


def check(repo_root: str) -> Dict[str, Any]:
    issues: List[Dict[str, str]] = []
    ok_items: List[str] = []

    skill_path = os.path.join(repo_root, "SKILL.md")
    if not os.path.exists(skill_path):
        issues.append({"item": "frontmatter", "detail": f"SKILL.md 不存在：{skill_path}"})
        return {"ok": False, "name": None, "description": None,
                "passed": ok_items, "issues": issues}
    text = open(skill_path, encoding="utf-8").read()

    # ---- 1. Frontmatter ---------------------------------------------------
    # 尾部换行可选：文件末尾恰好以 --- 结束且无换行时也能识别
    m = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    name: Optional[str] = None
    desc: Optional[str] = None
    if not m:
        issues.append({"item": "frontmatter", "detail": "缺少 YAML frontmatter（--- 包裹）"})
    else:
        fm = m.group(1)
        name_m = re.search(r"^name:\s*(\S+)\s*$", fm, re.M)
        # description 允许长文本（含中文标点）：优先单行取值；
        # 其次支持 YAML 块标量（>/| 缩进续行，按空格折叠拼接）
        desc_m = re.search(r"^description:\s*(.+?)\s*$", fm, re.M)
        if desc_m:
            desc_text: Optional[str] = desc_m.group(1)
        else:
            bm = re.search(r"^description:\s*[>|][+-]?\s*\r?\n((?:[ \t]+\S.*(?:\r?\n|$))+)",
                          fm, re.M)
            desc_text = (" ".join(ln.strip() for ln in bm.group(1).splitlines())
                         if bm else None)
        if not name_m:
            issues.append({"item": "name", "detail": "缺少必填字段 name"})
        else:
            name = name_m.group(1)
            if len(name) > 64 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                issues.append({"item": "name", "detail": f"{name!r} 不符合命名规则"
                            "（小写字母/数字/连字符，≤64 字符）"})
            else:
                ok_items.append(f"name 合规：{name}")
        if desc_text is None:
            issues.append({"item": "description", "detail": "缺少必填字段 description"})
        else:
            desc = desc_text
            if len(desc) > 1024:
                issues.append({"item": "description", "detail": f"超长（{len(desc)} > 1024 字符）"})
            elif len(desc) < 10:
                issues.append({"item": "description", "detail": "过短（<10 字符）"})
            else:
                ok_items.append(f"description 合规：{len(desc)} 字符（≤1024）")
        dir_name = os.path.basename(os.path.normpath(repo_root))
        if name and name != dir_name:
            issues.append({"item": "name", "detail": f"name({name}) 与目录名({dir_name}) 不一致"})
        elif name:
            ok_items.append(f"name 与目录名一致：{name}")

    # ---- 2. 渐进式披露 ----------------------------------------------------
    lines = text.splitlines()
    if len(lines) > 500:
        issues.append({"item": "progressive-disclosure",
                       "detail": f"SKILL.md {len(lines)} 行，超过 500 行建议上限"})
    else:
        ok_items.append(f"SKILL.md {len(lines)} 行（≤500，符合渐进式披露建议）")

    refs = set(re.findall(r"references/[\w.-]+", text))
    if not refs:
        issues.append({"item": "progressive-disclosure", "detail": "未引用任何 references/ 文件"})
    else:
        for r in sorted(refs):
            if not os.path.exists(os.path.join(repo_root, r)):
                issues.append({"item": "progressive-disclosure", "detail": f"引用文件不存在：{r}"})
        if not any("引用文件不存在" in i["detail"] for i in issues):
            ok_items.append(f"references/ 引用完整：{len(refs)} 个文件全部存在")
    if os.path.isdir(os.path.join(repo_root, "references")):
        n_refs = len([f for f in os.listdir(os.path.join(repo_root, "references"))])
        ok_items.append(f"references/ 目录存在，含 {n_refs} 个文件（按需加载，不进初始上下文）")

    # ---- 3. 零依赖 --------------------------------------------------------
    opt_path = os.path.join(repo_root, "scripts", "optimizer.py")
    if not os.path.exists(opt_path):
        issues.append({"item": "zero-dep", "detail": "scripts/optimizer.py 不存在"})
    else:
        src = open(opt_path, encoding="utf-8").read()
        imports = set(re.findall(r"^(?:import|from)\s+([\w.]+)", src, re.M))
        stdlib = set(getattr(sys, "stdlib_module_names", None) or _STDLIB_FALLBACK)
        third = sorted(i for i in imports
                       if i.split(".")[0] not in stdlib and i != "__future__")
        if third:
            issues.append({"item": "zero-dep", "detail": f"第三方依赖：{third}"})
        else:
            ok_items.append("optimizer.py 纯标准库零依赖：" + ", ".join(sorted(imports)))

    # ---- 4. Secrets -------------------------------------------------------
    for f in SECRET_SCAN_FILES:
        path = os.path.join(repo_root, f)
        if not os.path.exists(path):
            continue
        content = open(path, encoding="utf-8").read()
        for pat, label in SECRET_PATTERNS:
            hits = pat.findall(content)
            if hits:
                issues.append({"item": "secrets", "detail": f"{f}：{label} ×{len(hits)}"})
    if not any(i["item"] == "secrets" for i in issues):
        ok_items.append(f"敏感信息扫描通过（{len(SECRET_SCAN_FILES)} 个文件，无 Token/手机号）")

    return {"ok": not issues, "name": name, "description": desc,
            "passed": ok_items, "issues": issues}


def _safe_stdio() -> None:
    """GBK 控制台/管道下 emoji 输出防崩（同 optimizer._safe_stdio）。"""
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(errors="replace")
        except AttributeError:
            pass


def main() -> int:
    _safe_stdio()
    result = check(REPO_ROOT)
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Agent Skills 规范符合性检查")
        print("=" * 60)
        for p in result["passed"]:
            print(f"  ✅ {p}")
        for i in result["issues"]:
            print(f"  ❌ [{i['item']}] {i['detail']}")
        print("=" * 60)
        print("结论：" + ("全部通过 ✅" if result["ok"] else f"{len(result['issues'])} 项不符合，需修复"))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
