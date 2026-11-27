# 同桌猫

Describe what this plugin does and how to configure it.

## Development

The plugin source and its Git repository live at:

```text
N.E.K.O/plugin/plugins/agent_deskmate
```

插件源码及其 Git 仓库直接位于：

```text
N.E.K.O/plugin/plugins/agent_deskmate
```

プラグインのソースと Git リポジトリは次の場所にあります：

```text
N.E.K.O/plugin/plugins/agent_deskmate
```

When publishing to the plugin market, use this GitHub repository name:

发布到插件市场时，请使用以下 GitHub 仓库名：

プラグインマーケットへ公開する際は、次の GitHub リポジトリ名を使用してください：

```text
n.e.k.o_plugin_agent_deskmate
```

From this plugin repository root:

```bash
uvx ruff==0.12.4 check --ignore-noqa --config ruff.toml .
```

From the N.E.K.O repository root / 在 N.E.K.O 仓库根目录中 / N.E.K.O リポジトリのルートで：

```bash
uv run neko-plugin sync agent_deskmate --clean
uv run neko-plugin check agent_deskmate
uv run neko-plugin check -r agent_deskmate
```

Python runtime dependencies are declared in `pyproject.toml` and synced into
`vendor/` for packaging. The generated `vendor/` directory is not committed;
local builds and CI recreate it before release checks.

Python 运行时依赖声明在 `pyproject.toml` 中，并在打包时同步到 `vendor/`。
生成的 `vendor/` 不提交；本地构建和 CI 会在发布检查前重新生成它。

Python ランタイム依存関係は `pyproject.toml` に宣言し、パッケージ化時に
`vendor/` へ同期します。生成された `vendor/` はコミットせず、ローカルビルドと
CI が公開前チェックで再生成します。

## Market release / Market 发布 / Market 公開

Publish the version declared in `plugin.toml`. By default this pushes the Git
tag, waits for the standard GitHub Release, and notifies the plugin market.

发布 `plugin.toml` 中声明的版本。默认会推送 Git tag、等待标准 GitHub
Release，然后通知插件市场。

`plugin.toml` で宣言されたバージョンを公開します。既定では Git tag を
push し、標準 GitHub Release を待ってからプラグインマーケットへ通知します。

```bash
uv run neko-plugin publish agent_deskmate
```

To run only one half explicitly / 如需仅执行一部分 / 一方のみを実行する場合:

```bash
uv run neko-plugin publish github agent_deskmate
uv run neko-plugin publish market https://github.com/owner/repo/releases/tag/v0.1.0
```

The generated `.github/workflows/release.yml` builds and uploads
`agent_deskmate.neko-plugin`. The market independently verifies that Release
before publishing it.

生成的 `.github/workflows/release.yml` 会构建并上传插件包；Market 会独立验证
该 Release 后再发布。

生成された `.github/workflows/release.yml` がプラグインパッケージをビルドして
アップロードし、Market はその Release を独立検証してから公開します。

## Entry

```toml
entry = "plugin.plugins.agent_deskmate:AgentDeskmatePlugin"
```

---

# 同桌猫 · 项目状态（本项目自定义，非 init 模板）

N.E.K.O 的被动监听插件：替你看着本机的编码 Agent，在**需要审批、出错**时
按你可调的强度播报与解释，并在 N.E.K.O 里完成同意/拒绝——不必切进 Agent 界面。

slogan：**替你看着 Agent，大事问你。**

## 当前状态

**阶梯已走完（M0-lite-6），A/B/C 三批加固都已合，语音线 0.6.21 落地。**
观测轮询跑在 3 秒 timer 上，审批中继常驻（1 秒 timer），
另有 `scan_now` 只读诊断入口可手动触发一次（不播报）。
版本沿革与每版修了什么见 `08-发布归档/README.md` 的「当前归档」表——
那份是权威，这里只放状态。

> 分级只用三个名字，代码注释里可能还看得到 `L0/L1/L2`——那是内部引用，
> **给用户看的一律是下面这三个词**：只提醒 / 给建议 / 自动放行。

## 删版本控制目录：永远要人点（0.6.22）

`rm -rf .git` / `.hg` / `.svn` 是**唯一一类被单独摘出来**的删除目标，
因为档位判据对它失灵：

| 命令 | 档位 | 严重度 | 动作类别 | 结果 |
|---|---|---|---|---|
| `rm -rf .git` | normal | warn | **irreversible** | 弹卡，不自动放行、不送审 |
| `rm -rf ../.git` | extreme | critical | irreversible | 同上 |
| `rm -rf node_modules` | recoverable | notice | recoverable | 照旧可放行、可送审 |

