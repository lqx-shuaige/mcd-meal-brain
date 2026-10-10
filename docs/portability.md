# 可移植性说明（portability.md）

> **结论先行**：本 Skill（SKILL.md + references/ + scripts/）**符合 Agent Skills 开放规范**，
> frontmatter、渐进式披露、零依赖三项均通过自动化检查（`scripts/validate_skill.py`，8/8 通过）。
> 加载方式不绑定任何单一框架：Claude Code、Cursor、Cline、WorkBuddy 等支持该规范 /
> MCP 的框架均可加载。
>
> **验证状态（如实声明）**：本项目开发与实测环境为 **WorkBuddy**（含 35 个 MCP 工具的真实
> 调用，见 `MCP_INTEGRATION.md`）；本机未安装 Claude Code / Cursor / Cline，
> **未做跨框架加载实测**。下述各框架加载方式均引自其**官方文档**（检索于 2026-10-10），
> 未实测的框架已明确标注。

---

## 1. 规范符合性检查（自动化，可复现）

由 `scripts/validate_skill.py` 检查（纯标准库，无依赖）：

```bash
python scripts/validate_skill.py        # 人读输出
python scripts/validate_skill.py --json # 机器可读
```

2026-10-10 检查结果（8/8 通过）：

| # | 检查项 | 结果 | 说明 |
|---|---|---|---|
| 1 | frontmatter `name` | ✅ | `mcd-meal-brain`，小写字母+连字符，≤64 字符，与目录名一致 |
| 2 | frontmatter `description` | ✅ | 77 字符（≤1024），说明 what + when |
| 3 | SKILL.md 篇幅 | ✅ | 303 行（≤500 行建议上限），元数据→正文→引用文件三层渐进加载 |
| 4 | `references/` 引用完整性 | ✅ | 正文引用的 4 个文件（workflow / schema / tools / strategy）全部存在 |
| 5 | 渐进式披露结构 | ✅ | `references/` 目录存在，按需加载、不进初始上下文 |
| 6 | `optimizer.py` 零依赖 | ✅ | 仅标准库：hashlib / json / os / re / sys / time / typing / urllib |
| 7 | 敏感信息扫描 | ✅ | 6 个文件无 Token / 手机号 / Bearer 凭据 |
| 8 | 目录结构 | ✅ | `SKILL.md` + `references/` + `scripts/`，符合 Agent Skills 布局约定 |

**Agent Skills 规范要点对照**：

| 规范要求 | 本项目 |
|---|---|
| Skill = 目录 + `SKILL.md`，YAML frontmatter 必填 `name` / `description` | ✅ 满足 |
| `name`：小写字母/数字/连字符，≤64 字符 | ✅ `mcd-meal-brain` |
| `description`：≤1024 字符，说明功能与触发时机 | ✅ 77 字符 |
| 渐进式披露：元数据（name+description）→ SKILL.md 正文 → 链接文件 | ✅ 三层齐全 |
| 支撑文件放 `references/`（文档）、`scripts/`（可执行） | ✅ 4 个引用文档 + 3 个脚本 |
| 脚本可直接运行、无第三方依赖 | ✅ 纯标准库（Python 3.10+） |

---

## 2. 在各框架中加载（依据官方文档，检索于 2026-10-10）

### 2.1 Claude Code —— 官方声明遵循 Agent Skills 开放标准

Claude Code 文档原文："Claude Code skills follow the Agent Skills open standard, which
works across multiple AI tools."

```bash
# 项目级（随仓库共享，推荐放进本仓库即可被识别）
git clone https://github.com/lqx-shuaige/mcd-meal-brain.git
# Claude Code 会在项目会话中自动发现 .claude/skills/ 下的技能；
# 将本仓库目录复制/链接为项目内的技能目录：
mkdir -p .claude/skills
cp -r mcd-meal-brain .claude/skills/mcd-meal-brain

# 个人级（本机所有项目可用；Windows 路径为 C:\Users\<用户名>\.claude\skills\）
cp -r mcd-meal-brain ~/.claude/skills/mcd-meal-brain
```

- 加载路径：项目 `.claude/skills/<skill-name>/SKILL.md`；个人 `~/.claude/skills/<skill-name>/SKILL.md`
- 调用：对话内容匹配 `description` 时自动加载，或 `/mcd-meal-brain` 直接调用
- 来源：code.claude.com/docs/en/skills

### 2.2 Cursor —— 原生支持，且兼容读取 Claude 目录

Cursor 会自动扫描以下目录（含 monorepo 嵌套目录）；**并兼容加载 `.claude/skills/`、
`.codex/skills/`**，因此按上文放好的技能在 Cursor 中同样生效：

- 项目级：`.cursor/skills/` 或 `.agents/skills/`
- 个人级（本机全局）：`~/.cursor/skills/` 或 `~/.agents/skills/`

```bash
# 方式一：使用 Cursor 原生目录
mkdir -p .cursor/skills && cp -r mcd-meal-brain .cursor/skills/mcd-meal-brain

# 方式二：直接复用 Claude 目录（Cursor 会兼容读取，无需重复放置）
# （已放在 .claude/skills/mcd-meal-brain 即可）
```

- 调用：`/mcd-meal-brain` 执行，或 `@mcd-meal-brain` 附加为上下文；也可由 Agent 按描述自动匹配
- 技能名取自包含 `SKILL.md` 的**文件夹名**
- 来源：cursor.com/help/customization/skills

