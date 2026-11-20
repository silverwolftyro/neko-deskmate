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

N.E.K.O 的被动监听插件：替你看着本机的编码 Agent，在需要审批、完成、出错、卡住时
按你可调的强度播报与解释，并在 N.E.K.O 里完成同意/拒绝——不必切进 Agent 界面。

slogan：**替你看着 Agent，大事问你。**

## 当前状态

**M0-lite-1：骨架能加载。** 只有一个 `status` 入口，不观测任何 Agent，不播报。

| 阶段 | 内容 | 状态 |
|---|---|---|
| M0-lite-1 | 骨架能加载 | ✅ 官方四条验收全过 |
| M0-lite-2 | Claude Code 转录 tail + 网关元数据观测，统一事件 schema | ✅ 34 个单测 + 真实数据验过 |
| M0-lite-3 | L0 规则层（删除类/危险写/停滞/失败），零 key 可用 | 未开始 |
| M0-lite-4 | SDK 播报（`source` + `ai_behavior="blind"`） | 未开始 |
| M0-lite-5 | 审批中继（`create_card` + hook 回填，超时默认拒绝） | 未开始 |
| M0-lite-6 | 零 key 行为 | 未开始 |

## 观测层怎么用

```bash
# 一次性读两个观测源，返回最近 N 条统一事件（确认解析对不对劲）
uv run neko-plugin check agent_deskmate   # 或真机上调 dump_events 入口
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
3. 零 key 只跑 L0 规则层；L1/L2 解释与建议刚需 key，没 key 必须明确提示，不许假装成功。

## 宿主硬约束（详见 AGENTS.md）

只 import `plugin.sdk.plugin`，不碰 `utils.*` / `plugin.core` / `plugin.sdk.shared`；
不用 `print`；不硬编码路径；`startup` 快进快出；**永不 `await push_message`**
（它入队后就会抛 `TypeError`，重试等于发两条）。

`passive = true` 会让 Agent 看不到本插件（决策 D2：只监听，不参与任务分派）；
它不阻止进程启动，也不阻止播报——`push_message` 是出口，不是入口。