**为什么不升档位**：`rm -rf .git` 是两难的——刚 clone 完删它是常规，
有三天未提交工作删它是灾难，规则层分不出这两者。拿【严重】去描述一件
分不出轻重的事，正是这个项目明令禁止的（B8：「不许拿【严重】冒充一个
我们并不掌握的事实」）。

**所以只关掉"没人看就过去"的那两条路**：
① 动作类别顶成 `irreversible`，L2 闸门第一道墙拦住——2026-11-27 实测，
不补这一行，`auto_decide_max_severity` 的上限允许设到 `warn`，设到那儿
`rm -rf .git` 会被**自动放行**：没有卡片，Claude 直接删掉提交历史与全部
未提交工作；② 不许送 B11 审查——模型的授权是「识别常规」，
**不是「重新定危险」**，它一句"常规"就能把通知压掉。

判据按**路径分量**匹配，不按子串：`.git` / `./.git` / `../.git` /
`foo/.git` / `.git/objects` 都算（删对象库一样毁掉整个仓库），而
`.gitignore` / `.github` / `git` / `git-backup` 都不算。

| 阶梯 | 内容 | 状态 |
|---|---|---|
| M0-lite-1 | 骨架能加载 | ✅ 官方四条验收全过，真机确认「运行中」 |
| M0-lite-2 | Claude Code 转录 tail + 网关元数据观测，统一事件 schema | ✅ 34 个单测 + 真实数据验过 |
| M0-lite-3a | L0 规则层：删除类 + 失败类，零 key 可用 | ✅ 真实数据 25 命中，0 误报 |
| M0-lite-3b | 危险写 + 停滞 | **部分**：危险写走词典（`git clean` / `drop table` / `curl \| sh` …）；**停滞未实现（计划中）** |
| M0-lite-4 | SDK 播报（`source` + `ai_behavior="blind"`） | ✅ 代码完成，真机验收通过 |
| M0-lite-5 | 审批中继（`create_card` + hook 回填 + 审计 + 显式授权） | ✅ 已上线，A 批加固完 |
| M0-lite-6 | 零 key 行为 | ✅ 无 key 只跑规则层，建议层明确提示需配 key |

阶梯之外的加固批次（**这才是现在在做的事**）：

| 批次 | 内容 | 状态 |
|---|---|---|
| A 批 | 审批可信度：完整命令贯通、建议解析严格化、中继身份校验、脱敏补全 | ✅ 已完成（`9baa529`） |
| B 批 | 闸门与观测：命令归一化、按目标分档、观测源隔离、上游爬深、清理对齐、卡片瘦身 | B1–B8 已合；**B9+B10（自动审查档）已真机开着跑**（2026-11-27 日志：`自动放行：已开启`），默认仍是关、需单独签字 |
| C 批 | 进度与出错播报（四类状态跃迁）+ B11 播报审查 + B12 渠道 | ✅ 已完成。**语音线 2026-11-27 落地**（0.6.21），见下面「Yui 什么时候开口」 |

### Yui 什么时候开口（0.6.21 起）

只有三类会念出来，其余一律只出文字：

| 事件 | 念？ | 念的那一句长什么样 |
|---|---|---|
| 一轮跑完了 | ✅ | 「claude-code 这一轮跑完了，共 7 步，主要是 Bash。」 |
| 出事而且它没自己好 / 同一步连着失败在兜圈子 | ✅ | 「claude-code 出事了，它没自己恢复。」「…在兜圈子，同一步连着失败 3 次。」 |
| 它停下来等你了 | ✅（**但今天没有事件源，见下**） | 「claude-code 在等你，它停住了。」 |
| 每一步工具调用 | ❌ | — |
| 自动放行 | ❌ | 三行通知足够，念出来是纯噪音 |

三条硬规矩：

1. **念的那一句里没有路径、没有 UUID**。2026-11-26 真机上她把整条原文
   （含 `D:\workspace\…` 与会话 id）念出来过。所以语音侧单独走
   `tracker._says()`，只要 agent 名；完整精确的文字永远在 blind 留底那条
   聊天行里——**语音是附赠，文字才是凭据**。
2. **没 key 就只出文字**（红线 3）。`respond` 要主模型接一轮。
3. **能一票闭嘴**：`[agent_deskmate.progress].voice = false`。这是唯一总开关，
   因为上面三条都 bypass 了 `broadcast_speak`（那个管"让不让猫娘接话"、
   默认关）。语音是整套里最侵入的一种，关不掉用户会连插件一起关掉。

