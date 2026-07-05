# 06 · WorkLog × Notes:从语料蒸馏情景记忆与语义记忆

> 读者文档,与 [04 SkillSynapse](04-skillsynapse-loops.md) **平级**(不进三环)。定义两个新的语料「读者」——
> **WorkLog**(情景记忆,「发生过什么」)和 **Notes**(语义记忆,「查过什么、学到什么」)。语料底座与归档见
> [01](01-corpus-and-archive.md);hub 运行时与常驻循环见 [02 §4](02-transport-and-security.md#hub-知识库管理者);
> 挡位与 aggregator 见 [03](03-shared-primitives.md)。
>
> **本轮定调**:① WorkLog/Notes 与 SkillSynapse 是**平级读者**,共享语料底座与抽取 pass,不进三环;
> ② 逐级指针下钻是唯一读取路径,断指针的声明不许写。

## 1. 定位:一份语料,三种记忆

用认知科学的记忆分类看,整个生态从同一份经验流蒸馏三种记忆:

| 记忆类型 | 回答的问题 | 系统 | 产物 | 可分享性 |
|---|---|---|---|---|
| **情景** episodic | 「发生过什么」 | **WorkLog**(本文档) | 工作事件、工作线台账、日报/周报/季度叙事 | 天生私有(证据链扎进原始对话) |
| **程序** procedural | 「怎么做某类事」 | [SkillSynapse](04-skillsynapse-loops.md) | SKILL.md / slash-command | 可发布(蒸馏物,脱离原料) |
| **语义** semantic | 「什么是真的/查过什么」 | **Notes**(本文档) | 一句话教训、问题→出处映射 | 结论可分享,证据私有 |

关键不对称:**skill 是蒸馏物,出厂即脱离原料;WorkLog/Notes 是索引,永远押着原料。** SKILL.md 写完源会话删了
照样用,所以能走 git 发布;WorkLog 的每条声明靠 evidence 指针活着,指针断了就退化成无法验证的散文。这决定了两边
完全不同的存储/发布/归档纪律([01 §4 归档](01-corpus-and-archive.md#4-归档jsonl-升格为唯一不可重建资产))。

与 SkillSynapse 是**同层两个读者**,互相喂养:自评「想发展什么技能」直接变成定向环的 goal;SkillSynapse 本季度
沉淀的 skill/消灭的苦力反过来是「achievements」的素材。**季度自评只是第一个查询,不是目的**——同一台账层还服务
「上次我怎么解决 X 的」(日常最高频)、周报/月报(同一 roll-up 换时间窗)、**给未来 Claude 会话喂上下文**
(开工前查「做过类似的事吗/踩过这坑吗」——三个消费者里最值钱的)。**层是产品,查询是廉价视图。**

## 2. 写入:分层 map-reduce,分流器抽取

一个季度的语料远超任何一次调用的上下文,「总结」必须是分层、增量、留持久中间产物的 map-reduce。夜间持续消化,
季度末只是对中间产物做一次查询。

### 2.1 第一层:episode → 分流器抽取 pass(map)

每个 [episode](01-corpus-and-archive.md#2-处理底座scannerepisodeextractor) 过一遍 LLM,**同时产出三种流**——
语料只读一遍,LLM 只过一遍:

```
episode ──→ 抽取 pass ──┬─→ 工作事件   (情景) → WorkLog
                        ├─→ skill 候选 (程序) → SkillSynapse 三环
                        └─→ 知识笔记   (语义) → Notes 库
```

现有 `extractor.py` 的 NEW/UPDATE/PITFALL/SKIP 判断本就是这个 pass 的一部分,将来合流;初期 WorkLog 抽取可先
独立跑,避免动已有管线。

**工作事件 schema**(每字段带 evidence 指针,防幻觉的唯一可靠手段):

```yaml
date: 2026-05-14
machine: <hostname>
project: dfsfm                    # projects/<path> 目录名即有
intent: 修复 matcher 在低纹理场景的漂移
outcome: shipped | partial | failed | abandoned
deliverables: [commit a3f21c, PR #47]     # 从 Bash 里的 git 命令抓
challenges: [CUDA OOM 排查 3h,最后是 batch 维度 bug]
duration_est: 4h
evidence: [<hostname>/dfsfm/session-uuid.jsonl#L120-L340]
```

`outcome` 保留 failed/abandoned——git 只记成功着陆的东西,而「challenges」恰恰藏在跑三天最后放弃的实验线里,
只有会话记录有。

### 2.1.1 分流器 taxonomy:三出口的硬判据

> 分流器最容易滑回「按话题分类」(→ 又写满专有名词)。判据必须咬住**记忆类型的本质属性**——三种记忆是三条正交轴,
> 不是三个互斥桶。**每个出口独立过一次闸门**,一个 episode 可同时出 skill + note + event。建模成「三选一互斥」是必踩的坑。

**三条硬闸门(每条 yes/no,全中才出该出口):**

| 出口 | 闸门(全中) | 一句话测试 |
|---|---|---|
| **Skill**(程序) | **S1 可复用**:对未来、不同输入也成立(不止这一个文件/scene/相机)· **S2 可祈使**:能写成「做 X / 跑 Y」的步骤 · **S3 非平凡**:含聪明操作员也会错的决策或次序 | 下次换个输入,我会重跑这套步骤吗? |
| **Note**(语义) | **N1 命题性**:是事实/约束的断言,不是指令 · **N2 持久**:脱离这次运行仍为真,开工前想先知道它 · **N3 挣来的**:撞到 ≥2 次,或发现它花了真代价 | 这是我想在相关任务开工前**知道**的一句事实吗? |
| **WorkEvent**(情景) | **W1**:发生了值得记一行台账的事——有 outcome、有交付物、或有多小时 challenge | 这是「我做了 X、某天 shipped/failed」的记录吗? |

Note 两子类:**学到的事实**(教训/gotcha/约束)、**查阅轨迹**(问题→出处映射)。**注意 SOP 不是 Note**——
SOP 是程序记忆,就是 SKILL.md 的形状,归 [SkillSynapse 归纳环](04-skillsynapse-loops.md)。N3 的重复门与
[aggregator 的跨会话模式检测](03-shared-primitives.md#aggregator)同构(自动化环挖反复的机械动作,Notes 挖反复的查阅)。

**「两者都不是」要拆成两种**:发生过但无蒸馏物 → **WorkEvent**(失败/放弃的线正是靠它兜住,git 里没有);
真·啥也没有 → **SKIP**。

**两条消歧规则(专治 skill 库里的错分):**

- **D1 · 方法 vs 结论**:把 episode 的收获拆成「怎么弄明白的(方法)」和「得出了啥(结论)」。会泛化的方法 → Skill;
  关于**这个项目/这批数据**的结论 → Note / WorkEvent。**方法平凡时,只有结论存活,进 Note。** 治
  `explain-* / assess-* / decide-whether-*` 这类假 skill。
- **D2 · 指令 vs 陈述**:祈使句(「BA 前永远锁死出厂内参」)→ Skill/SOP;陈述句(「出厂内参已准到 0.2px」)→ Note。
  同一发现常两边都出:观察 → Note,由它固化出的规则 → SOP/skill。

**判例(拿真实条目对):**

| 候选 | 判归 | 依据 |
|---|---|---|
| `calibrate-mono-intrinsics-from-board-captures` | **Skill** | S1-3 全中,换板型/相机都能跑 |
| `explain-fiducial-pose-error-vs-distance` | **Note**(事实) | 命题;做图的方法平凡(D1) |
| `label-rig-intrinsics-provenance-factory-vs-refined` | **Note + WorkEvent** | 关于这批文件的发现,无可复用流程 |
| `set-rig-ba-refine-policy-for-factory-intrinsics` | **Note**(约束) | D1/D2 双拆的典型 |
| 「faster-whisper 静音时蹦『谢谢观看』」 | **Note** | gotcha,N1-3 全中 |
| 「分体键盘 = 两个键盘设备 = 中文输入卡」 | **Note** | skill 判 SKIP 正确,gotcha 靠 Note 回收 |
| keyd 合并分体键盘的配置 | **Skill** | 可复用流程 |

> **反面锚点**:上表最后两行来自同一会话——skill 出口**正确地判了 SKIP**(multi-topic-drift),但那条「分体键盘导致
> 中文输入卡」的真教训,若无 Note 出口就被直接扔了。三路分流器对同一个 SKIP,信息回收率天差地别。这是「抽取合流、
> 别等 v0.2」的最强论据。

**工程含义**:分流器 = `extractor.py` 的升级,NEW/UPDATE/PITFALL/SKIP 退化为 **skill 出口的判决**,另加 `notes[]`、
`events[]` 两个出口,**同一次 LLM 调用**产出。router prompt 按「每出口独立过闸门」组织,不写成三选一。

> **待定(影响成本)**:WorkEvent 是否真「近乎每个实质 episode 都出一条」。若是,台账 roll-up 的归属判断量级要按此设计——
> 这是 §2.2「唯一需要智力的一步」的调用频率上界。

### 2.2 第二层:两个正交的 roll-up 轴

**时间轴(日报)**:按天聚合,偏运营用途,对季度自评作用有限。

**工作线轴(workstream ledger)——自评真正要读的东西。** 季度叙事的单位不是天,是「事」:一条工作线可能横跨六周、
二十个会话、三台机器。按主题增量维护台账:

```
workstreams/dfsfm-low-texture.md
  状态: shipped (2026-06-10)
  时间线: 04-28 启动 → 05-14 方案A失败 → 05-20 换方案B → 06-10 合入
  产出: PR #47, #52; benchmark 提升 12%
  挑战: 方案A的三周弯路 (evidence: ...)
```

夜间任务:新工作事件 → 判归属哪条已有工作线(或开新线)→ 增量更新台账。**这是全系统唯一需要智力的一步**,
其余全是机械聚合。

### 2.3 第三层:查询时合成

自评四道题只是台账的不同查询视角:

| 题目 | 查询 |
|---|---|
| major achievements | `outcome=shipped` 的工作线,按产出影响排序 |
| challenges | 各线 `challenges` 字段 + `abandoned` 的线 |
| 价值观举例 | 从工作线挑符合叙事的具体事件(带 evidence,例子是真的) |
| 想发展的技能 | coverage_gaps 聚类 → 直接接[定向环](04-skillsynapse-loops.md#3-定向环directed-loop)输入 |

输入 = 几十条台账 + git log,一个上下文装下。**git log 做骨架,会话做血肉**:先 `git log --author --since` 扫全部
repo 拿「确定发生过的产出」,再用工作事件解释每个产出背后的过程。**盲区要认账**:只看得见终端里的工作,会议/
code review/带人/跨组协调不可见——草稿显式标注「以下仅覆盖编码类工作」,其余靠日历/Jira(以后接)或手补。

## 3. 读取:逐级指针下钻

写入自底向上,读取自顶向下。evidence 指针不只是防幻觉的引用,**它就是索引结构本身**。三条规则:

1. **永远从最粗层进入,能停就停。** 台账索引(一线一行,几十行)全量入上下文——这层是目录,直接读。
   「这季度做了什么」读台账正文即答,不下钻。
2. **只对需细节/需验证的点下钻,沿指针走,不做全文搜索。** 台账某线 → 它引用的几个工作事件 → 必要时沿 `evidence`
   打开原始 JSONL 那一段。每跳扇出有界,成本可控、路径可复现。
3. **原始层只用来验证和引用原话,永远不用来「找」。** 若某问题要 grep 原始 JSONL 才能答,说明中间层抽取漏了信息——
   当成第一层 schema 的 bug(加字段),而不是加强底层搜索。这条逼中间层保持「足够回答问题」的质量。

推论:**不需要向量库/RAG**。顶层小到全量入上下文,往下每步是确定性指针跳转,Claude 当导航员即可。同构先例:
memory 系统(MEMORY.md 索引→记忆文件)、skills(描述行→SKILL.md 全文)。**工程纪律:每层每条声明必须带指向下一层的
指针,断指针的声明宁可不写。** 容量预算:台账索引 ≤ 几 K token(保证永远装得下),单线正文 ≤ 几百 token,
工作事件不限量(只按指针取)。

> 归档区、蒸馏运行时、下钻解引用同机(见 [01 §4](01-corpus-and-archive.md#4-归档jsonl-升格为唯一不可重建资产) /
> [02 §4](02-transport-and-security.md#hub-知识库管理者));各机开工时索引层已在本地(下行同步),粗粒度问题本地即答,
> 需证据才回 hub 解引用(不在线时降级为「有结论无证据」)。

## 4. 挡位与安全

- **挡位**(定义见 [03 §1](03-shared-primitives.md#挡位)):WorkLog/Notes 产物是**增量式私有记录,不改任何生效环境**,
  风险面远小于三环——工作事件/台账直接**挡4**(写入即生效);Notes 初期**挡3**(草稿区,人扫一眼再入索引),跑稳上挡4。
- **安全**:证据链天生私有、天生不出 tailnet,与[内外网隔离红线](02-transport-and-security.md#安全内外网隔离)严丝合缝,无需新增机制。

## 5. 落地顺序

1. **修归档 bug**([01 §4.1](01-corpus-and-archive.md#41-现有部署的真-bug证据链活不过一个月)):源机调 `cleanupPeriodDays` +
   hub 加着陆区→归档区夜间搬运。先做——每天都在丢证据。
2. **第一层 map 脚本**:episode → 工作事件 JSONL(先只抽情景流,不动 extractor)。写完即可对本季度存量做一次 backfill,
   直接回答这次自评——不等整套系统。
3. **工作线归属 + 台账 roll-up**:夜间增量。
4. **查询视图**:季度自评 / 周报 /「做过类似的事吗」slash-command。
5. **Notes 流 + 重复检测**:蹭 [aggregator 跨会话模式检测](03-shared-primitives.md#aggregator),一起做。
6. **索引层下行同步 + 各机开工查询**:最值钱的消费者,放最后是因为它依赖前面全部。

未定事项:工作线归属判断的具体 prompt 与去重策略;Notes 重复检测与自动化环共用哪部分代码;hub 解引用服务用
SSH 还是 MCP;日历/Jira 等非 CC 信号源接入(补盲区)。
