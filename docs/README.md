# SkillSynapse 设计文档

夜间从多机 Claude Code 会话语料里蒸馏可复用知识。本目录是一组分层设计文档;
本页是**唯一入口**——全景、术语、阅读顺序、落地状态都在这里,子文档只讲自己那一件事。

## 全景:一份语料,多个读者

底座是同一份**汇聚来的 CC 会话 JSONL 语料**;上面挂多个互不干扰的「读者」,各读一种信号。

```
              ┌─────────────────── 数据平面(底座) ───────────────────┐
   各机 CC 会话 JSONL ──[02 传输/Syncthing/Tailscale]──▶ hub 着陆区
                                                          │
                                              [01 归档:只增不删]
                                                          ▼
                                                    cc-archive/(地基)
                                                          │
                          ┌───────────── episode 切片 + 抽取 pass ─────────────┐
              ┌───────────┴───────────┐          ┌───────┴───────┐    ┌────────┴────────┐
   [05 标记]──▶  04 三环进化           │          │ 06 WorkLog    │    │ 06 Notes         │
   当场盖章      归纳/定向/自动化       │          │ 情景记忆       │    │ 语义记忆         │
   (最高质量信号) → skill/command      │          │ 「做过什么」   │    │ 「学到/查过什么」│
              └───────────────────────┘          └───────────────┘    └─────────────────┘
                          共享:03 挡位 / 产物 git 发布 / 跨会话聚合底座
```

一句话分工:**05 喂信号 → 04 长 skill;06 从同一份语料另抽情景/语义记忆;01/02/03 是三者共用的底座。**
**07 是横挂在这些出口上的闸门**:候选打分排序 → 人 accept 才进生效路径(当前缺失——产物一律平权直接生效)。

## 文档地图(依赖自上而下,无环)

| # | 文档 | 讲什么 | 被谁依赖 |
|---|---|---|---|
| — | 本页 | 全景 / 术语 / 阅读顺序 / 落地状态 | — |
| 01 | [corpus-and-archive](01-corpus-and-archive.md) | 语料是什么、scanner→episode→extractor 底座、JSONL 升格为归档资产 | 04 / 05 / 06 |
| 02 | [transport-and-security](02-transport-and-security.md) | Tailscale/Syncthing 汇聚、**内外网隔离红线**、**同步通道矩阵**、hub 角色、部署 | 全部 |
| 03 | [shared-primitives](03-shared-primitives.md) | **挡位 0–4**、产物 git 发布/staging 模型、**跨会话聚合底座** | 04 / 05 / 06 |
| 04 | [skillsynapse-loops](04-skillsynapse-loops.md) | 三环:归纳 / 定向 / 自动化 | 05 / 06 |
| 05 | [marking-signal](05-marking-signal.md) | 现场盖章的第四信号,喂给三环 | — |
| 06 | [worklog-and-notes](06-worklog-and-notes.md) | 情景(WorkLog)/ 语义(Notes)记忆,与 SkillSynapse 平级 | — |
| 07 | [triage-and-ranking](07-triage-and-ranking.md) | **优先级维度**:候选打分排序 + `skill review` 人工分诊闸门 + 库存量上限 | — |

**读法**:先本页 → 底座 01/02/03 → 按兴趣读 04/05/06 → 07 讲的是它们出口上的闸门。
每份子文档开头只列它**新增**的东西,共享概念一律指回 03,不重述。

## 术语表(跨文档共用,定义只此一处)

| 术语 | 含义 | 详见 |
|---|---|---|
| **语料 / corpus** | 各机 `~/.claude/projects/**/*.jsonl`,明文落盘的会话事件流 | 01 |
| **episode** | `episode_detector` 从一个会话里切出的一段连贯工作 | 01 |
| **抽取 pass** | 每个 episode 过一次 LLM,同时产出 skill 候选 / 工作事件 / 笔记(三出口分流) | 06 §2 |
| **hub** | 汇聚 + 归档 + 蒸馏 + 分发的中心机(mini 到货前 = 临时 hub) | 02 |
| **mesh** | Tailscale WireGuard 内网;安全边界 = mesh 边界 | 02 |
| **挡位 / intensity** | 0关/1观测/2建议/3草稿/4生效,一条环自主走多远的离散档 | 03 |
| **staging `_pending/`** | 挡3 草稿落盘处,人 promote 才生效 | 03 |
| **mark / 标记** | 会话当场对某段打的 ground-truth 标签(learn/pitfall/toil) | 05 |
| **provenance** | 标记来源:human(权重≈1)/ agent(权重<1) | 05 |
| **aggregator** | 跨会话模式聚类→排序→人确认→进闭环的共用底座 | 03 |
| **priority_score** | 候选的重要性分数 `repeat × cost × novelty × mark × recency`,只排序不自动生效 | 07 |
| **分诊 / triage** | `skill review`:按 priority 排好序给人 accept/reject/defer,accept 才进生效路径 | 07 |
| **workstream / 台账** | 横跨多会话/多机/多周的一条「工作线」增量记录 | 06 §2.2 |

## 安全红线(一句话,详见 02)

**原始 JSONL 是机密,绝不出 Tailscale mesh。** 唯一对外通道是过 `sanitizer.scrub()` 脱敏后的
session brief 与 skill 文件。完整红线见 [02 §安全](02-transport-and-security.md#安全内外网隔离) 与仓库根 `CLAUDE.md`。
内网拓扑(机器名↔tailnet IP)只存 gitignored 的 `LOCAL-TOPOLOGY.md`,公开文件一律占位符。

## 落地状态(高层,细节各文档自带)

| 模块 | 状态 |
|---|---|
| 传输层 Syncthing↔Tailscale(源机↔临时 hub) | ✅ 实测打通(2026-07-03,~4.9G 同步过) |
| scanner 多 root(`aggregation_root`) | ✅ 已实现 |
| extractor 脱敏 `scrub()` 接入 | ✅ 已实现(session brief + skill 落盘双 scrub) |
| headless 历史隔离(`CLAUDE_CONFIG_DIR`) | ✅ 已实现 |
| 三环:归纳环 | ✅ v0.1;定向/自动化环 📐 设计中(04) |
| 标记信号 | 📐 设计中(05) |
| WorkLog / Notes | 📐 设计中(06);归档 bug 待修(01 §归档) |
| 排序 / 人工分诊 / prune | ❌ 未实现(07):候选一律平权直接生效,库只进不出 |

> 讨论稿性质,非最终规格。各文档保留原始的「本轮定调」决定与落地顺序。
