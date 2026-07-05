# 05 · 标记信号:现场盖章的第四信号

> 读者文档,喂给 [04 三环](04-skillsynapse-loops.md)。三环全是**被动推断**(夜间 extractor 读压缩 brief、
> 隔一天,猜 NEW/PITFALL/toil);本文补一条**人 / 干活 agent 在会话当场主动打信号**的路径:标记(mark)。
>
> **本轮敲定**:① 通道走 transcript 为主(`/skillsynapse mark`,前门避开 CC 自带 `/skill`),不走本地 sidecar;
> ② 标记既是**入抽取的入场券**、也开**快速通道**(带 probation 折扣);③ 人和 agent 都能标,但**按 provenance 给权重**
> (人≈1,agent<1);④ 部署必须同时下发**发现入口**,否则别机 agent 不知道这功能存在。

## 1. 定位:不是第四条环,是喂给三环的 ground-truth 标签

三环的信号全是**推断**(LLM 读 transcript 猜)。标记是**当事人现场盖章**——不用猜。价值不在「再开一条 pipeline」,
而在给已有 pipeline 注入 ground-truth 标签。

| 信号 | 谁产 | 何时 | 上下文 | 质量 |
|---|---|---|---|---|
| 归纳 / 定向 / 自动化 | 夜间 extractor | 隔天 | **压缩 brief** | 推断,可能漏可能错 |
| **标记** | **会话当场的人 / 干活 agent** | **当场** | **全上下文** | **ground truth / 全信息判断** |

**为什么干活 agent 该有这个能力(核心动机)**:夜间 extractor 读的是删掉 tool_result 的摘要、还隔了一天;
干活当场的 agent 拥有完整上下文——它最清楚「这次踩的坑值得记」「这个流程能复用」「这段又烦又机械该自动化」。
让它当场盖章,准度碾压夜里另一个 LLM 猜。

## 2. 三种极性(直接映射三环的已有出口,不新建管道)

| 极性 | 喂给 | 落到 | 语义 |
|---|---|---|---|
| **`LEARN`** | 归纳环 | extractor NEW 路径 | 「这是个好的可复用流程,抽 skill」 |
| **`PITFALL`** | (pitfall) | `OrphanPitfall` 路径 | 「这个坑记下来别再犯」 |
| **`TOIL`** | 自动化环 | `toil_miner` 候选 | 「这活又烦又机械,封成命令/脚本」 |

标记只是给这三条已有出口一个**高置信入口**,不改下游验证闭环。

## 3. 捕获通道:走 transcript,不走本地 sidecar

标记**写进会话 transcript**,而不是本地 DB。三个硬理由:

