# 04 · SkillSynapse 三环进化:归纳 × 定向 × 自动化

> 读者文档。从会话语料长出可复用 skill 的三条路径。语料底座见 [01](01-corpus-and-archive.md);
> **挡位、产物 git 发布、aggregator 底座**见 [03](03-shared-primitives.md),本文只讲三环各自的逻辑与落点。
>
> **本轮定调**:定向环**只偏置不主动合成**;目标**手动为主 + 从反复 gap 自动建议**;自动化环产物
> **生成草稿落 staging 等人批**,不自动改环境。

## 1. 现状:只有「归纳环」

现有 pipeline(`main.py` `run_pipeline`)是纯自底向上、被动的:

```
现实会话 → episode 切片 → extractor(判 NEW/UPDATE/PITFALL/SKIP)→ 落 skill → metrics 打分 → 进化门控
```

`extractor.py` 四个动作全是对既有会话的**反应**;没做过的事永远长不出对应能力。这条线擅长「把你已做好的事
泛化成可复用 skill」,但它是唯一一条,盲区两块:① 你**想发展但还没系统做过**的能力;② 你**反复在做、又烦又慢**、
本该自动化掉的苦力活。补两条主动线路填这两块。

## 2. 三环全景

| 环 | 信号 | 处理单元 | 产物 | 打分 | 状态 |
|---|---|---|---|---|---|
| **归纳** inductive | 「这次会话是个好的可复用流程」 | 单 episode | SKILL.md | `completion_rate` | 已有(v0.1) |
| **定向** directed | 「你声明了要发展的能力」 | goal | 抽取偏置 + 定向 gap + 进度板 | `coverage × health` | §3 |
| **自动化** toil | 「一个机械动作跨天反复出现」 | **跨会话模式** | slash-command / 脚本 / hook | `toil_saved` | §4 |

一句话分工:**归纳=把你做好的事泛化;定向=追你声明的方向;自动化=消灭你反复在磨的苦力活。**

**汇合点**是同一张 `skills` 表和同一套 probation/metrics 门控(`metrics.py`)。定向环和自动化环都从「顶部」
进入这条已有验证流水线——任何主动产出的 skill 都逃不过「现实里没人用就被剪掉」,不会污染池子。

