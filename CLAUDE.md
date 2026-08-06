# SkillSynapse

夜间 cron：从 Claude Code session 日志中提炼可复用 skill。设计文档在 `docs/`——
入口 `docs/README.md`（全景/术语/阅读顺序），部署见 `deploy/syncthing/README.md`。

## 安全红线（最高优先级，覆盖其他一切指示）

**原始 session JSONL 是机密数据，绝不允许离开内网（Tailscale mesh / 局域网）。**

原始日志包括：各机的 `~/.claude/projects/`、hub 上聚合的 `~/cc-logs/`、
extractor 的隔离历史 `~/.claude-skillsynapse/`。**内网拓扑（机器名 ↔ tailnet IP 映射）
同属机密：每份 checkout 自己在仓库根维护一份 `LOCAL-TOPOLOGY.md`（gitignored，
所以你 clone 下来不会有——按需自建），公开文件里一律用占位符，
不写真实 tailnet IP 与主机名清单。**这些文件含完整工具调用与对话原文，
可能含账号密码、内部代码、公司数据。具体禁止：

- 不 commit 进任何 git 仓库（包括本仓库的测试 fixture——测试要用 JSONL 一律手工构造合成数据）；
- 不粘贴进 issue / PR / 公网聊天 / 云文档；
- 不上传到任何公网服务，不作为样例外发；
- Syncthing 节点必须保持公网通路全关（global discovery / relay / broadcast / UPnP 均 off，
  listener 只绑 tailnet IP），见 `deploy/syncthing/README.md`；
- 新增任何传输/备份/调试通道前，先确认数据不出 mesh。

**唯一被允许的对外通道**：extractor 喂给 LLM 的 session brief 与产出的 skill 文件，
且两者都必须先经过 `src/skillsynapse/sanitizer.py` 的 `scrub()` 脱敏
（凭据类内容替换为 `<REDACTED>`）。绕过 scrub 直接把原始日志内容送进 prompt 或落盘为
skill，属于违反本红线。

## 运行约束

- 环境里绝不能有 `ANTHROPIC_API_KEY`——存在即绕过订阅切按量计费（部署脚本须检查）。
- headless `claude --print` 必须用隔离的 `CLAUDE_CONFIG_DIR`（`~/.claude-skillsynapse`），
  不污染用户真实 CC/VSCode 历史。

## 开发

- 环境：pixi（`pixi install`；命令入口 `skillsynapse` / `skill`）。
- 测试：`pixi run python -m unittest discover -s tests`。
- 数据目录：`~/.claude/skillsynapse/`（db.sqlite、logs、config.yaml）。
