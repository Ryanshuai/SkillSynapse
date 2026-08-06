# 02 · 传输、安全与 hub

> 底座文档。数据平面的**唯一真相源**:数据怎么在机器间流动、安全边界在哪、
> **同步通道矩阵**(什么走什么)、hub 是什么、怎么部署。其它文档一律引用本页,不重述。
>
> **v0.2 定调**:安全边界从「内容脱敏」改为「内外网隔离」——tailnet 内部各机之间原始 JSONL
> 自由同步、不做脱敏(小团队,效率优先);唯一红线是**汇聚流量绝不出 Tailscale mesh**。

## 1. 汇聚架构:推送式,不用拉取式

**选型:Syncthing 单向同步(send-only → receive-only),汇聚到 hub。**

```
每台机器  ~/.claude/projects/   (send-only folder)
                 │
                 ▼  Syncthing over Tailscale
hub       ~/cc-logs/<hostname>/   (receive-only)  →  夜间归档进 cc-archive/(见 01)
```

选推送而非拉取(hub 半夜 SSH 去各机 rsync)的原因:

- **无人值守容错**:笔记本合盖、机器关机时拉取扑空;推送模式下机器上线后自动补传,夜间任务读的是
  「已缓存到本地的最新数据」。
- 复用现有 Tailscale mesh。替代方案 `rsync over Tailscale SSH` 也可行(约十行),但要接受
  「机器不在线则缺当天数据」。

### 1.1 传输层:Tailscale

mesh 已建好(2026-07-03 实测)。**具体节点清单(机器名↔tailnet IP↔角色)属拓扑机密,只存本地
`LOCAL-TOPOLOGY.md`(gitignored)**;结构上是:若干源机 + 一台常在线 Linux 机作**临时 hub** +
一台最终的**常驻 hub**(尚未就位)。

> 常驻 hub 就位前用常在线的临时 hub 先把 nightly loop 跑起来,就位后把 receive-only 目录迁过去即可。
> 注意 `tailscale status` 报过 DNS 健康告警(configured DNS 不可达),可能影响 MagicDNS——所以
> Syncthing 设备地址一律**硬编码 tailnet IP**,不依赖 MagicDNS。

## 2. 安全:内外网隔离(不做内容脱敏) {#安全内外网隔离}

**红线:数据绝不出 Tailscale mesh。** mesh 之内当可信内网(明文随便传),mesh 边界就是内外网边界。
放弃「公司机本地脱敏、原始 JSONL 不出本机」这类内容级管控,换成网络层隔离,省掉每台机器写脱敏脚本的成本。
隔离靠三层叠加,每层独立成立:

1. **Tailscale 传输**:所有汇聚流量走 WireGuard 加密 mesh,本身不暴露公网。
2. **Syncthing 关公网路径**(见 §2.1):全局发现/中继/本地广播/UPnP 全关,设备地址硬编码 tailnet IP。
   Syncthing 因此**没有任何触达公网的代码路径**。
3. **Tailscale ACL**:admin 面板限定 Syncthing 端口(22000)只在「源机↔hub」之间可达,无关设备连不到。

**hub 侧收敛**:`~/cc-logs/` 权限 700,只有 hub 本人可读。**真正的内→外出口是 hub 上的外发环节,不是
mesh 内部**:知识日报同步到云笔记、Step C 调云端服务——这些点才需单独把关(内容是否可外发,过 `scrub()`)。
mesh 内部同步不设防。

> **完整红线清单**见仓库根 `CLAUDE.md`(最高优先级)。要点:原始日志(各机 `~/.claude/projects/`、
> hub `~/cc-logs/`、extractor 隔离历史 `~/.claude-skillsynapse/`)不 commit、不外发、不上公网服务;
> 唯一对外通道是过 `sanitizer.scrub()` 的 session brief 与 skill 文件;拓扑清单只存 `LOCAL-TOPOLOGY.md`。

### 2.1 Syncthing 隔离配置(硬约束)

各机 `~/.claude/projects/` 配 send-only folder,hub 侧 receive-only 落到 `~/cc-logs/<hostname>/`。
**为保证数据永不出 mesh,必须关掉一切公网路径**:

