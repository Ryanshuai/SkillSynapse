# SkillSynapse

夜间 cron:从 Claude Code 的会话日志里蒸馏出可复用的 skill。

你每天和 Claude Code 干的活里,有一部分是**可复用的流程** —— 但它们只以「聊天记录」的形式
躺在 `~/.claude/projects/**/*.jsonl` 里,下次还得重讲一遍。SkillSynapse 每晚扫这些日志,
把值得沉淀的那些抽成 `~/.claude/skills/<name>/SKILL.md`,让 Claude Code 下次自己就会。

---

## ⚠️ 先读这一条:原始会话日志是机密数据

**这个工具读的是你完整的对话原文与工具调用记录** —— 里面可能有账号密码、内部代码、
客户数据。仓库的第一红线是:

> **原始 session JSONL 绝不允许离开内网**(Tailscale mesh / 局域网)。
> 唯一被允许的对外通道,是 `src/skillsynapse/sanitizer.py` 的 `scrub()` 脱敏之后的
> session brief 与 skill 文件。

具体禁止项(不 commit、不贴 issue、不上传公网服务、Syncthing 必须关全部公网通路)
写在 [CLAUDE.md](CLAUDE.md),动手前请完整读一遍。测试用的 JSONL **一律手工构造合成数据**。

> **多人使用时还有一层没定的问题**:每个人的会话日志属于他自己。这个仓库现在的传输
> 设计([docs/02](docs/02-transport-and-security.md))是「多台机器 → 一个 hub」的
> **单人多机**模型,不是「多人共享」模型。要在团队里跑之前,先回答:谁的日志汇到哪、
> 谁能读。这个问题仓库里还没有答案。

---

## 现在能跑什么

| | 状态 |
|---|---|
| **归纳环**(会话 → episode 切片 → LLM 抽取 → 落 SKILL.md → 渲染索引) | ✅ v0.1 可用 |
| 脱敏 `scrub()`(session brief + skill 落盘双重) | ✅ 已接入 |
| headless 历史隔离(独立 `CLAUDE_CONFIG_DIR`,不污染你真实的 CC 历史) | ✅ 已接入 |
| 多机汇聚(Syncthing over Tailscale,单向 send-only → hub) | ✅ 实测打通 |
| skill 去重/合并(`consolidator.py`) | ⚠️ 代码在,**没有任何入口**,从未执行过 |
| 定向环 / 自动化环 / 标记信号 / 排序分诊 / prune | 📐 只有设计文档,无实现 |

**当前最大的缺口**:产出的 skill 一律平权直接生效,库只进不出 —— 没有优先级排序,
也没有人工闸门。设计见 [docs/07](docs/07-triage-and-ranking.md)。

## 跑起来

```bash
pixi install
pixi run python -m unittest discover -s tests     # 31 个 sanitizer 测试

skill list          # 现有 skill
skill show <name>   # 单个 skill 的正文 + 指标
skill health        # 库整体健康度

skillsynapse --dry-run        # 不调 LLM、不写 SKILL.md
skillsynapse --hours-back 48  # 正式跑一晚
```

⚠️ **环境里绝不能有 `ANTHROPIC_API_KEY`** —— 存在即绕过订阅、切成按量计费。

⚠️ `--dry-run` **不是只读**:Step 0 的 manual skill 发现与 Step 2 的 metrics 采集在
dry_run 判断之前就会写 `~/.claude/skillsynapse/db.sqlite`。要完全无副作用地试,
把 `paths.*` 指到临时目录(见 [docs/README.md](docs/README.md) 的「测试与可验证性」)。

数据目录:`~/.claude/skillsynapse/`(`db.sqlite` / `logs/` / `config.yaml`)。

## 代码地图

```
src/skillsynapse/
  main.py            夜间 pipeline 编排(cron 入口)
  scanner.py         扫 .jsonl、解析成事件流
  episode_detector.py  把一个会话切成若干段连贯工作
  extractor.py       每段过一次 LLM,判 NEW / UPDATE / PITFALL / SKIP
  indexer.py         写 SKILL.md + 渲染 _index.md / _categories.md
  metrics.py         回读会话,统计 skill 被选中/用完的比例
  store.py           SQLite + decisions.jsonl 审计
  sanitizer.py       scrub() —— 唯一的对外脱敏闸门
  consolidator.py    去重/合并(未接入入口)
deploy/syncthing/    多机汇聚部署(每台幂等跑一次)
docs/                设计文档,入口 docs/README.md
```

## 已知的坑

- **16 个模块里只有 1 个有测试**(只有 `sanitizer.py`)。改任何别的模块都没有验证下限,
  动手前先补测试,或者用临时目录沙箱手工比对前后输出。
- `consolidator.py` 有 448 行完整实现但没有 CLI 入口,`decisions.jsonl` 里也从没有过
  它的执行记录。别照着它扩展,先确认它到底要不要。

## 设计文档

从 [docs/README.md](docs/README.md) 进 —— 那里有全景图、术语表和阅读顺序。
