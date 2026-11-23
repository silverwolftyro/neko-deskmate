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

**阶梯已走完（M0-lite-6），现在在 A/B 两批加固里。** 观测轮询跑在 3 秒 timer 上，
审批中继常驻（1 秒 timer），另有 `scan_now` 只读诊断入口可手动触发一次（不播报）。

> 分级只用三个名字，代码注释里可能还看得到 `L0/L1/L2`——那是内部引用，
> **给用户看的一律是下面这三个词**：只提醒 / 给建议 / 自动放行。

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
| B 批 | 闸门与观测：命令归一化、按目标分档、观测源隔离、上游爬深、清理对齐、卡片瘦身 | B1–B8 已合；**B9+B10（自动审查档）代码已合，默认关、需单独签字，真机未开** |

### 计划中 / 还没做的（别在文档里写成已有功能）

- **`turn_end` 只解析、不播报**：事件解析是通的，但没有对应规则，所以它不会
  产生任何播报。要"一轮说完了"提醒的话得先定规则（比如合并成一条会话小结）。
- **停滞播报未实现**：配置里原来有个 `stall_minutes`，代码零读取——
  B6 已把它从 `config.example.toml` 与 `config.schema.json` 里删掉。
  要做的话是单独一轮（含"什么算停滞"的判据）。
- **L2 判断后端未接**：`auto_decide` 的门控（只读白名单是其中一道条件）今天**没有后端可问**，
  所以门控通过也不会自动放行。只读白名单是门控的**一道条件，不是一条独立的放行通道**。
  2026-11-23 修（命题 3）：修复前 `cat x` / `git status` 这类最安全的命令反而一定弹卡，
  不在白名单里的倒能落到 B9 被自动放行，方向是反的；现在门控过了就落到 B9 用同一道门再判。
  模型审查档（B9/B10）代码已合但**默认关**，开启要走单独签字，
  且只有模型判「低风险且允许」时才放行——本地仍保留四道否决权
  （极高危严重度 / 极高危档位 / 词典认定的不可恢复动作 / 未签字）。
  没接后端又没开 B9 时，插件**不自己拍一个**，请求留给用户。

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