### 2.3 Cline（VS Code 扩展，3.48.0+）

Cline 自 3.48.0 起「adds Skills compatibility」，结构与本规范一致
（目录 + SKILL.md + frontmatter name/description，按需渐进加载）：

```bash
# 项目级（工作区内）
mkdir -p .cline/skills && cp -r mcd-meal-brain .cline/skills/mcd-meal-brain

# 个人级（Windows：C:\Users\<用户名>\.cline\skills\）
cp -r mcd-meal-brain ~/.cline/skills/mcd-meal-brain
```

- 首次使用需开启：Settings → Features → **Enable Skills**
- 会话开始时 Cline 只读取 name + description，请求匹配时才加载全文（渐进式披露）
- 同名时全局技能优先于项目技能
- 来源：cline.bot/blog/cline-3-48-0-skills-and-websearch-make-cline-smarter

### 2.4 WorkBuddy（本项目开发实测环境 ✅ 唯一实测过的框架）

```bash
# 项目级（当前工作区生效）
mkdir -p .workbuddy/skills && cp -r mcd-meal-brain .workbuddy/skills/mcd-meal-brain

# 用户级（跨项目可用）
cp -r mcd-meal-brain ~/.workbuddy/skills/mcd-meal-brain
```

- MCP 连接配置：`~/.workbuddy/mcp.json`（见下节）
- 本 Skill 的全部功能（含 35 个 MCP 工具真实调用、核价、幂等下单）已在 WorkBuddy 环境实测

### 2.5 通用兜底：任意支持 MCP / 自定义指令的框架

任何不识别 Agent Skills 目录约定的框架，仍可按以下方式使用本项目：

1. **提示词注入**：把 `SKILL.md` 正文（含 frontmatter）粘贴进系统提示 / 自定义指令 /
   `.clinerules`（Cline 规则文件）等入口；
2. **MCP 工具直连**：配置 `mcd-mcp` 服务（见下节）后，按 `references/tools.md` 的参数
   说明直接调用 35 个工具；
3. **脚本独立运行**：`python scripts/optimizer.py` 完全离线可用（不依赖任何框架）。

---

## 3. MCP 连接配置

### 3.1 服务信息

| 项 | 值 |
|---|---|
| 服务名 | `mcd-mcp` |
| URL | `https://mcp.mcd.cn/mcp-servers/mcd-mcp` |
| 传输方式 | HTTP（Streamable） |
| 鉴权 | `Authorization: Bearer <Token>` |
| Token 获取 | https://open.mcd.cn/mcp |

### 3.2 标准配置块（与 `mcp-config.example.json` 一致）

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "url": "https://mcp.mcd.cn/mcp-servers/mcd-mcp",
      "transport": "http",
      "headers": {
        "Authorization": "Bearer ${MCD_MCP_TOKEN}"
      }
    }
  }
}
```

各框架的 MCP 配置入口（JSON 结构同上，文件名以各框架最新文档为准）：

| 框架 | MCP 配置入口 |
|---|---|
| Claude Code | 项目 `.mcp.json` 或 `~/.claude.json` |
| Cursor | `.cursor/mcp.json`（项目）或 `~/.cursor/mcp.json`（全局） |
| Cline | `cline_mcp_settings.json`（VS Code 扩展设置入口） |
| WorkBuddy | `~/.workbuddy/mcp.json` |

### 3.3 环境变量 `MCD_MCP_TOKEN` 的设置

Token 一律通过环境变量注入，**不写进任何入库文件**（示例配置用 `${MCD_MCP_TOKEN}` 占位）：

```bash
# Linux / macOS
export MCD_MCP_TOKEN="你的Token"

# Windows PowerShell（当前会话）
$env:MCD_MCP_TOKEN = "你的Token"

# Windows PowerShell（永久，写入用户环境变量）
[Environment]::SetEnvironmentVariable("MCD_MCP_TOKEN", "你的Token", "User")

# Windows Git Bash
export MCD_MCP_TOKEN="你的Token"
```

> 安全提醒：Token 等同于账户凭证，切勿提交到仓库或粘贴到公开场合；
> 本仓库已通过敏感信息扫描（`validate_skill.py` 第 7 项）。

---

## 4. 运行环境要求

| 组件 | 要求 | 说明 |
|---|---|---|
| Python | 3.10+ | `optimizer.py` 纯标准库（hashlib/json/os/re/sys/time/typing/urllib），无需 pip install |
| MCP 连接 | 可选 | 无 Token 时全部离线功能（优化、自检、契约校验）照常可用；仅核价/下单等在线功能不可用 |
| 操作系统 | 无限制 | Windows / macOS / Linux 均可（脚本不依赖平台特性） |

---

## 5. 验证状态一览（如实声明）

| 事项 | 状态 | 依据 |
|---|---|---|
| Agent Skills 规范符合性（8 项） | ✅ 已验证 | `scripts/validate_skill.py` 自动检查，2026-10-10 全部通过 |
| WorkBuddy 加载 + MCP 全链路 | ✅ 已实测 | 35 个工具真实调用记录见 `MCP_INTEGRATION.md`；核价实测见 `docs/pricing-verification.md` |
| 离线脚本独立运行 | ✅ 已实测 | `--selftest` 17 组断言 + pytest 83 用例全绿（CI 双版本） |
| Claude Code / Cursor / Cline 加载 | ⚠️ 未实测 | 本机未安装上述框架；加载方式依据其官方文档（检索于 2026-10-10，来源见各小节） |
