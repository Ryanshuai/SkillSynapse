# 03 · 共享原语:挡位 · 产物发布 · 聚合底座

> 底座文档。三个被多个读者(04/05/06)复用的机制,定义只此一处。任何文档提到
> 「挡位」「staging/git 回滚」「跨会话聚合」都以本页为准。

## 1. 挡位:方向 × 强度的离散档 {#挡位}

一条环 = 一个**方向**;强度 = 它**自主走多远**。强度**不做线性滑杆,离散成几档**——每挡是**质变**
(改变环做的事的**种类**,不是「同一件事做多点」),像相机的自动/光圈优先/手动挡,不像亮度条。

| 挡 | 名 | 这一挡做什么 | 红线 |
|---|---|---|---|
| **0** | 关 | 不跑 | — |
| **1** | 观测 | 只把信号显式化到看板(「我看到你反复在 X」),**零产物** | 不产任何候选 |
| **2** | 建议 | 产出具体候选(建议的 skill/goal/自动化),**不写任何真实文件** | 不落盘 |
| **3** | 草稿 | 产物写进 staging `_pending/`,惰性,人 promote 才生效 | 不进生效路径 |
| **4** | 生效 | 直接写且启用,人**事后**可否决/回滚 | 改真实文件 |
| ~~5~~ | ~~自驱~~ | ~~主动 headless 演练+启用,不等人~~ | 预留,默认禁用 |

**每条环/每个目标独立设挡,随信任度上调**。挡位是**运行期配置,不是代码分叉**——同一套代码按 `intensity`
分支。实现:每条环在 config 里带 `intensity: 0..4`;候选产物落盘前统一过一个 `gate(intensity)` 决定
「只记录 / 出建议 / 写 staging / 写生效路径」。

**各读者当前落点**(依据见各自文档):

| 读者/产物 | 当前挡 |
|---|---|
| 三环·归纳 | 挡4 生效(`realize_candidate` 本就直接落盘,最成熟) |
| 三环·自动化 | 挡3 草稿(生成草稿等人批) |
| 三环·定向 | 挡2 建议(只偏置不合成) |
| 标记·`/skillsynapse new` | 挡3 草稿 |
| WorkLog / 台账 | 挡4(增量私有记录,不改生效环境,风险面小) |
| Notes | 挡3 起(人扫一眼再入索引),跑稳上挡4 |

## 2. 产物发布模型:staging + git + symlink {#git-发布模型}

**定调:synapse 产出的 skill/command 带完整 git 历史。** 一旦有环上到挡4(自动生效),就必须能干净回滚;
文件型产物的回滚,git 最干净(原子撤销、熟悉工具看 diff、跨机 merge)。挡3 及以下不强制 git——草稿删掉即可——
**git 是「上挡4」的入场券**。

### 2.1 谁需要历史

| 产物 | 回滚手段 |
|---|---|
| DB 里的 skill 内容 | `models.py` 已预留 version DAG(`version / parent_skill_ids / content_snapshot / content_diff`)+ decisions.jsonl,app 级即可回滚 |
| **文件型产物**(SKILL.md / slash-command / 脚本) | **git**——DB 不是源头,文件才是 |

### 2.2 目录布局:synapse 自有 repo 当源头,`~/.claude` 只是发布视图

**不 git 整个 `~/.claude`**(4.9G JSONL、共享目录、多写入者)。git 一个 synapse **单写入者**的自有 repo:

```
~/.claude/skills/                 ← manual root(手写 skill,bootstrap 扫这里,永不 git)
    my-hand-skill/SKILL.md
    synapse/  ───── symlink ──────┐  ← 发布视图(挡4 才建此链接)
                                  │
~/synapse/skills-repo/  (.git)◄───┘  ← synapse 自有 root = 源头 = git repo(单写入者)
    _pending/<cand>/              ← 挡3 草稿:已 commit(留历史)但未 symlink,CC 看不到
    active/<skill>/SKILL.md       ← 挡4 生效:被 symlink 进 ~/.claude/skills/synapse
```

- **源头**在 synapse 自有 repo;**生效**靠 symlink 把 `active/` 挂进 CC 扫描路径。回滚在 repo 里 `git revert`,
  symlink 指向内容即时变,无需再 copy。
- 每晚一次 run = 一个 commit;每次 promote/生效 = 一条带清楚 message 的 commit。

### 2.3 挡位 ↔ git 动作

- **挡3 草稿** = 写 `_pending/` 并 commit(草稿也进历史 =「那晚提议过什么、你拒了什么」的审计),**不 symlink**。
- **挡4 生效** = 挪到 `active/` + 建/更新 symlink。**「发布」这个动作本身就是挡4。**

### 2.4 两条约束

1. **按 blast radius 给 artifact-kind 封顶挡位**:`settings.json` 的 hook 最危险(自动执行)且文件共享、
   不好 git → **永远封在挡3(只草稿)**,再信任也不自动生效;skill/command 可以挡4+git。
2. **bootstrap 别吃自己**:`bootstrap.py` 现扫 `~/.claude/skills/**/SKILL.md` 当 manual 导入;symlink 进去后
   会把 synapse 自造的 skill 当 manual 再导一遍。故 manual 发现必须**排除 `synapse/` 子树**——`resolve_paths`
   把 `skills_root` 拆成 `manual_skills_root`(纯扫描根)与 `synapse_skills_root`(默认 `~/synapse/skills-repo`,git 管)。

> 与[同步矩阵](02-transport-and-security.md#同步通道矩阵)对齐:skill/command 走 **git**(要历史+跨机 merge),
> 原始 JSONL 走 Syncthing(大、不要历史)。两种数据两种同步。

## 3. 跨会话聚合底座(aggregator) {#aggregator}

多个读者都要「从跨会话模式里挖东西」,底层是同一套机制,抽成共用 `aggregator`:

```
跨会话聚合模式 → 排序 → 人确认 → 进验证闭环
```

各读者只定义「聚什么模式、产什么物」,不各写一遍聚合:

| 复用方 | 聚什么模式 | 产什么 |
|---|---|---|
| 三环·自动化([04 §4](04-skillsynapse-loops.md)) | 反复出现的机械动作(命令/工具序列 n-gram) | 自动化建议(command/脚本/hook 草稿) |
| 三环·定向([04 §3](04-skillsynapse-loops.md)) | 反复出现的 coverage_gap | 能力目标建议 |
| Notes 入库([06 §2](06-worklog-and-notes.md)) | 反复的查阅/事实(N3「挣来的」重复门) | Notes 条目 |

> 落地顺序上建议**自动化环先行**,`aggregator` 从 `toil_miner` 里抽出,再供定向环 / Notes 复用。