**挡位落点**(挡位定义见 [03 §1](03-shared-primitives.md#挡位)):归纳=挡4 生效、自动化=挡3 草稿、
定向=挡2 建议。自动化跑稳可上调挡4;新方向不放心可压挡1只看不动。

**产物历史**:上到挡4 的文件型产物带 git 历史,目录布局/回滚/blast-radius 封顶见 [03 §2](03-shared-primitives.md#git-发布模型)。

## 3. 定向环(directed loop)

### 3.1 新一等公民:Capability Goal(能力目标 / 进化方向)

新表 `capability_goals`:

```
id, name, description         -- "把 DFSfM 一键部署到任意裸机" / "精通多机日志汇聚"
rubric: list[sub-capability]  -- LLM 从 description 展开成子能力清单(检查点)
linked_skill_ids              -- 哪些现有 skill 服务于这个目标
priority, status, created_at
strength: 1..3                -- 该目标的离散强度挡(挡位同 [03 §1],作用在单方向上)
coverage_score, health_score  -- 进度(§3.3)
```

**每个 goal 自带离散强度挡**:「我要发展 X」可以轻度关注或重点主攻。挡低 = 只在这方向显式化 gap;
挡高 = 激进调低 `min_tool_calls`、连失败/探索会话都挖(§3.2 钩子1 的偏置力度由此挡控制)。

### 3.2 四个钩子(全部复用现有机制,**不做主动合成**)

skill 仍 100% 来自真实会话;定向环只负责「朝目标看得更细 + 指出缺口 + 显示进度 + 建议目标」。

1. **抽取偏置** — active goals 塞进 `build_extraction_prompt`。命中目标领域的会话:调低 `min_tool_calls`、
   倾向 NEW/UPDATE、**连失败/探索性会话也挖**(现在这些被 `AD_HOC_DEBUG`/`SESSION_FAILED` 直接 SKIP——
   但在你的**目标方向**上,一次失败是信号不是噪声)。非目标会话行为完全不变。
2. **定向 gap** — `coverage_gaps` 加列 `goal_id`。对每个 goal 算覆盖:rubric 里哪些子能力**没有任何健康 skill 覆盖**,
   发成挂在 goal 上的定向 gap =「朝这能力前进的 TODO」。
3. **能力记分板** — `indexer.py` 加 per-goal 进度视图:`coverage%`(rubric 被覆盖比例)+ `health%`
   (这些 skill 的 `completion_rate` 加权)+「下一个缺口」。这是你能**盯着看它长**的进化仪表盘。
4. **目标来源** — 手动为主 + 自动建议:`skill goal add "…"` 手动声明(权威来源);夜间 job 把**反复出现的
   coverage_gap 聚类**([aggregator](03-shared-primitives.md#aggregator))产出建议「你好像反复卡 X,要不要立个目标?」——
   **等你确认才成为 goal**。

### 3.3 为什么不做主动合成(本轮决定)

激进版会让 LLM 凭 rubric 缺口「凭空造 seeded skill」。已否决:空想 skill 会污染池子,且在你没真正做过那件事时,
合成步骤大概率是错的。保守版坚持**skill 只从现实里长**,定向环只是「更聚焦的放大镜」。代价:目标方向上没有现成
skill 时只能等你自己去做——但这符合「进化=现实驱动」的本意。

## 4. 自动化环(toil loop)—— 消灭重复苦力

### 4.1 本质:信号是「跨天的重复」,不是「单次的质量」

归纳环看一次会话好不好;自动化环看**同一个机械动作是不是天天在犯**。所以它必须**跨会话聚合**,不能 per-session
判断。新模块 `toil_miner.py`,跨会话跑(复用 [aggregator](03-shared-primitives.md#aggregator))。判据:费时、人做很烦、很慢、
易出错,但 **AI 做很容易、很靠谱**的动作。

### 4.2 苦力评分

```
toil_score = frequency × human_cost × ai_fitness
```

| 因子 | 语义 | 数据来源(全从 JSONL 读,不用埋点) |
|---|---|---|
| `frequency` | 「重复」 | canonical 化后的 Bash 命令 / 工具序列 n-gram 跨会话计数;相似 first-user-message 用 LLM 聚类 |
| `human_cost` | 「费时、烦、慢、易错」 | 同一意图消耗的工具步数、`session_error_rate`、`metrics.py` 已有的 `_CORRECTION_RE`(不对/重来/错了)命中 |
| `ai_fitness` | 「AI 做容易、靠谱」 | LLM 判「输入确定→输出确定、不需人拍板」。**要人做判断的动作不算苦力**(那是判断力,不该自动化) |

### 4.3 产物:可执行自动化,不是给人读的 SKILL.md

高 `toil_score` 候选产物应能直接跑,按 AI 触碰程度分三档:

- **slash-command**(主力):`/xxx` 封整套苦力动作。天然接上现有反馈闭环——metrics 已在追 slash-command 使用率,
  能直接量「自动化后你还手动犯这活吗」。
- **脚本 / Makefile / pixi task**:纯确定性的封成脚本,skill 只写「跑 `pixi run xxx`」。
- **hook**:真正无脑重复的(如每次提交前跑 lint)写进 settings.json。⚠️ hook blast radius 最大,
  **永远封在挡3 只出草稿**(见 [03 §2.4](03-shared-primitives.md#git-发布模型))。

### 4.4 交付策略:**生成草稿等你批**(本轮决定,挡3)

夜间把 slash-command/脚本**草稿**写到 staging(`_pending/<candidate-name>/`:草稿正文 + `RATIONALE.md`
记复现 N 次 / toil_score 分解 / 建议封装形式)。生成但不自动生效——早上 review,满意就 promote,不满意就删。
红线:**绝不自动改 settings.json 或写真实可执行文件到生效路径**。

### 4.5 验证闭环:「自动化后还在磨」是最强再迭代信号

产出并启用后,若那苦力动作**仍在手动复现** → 自动化没做好(不好用/发现不了),`toil_score` 不降反升,
系统把它重新顶到候选前列。反之手动复现归零 = 这条苦力被成功消灭,候选归档。

## 5. 集成面(尽量不动现有代码)

| 位置 | 改动 |
|---|---|
| 新增 `goals.py` + `capability_goals` 表 | 定向环 |
| 新增 `toil_miner.py` + `automation_candidates` 表(或 skills 表加 `kind`) | 自动化环 |
| 新增 `aggregator.py`(见 [03 §3](03-shared-primitives.md#aggregator)) | 两环共用 |
| `coverage_gaps` 加列 `goal_id` | 定向 gap |
| `build_extraction_prompt` 加 goals 段 | 抽取偏置 |
| `indexer.py` 加 per-goal 进度视图 | 记分板 |
| `commands.py` 加 `skill goal add/list`、`skill toil list` | CLI |
| `main.py` 加 Step:定向建议聚类、toil 挖掘(均产 staging,不自动生效) | 编排 |
| **probation / metrics / 门控** | **完全不动**——三环共用同一套 |

`SkillRecord.origin`:定向环不加(只偏置);自动化环产物若并入 skills 表,加 `origin="toil"` +
`artifact_kind ∈ {command, script, hook}`。

## 6. 落地顺序

1. **自动化环先行**——收益最直接、最独立(不依赖 goal 概念),`toil_miner` + staging 就能跑出「待自动化清单」。
2. **`aggregator` 底座**——从 toil_miner 里抽出跨会话聚合(见 [03 §3](03-shared-primitives.md#aggregator))。
3. **定向环**——`capability_goals` + 抽取偏置 + gap 聚类建议(复用 aggregator)+ 记分板。

三步每步都能独立上线、独立验收,不必等齐。
