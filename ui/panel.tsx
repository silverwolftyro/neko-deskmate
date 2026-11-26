import {
  ActionButton,
  Alert,
  Button,
  Card,
  EmptyState,
  Field,
  Grid,
  Inline,
  Page,
  Stack,
  StatCard,
  StatusBadge,
  Tabs,
  Text,
  Textarea,
  useConfirm,
  useLocalState,
  useToast,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps, Tone } from "@neko/plugin-ui"

type HookState = {
  state?: string
  tone?: string
  title?: string
  detail?: string
  action?: string | null
}

type AttentionItem = {
  ts?: number
  tone?: string
  title?: string
  detail?: string
  action?: string | null
}

type LogItem = { ts?: number; tone?: string; text?: string }

type SessionRow = {
  agent?: string
  short?: string
  workspace?: string
  light?: string
  rounds?: number
  last_event_at?: number
}

/** 发现层的一个 Agent。字段与 ``adapters/discovery.py`` 的 ``as_dict`` 对齐。 */
type AgentRow = {
  badge?: string
  label?: string
  detail?: string
  state?: string
  session_total?: number
  session_live?: number
  sessions?: SessionGlance[]
}

/** 一个会话的一瞥。字段与 ``adapters/sessions.py`` 的 ``as_dict`` 对齐。 */
type SessionGlance = {
  session?: string
  last_seen?: number
  size_bytes?: number
  where?: string
  live?: boolean
}

/** 徽标 key → 色调。key 来自 Python 的 ``AgentProbe.badge``：
 *  observable 时按观测深度细分（full / metadata），其余直接用 state。 */
const AGENT_TONE: Record<string, Tone> = {
  full: "success",
  metadata: "info",
  conditional: "warning",
  feasible: "info",
  blind: "default",
}

function agentTone(badge?: string): Tone {
  return AGENT_TONE[badge || ""] || "default"
}

function sizeText(bytes?: number): string {
  const n = bytes || 0
  if (n <= 0) return ""
  if (n < 1024) return n + " B"
  if (n < 1024 * 1024) return (n / 1024).toFixed(0) + " KB"
  return (n / 1048576).toFixed(1) + " MB"
}

type DeskmateState = {
  running?: boolean
  hook?: HookState
  roots?: string[]
  api_key_set?: boolean
  auto_review?: {
    enabled?: boolean
    signed?: boolean
    advice_signed?: boolean
    auto_signed?: boolean
  }
  explain_verbosity?: string
  warning_emphasis?: string
  warning_first_n?: number
  // 播报方式。``speak_available`` 由后端算好——没配 key 时 respond 走不了，
  // 前端不该自己去推这个结论（推错了就是"能点、点了不生效"）。
  speak?: boolean
  speak_available?: boolean
  // B11：播报审查。``broadcast_review_available`` 同 speak_available 的理由——
  // 后端算好，前端不自己推。已经开着的一直显示可用（用户开着时把 key 删了，
  // 控件不该当场变灰，那会让人以为设置丢了）。
  broadcast_review?: boolean
  broadcast_review_available?: boolean
  broadcast_review_stats?: Record<string, number>
  session_count?: number
  last_broadcast_at?: number
  sessions?: SessionRow[]
  attention?: AttentionItem[]
  recent?: LogItem[]
  agents?: AgentRow[]
  agent_summary?: Record<string, number>
}

const TONE_OF: Record<string, Tone> = {
  success: "success",
  warning: "warning",
  danger: "danger",
  info: "info",
  default: "default",
}

function toneOf(value?: string): Tone {
  return TONE_OF[value || ""] || "default"
}

function whenText(ts?: number): string {
  if (!ts) return "—"
  const d = new Date(ts * 1000)
  const pad = (n: number) => (n < 10 ? "0" + n : "" + n)
  return (
    d.getFullYear() +
    "-" +
    pad(d.getMonth() + 1) +
    "-" +
    pad(d.getDate()) +
    " " +
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes())
  )
}