1. **跨机零成本**:标记随 `~/.claude/projects/*.jsonl` 被 Syncthing 免费同步到 hub
   ([同步矩阵](02-transport-and-security.md#同步通道矩阵));本地 sidecar DB 不进同步链,等于白标。
2. **episode 定位**:标记带它在 transcript 里的 event 下标,天然知道值得学的是会话里**哪一段**,
   喂给 `episode_detector` 精确定位。
3. **人 / agent 同一通道**:人打 `/skillsynapse mark`、agent 吐同一个 sentinel,scanner 一套正则捞两者,
   只在 provenance 上分。

事后补标的 `skill mark <session-id>` CLI 作为**补充通道**(忘了当场标时用),但主力是 transcript。

### 3.1 sentinel 格式:一个正则捞两种来源

人和 agent 产出**同一个 token**,scanner 只维护一条正则:

```
⟦synapse:mark kind=learn⟧ 部署时 symlink 必须先于 activate,否则 CC 扫不到
⟦synapse:mark kind=pitfall⟧ DNS 告警下 MagicDNS 不可靠,设备地址硬编码 tailnet IP
⟦synapse:mark kind=toil⟧ 每次起 Lambda 都手抄 IP 改 ssh config,该自动化
```

- **人**:`/skillsynapse mark <kind> <note>`(见 §6.1)的 `.md` body 展开成上面这行 sentinel(slash command
  本质是 prompt 模板,展开文本落进 transcript,顺带 `<command-name>` 也在,被 `slash_command_parser` 认出)。
  **命名避让**:前门用 `/skillsynapse` 而非 `/skill`(后者撞 CC 自带 skill 机制)。
- **agent**:干活 agent 直接在回复里吐这一行 sentinel。
- **provenance 自动判定**:sentinel 落在 `user` 事件 = human;落在 `assistant` 事件 = agent。scanner 已知
  `event.type`,零额外埋点。

## 4. provenance 权重:人≈1,agent<1

标记是**带权重的信号**,权重由 provenance 决定,调制两件事:**(A) 覆盖 PRE-SKIP 的力度**、
**(B) 快速通道的 probation 折扣**。

```yaml
# config_default.yaml 新增
marking:
  weight_human: 1.0     # 人盖章 ≈ ground truth
  weight_agent: 0.4     # 干活 agent 盖章:全上下文的判断,但仍是判断,不是事实
  override_skip_threshold: 0.8   # weight ≥ 此值 → 硬覆盖 PRE-SKIP(仅人达标)
  probation_floor_uses: 1        # 无论权重多高,转正前至少 1 次真实成功使用(不变量红线)
```

**(A) 覆盖 PRE-SKIP** —— 现在 `extractor` 的 AD_HOC_DEBUG / SESSION_FAILED / MULTI_TOPIC_DRIFT /
`min_tool_calls` 都强制 SKIP:

- `weight ≥ threshold`(**人**):**硬覆盖**——「当事人说学,哪怕看着像失败/瞎试也照挖」
  ([定向环 §3.2 钩子1](04-skillsynapse-loops.md#32-四个钩子全部复用现有机制不做主动合成) 的极端版)。
- `weight < threshold`(**agent**):**只强偏置不硬覆盖**——豁免 `min_tool_calls`、强烈倾向 NEW,
  但 `SESSION_FAILED` 这种硬失败仍尊重(除非极性是 `PITFALL`——失败会话正是 pitfall 富矿)。

**(B) 快速通道** —— 标记驱动抽出的 skill,`probation` 期按权重打折:

```
probation_selections_needed = base * (1 - weight * fast_track_factor)
                              # 但结果 ≥ probation_floor_uses(红线,永不为 0)
```

- 人盖章(1.0):折扣最狠,最快转正——但**仍需 ≥1 次真实成功使用**才出 probation。
- agent 盖章(0.4):折扣浅,略微加速。
- **不变量红线**:标记开的是「更快验证」,不是「免验证」。`probation_floor_uses ≥ 1` 钉死——
  「现实里没人用就被剪掉」([三环汇合点](04-skillsynapse-loops.md#2-三环全景))对标记同样成立。人能让 skill
  **快转正**,但不能让一个**从没被真实用过**的 skill 转正。

## 5. Pipeline 触点(尽量落在已有代码上)

| 触点 | 改动 |
|---|---|
| `models.py` | 新 `Mark` dataclass(`kind ∈ {learn,pitfall,toil}` / `note` / `provenance ∈ {human,agent}` / `event_idx` / `timestamp`);`SessionMeta.marks: list[Mark]` |
| `scanner.py` | `parse_jsonl` 时用 sentinel 正则捞 mark,按事件 role 定 provenance,挂到 `SessionMeta.marks` |
| `episode_detector.py` | 命中 mark 的 episode **豁免 `min_tool_calls`**;mark 的 `event_idx` 映射到所属 episode |
| `extractor.py` | 抽取前查 mark:按 §4 权重决定覆盖/偏置;`build_extraction_prompt` 注入 `⚑ 本会话被{人/agent}标记为 {kind}:'{note}'` |
| probation 门控 | 标记驱动 SkillRecord 按 §4(B) 设 `probation_selections_needed`,不动 metrics 其余逻辑 |
| decisions.jsonl | 记 `mark_driven_extraction`(provenance / kind / note),留审计 |
| `commands.py` | 补 `skill mark <session-id> --kind learn --note "..."` 事后补标 CLI |

**`SkillRecord.origin`**:标记驱动抽出的 skill 加 `origin="marked"`(与 captured/manual 并列),
下游一眼可辨、可单独统计「标记信号的转正率」。

## 6. 部署与发现:让别机 agent 不再一脸懵逼(命门) {#6-部署与发现}

**这功能能不能真跑起来,全看部署时有没有把「发现入口」下发到每台机器。** slash command 文件存在 ≠ agent
知道该用;agent 要**主动**自标,必须在它上下文里被告知。缺了这步,任意新起的 agent 根本不知道 `/skillsynapse` 存在。

现状:`~/.claude/commands/` **不存在**,全局 `~/.claude/CLAUDE.md` 只有几行——发现入口是**空的**。
三件套每台机器都要下发(幂等脚本,平行于 `deploy/syncthing/`)。

> **为什么靠 deploy 不靠同步**:`~/.claude/commands/` 和全局 `CLAUDE.md` **不进 Syncthing**
> (Syncthing 只同步 `~/.claude/projects/` 单向,见[同步矩阵](02-transport-and-security.md#同步通道矩阵))。
> 所以发现入口靠 deploy 脚本逐机下发。onboard 新机器 = 跑一次 `deploy/skillsynapse-cmd/install.sh`。

### 6.1 `/skillsynapse` 命令族(给人用的前门)

一个 `/skillsynapse` 前门收编所有交互——`mark` 只是其一。**单分发器模式**:一个
`~/.claude/commands/skillsynapse.md`,用 `$ARGUMENTS` 拿到 `mark …` / `remember …` 整串,body 按第一个词分发。
好处:一条命令引出所有子命令(autocomplete + 空参打印菜单),权限只需放行一条。

| 子命令 | 干什么 | 落地 | 与 mark 的区别 |
|---|---|---|---|
| `mark <kind> <note>` | 给本会话/当前段盖章(learn/pitfall/toil),交夜间 extractor 挖 | body 展开成 §3.1 sentinel,进 transcript | 异步——**指方向,让 pipeline 抽** |
| `new <note>` | **当场快速建 skill 草稿**:口述成一条 skill,落 staging | 写[挡3 草稿](03-shared-primitives.md#挡位)(不自动生效) | 同步——**人当场把 skill 写出来** |
| `remember <note>` | 记一条**轻量记忆/事实**(比 skill 轻,不成流程),挂给 synapse 当 seed/笔记 | 落 note 表 / seed 文件 | 记事实,不是流程也不是坑 |
| `list` · `status` | 查现有 skill / 本方向进度(接 `indexer` 记分板) | 只读,回填会话 | 查询,不产信号 |
| (空参) | 打印上面这张菜单 | 纯文本 | — |

`mark` / `new` / `remember` 都把对应 sentinel 或草稿落进 transcript/staging,scanner 夜间统一收;provenance 判定
(§3.1)对三者一致。简写可选:再下发 `~/.claude/commands/syn.md` 别名转发同一 body(默认只装全名)。

### 6.2 全局 CLAUDE.md 广告块(给 agent 用的前门 —— 关键)

往每台机 `~/.claude/CLAUDE.md` 注入一段**能力广告 + 触发时机**,这才是治「懵逼」的药:

```markdown
## SkillSynapse 标记:干完值得沉淀的事,当场盖章

你干活当场拥有全上下文,比夜间 extractor 更清楚什么值得学。遇到下面三种情况,
在回复里输出一行 sentinel(会被 SkillSynapse 夜间 pipeline 捞去沉淀):

- 走通一个**可复用流程** → ⟦synapse:mark kind=learn⟧ <一句话:这流程是什么>
- 踩到一个**值得记的坑** → ⟦synapse:mark kind=pitfall⟧ <一句话:坑在哪、怎么绕>
- 磨了一段**又烦又机械该自动化的苦力** → ⟦synapse:mark kind=toil⟧ <一句话:什么活>

节制:只标真正值得跨会话复用的;琐碎/一次性的别标。人手动等价于 /skillsynapse mark。
```

- 这段是 agent **主动自标**的唯一触发源——写进 CLAUDE.md 才进 agent 上下文。
- 幂等:脚本用 marker 注释包裹(`<!-- synapse:begin -->…<!-- synapse:end -->`),重复部署只替换块内,
  不重复追加、不碰用户其它内容。

### 6.3 deploy 脚本

新增 `deploy/skillsynapse-cmd/install.sh`(每台跑一次):落 `~/.claude/commands/skillsynapse.md`(§6.1)、
幂等注入 CLAUDE.md 块(§6.2);`uninstall.sh` 对称移除。跟 syncthing 一样每机幂等、可重入。

## 7. 落地顺序

1. **sentinel + scanner + models**——先能把 mark 从 transcript 捞进 `SessionMeta.marks`,
   `skill scan` 能打印「本次扫到 N 个 mark」。独立可验。
2. **extractor 接 mark**——§4 权重覆盖/偏置 + prompt 注入 + `origin="marked"`;decisions.jsonl 审计。
3. **probation 快速通道**——§4(B) 折扣 + `probation_floor_uses` 红线。
4. **部署三件套**(§6)——**这步不做,前三步对别机 agent 等于不存在。**
5. 事后补标 CLI(§5)——锦上添花。

第 1、4 步是「能被产出 / 能被发现」的两个端点,建议优先打通,中间的 2、3 再补力度。
