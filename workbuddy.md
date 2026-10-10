# WorkBuddy 开发上下文（workbuddy.md）

> 本文件用于申报麦当劳程序员创意开发大赛 **WorkBuddy 联动专项奖励**。
> 记录本项目使用腾讯 WorkBuddy 智能体开发的真实过程。

## 开发环境声明

本项目从 MCP 接入调研、架构设计、核心算法实现到实测联调，全程在 **WorkBuddy**（腾讯官方 AI 智能体，本次活动官方合作伙伴）中完成：

- WorkBuddy 自定义连接器接入麦当劳官方 MCP（`https://mcp.mcd.cn`，streamablehttp + Bearer Token）；
- 通过 WorkBuddy 会话驱动 35 个 MCP 工具的全链路实测，发现并记录 16 项 schema 实测偏差（详见 `MCP_INTEGRATION.md`）；
- 核心引擎 `scripts/optimizer.py`（约束解析 / 动态规划 / 帕累托剪枝 / NSGA-II / 官方核价）、测试用例（89 个 pytest 用例）、CI 配置与全部文档均在 WorkBuddy 会话中生成与迭代；
- 使用 WorkBuddy 执行本地命令完成自测（`--selftest` 断言全绿、pytest 回归全绿）与 git 版本管理（提交 / 推送 / 远端核验）。

## 完整开发时间线（均在 WorkBuddy 会话中完成，commit 可在仓库历史核验）