```
每台机 Syncthing 配置(config.xml / GUI Advanced):
  · folderType            = sendonly / receiveonly     # 源机只推,hub 只收
  · globalAnnounceEnabled = false      # 关全局发现,不上报公网发现服务器
  · relaysEnabled         = false      # 关中继,不经 Syncthing 公网 relay
  · localAnnounceEnabled  = false      # 关本地广播(tailnet 非 L2 广播域)
  · natEnabled            = false      # 关 UPnP/NAT-PMP,不主动打洞
  · 每个 peer 的 device address 硬编码:tcp://100.x.x.x:22000   # 只走 tailnet IP
  · GUI 只绑 127.0.0.1:8384 + 强密码   # 管理面不进 tailnet,更不进公网
```

数据流被钉死在 `100.x.x.x:22000`(tailnet,WireGuard 加密)。**内外网隔离由此在同步层落地。**

> **验证(2026-07-03)**:源机(sendonly)↔临时 hub(receiveonly)实测打通。日志确认
> `Established secure connection … connection.lan=false connection.crypto=TLS1.3`,连接建立在两机
> tailnet IP 之间;`~/.claude/projects`(4.9 G / 4200+ 文件)全量同步通过。用 v2.1.1 静态二进制免 root
> 装于 `~/.local/bin`。配置脚本见 `deploy/syncthing/`。

## 3. 同步通道矩阵(唯一 owner) {#同步通道矩阵}

系统里流动着几类数据,**各走各的通道,不要混**。任何文档提到「X 怎么同步」都以本表为准:

| 数据 | 通道 | 方向 | 要历史? | 理由 |
|---|---|---|---|---|
| 原始 JSONL 语料 | **Syncthing** | 源机 → hub 单向 | 否 | 大(GB 级)、只增、不需 merge |
| 进化出的 skill / command | **git**(push/pull) | 双向 | 是 | 小、要回滚 + **跨机 merge**(多源改同一批 skill,git 合并优于单向覆盖),见 [03 §git 发布](03-shared-primitives.md#git-发布模型) |
| 台账 / Notes **索引层**(几 K token) | 反向 Syncthing folder 或 synapse repo push | hub → 各机 | 否 | 小,供各机开工时本地查(见 [06 §3](06-worklog-and-notes.md)) |
| `~/.claude/commands/` + 全局 `CLAUDE.md`(发现入口) | **deploy 脚本逐机下发** | 无同步 | — | 每机幂等安装,不进任何同步链,见 [05 §6](05-marking-signal.md#6-部署与发现) |

**推论**:标记随 `~/.claude/projects/*.jsonl` 免费搭 Syncthing 同步到 hub(所以 [11](05-marking-signal.md) 走
transcript 而非本地 sidecar DB——sidecar 不进同步链等于白标);而发现入口(commands/CLAUDE.md)**不同步**,
必须靠 deploy 脚本逐机下发。**原料上行、蒸馏物下行**是总方向。

## 4. hub = 知识库管理者 {#hub-知识库管理者}

hub 身份:从「夜间任务的读取暂存区」升格为**知识库运行时**。四块职责:

| 职责 | 内容 | 详见 |
|---|---|---|
| **归档** | 着陆区→归档区搬运、只增不删、备份、指针可解引用 | [01 §4](01-corpus-and-archive.md#4-归档jsonl-升格为唯一不可重建资产) |
| **蒸馏** | 夜间跑抽取 pass + 台账 roll-up + 日报(headless `claude -p`,订阅额度) | [06 §2](06-worklog-and-notes.md) |
| **索引** | 维护台账/Notes 索引,是逐级下钻的入口与解引用服务 | [06 §3](06-worklog-and-notes.md) |
| **分发** | 蒸馏物下行:skill 走 synapse repo→`~/.claude` 发布视图;索引层同步回各机 | §3 矩阵 |

### 4.1 常驻循环:hub 是服务,不是单次夜间批任务

「不停拉取管理」不等于所有环节同频。同步接收由 Syncthing 推送解决(hub 被动收,无需拉);要常驻的是
处理侧几个循环,各有自然节奏:

| 循环 | 频率 | 成本 | 说明 |
|---|---|---|---|
| 同步接收 | 实时 | 零 | 源机推送,hub 被动收 |
| 归档搬运 | 每小时 | 纯文件操作 | 着陆区新增→`cc-archive/`;越频繁「未归档先被删」窗口越小 |
| 抽取 pass | 滚动,每 2–4h 一批 | LLM(订阅) | 只处理**静默 ≥ 30min** 的会话(进行中的 JSONL 还在追加,episode 未闭合) |
| 台账 roll-up + 索引重建 | 抽取后触发 | LLM(轻) | 事件驱动,不独立设频 |
| 索引层下行分发 | 索引变更后 | 零 | 反向 Syncthing / repo push |
| 日报合成 | 每晚一次 | LLM | 唯一真正「夜间」的环节 |
| 备份 + 健康自检 | 每日 | 低 | 各源机 last-seen / 积压量 / 失败数并入日报——管道坏了第二天早上就知道 |

**增量纪律是「不停跑」的安全前提**:每循环幂等 + 带水位线(状态表记录每个 session 文件的已处理行偏移);
重跑无副作用,漏跑自动补上。机器关机/循环挂掉只是延迟,不丢数据(数据安全由归档层保证)。实现不用自写
daemon:沿用 deploy 里的 `systemd --user` 模式(syncthing-cc.service 同款),每循环一个 timer 调幂等入口。

## 5. 额度与计费(订阅方案)

- CLI 用 claude.ai 账号 OAuth 登录,与网页端/IDE 共享同一订阅额度池,**不额外付费**。
- **两个必须避开的坑**:
  1. cron/systemd 环境变量里**绝不能有 `ANTHROPIC_API_KEY`**——存在即切 API 按量计费,绕过订阅。
     部署脚本须显式检查(红线,见 `CLAUDE.md` 运行约束)。
  2. 撞限额时 CLI 提示「用 API credits 继续」;Console 侧不开 auto-reload。最坏是任务失败等窗口重置,不多扣钱。
- v0.1 `llm_provider` 已内置 rate limit guard(撞限额 defer)。额度充裕(Max $200),放开跑,别加保守封顶;
  唯一红线是环境无 `ANTHROPIC_API_KEY`。
- headless `claude --print` 必须用隔离 `CLAUDE_CONFIG_DIR`(`~/.claude-skillsynapse`),不污染用户真实
  CC/VSCode 历史(已实现,见 `llm_provider.py`)。
- 补充参照:官方 Claude Code Routines(云端定时,Max 15 次/天)适合**不依赖本地环境**的纯任务;
  凡碰本地文件/设备/mesh 的归本地调度。

## 6. 部署

> **已落地为脚本**:`deploy/syncthing/`(`setup-hub.sh` / `onboard-source.sh` / `add-source.sh` +
> `stconfig.py`)。源机↔临时 hub 链路实测打通(§2.1)。发现入口的 deploy 见 [05 §6](05-marking-signal.md#6-部署与发现)。

**A. 现在就能做(不依赖常驻 hub)**

- [x] 常在线 Linux 机起临时 hub:装 Syncthing,建 `~/cc-logs/` + 权限 700 — 已完成并验证
- [ ] 各源机装 Syncthing,`~/.claude/projects/` 配 send-only → hub receive-only(按 `<hostname>` 分子目录)
- [ ] **内外网隔离硬措施**(每台,§2.1):关 globalAnnounce/relays/localAnnounce/nat;地址硬编码;GUI 绑 127.0.0.1
- [ ] Tailscale ACL:端口 22000 仅「源机↔hub」可达
- [x] scanner 多 root 改造 — 已实现(见 [01 §2](01-corpus-and-archive.md#2-处理底座scannerepisodeextractor))
- [ ] 预处理脚本(原始 JSONL → 摘要,供日报;抽取 pass 仍吃 episode)
- [ ] 归档 bug 修复(见 [01 §4.1](01-corpus-and-archive.md#41-现有部署的真-bug证据链活不过一个月))——每天都在丢证据,优先
- [ ] 部署前检查:环境无 `ANTHROPIC_API_KEY`;Console 未开 auto-reload

**B. 常驻 hub 就位后**

- [ ] 常驻 hub 加入 tailnet;`~/cc-logs/` 或 `/data/cc-logs/` + 权限 700
- [ ] 各源机 send-only peer 从临时 hub 切到常驻 hub(或过渡期两者都收)
- [ ] 定时器:macOS 用 launchd(不用 cron——睡眠/权限不可靠),Linux 用 `systemd --user` timer
- [ ] 临时 hub 退役或降级为热备

## 7. 验收标准(30 天军令状)

常驻 hub 就位 30 天内跑通三件事,否则说明瓶颈不在硬件:

1. Telegram bot 常驻(交互入口)
2. 夜间多机知识总结任务上线
3. SkillSynapse v0.1 nightly loop 跑通