export default function DeskmatePanel(props: PluginSurfaceProps<DeskmateState>) {
  const t = props.t
  const state = props.state || {}
  const running = !!state.running
  const hook = state.hook || {}
  const attention = state.attention || []
  const sessions = state.sessions || []
  const recent = state.recent || []
  const roots = state.roots || []
  const agents = state.agents || []
  const summary = state.agent_summary || {}
  // 磁盘上真实存在的会话数（绕开事件流水线单独盘的）。
  // 与上面的 ``sessions``（流水线读到的）是两回事，空状态文案要靠它区分。
  const diskSessions = agents.reduce(
    (sum: number, a: AgentRow) => sum + (a.session_total || 0), 0)
  const autoReview = state.auto_review || {}

  const toast = useToast()
  const confirm = useConfirm()
  const [rootsDraft, setRootsDraft] = useLocalState(
    "roots_draft",
    roots.join("\n"),
  )
  const [savingRoots, setSavingRoots] = useLocalState("saving_roots", false)
  const [savingSpeak, setSavingSpeak] = useLocalState("saving_speak", false)
  const [savingReview, setSavingReview] = useLocalState("saving_review", false)
  const [speakDraft, setSpeakDraft] = useLocalState("speak_draft", "")
  const [speaking, setSpeaking] = useLocalState("speaking", false)
  // Tabs 的 activeId 是受控的：状态页的「去声明」要能把它切到设置页。
  // （原来用 querySelector 猜 data-neko-tab，Tabs 没这个属性，点了没反应。）
  const [tab, setTab] = useLocalState("tabs:deskmate", "status")

  const hasAction = (id: string): boolean =>
    (props.actions || []).some(
      (a: any) => a.id === id || a.entry_id === id,
    )

  const onInstallHook = async () => {
    const ok = await confirm({
      title: t("panel.action.install", { defaultValue: "装配" }) + " hook",
      message:
        "会写入 Claude Code 的 ~/.claude/settings.json（写之前先备份），" +
        "并重启 Claude Code 后生效。插件没启动时中继输出空对象，" +
        "Claude Code 回退原生权限弹窗，不会卡住你。",
      confirmLabel: t("panel.action.install", { defaultValue: "装配" }),
    })
    if (!ok) return
    try {
      const res = await props.api.call(
        "hook_install",
        {},
        { userInitiated: true },
      )
      await props.api.refresh()
      const msg =
        res && typeof res === "object" && "message" in res
          ? String((res as any).message)
          : "已装配。重启 Claude Code 后生效。"
      toast.success(msg)
    } catch (err: any) {
      toast.error(installErrorText(err, running))
    }
  }

  const onSaveRoots = async () => {
    const list = rootsDraft
      .split("\n")
      .map((s: string) => s.trim())
      .filter((s: string) => s.length > 0)
    setSavingRoots(true)
    try {
      await props.api.call(
        "settings_set_roots",
        { roots: list },
        { userInitiated: true },
      )
      await props.api.refresh()
      toast.success(t("panel.settings.saved", { defaultValue: "已保存" }))
    } catch (err: any) {
      toast.error(String(err && err.message ? err.message : err))
    } finally {
      setSavingRoots(false)
    }
  }

  const onSetBroadcastReview = async (want: boolean) => {
    setSavingReview(true)
    try {
      await props.api.call(
        "settings_set_broadcast_review",
        { enabled: want },
        { userInitiated: true },
      )
      await props.api.refresh()
      toast.success(t("panel.settings.saved", { defaultValue: "已保存" }))
    } catch (err: any) {
      toast.error(String(err && err.message ? err.message : err))
    } finally {
      setSavingReview(false)
    }
  }

  const onSetSpeak = async (want: boolean) => {    setSavingSpeak(true)
    try {
      await props.api.call(
        "settings_set_speak",
        { speak: want },
        { userInitiated: true },
      )
      await props.api.refresh()
      toast.success(t("panel.settings.saved", { defaultValue: "已保存" }))
    } catch (err: any) {
      toast.error(String(err && err.message ? err.message : err))
    } finally {
      setSavingSpeak(false)
    }
  }

  const onSpeakTest = async () => {
    const body = speakDraft.trim()
    if (!body) {
      toast.error(t("panel.settings.speak_test_empty", {
        defaultValue: "先写一句要她念的话。",
      }))
      return
    }
    setSpeaking(true)
    try {
      const res = await props.api.call(
        "speak_test",
        { text: body },
        { userInitiated: true },
      )
      const msg =
        res && typeof res === "object" && "message" in res
          ? String((res as any).message)
          : t("panel.settings.speak_test_done", { defaultValue: "发了。" })
      toast.success(msg)
    } catch (err: any) {
      toast.error(String(err && err.message ? err.message : err))
    } finally {
      setSpeaking(false)
    }
  }

  // ── 状态页 ─────────────────────────────────────────────────────────────
  const statusStrip = (
    <Alert
      tone={running ? "success" : "danger"}
      message={
        running
          ? t("panel.status.running", { defaultValue: "观测中" }) +
            " · " +
            (state.session_count || 0) +
            " 个会话"
          : t("panel.status.stopped", { defaultValue: "已停止" })
      }
    />
  )

  const hookTone: Tone = hook.state === "ok" ? "success" : toneOf(hook.tone)
  const cards = (
    <Grid cols={2}>
      <StatCard
        label={t("panel.card.approval", { defaultValue: "审批功能" })}
        value={
          <StatusBadge
            tone={hookTone}
            label={
              hook.state === "ok"
                ? t("panel.hook.ok", { defaultValue: "已装配" })
                : t("panel.hook.broken", { defaultValue: "需要装配" })
            }
          />
        }
      />
      <StatCard
        label={t("panel.card.roots", { defaultValue: "高危归因" })}
        value={
          <StatusBadge
            tone={roots.length > 0 ? "success" : "warning"}
            label={
              roots.length > 0
                ? "已声明 " + roots.length + " 个目录"
                : "未声明"
            }
          />
        }
      />
      <StatCard
        label={t("panel.card.sessions", { defaultValue: "观测会话" })}
        value={String(state.session_count || 0)}
      />
      <StatCard
        label={t("panel.card.last_broadcast", { defaultValue: "最近播报" })}
        value={whenText(state.last_broadcast_at)}
      />
    </Grid>
  )

  const actionRow = (
    <Inline gap="sm">
      {hasAction("hook_install") && hook.state !== "ok" ? (
        <Button tone="primary" onClick={onInstallHook} disabled={!running}>
          {t("panel.action.install", { defaultValue: "装配" })} hook
        </Button>
      ) : null}
      {roots.length === 0 ? (
        <Button
          tone="primary"
          onClick={() => {
            setTab("settings")
          }}
        >
          {t("panel.action.declare", { defaultValue: "去声明" })}
        </Button>
      ) : null}
      <RefreshButton
        label={t("panel.action.refresh", { defaultValue: "刷新" })}
      />
    </Inline>
  )

  const statusTab = (
    <Stack gap="md">
      {statusStrip}
      {cards}
      {actionRow}
      {agents.length > 0 ? (
        <Card title={t("panel.card.agents", { defaultValue: "本机 Agent" })}>
          <Stack gap="sm">
            <Text>
              {t("panel.card.agents_hint", {
                defaultValue:
                  "只列目录在不在，不读里面任何内容。「暂无数据源」不是坏，是还没接。",
              })}
            </Text>
            <Text>
              {t("panel.agents.summary", {
                defaultValue:
                  "共 {total} 个：完整观测 {full}，仅请求记录 {meta}，有条件 {cond}，数据在本机 {fea}，暂无数据源 {blind}",
                total: String(agents.length),
                full: String(summary.full || 0),
                meta: String(summary.metadata || 0),
                cond: String(summary.conditional || 0),
                fea: String(summary.feasible || 0),
                blind: String(summary.blind || 0),
              })}
            </Text>
            {agents.map((a: AgentRow, i: number) => (
              <Stack key={i} gap="sm">
                <Inline gap="sm">
                  <StatusBadge
                    tone={agentTone(a.badge)}
                    label={t("panel.agents.badge." + (a.badge || "unknown"), {
                      defaultValue: "状态未知",
                    })}
                  />
                  <Text>{a.label || "agent"}</Text>
                </Inline>
                {a.detail ? <Text>{a.detail}</Text> : null}
                {a.session_total ? (
                  <Text>
                    {t("panel.agents.session_count", {
                      defaultValue: "{live} / {total} 个会话最近有活动",
                      live: String(a.session_live || 0),
                      total: String(a.session_total || 0),
                    })}
                  </Text>
                ) : null}
                {(a.sessions || []).map((s: SessionGlance, j: number) => (
                  <Inline key={j} gap="sm">
                    <StatusBadge
                      tone={s.live ? "success" : "default"}
                      label={s.live
                        ? t("panel.agents.session.live", {
                            defaultValue: "有活动",
                          })
                        : t("panel.agents.session.idle", {
                            defaultValue: "静默",
                          })}
                    />
                    <Text>{s.session ? s.session.slice(0, 8) : "—"}</Text>
                    <Text>{whenText(s.last_seen)}</Text>
                    <Text>{sizeText(s.size_bytes)}</Text>
                    <Text>{s.where || ""}</Text>
                  </Inline>
                ))}
              </Stack>
            ))}
          </Stack>
        </Card>
      ) : null}
      <Text>
        {t("panel.status.idle_hint", {
          defaultValue: "不响是正常的——它只在你需要决定时开口。",
        })}
      </Text>
      {sessions.length === 0 ? (
        /* 「还没看到任何会话」这句话原来的意思是"插件启动后没来新事件"，
           而用户读成的是"本机没有会话"——2026-11-25 真机：用户开着
           Claude Code 在跑，面板写这一句，直接被问"没在跑？"。
           所以这里必须分两种情况说，而且**磁盘上有会话时不许用前一种文案**。 */
        diskSessions > 0 ? (
          <EmptyState
            title={t("panel.status.no_event_session", {
              defaultValue: "磁盘上有 {n} 个会话，但插件还没读到它们的事件",
              n: String(diskSessions),
            })}
            description={t("panel.status.no_event_session_hint", {
              defaultValue:
                "上面「本机 Agent」里能看到它们。插件启动前就开始的会话按设计不回溯历史，"
                + "要等它产生新事件才会进观测——这是故意的，不是坏了。",
            })}
          />
        ) : (
          <EmptyState
            title={t("panel.status.no_session", {
              defaultValue: "还没看到任何会话",
            })}
            description={t("panel.status.no_session_hint", {
              defaultValue: "去跑一个 agent，它就会出现在这里。",
            })}
          />
        )
      ) : (
        <Card title={t("panel.card.sessions", { defaultValue: "观测会话" })}>
          <Stack gap="sm">
            {sessions.map((s: SessionRow, i: number) => {
              const lightTone: Tone = s.light === "idle" ? "default" : "info"
              return (
              <Inline key={i} gap="sm">
                <StatusBadge
                  tone={lightTone}
                  label={s.agent || "agent"}
                />
                <Text>{s.short || "—"}</Text>
                <Text>{s.workspace || ""}</Text>
                <Text>{whenText(s.last_event_at)}</Text>
              </Inline>
              )
            })}
          </Stack>
        </Card>
      )}
    </Stack>
  )

  // ── 设置页 ─────────────────────────────────────────────────────────────
  const settingsTab = (
    <Stack gap="md">
      <Card title={t("panel.settings.roots", { defaultValue: "你自己的目录" })}>
        <Stack gap="sm">
          <Field
            label={t("panel.settings.roots", { defaultValue: "你自己的目录" })}
            help={t("panel.settings.roots_help", {
              defaultValue:
                "落在这些目录里的会话，播报会说「你自己开的」。留空则高危通知显示「发起者未知」。一行一个。",
            })}
          >
            <Textarea
              value={rootsDraft}
              placeholder={t("panel.settings.roots_placeholder", {
                defaultValue: "D:\\workspace",
              })}
              onChange={(v: string) => setRootsDraft(v)}
            />
          </Field>
          <Button
            tone="primary"
            disabled={!running || savingRoots}
            onClick={onSaveRoots}
          >
            {t("panel.settings.save", { defaultValue: "保存" })}
          </Button>
        </Stack>
      </Card>

      <Card title={t("panel.settings.speak", { defaultValue: "播报方式" })}>
        <Stack gap="sm">
          {/* 两个按钮而不是下拉：只有两个值，而且当前值要一眼看得出来。
              下拉默认显示当前值，用户分不清"这是设置"还是"这是状态"。 */}
          <Inline gap="sm">
            <Button
              tone={state.speak ? "default" : "primary"}
              disabled={!running || savingSpeak || state.speak === true}
              onClick={() => {
                onSetSpeak(false)
              }}
            >
              {t("panel.settings.speak.off", { defaultValue: "只出文字" })}
            </Button>
            <Button
              tone={state.speak ? "primary" : "default"}
              disabled={!running || savingSpeak || !state.speak_available}
              onClick={() => {
                onSetSpeak(true)
              }}
            >
              {t("panel.settings.speak.on", { defaultValue: "让猫娘说一说" })}
            </Button>
          </Inline>
          <StatusBadge
            tone={state.speak ? "success" : "default"}
            label={state.speak
              ? t("panel.settings.speak.on", { defaultValue: "让猫娘说一说" })
              : t("panel.settings.speak.off", { defaultValue: "只出文字" })}
          />
          <Text>
            {t("panel.settings.speak_help", {
              defaultValue:
                "「让猫娘说一说」= 每条播报让主模型接一轮，用她自己的话复述。"
                + "原文照旧上屏，所以语音漏了你也看得见。",
            })}
          </Text>
          {/* 没配 key 时必须说清为什么点不了——红线 3。 */}
          {!state.speak_available ? (
            <Alert
              tone="warning"
              message={t("panel.settings.speak_need_key", {
                defaultValue:
                  "没配 API key，只能用「只出文字」——让猫娘复述需要主模型接一轮。"
                  + "去上面填 key。",
              })}
            />
          ) : null}
          <Text>
            {t("panel.settings.speak_voice_note", {
              defaultValue:
                "它不保证语音：那一轮话会不会被念出来，取决于你在 N.E.K.O 里"
                + "有没有开角色语音。插件只能让她开口，不能让她出声。",
            })}
          </Text>
          {/* 30 秒限制必须写在明面上。2026-11-26 真机：用户连着点三次，
              后两次被静默挡下，聊天框里只剩文字没有声音，看着像"该说的没说"。
              用户的裁决是限制保留、但不许静默——所以它是常驻文案，不是注释。 */}
          <Alert
            tone="info"
            message={t("panel.settings.speak_cooldown", {
              defaultValue:
                "有 30 秒的时间限制：30 秒内不会重复让她说，只把文字留在聊天窗。"
                + "这是为了绕开宿主前端一个连发声响会弄坏音频的问题。",
            })}
          />
        </Stack>
      </Card>

      {/* B11：让模型先过一遍，不值一提的就不播。
          设计依据（2026-11-26 实测 120 条删除命中）：L0 判出 69 条【警告】，
          其中 59 条目标是工作区内的相对路径——agent 在自己项目里清理。
          这类播报每条都要人看一眼，看多了就是"见到红条就点确认"。
          铁律：只有模型明确说 low+allow 才不播；任何失败照旧播；
          【严重】永远不送审。 */}
      {hasAction("settings_set_broadcast_review") ? (
        <Card
          title={t("panel.settings.broadcast_review", {
            defaultValue: "先审一遍再播",
          })}
        >
          <Stack gap="sm">
            <Inline gap="sm">
              <Button
                tone={state.broadcast_review ? "default" : "primary"}
                disabled={
                  !running ||
                  savingReview ||
                  state.broadcast_review === false
                }
                onClick={() => {
                  onSetBroadcastReview(false)
                }}
              >
                {t("panel.settings.broadcast_review.off", {
                  defaultValue: "全部照旧播",
                })}
              </Button>
              <Button
                tone={state.broadcast_review ? "primary" : "default"}
                disabled={
                  !running ||
                  savingReview ||
                  !state.broadcast_review_available
                }
                onClick={() => {
                  onSetBroadcastReview(true)
                }}
              >
                {t("panel.settings.broadcast_review.on", {
                  defaultValue: "让模型先审一遍",
                })}
              </Button>
            </Inline>
            <StatusBadge
              tone={state.broadcast_review ? "success" : "default"}
              label={
                state.broadcast_review
                  ? t("panel.settings.broadcast_review.on", {
                      defaultValue: "让模型先审一遍",
                    })
                  : t("panel.settings.broadcast_review.off", {
                      defaultValue: "全部照旧播",
                    })
              }
            />
            <Text>
              {t("panel.settings.broadcast_review_help", {
                defaultValue:
                  "开着的时候，【警告】和【提示】两档会先送模型问一句"
                  + "「这事值不值得说」。它认为不值一提的就不再播，"
                  + "其余的照旧播。【严重】永远不送审。",
              })}
            </Text>
            {/* 三个必须说清的点，缺一个就是假控件。 */}
            <Alert
              tone="info"
              message={t("panel.settings.broadcast_review_scope", {
                defaultValue:
                  "压制的只有【警告】和【提示】。【严重】一条不动——"
                  + "删到家目录、删到工作区外、目标说不清的，照样整行红字。",
              })}
            />
            {/* 明说门槛：只有 key 与连通性。成本是事实不是警告——
                要不要花是用户自己的取舍，不由插件替他紧张。 */}
            <Alert
              tone="info"
              message={t("panel.settings.broadcast_review_cost", {
                defaultValue:
                  "要填 API key 才能用：送审走你自己的模型端点，"
                  + "每一批最多 5 条、每条一次请求。填了 key 的日常情况下，"
                  + "agent 在自己项目里清理这类不值一提的播报大部分会被压掉，"
                  + "不用你一条条点过去。没有 key 这条路整个不存在，播报照旧。",
              })}
            />
            <Alert
              tone="info"
              message={t("panel.settings.broadcast_review_failopen", {
                defaultValue:
                  "任何岔子都照旧播：没填 key、连不上模型、答得不像样、"
                  + "它判「中等」或「高风险」——全部当没审过。"
                  + "答得不像样时会按原样再问，最多 3 遍；3 遍都答不齐"
                  + "就放弃，那几条照旧播。"
                  + "只有它明确说「低风险 + 可以不说」才不播。"
                  + "所以开着它不会让该说的变成不说。",
              })}
            />
            {state.broadcast_review ? (
              <Text>
                {t("panel.settings.broadcast_review_stats", {
                  defaultValue:
                    "本次启动以来：送审 {submitted} 条，"
                    + "压掉 {suppressed} 条，没审成 {failed} 条，"
                    + "一共问了模型 {attempts} 遍。",
                  submitted: String(
                    (state.broadcast_review_stats || {}).submitted || 0,
                  ),
                  suppressed: String(
                    (state.broadcast_review_stats || {}).suppressed || 0,
                  ),
                  failed: String(
                    (state.broadcast_review_stats || {}).failed || 0,
                  ),
                  attempts: String(
                    (state.broadcast_review_stats || {}).attempts || 0,
                  ),
                })}
              </Text>
            ) : null}
            {!state.broadcast_review_available ? (
              <Alert
                tone="warning"
                message={t("panel.settings.broadcast_review_need_key", {
                  defaultValue:
                    "没填 API key，用不了——送审走你自己的模型端点。"
                    + "去下面填 key，日常那些不值一提的播报就能大部分压掉。",
                })}
              />
            ) : null}
          </Stack>
        </Card>
      ) : null}

      {/* 语音链调试入口。存在的理由：插件能自主触发的播报只有规则命中，
          那条路的文本永远是「事件报告：<命令>」形状，指示会被包在里面，
          她合理地当成命令文本——所以「让她按脚本说话」必须有一个能推
          任意文本的口子，这就是那个口子。 */}
      {hasAction("speak_test") ? (
        <Card
          title={t("panel.settings.speak_test", {
            defaultValue: "让她念一句（调试）",
          })}
        >
          <Stack gap="sm">
            <Text>
              {t("panel.settings.speak_test_help", {
                defaultValue:
                  "验证语音链用的。她会把这句话原样念出来，同时精确文字也留一份在聊天窗。"
                  + "有 30 秒的时间限制，30 秒内不会重复让她说。",
              })}
            </Text>
            <Field
              label={t("panel.settings.speak_test_label", {
                defaultValue: "要她念的话",
              })}
            >
              <Textarea
                value={speakDraft}
                placeholder={t("panel.settings.speak_test_placeholder", {
                  defaultValue: "例如：已自动放行，这条命令只是打印一句话，风险很低。",
                })}
                onChange={(v: string) => setSpeakDraft(v)}
              />
            </Field>
            <Button
              tone="primary"
              disabled={!running || !state.api_key_set || speaking}
              onClick={onSpeakTest}
            >
              {speaking
                ? t("panel.settings.speak_test_running", {
                    defaultValue: "在等她说完…",
                  })
                : t("panel.settings.speak_test_go", {
                    defaultValue: "让她念",
                  })}
            </Button>
            {!state.api_key_set ? (
              <Alert
                tone="warning"
                message={t("panel.settings.speak_need_key", {
                  defaultValue:
                    "没配 API key，只能用「只出文字」——让猫娘复述需要主模型接一轮。"
                    + "去上面填 key。",
                })}
              />
            ) : null}
          </Stack>
        </Card>
      ) : null}

      <Card title={t("panel.settings.api_key", { defaultValue: "API key" })}>
        <Stack gap="sm">
          <StatusBadge
            tone={state.api_key_set ? "success" : "warning"}
            label={
              state.api_key_set
                ? t("panel.settings.api_key_set", { defaultValue: "已设置" })
                : t("panel.settings.api_key_empty", { defaultValue: "未设置" })
            }
          />
          <Text>
            {t("panel.settings.api_key_help", {
              defaultValue:
                "只用于模型建议与自动审查。不写日志、不进卡片、不给模型看。要改请在插件管理器的「配置」页。",
            })}
          </Text>
        </Stack>
      </Card>

      <Card
        title={t("panel.settings.elsewhere", {
          defaultValue: "其他设置",
        })}
      >
        <Text>
          {t("panel.settings.elsewhere_help", {
            defaultValue:
              "讲解程度、警示强度、自动审查等都在插件管理器的「配置」页。"
              + "这里只放会直接影响「它看起来是否在工作」的那几项。",
          })}
        </Text>
      </Card>
    </Stack>
  )

  // ── 日志页 ─────────────────────────────────────────────────────────────
  const logTab = (
    <Stack gap="md">
      <Card title={t("panel.log.attention", { defaultValue: "需要你注意" })}>
        {attention.length === 0 ? (
          <EmptyState
            title={t("panel.log.empty", { defaultValue: "没有待处理的事。" })}
          />
        ) : (
          <Stack gap="sm">
            {attention.map((a: AttentionItem, i: number) => (
              <Stack key={i} gap="sm">
                <Text>
                  {whenText(a.ts)} · {a.title || ""}
                </Text>
                <Alert tone={toneOf(a.tone)} message={a.detail || ""} />
              </Stack>
            ))}
          </Stack>
        )}
      </Card>
      <Card title={t("panel.log.recent", { defaultValue: "最近发生" })}>
        {recent.length === 0 ? (
          <EmptyState
            title={t("panel.log.empty", { defaultValue: "还没有记录。" })}
          />
        ) : (
          <Stack gap="sm">
            {recent.map((item: LogItem, i: number) => (
              <Inline key={i} gap="sm">
                <Text>{whenText(item.ts)}</Text>
                <StatusBadge tone={toneOf(item.tone)} label="" />
                <Text>{item.text}</Text>
              </Inline>
            ))}
          </Stack>
        )}
      </Card>
      {/* B11 的账也放一份在日志页。计数器原本只在「设置」页，于是"压掉了什么"
          在这一页完全看不见——2026-11-26 真机：用户打开日志页看到
          「还没有记录。」，而插件刚刚静默吞了两条删除播报。
          开关关着、又什么都没压过时不渲染，别拿一排 0 占地方。 */}
      {state.broadcast_review === true ||
      ((state.broadcast_review_stats || {}).suppressed || 0) > 0 ? (
        <Card
          title={t("panel.log.review_stats", {
            defaultValue: "先审一遍再播（本次启动以来）",
          })}
        >
          <Stack gap="sm">
            <Text>
              {t("panel.log.review_stats.line", {
                defaultValue:
                  "送审 {submitted} 条，压掉 {suppressed} 条，"
                  + "没审成 {failed} 条，一共问了模型 {attempts} 遍。",
                submitted: String(
                  (state.broadcast_review_stats || {}).submitted || 0,
                ),
                suppressed: String(
                  (state.broadcast_review_stats || {}).suppressed || 0,
                ),
                failed: String(
                  (state.broadcast_review_stats || {}).failed || 0,
                ),
                attempts: String(
                  (state.broadcast_review_stats || {}).attempts || 0,
                ),
              })}
            </Text>
            <Text>
              {t("panel.log.review_hint", {
                defaultValue:
                  "上面写着「没播」的条目，就是被它压下来的播报——"
                  + "不是出了事，是模型判了低风险、觉得不值一提。"
                  + "最多在这里留 20 条，更早的被挤掉（插件日志里仍有全量）。"
                  + "开关在「设置」页。",
              })}
            </Text>
          </Stack>
        </Card>
      ) : null}
    </Stack>
  )

  return (
    <Page
      title={t("panel.title", { defaultValue: "同桌猫" })}
      subtitle={t("panel.subtitle", {
        defaultValue: "它在干活吗、缺什么、刚才说了什么",
      })}
    >
      <Tabs
        id="deskmate"
        activeId={tab}
        onChange={(id: string) => {
          setTab(id)
        }}
        items={[
          {
            id: "status",
            label: t("panel.tab.status", { defaultValue: "状态" }),
            content: statusTab,
          },
          {
            id: "settings",
            label: t("panel.tab.settings", { defaultValue: "设置" }),
            content: settingsTab,
          },
          {
            id: "log",
            label: t("panel.tab.log", { defaultValue: "日志" }),
            content: logTab,
          },
        ]}
      />
    </Page>
  )
}

function installErrorText(err: any, running: boolean): string {
  const msg = String(err && err.message ? err.message : err)
  if (/409/.test(msg) || !running) {
    return "插件没在运行，按钮点不动。先在插件管理器里启动它。"
  }
  if (/403/.test(msg)) {
    return "这个操作没有暴露给面板（403）。"
  }
  return "装配失败：" + msg
}