| # | 时间 | 阶段 | 代表性指令（用户 → WorkBuddy） | 产出（commit） |
|---|---|---|---|---|
| 1 | 10-09 | MCP 接入与全量踩坑 | "把 mcd-mcp 接进来，把 35 个工具全部实测一遍，记录返回结构和文档不符的地方" | `MCP_INTEGRATION.md`：8 条脱敏调用日志 + 实测 schema 偏差（累计 16 项） |
| 2 | 10-09 | 核心算法 | "多人约束互相冲突，先用硬约束过滤，再用 DP 求最省钱、帕累托剪枝做多目标" | `optimizer.py`：无界背包 DP + 券组合 DP + 帕累托剪枝 |
| 3 | 10-10 早 | 得来速 + 渠道比价 | "得来速 beType 到底是几？渠道之间到手成本怎么比？" | beType=5 端到端实测（7b65148）；三渠道到手成本恒等式校验 |
| 4 | 10-10 早 | Skill 架构分层 | "SKILL.md 太长，按 Agent Skills 规范拆分层" | SKILL.md 340→168 行 + `references/` 渐进披露 + JSON Schema 输出契约（179d8c4） |
| 5 | 10-10 | 专业加固四轮 | "模糊输入怎么处理 / 一半要辣一半不要辣 / MCP 超时重试幂等 / 回归测试" | 模糊输入三层询问（6f21ec1）、群体公平性 + 木桶效应（f63bf76）、健壮性 降级重试幂等日志脱敏（e534a69）、25 用例 pytest + GitHub Actions 双版本 CI（29535ea）；期间捕获「4个人」人数解析 bug |
| 6 | 10-10 | 演示可视化 | "生成一个零依赖的可视化报告页" | `docs/demo.html`：真实优化链路输出 → 纯 HTML+SVG 单页（1123958） |
| 7 | 10-10 | 真实核价 | "用 calculate-price 对最省钱方案真实核价，对比本地估算偏差" | `docs/pricing-verification.md`：¥44.00 = ¥44.00，偏差 0%（b555de4） |
| 8 | 10-10 | 可移植性 | "按 Agent Skills 开放规范做符合性检查，写清各框架怎么加载" | `validate_skill.py` 8/8 项通过 + `docs/portability.md`（f55a423） |
| 9 | 10-10 | 算法精度升级 | "支持每人预算上限 / 券可用性 / 极端预算降级" | 双预算硬约束、券可用性分级（绝不编造概率）、减配方案（9c650ed），pytest 25→43 用例 |
| 10 | 10-10 | 全链路真实化 | "任意输入默认走实时 MCP 数据，失败就如实报错，绝不偷偷用假数据" | `acquire_planning_data()` 实时链路 + traceId 自证 + 兜底数据强制标注横幅 + `--strict` 绝不降级（6f37297）；实测 3 随机场景核价偏差均 0.00%，早餐 96 品→正餐 129 品实时切换铁证 |
| 11 | 10-10 | 范围扩展两批 | "积分商城 / 预约取餐 / 活动抽奖 / 热量预算配餐 / 评价领券，逐个过 schema 实测" | 五个扩展分支全部 MCP schema 核实（f5ee5bc） |
| 12 | 10-10 | 算法升级 NSGA-II | "加权评分换成真正的多目标演化算法" | NSGA-II 四目标帕累托演化 + ReflectCache 参数级缓存，pytest 43→66 用例（5ac7e30） |
| 13 | 10-10 | 参赛合规 | "逐字核对官方仓库规则，把提交材料对齐" | CONTEST_DECLARATION.md 对齐官方原版、workbuddy.md、README 参赛横幅（d1b87e8） |
| 14 | 10-10 | 全面审计与修复 | "全面检查这个 skill 有没有 bug，有没有问题，然后有没有能改进的地方，保证都是真实的数据，能正常的使用" | 双代理逐行审计（代码 + 文档/数据）+ 实时 MCP 复验裁决：修复 2 HIGH + 6 MED + 8 LOW 代码缺陷（clarify 预算合并、名称归一化、口味否定式、降级方案契约、GBK 防崩、演示页营养二次除人数等），新增 7 个审计回归用例（pytest 66→83 全绿），勘误 takeWayList（take-in-store→locker-in，实调 traceId cb4abf9e…）与 query-nearby-stores beType=6 失实记录，核价改同篮对比口径（0.00% 可复现），全部文档数字对齐引擎真实输出（248d58a） |
| 15 | 10-10 | Skill 标准化与分发 | "按官方规范把 skill 校验、标准化，打一个规范分发包" | 官方 quick_validate.py 通过 + 自带 validate_skill.py 8/8；frontmatter 补 `agent_created` 标志；缓存清理（回收站可恢复）；`dist/mcd-meal-brain.zip` 规范分发包（staging 纯净副本 17 文件，排除 .git/logs 等）（ce2b00e） |
| 16 | 10-10 | 四维优化：效率/准确度/排版/token | "优化效率、提高准确度、方案合适程度、结果排版、尽量省 token" | 准确度：「最健康」尊重显式脂肪目标（脂肪 30.5g>25g 曾顶着最健康标签，修复后压线达标）；效率：菜单/营养/门店券并行拉取（3 RTT→2 RTT）+ NSGA-II 前沿稳定早停 + 核价默认只核 Top-1；排版：CJK 显示宽度对齐、金额右对齐、多券合并、约束 45 行→1 行摘要、方案对比总表+💡推荐；token：SKILL.md 304→198 行（技能初始上下文约省 40%），细节反向增补 workflow.md（bb8ce1d） |
| 17 | 10-10 | 方案席位自适应 + v1.1.0 版本化 | "为什么一定是三个方案呢？有好的就全部拿出来，没有好的给一个两个也行啊" | 固定 Top-3 改为席位自适应（1–5 套）：删两处凑数逻辑、`_plans_distinct` 明显差异判据（价差 ≥max(¥1,5%) / 热量差 ≥75kcal / 蛋白·脂肪差 ≥6g）、均衡补席 🍱 与「最健康」同脂肪口径、极值撞篮并列顶替、`top_n` 参数化；实证标准场景 3→5 套（方案 E ¥80.05 = 全场最便宜的营养达标方案）；修热量预算单组合重复、gen_demo 同标签折叠两 bug；pytest 83→89；schema `plans` 上限 3→5（向后兼容）；demo.html 重生成五方案版；新增 CHANGELOG.md 并打 **v1.1.0** 标签（ea1d431 / c8903d9） |

## WorkBuddy 会话中的典型工作方式

1. **真实 MCP 实测优先**：所有功能先在 WorkBuddy 中通过 mcd-mcp 连接器真实调用（traceId 可查），再写代码，绝不编造接口行为；
2. **测试驱动**：每轮加固都同步扩 pytest 用例（25 → 43 → 66 → 83 → 89 全绿），并在 WorkBuddy 中实际运行验证；
3. **git 全程托管**：WorkBuddy 会话内完成 commit / push / 远端 HEAD 核验，17 个功能 commit 串联完整开发史（含 v1.1.0 标签）；
4. **合规内建**：日志脱敏（Token/手机号/地址）、`.gitignore` 排除敏感文件、配置占位符等均由 WorkBuddy 在开发中直接落实。

> 以上时间线与仓库 git 历史（`git log --oneline`）一一对应，可交叉核验。
