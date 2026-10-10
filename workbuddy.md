# WorkBuddy 开发上下文（workbuddy.md）

> 本文件用于申报麦当劳程序员创意开发大赛 **WorkBuddy 联动专项奖励**。
> 记录本项目使用腾讯 WorkBuddy 智能体开发的真实过程。

## 开发环境声明

本项目从 MCP 接入调研、架构设计、核心算法实现到实测联调，全程在 **WorkBuddy**（腾讯官方 AI 智能体，本次活动官方合作伙伴）中完成：

- WorkBuddy 自定义连接器接入麦当劳官方 MCP（`https://mcp.mcd.cn`，streamablehttp + Bearer Token）；
- 通过 WorkBuddy 会话驱动 35 个 MCP 工具的全链路实测，发现并记录 15+ 项 schema 实测偏差（详见 `MCP_INTEGRATION.md`）；
- 核心引擎 `scripts/optimizer.py`（约束解析 / 动态规划 / 帕累托剪枝 / 官方核价）、测试用例、CI 配置与全部文档均在 WorkBuddy 会话中生成与迭代；
- 使用 WorkBuddy 执行本地命令完成自测（`--selftest` 断言全绿、pytest 回归全绿）与 git 版本管理。

## 开发阶段与代表性对话记录

| 阶段 | 时间 | 代表性指令（用户 → WorkBuddy） | 产出 |
|---|---|---|---|
| MCP 接入与踩坑 | 2026-10-09 | "把 mcd-mcp 接进来，把 35 个工具全部实测一遍，记录返回结构和文档不符的地方" | `MCP_INTEGRATION.md`：8 条脱敏调用日志 + 15 项实测 schema 偏差 |
| 核心算法 | 2026-10-09 | "多人约束互相冲突，先用硬约束过滤，再用 DP 求最省钱、帕累托剪枝做多目标" | `optimizer.py`：无界背包 DP + 券组合 DP + 帕累托剪枝 |
| 真实核价 | 2026-10-10 | "用 calculate-price 对最省钱方案真实核价，对比本地估算偏差" | `docs/pricing-verification.md`：¥44.00 = ¥44.00，偏差 0% |
| 全链路真实化 | 2026-10-10 | "任意输入默认走实时 MCP 数据，失败就如实报错，绝不偷偷用假数据" | `acquire_planning_data()` 实时链路 + traceId 自证 + 兜底数据强制标注 |
| 群体公平性 | 2026-10-10 | "一半人要辣一半不要辣怎么办？给出冲突检测和妥协方案" | `aggregate_preferences()` + A/B 双备选 + 木桶效应公平性评分 |

<!-- TODO(提交前必做)：请从 WorkBuddy 客户端导出完整对话记录（会话设置 → 导出对话），
     将导出的 Markdown/文本全文粘贴到下方，替换本注释。 -->
