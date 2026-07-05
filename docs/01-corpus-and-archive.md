# 01 · 语料底座与归档

> 底座文档。定义所有读者(04/05/06)共用的**语料是什么**、**怎么切成可处理单元**、
> 以及**为什么原始 JSONL 必须当不可重建资产归档**。传输与安全见 [02](02-transport-and-security.md);
> 共享的挡位/聚合原语见 [03](03-shared-primitives.md)。

## 1. 语料:明文 JSONL,直接读文件

Claude Code 会话记录明文落盘,无需任何 API:

```
~/.claude/projects/<project-path-encoded>/<session-id>.jsonl
```

每会话一个 JSONL,每行一个事件(user / assistant / tool_use / tool_result / system),带时间戳。
这是整个生态**唯一的一手经验流**——三种记忆(程序/情景/语义,见 [06 §1](06-worklog-and-notes.md))全从它蒸馏。

## 2. 处理底座:scanner → episode → extractor

现有 v0.1 管线,是所有读者的公共前段:

```
scanner.py 扫 ~/.claude/projects/**/*.jsonl
   → episode_detector 切片(一段连贯工作 = 一个 episode)
   → extractor(LLM 抽取 pass)
   → store(SQLite) / indexer 渲染
```

- **episode 是公共处理单元**:三环、WorkLog、Notes 都以 episode 为粒度读语料。抽取 pass 将来合流成
  「一次 LLM 调用、多出口」(见 [06 §2.1](06-worklog-and-notes.md));初期各读者可先独立跑,不动已有管线。
- **多机扫描(已实现,2026-07-03)**:配置项 `paths.aggregation_root`(hub 上设为 `~/cc-logs`),
  `scanner.scan_roots()` 自动把它下面每个子目录枚举成一台机器的 root(hostname = 目录名),
  **新机器 onboard 无需改配置**。`SessionMeta.hostname` 由所属 root 注入,不从路径反推。
  单机模式(`aggregation_root: null`)行为不变。改动落在 `config_default.yaml` / `config.py` /
  `models.py` / `scanner.py` / `main.py`,已用真实目录验证多机扫描 + hostname 归属 + subagents 排除。

> ⚠️ **不要**把多机 root 设成单一聚合根 `~/cc-logs/`:`scanner._derive_project()` 会把 `rel.parts[0]`
> 取成 hostname 而非 project——不抛异常,而是所有会话 project 静默退化成机器名,下游归组全乱。
> 必须让每个 root = `.../<hostname>/`(现方案即如此)。

## 3. 预处理:喂 LLM 前先压缩

原始 JSONL 带全部工具调用输出,极其啰嗦,直接喂 Claude 会白烧几十万 token。凡走 LLM 的环节
(知识日报、抽取 pass)先做纯本地预处理:找出过去窗口有更新的 JSONL、抽 user 消息 + 关键 assistant 回复、
剔除冗长 tool_result 原文、按 hostname/project 归组。**注意与 SkillSynapse 抽取的区别**:抽取 pass
仍吃相对完整的 episode(见 [06 §2](06-worklog-and-notes.md));只有「日报总结」这类 roll-up 吃高度压缩的 brief。

喂给 LLM 的 brief 与产出的 skill 都必须先过 `sanitizer.scrub()` 脱敏(凭据类→`<REDACTED>`),
这是[安全红线](02-transport-and-security.md#安全内外网隔离)的唯一对外通道约束。

## 4. 归档:JSONL 升格为唯一不可重建资产

中间层(工作事件库、skill、Notes、台账)全可从原料重跑——schema 错了全量重建即可,是物化视图性质;
**原料没了就是没了**。所以原始 JSONL 从「碰巧存在的临时日志」升格为「知识库地基」,需要归档纪律。

### 4.1 现有部署的真 bug:证据链活不过一个月

Claude Code `cleanupPeriodDays`(默认 30 天)清理 `~/.claude/projects` 旧会话;Syncthing
send-only→receive-only 拓扑挡住 hub 改动回流,**但挡不住源机删除的传播**——源机 CC 清掉旧 JSONL,
hub 跟着没。季度回看三个月,[06 的 evidence 指针](06-worklog-and-notes.md#3-读取逐级指针下钻)第 31 天起 404。
`deploy/syncthing/README.md` 目前未处理。**两层修法都做**:

- 各源机 settings 调大 `cleanupPeriodDays`(治标,缩小「未同步先被删」窗口);
- hub 把 Syncthing 同步目录当**着陆区**,夜间归档步骤把新增/变更 JSONL 搬进只增不删的 `cc-archive/`,
  **所有指针一律指向归档区**。比 Syncthing `ignoreDelete` 干净(官方不推荐,造成永久 out-of-sync)。

### 4.2 归档区纪律

- **只增不删**;进备份(唯一不可重建资产)。
- 指针地址格式固定:`<hostname>/<project-dir>/<session-id>.jsonl#L120-L340`——[06](06-worklog-and-notes.md) 的下钻靠它。
- 体量无虞:JSONL 压缩比高,zstd 后一年几个 GB;冷段压缩存放,解引用时透明解压。

> 归档只是 hub 的四大职责之一(归档/蒸馏/索引/分发),hub 完整角色与常驻循环见 [02 §hub](02-transport-and-security.md#hub-知识库管理者)。
> 归档区、蒸馏运行时、下钻解引用**同机**(mini 到货前 = 临时 hub),摸原文零成本;
> 证据链天生私有、天生不出 tailnet,与[安全红线](02-transport-and-security.md#安全内外网隔离)严丝合缝,无需新增机制。