⚠️ **「在等你」今天产不出来**：`progress._ROLE_FOR_EVENT` 只映射
`tool_use` / `turn_end` / `error` / `tool_result`，没有一条进 `waiting` 角色，
所以 `tracker._on_waiting` 只能被测试直接喂 Signal 触发。它留在语音集合里
是对的（状态机支持这个角色，接上源就不用再改语音侧），但**别在文档里
把它写成已有功能**。审批挂起时 Claude 正被卡住——那是天经地义的一个源。

### 计划中 / 还没做的（别在文档里写成已有功能）

- **`turn_end` 只解析、不播报**：事件解析是通的，但没有对应规则，所以它不会
  产生任何播报。要"一轮说完了"提醒的话得先定规则（比如合并成一条会话小结）。
- **停滞播报未实现**：配置里原来有个 `stall_minutes`，代码零读取——
  B6 已把它从 `config.example.toml` 与 `config.schema.json` 里删掉。
  要做的话是单独一轮（含"什么算停滞"的判据）。
- **`waiting` 角色没有事件源**：见上一节。状态机、语音、文案都齐了，缺的只是
  「哪类事件算"它停下来等你了"」。审批挂起是现成的一个源。
- **L2 判断后端未接**：`auto_decide` 的门控（只读白名单是其中一道条件）今天**没有后端可问**，
  所以门控通过也不会自动放行。只读白名单是门控的**一道条件，不是一条独立的放行通道**。
  2026-11-23 修（命题 3）：修复前 `cat x` / `git status` 这类最安全的命令反而一定弹卡，
  不在白名单里的倒能落到 B9 被自动放行，方向是反的；现在门控过了就落到 B9 用同一道门再判。
  模型审查档（B9/B10）代码已合但**默认关**，开启要走单独签字，
  且只有模型判「低风险且允许」时才放行——本地仍保留四道否决权
  （极高危严重度 / 极高危档位 / 词典认定的不可恢复动作 / 未签字）。
  没接后端又没开 B9 时，插件**不自己拍一个**，请求留给用户。
- **B11 播报审查已接上但有边界**：`critical` 里"只因为目标被变量/通配符藏住
  而说不清"的那一类可以送审（0.6.18），真机上压掉过；`~` / 盘根 / 系统目录 /
  工作区外 / 无界通配符**一律不送审**，直接播。没 key 时那条降级路走
  `_downgrade_unconfirmed_critical()`，对真有危险目标在里面的 critical 是恒等的。
- **多工作区仍会误报**：会话工作目录是源码树、而删除目标在文档工作区时，
  档位按"工作目录外"判 critical。`[agent_deskmate.mine].roots` **只改归因**
  （「发起者未知」→「你自己」），**不改档位**——加了照样红。正解是让档位也认
  roots，但那动的是档位判据本身；而且我已经从源头不再生成那条命令
  （备份由 `host_meta_compat.py` 自己删、commit 走 `git commit -F -` 从 stdin 读）。

## 自动放行（默认关）

自动放行**默认是关的**，打开要两次确认：先把开关打开，再在卡片上单独签字。
签字是第三个范围，与「模型建议」「低风险时自动放行」各自独立——
拿了一个不等于拿到另一个，重启也不会把已做的确认吞掉。

放行时只发一条通知，**固定三行，不加第四行**：

```text
已自动放行（低风险）
它要删除：build/
理由：构建产物，重建得回来
```

第二行说的是它到底要干什么：删除类写「它要删除：<目标>」，其余写「它要执行：<命令>」。
模型名、授权版本、「模型建议 / 让模型先审一次」这类机制说明、以及怎么关掉，
**一律不进通知**，只进审计（`audit_list` 看得见）——用户在这一刻要回答的是
"它干了什么、凭什么"，不是"这是哪一版授权"。「怎么关」只在设置页出现一次，
写成「自动放行：已开启（可关闭）」；通知里每件事都提醒一遍，
用户就会被训练成无视提示。

本地层始终留着否决权，模型再放话也过不来：开关没开、没签字、极高危严重度、
极高危档位（工作区外 / 家目录 / 盘根 / 系统目录 / 目标不可解析）、
词典已认定不可恢复的动作——五种情形一律不问模型。

模型那边也只有一种组合能放行：**低风险且允许**。判「中风险」照旧弹卡等人点，
判「高风险且拒绝」也只是把请求摆到人面前。插件**永不自动拒绝**——
拒绝必须由用户亲手点。

## 通知渠道口径：只用 chat，hud 已弃用

`push_message(visibility=[...])` 两条通道，**真机实测**结论：

| 通道 | 实测表现 | 结论 |
|---|---|---|
| `["chat"]` | 落在右下角聊天流，带来源 `agent_deskmate` + 时间，可往上翻历史 | ✅ 唯一在用 |
| `["hud"]` | 后端日志确认 `agent_notification sent to frontend`，但角色旁那行小字**始终不显示**；切到别的页面更是彻底看不见 | ❌ 弃用 |

