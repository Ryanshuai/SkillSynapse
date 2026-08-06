# 07 · 分诊与排序:给候选排个序,人只做「重要的」

> 横切文档,作用在 [04 三环](04-skillsynapse-loops.md) 与 [05 标记](05-marking-signal.md) 的**出口**上。
> 挡位 / staging / aggregator 见 [03](03-shared-primitives.md),本文只讲**优先级**这个新维度。
>
> **本轮定调**:① 排序落在**产物侧**(抽完再排),不在候选侧——额度充裕,不为省 LLM 在上游设闸,
> 且人看完整草稿比看原始 episode 元数据判得准;② 归纳环从挡4 降到挡3,`skill review accept` 才生效;
> ③ **分数全自动算,人只裁决**(accept/reject/defer),绝不让人去打分;④ 排序不是过滤——低分候选留在队列里,
> 「只做重要的」= 只 **promote** 重要的,不是只 **抽** 重要的;⑤ 没有任何分数高到可以跳过人。

## 1. 现状:只有「产不产」,没有「先做哪个」

现有全部判断都是**二值**的,而且没有一个是「人在事前挑重点」:

| 现有机制 | 在哪 | 是排序吗 | 是人工闸门吗 |
|---|---|---|---|
| extractor 判 `NEW / SKIP` | `extractor.py` | ❌ 二值,单 episode,不跨会话比较 | ❌ 全自动 |
| `probation` + `metrics` | `metrics.py:277` | ❌ 事后看用量,不是事前排序 | ❌ 全自动 |
| `pruning:` 配置块 | `config_default.yaml:24` | — | **配置在,代码没实现**(v0.1 deferred) |
| `pending_changes` 表 | `store.py:140` | ❌ | **空壳**:0 行,无写入方,只有 `count_pending_reviews()` 被 `/skill health` 读 |
| consolidator `plan → apply` | `consolidator.py:399` | ❌ 管去重合并 | ✅ **唯一真正落地的人确认边界**,但不管重要性 |
| indexer 渲染 | `indexer.py:4` | ❌ 注释明写 "no runtime ranking, no top-k truncation" | ❌ |
| aggregator「聚类→排序→人确认」 | [03 §3](03-shared-primitives.md#aggregator) | ✅ 设计里有,**代码不存在** | ✅ 设计里有,**代码不存在** |

后果是**只进不出、一律平权**:`realize_candidate` 直接挡4 落盘生效,prune 没实现,
于是每个够格的候选都无差别写进 `~/.claude/skills/` ——**而每个 active skill 都占掉每个 CC 会话的上下文预算**。
这是真实成本,不是审美问题。本文补的就是缺的那一维:**优先级 + 人工分诊**。

## 2. priority_score:跟 toil_score 同构的乘性打分

沿用 [04 §4.2](04-skillsynapse-loops.md#42-苦力评分) 的乘性风格,五个因子**全部能从 JSONL / 现有表自动算**:

```
priority = repeat × cost × novelty × mark_boost × recency
```

| 因子 | 语义(「为什么重要」) | 数据来源 | 大致范围 |
|---|---|---|---|
| `repeat` | **这事你干过几回** —— 最强的重要性代理 | [aggregator](03-shared-primitives.md#aggregator);它落地前用 `session_index` FTS 拿 episode 摘要检索近邻计数 | `1 + log₂(n)` |
| `cost` | **这回干得多贵** —— 越贵越值得沉淀 | episode 的 tool_call 数 / 时间跨度(`Episode.start_time/end_time`);`metrics._CORRECTION_RE` 纠错命中数 | 0.5 – 2.0 |
| `novelty` | **池子里有没有覆盖** —— 已有 skill 覆盖到的降权 | 与现有 active skill 的描述相似度(复用 consolidator 的 cluster 判据);命中 `coverage_gaps` 加分 | 0.2 – 1.5 |
| `mark_boost` | **人/agent 当场盖过章**([05](05-marking-signal.md)) | `SessionMeta.marks` 的 provenance 权重(human 1.0 / agent 0.4) | 1.0 / 1.5 / 3.0 |
| `recency` | **还在不在做** —— 三个月前干过一次的沉底 | episode 时间 vs now,半衰期 30d | 0.3 – 1.0 |

- **存哪**:`pending_changes` 表加两列 `priority_score REAL` + `score_breakdown TEXT`(json 存各因子),
  复用现成空壳表,不新建表。
- **可审计**:每次打分把 breakdown 写 `decisions.jsonl`,事后能回答「它凭什么排第一」。
- **红线**:分数**只排序,不自动 promote**。与 [05 §4 的 `probation_floor_uses`](05-marking-signal.md) 同构的不变量——
  排序开的是「先看哪个」,不是「免审」。

## 3. `skill review`:人类分诊台

```
$ skill review
 #  SCORE  REPEAT COST NOV  MARK    NAME                          WHY
 1   8.4   ×4     ×1.8 1.2  ⚑human  deploy-syncthing-over-tailnet 4 次跨机部署,平均 37 步 + 2 次返工
 2   3.1   ×2     ×1.1 0.9  -       sfm-scale-from-rig-baseline   2 次,单次 21 步
 3   1.2   ×1     ×0.8 0.4  -       fix-one-off-yaml-typo         1 次,池子里已有近邻
                                              (17 more below threshold — `skill review --all`)

$ skill review accept 1 2        # 挡3 → 挡4:promote 到 active + 写 SKILL.md + symlink
$ skill review reject 3 --note "一次性的"
$ skill review defer 4           # 留在队列,下次继续参与排序
```

- `accept` = [03 §2.3](03-shared-primitives.md#git-发布模型) 的 promote 动作,是**唯一**进生效路径的门。
- `reject` 落 `decisions.jsonl`,并**留作负样本**:被拒的特征先只记录,不做在线学习(避免早期噪声反噬打分)。
- **先做非交互 CLI,不做 TUI**:可脚本化、可 cron 出「今晚待分诊 N 条」的报表,也便于测试。

## 4. 上限:排序管顺序,预算管总量

排序只解决「先看哪个」,不解决「一共多少」。两个硬闸:

```yaml
# config_default.yaml 新增
review:
  top_n: 10                  # 每晚只把前 N 个推进分诊队列,其余沉底(仍在库,--all 可见)
library:
  max_active_skills: 40      # 软上限:超了就在 review 里同时提示「该剪谁」
```

超上限时 `skill review` 同屏给出**剪枝建议**(按 `effective_rate` 升序 + 长期零 selection 的 probation 项),
把 v0.1 deferred 掉的 prune 先用**人工闸门**补上——比再写一套自动 prune 门控稳,且立刻可用。

## 5. 存量分诊(第 0 步,不依赖上面任何代码)

现在 `~/.claude/skills/` 已有 23 个 skill(14 captured 全在 probation、从未被剪),它们**无差别挂在每个会话上下文里**。
先跑一次离线补分:对现有 active skill 按 §2 同一公式打分(`repeat` 用 `source_sessions` 数量代理,
无 episode 历史的 `cost` 取 1),`skill review --existing` 让人一次性过一遍,砍掉不该常驻的。

## 6. 落地顺序

1. **`priority.py` 打分 + `pending_changes` 加两列** —— 纯计算,不改任何现有行为,可单测。
2. **`skill review` list/accept/reject/defer** —— 队列先空跑,人已经能用。
3. **归纳环降挡3**(`loops.inductive.intensity: 3`):`realize_candidate` 改落 `_pending/`,新候选默认进队列。
4. **存量分诊**(§5)。
5. **`repeat` 接真 aggregator**([03 §3](03-shared-primitives.md#aggregator))—— 在那之前用 FTS 代理。

第 1、2 步不改现有行为,第 3 步才切开关——可灰度、可回退。
