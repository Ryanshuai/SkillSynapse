# 这台机器是 mac-mini —— 车队的 hub

**你现在就跑在它上面。不要试图 ssh 到 mac-mini,你已经在这儿了。**

这份文件是**这台机器私有的**,不进 git、不被 syncthing 同步。三台共用的那份在
`~/.claude/CLAUDE.md`(软链到 `code/claude-config/CLAUDE.md`)—— 机器私事不写进那里,
否则会灌到另外两台上去。

## 车队

| 机器 | 是什么 | 怎么到 |
|---|---|---|
| **mac-mini**(本机) | 常开的 hub。服务、语料汇总、skill 合并都在这 | — |
| **home-desktop** | Windows 主力机。`pubg_derecoil` 锁死在这台(要抓屏 + Pico 控鼠标) | `ssh home-desktop` |
| **company-laptop** | Linux 笔记本。语料的大头(2800+ 会话) | `ssh company-laptop` |

tailnet 是 `tail1a4a56.ts.net`,MagicDNS 直接用短名。

## 本机跑着什么

| 服务 | 端口 | 对外路径 | launchd label |
|---|---|---|---|
| code-server(VS Code + Claude Code 扩展) | 8080 | `https://mac-mini…/` | `net.skillsynapse.codeserver` |
| Homepage(首页) | 3000 | `https://mac-mini…:8443/` | `net.skillsynapse.homepage` |
| ask(首页底部那一行问答条的后端) | 7788 | `…:8443/ask` | `net.skillsynapse.ask` |
| syncthing GUI | 8384 | `https://mac-mini…/st/` | 见 `homebrew.mxcl.*` |
| ttyd(裸 shell) | 7681 | `https://mac-mini…/term/` | `net.skillsynapse.ttyd` |
| skill 合并 | — | 无 UI | `net.skillsynapse.merge` / `.merge.scan` |
| 书签+图标刷新(5 分钟一次) | — | 无 UI | `net.skillsynapse.bookmarks` |

`sudo` 是 **NOPASSWD**,所以 `sudo -n launchctl …` 在脚本里能直接用。

## 东西放在哪

```
~/code/                     项目(SkillSynapse / claude-config / homepage)
~/agent_space/              agent 的工作区(HAClaw 等)
~/cc-logs/<host>/           三台机器上行的对话语料;mac-mini 那格是指向 ~/.claude/projects 的软链
~/cc-icons/<host>/          别的机器送来的项目图标(图 + sidecar)
~/.claude/skillsynapse/     状态:icons.json、db.sqlite、logs/merges.jsonl
~/.config/haclaw/secrets.env  凭据(600)。**只取需要的那一个键,不要 source 整个文件**
```

## 几条只有这台机器上才会咬人的事

- **`~/.claude/skills` 是软链**,指向 `code/claude-config/skills`。数东西的时候两个根会
  重复计数 —— `find` 不跟软链、`rglob` 跟,两个答案都不对。按 `resolve()` 之后去重。
- **Homepage 的 `public/` 在服务启动时被扫成一张表**,之后新增的文件永远 404。加了
  静态文件要 `sudo -n launchctl kickstart -k system/net.skillsynapse.homepage`。
  改文件内容不用重启,**新增路径才要**。
- **Homepage 的图标只认 `http` / `/` / `mdi-`/`si-`/`sh-` 前缀。** `data:` URI 会被当成
  图标名拼进 jsdelivr 的 URL,404 成一排破图。
- **`sips --cropOffset` 的原点不是左上角**(文档没说是什么)。裁图用 `ffmpeg -vf crop=w:h:x:y`。
- Homepage 改配置不用重启,`config/*.yaml`、`custom.css`、`custom.js` 都是每次请求现读。

## 回答风格

这台机器上的 agent 调用大多来自**自动化**(合并 daemon、选图、首页那一行问答),不是
交互式对话。**直接给结果,不要反问,不要罗列可选项。**