**所以：插件所有"要让用户看见"的文案一律走 `visibility=["chat"]`，不再往 hud 发任何东西。**
`tests/test_p0_p1_p2_p3_regressions.py` 里有一条 AST 扫描盯着这件事——
源码里任何一处 `visibility` 字面量出现 `"hud"` 即测试失败，防止以后有人顺手加回去。

同理，**未决条数不推**。一次自动放行会让计数在审查的 1–2 秒里 0→1→0，
于是先播「有 1 条审批待判断」、两秒后又播「没有了」——用户看到的是假警报，
那比没提示更坏。要知道还有几条待判断，用 `status` 入口自己查。

## 观测层怎么用

```bash
# 真机上三个入口：
#   status     —— 插件自身状态（运行时长 / 阶段 / 适配器就绪）
#   scan_now   —— 立即读一次观测源 + 跑规则，只读诊断，不播报
#   dump_events—— 返回最近 N 条统一事件，确认解析对不对劲
uv run neko-plugin check agent_deskmate
```

数据源路径在 `config.example.toml` 的 `[agent_deskmate.watch]`，代码里不写死绝对路径：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `claude_projects_dir` | `~/.claude/projects` | Claude Code 转录（`**/*.jsonl`），按字节 offset 增量 tail |
| `cc_switch_db` | `~/.cc-switch/cc-switch.db` | 网关账本 `proxy_request_logs`，按 `rowid` 水位增量，**只读打开** |

### 已实测的数据特征（2026-11-21，别当新发现）

- 转录有 **17 种记录类型**，只取三种信号：`tool_use`（assistant 的 `content[]` 里
  `type=="tool_use"` 的块）、`turn_end`（`stop_reason=="end_turn"`）、
  `error`（`system` 且 `level=="error"`，实测即 `subtype=="api_error"`）。
- `Bash` 357 次远超其他工具——**L0 规则层的主要靶子**。
- 网关表 57146 行、28 列；`total_cost_usd` 是 **TEXT**，必须容错转 float。
- 网关的 `session_id` 与转录的 `sessionId` **是同一个 UUID**，两边可关联。
- 网关表**请求结束后才写入**（延迟 <1s，无 in-flight）→ 只能做事后账本。

### 踩过的坑（测试盯着，别改回去）

- **offset 必须按字节算**：文本模式读在 Windows 上把 `\r\n` 归一成 `\n`，
  offset 每次都漂，几轮后从半行中间开始读。已改二进制读，测试盯着。
- **网关库必须 `mode=ro` 打开**：写模式会冒 `-wal`/`-shm`，等于改别人的数据库。
  测试会断言这些文件不出现。

## 三条红线

1. 播报只能走 `push_message(source=..., visibility=[...], ai_behavior=...)`；
   `source` 必须显式传，纯播报必须 `ai_behavior="blind"`（默认 `"respond"` 会让角色开口）。
2. 审批交互只能走 `create_card` + `data-neko-action`；`push_message` 的 parts 没有按钮。
3. 零 key 只跑「只提醒」这一档；「给建议」与「自动放行」刚需 key，
   没 key 必须明确提示，不许假装成功。

> 这三档在用户可见的文案里就叫**只提醒 / 给建议 / 自动放行**。
> 代码与注释里的 `L0/L1/L2` 是内部引用，不许出现在卡片、播报、审计里
> （`commands.BANNED_INTERNAL` 盯着，测试拿 `first_banned` 自检）。

## 宿主硬约束（详见 AGENTS.md）

只 import `plugin.sdk.plugin`，不碰 `utils.*` / `plugin.core` / `plugin.sdk.shared`；
不用 `print`；不硬编码路径；`startup` 快进快出；**永不 `await push_message`**
（它入队后就会抛 `TypeError`，重试等于发两条）。

`passive = true` 会让 Agent 看不到本插件（决策 D2：只监听，不参与任务分派）；
它不阻止进程启动，也不阻止播报——`push_message` 是出口，不是入口。

## 设置表单（config.schema.json）

`config.schema.json` 描述插件管理器里那张设置表单：每个字段的中英文标题、
一句话说明、类型、取值范围与默认值，密钥字段标 `writeOnly`（永不回显）。

它**只影响表单展示，不是校验边界**——真正的兜底全在代码里
（`_to_bool` / `_to_number` / `effective_max_severity` 等运行时函数）。
手改配置文件的用户根本不过这张表单，所以坏值、缺值、越界值都必须由代码接住。
`tests/test_config_schema.py` 盯着：schema 与 `config.example.toml` 字段一一对应、
enum 取值与代码常量一致、给用户看的文字里不出现配置键名。
