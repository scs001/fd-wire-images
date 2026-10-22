// 副驾会话主体（wire-assistant-panel → wire-copilot-action-gate 任务 3.1）：
// 单一副驾，问/做不再分轨——全部消息走统一循环
// （POST /v1/ai/conversations/{id}/messages，响应即本轮全部消息）。
// 自然语言问答、读类工具与动作提议对任意档位可用；构建类动作由后端闸
// 独立校验档位（preflight 拦截如实说明），前端不隐藏入口、不以前端隐藏
// 作为保护——免费档只渲染一个能力说明徽章。
//
// 与外壳分离：`AssistantPanel`（右下角浮动壳）与 `ChatPage`（整页形态）都
// 复用它——同一套交互与失败纪律，避免两处各写一套后逐渐漂移。
//
// 会话（design D7）：只维持**一个活会话**，多轮追加到同一 conversation_id，
// 直到客户主动「新对话」——一次使用只消耗一个日限额。
//
// 可见性（design D1）：循环同步、可能数十秒，期间按 after_id 轮询增量消息，
// 把已落库的中间步骤（读工具留痕、propose_action 动作卡）真实展示出来，
// 避免长时间静默。
//
// 动作卡（任务 3.2-3.4）：messages 里 tool_name === "propose_action" 的
// tool_trace 留痕渲染为 ActionCard（两态以本地确认为准）；会话建立后另拉
// GET /actions?conversation_id= 做历史回放终态对照、GET /artifacts 拉本会话
// 草稿（死代码收口：草稿在面板可见、可一键发起转正提议）。
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  createConversation,
  fetchArtifacts,
  fetchConversationActions,
  fetchMessagesAfter,
  postMessage,
  proposeAction,
  type AssistantContext,
  type ChatMessage,
  type DraftArtifact,
  type WireAction,
  type WireActionState,
} from "../api";
import { YIZUO_PLATFORM_URL } from "../../components/footerConstants";
import { ActionCard, type ActionCardProposal } from "./ActionCard";

/** 轮询间隔（ms）：有新增即尽快再看，连续无新增则退避。 */
const POLL_FAST_MS = 900;
const POLL_SLOW_MS = 2500;
/** 连续多少次无新增后转慢速。 */
const POLL_BACKOFF_AFTER = 3;

export interface AssistantConversationProps {
  /** 当前页签（作为上下文注入，让指路答案落在客户所在页）。 */
  tab: string;
  /** 当前档位 code（免费档渲染能力说明徽章；档位校验在后端闸）。 */
  planCode: string;
  /** 当前正在查看的产物/数据集标识（可选，由宿主页面提供）。 */
  context?: AssistantContext;
  /** 整页形态下页面自己承载壹座入口，壳内不再重复。 */
  hideBaseLink?: boolean;
  /** 事务区高度类（浮层给固定高度，整页给更大空间）。 */
  transcriptClassName?: string;
}

function isFreeTier(planCode: string): boolean {
  return planCode === "free";
}

/** tool_trace 留痕 → 一行人类可读的进展文案（不展示原始 JSON）。 */
function progressLabel(raw: string, t: (key: string) => string): string {
  try {
    const payload = JSON.parse(raw) as { tool_name?: unknown };
    const name = payload.tool_name;
    if (typeof name === "string" && name.trim()) {
      return t("console.assistant.progressTool").replace("{{tool}}", name);
    }
  } catch {
    /* 非 JSON：按通用进展文案兜底 */
  }
  return t("console.assistant.progressGeneric");
}

interface TranscriptEntry {
  id: number;
  role: "user" | "assistant" | "progress" | "action";
  content: string;
  /** role=action 时的提议数据（propose_action 留痕解析结果）。 */
  proposal?: ActionCardProposal;
}

/**
 * propose_action 留痕宽容解析：arguments 兼容 {action_id, args:{…}} 与
 * 扁平 {action_id, …} 两种形状（后端循环并行实现，spec 以 arguments.spec
 * 直给为准——两种都收，解析失败返回 null 走普通进展行）。
 */
function parseProposeTrace(
  raw: string,
): { entry: Omit<TranscriptEntry, "id"> } | null {
  try {
    const payload = JSON.parse(raw) as {
      tool_name?: unknown;
      arguments?: unknown;
      result?: unknown;
    };
    if (payload.tool_name !== "propose_action") return null;
    const args = payload.arguments;
    if (!args || typeof args !== "object" || Array.isArray(args)) return null;
    const record = args as Record<string, unknown>;
    const actionId = record.action_id;
    if (typeof actionId !== "string" || !actionId) return null;
    // args 归一：嵌套 args 优先，否则取扁平剩余键（action_id 除外）。
    const inner =
      record.args && typeof record.args === "object" && !Array.isArray(record.args)
        ? (record.args as Record<string, unknown>)
        : Object.fromEntries(
            Object.entries(record).filter(([key]) => key !== "action_id"),
          );
    const result =
      payload.result && typeof payload.result === "object" && !Array.isArray(payload.result)
        ? (payload.result as Record<string, unknown>)
        : {};
    const proposalId = result.proposal_id;
    const state = result.state;
    const tier = result.tier;
    const impact = result.impact;
    if (typeof proposalId !== "number") return null;
    const proposal: ActionCardProposal = {
      proposalId,
      actionId,
      args: inner,
      state:
        state === "pending_confirm" ||
        state === "pending_approval" ||
        state === "executed" ||
        state === "rejected" ||
        state === "expired" ||
        state === "failed"
          ? state
          : "pending_confirm",
      ...(tier === "inline" || tier === "second" || tier === "async"
        ? { tier }
        : {}),
      ...(typeof impact === "string" && impact ? { impact } : {}),
    };
    return {
      entry: { role: "action", content: raw, proposal },
    };
  } catch {
    return null;
  }
}

function toEntries(
  messages: ChatMessage[],
  t: (key: string) => string,
): TranscriptEntry[] {
  return messages.map((message) => {
    if (message.role === "tool_trace") {
      const propose = parseProposeTrace(message.content);
      if (propose) return { id: message.id, ...propose.entry };
      return {
        id: message.id,
        role: "progress",
        content: progressLabel(message.content, t),
      };
    }
    return {
      id: message.id,
      role: message.role === "assistant" ? "assistant" : "user",
      content: message.content,
    };
  });
}

/** 按 id 去重合并（轮询与主响应会覆盖同一批消息）。 */
function mergeEntries(
  current: TranscriptEntry[],
  incoming: TranscriptEntry[],
): TranscriptEntry[] {
  if (incoming.length === 0) return current;
  const seen = new Set(current.map((entry) => entry.id));
  const merged = [...current];
  for (const entry of incoming) {
    if (!seen.has(entry.id)) {
      merged.push(entry);
      seen.add(entry.id);
    }
  }
  return merged.sort((a, b) => a.id - b.id);
}

/** 草稿条目标题：spec 信封的 title 优先，缺省用 kind 兜底（绝不空白行）。 */
function draftTitle(draft: DraftArtifact): string {
  const title = draft.spec?.title;
  return typeof title === "string" && title.trim() ? title : draft.kind;
}

/** 面板外发起的提议（草稿转正按钮）按消息语义追加渲染为动作卡。 */
interface ExtraProposal {
  key: string;
  proposal: ActionCardProposal;
}

let extraSeq = 0;

export function AssistantConversation(props: AssistantConversationProps) {
  const { t } = useTranslation();
  // tab/context 注入统一循环（wire-copilot-action-gate：/messages 已收
  // context 键，服务端白名单净化后进 system 提示——只含标识不含数据行）。
  const {
    planCode,
    hideBaseLink = false,
    transcriptClassName = "min-h-[12rem]",
    context,
  } = props;
  const free = isFreeTier(planCode);
  const [draft, setDraft] = useState("");
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [conversationId, setConversationId] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<DraftArtifact[]>([]);
  const [promotingId, setPromotingId] = useState<number | null>(null);
  const [promoteError, setPromoteError] = useState<string | null>(null);
  // 历史回放终态对照（proposal_id → 服务端状态行）：会话重开后已执行/已取消
  // 的提议按服务端真值回放 resolved 面；会话内的本地确认以卡内 state 为准。
  const [actionsById, setActionsById] = useState<Map<number, WireAction>>(
    () => new Map(),
  );
  // 转正按钮发起的提议（不在消息留痕里，本地追加渲染）。
  const [extras, setExtras] = useState<ExtraProposal[]>([]);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  // 已读到的最新消息 id（增量轮询基线）。用 ref 而非 state：轮询回调需要读
  // 最新值，不能吃闭包里的旧快照。
  const latestIdRef = useRef(0);

  const resetConversation = useCallback(() => {
    setConversationId(null);
    setEntries([]);
    setError(null);
    setProgress(null);
    setDrafts([]);
    setPromoteError(null);
    setActionsById(new Map());
    setExtras([]);
    latestIdRef.current = 0;
  }, []);

  /** 会话建立后拉草稿（任务 3.4：草稿看得到）；失败静默——草稿区是增补
   *  信息，不该阻塞副驾本体。 */
  const refreshDrafts = useCallback((conversationId: number) => {
    fetchArtifacts(conversationId)
      .then((list) => setDrafts(list))
      .catch(() => setDrafts([]));
  }, []);

  // 会话切换：拉草稿 + 拉提议列表（回放终态）。alive 防卸载后 setState。
  useEffect(() => {
    if (conversationId == null) return;
    let alive = true;
    refreshDrafts(conversationId);
    fetchConversationActions(conversationId)
      .then((rows) => {
        if (alive) {
          setActionsById(new Map(rows.map((row) => [row.proposal_id, row])));
        }
      })
      .catch(() => {
        /* 提议列表失败不阻塞：卡回退留痕初始态（pending），确认时后端仍校验 */
      });
    return () => {
      alive = false;
    };
  }, [conversationId, refreshDrafts]);

  /** 统一发送：同步请求 + 期间按 after_id 轮询增量消息（design D1）。 */
  const send = useCallback(
    async (content: string) => {
      setBusy(true);
      setError(null);
      setProgress(t("console.assistant.progressStarting"));
      let active = true;
      let lastId = latestIdRef.current;

      const poll = async (targetId: number) => {
        let quiet = 0;
        while (active) {
          await new Promise((resolve) =>
            setTimeout(
              resolve,
              quiet >= POLL_BACKOFF_AFTER ? POLL_SLOW_MS : POLL_FAST_MS,
            ),
          );
          if (!active) return;
          try {
            const fresh = await fetchMessagesAfter(targetId, lastId);
            if (fresh.length > 0) {
              quiet = 0;
              lastId = fresh[fresh.length - 1].id;
              latestIdRef.current = lastId;
              setEntries((current) =>
                mergeEntries(current, toEntries(fresh, t)),
              );
            } else {
              quiet += 1;
            }
          } catch {
            /* 轮询失败不该打断主请求：静默跳过这一拍 */
          }
        }
      };

      try {
        let targetId = conversationId;
        if (targetId == null) {
          const detail = await createConversation({});
          targetId = detail.id;
          setConversationId(targetId);
        }
        const polling = poll(targetId);
        const appended = await postMessage(targetId, content, context);
        active = false;
        await polling;
        setEntries((current) => mergeEntries(current, toEntries(appended, t)));
        // 主响应是权威集合：基线推到最大 id，避免下一轮重复拉取。
        latestIdRef.current = appended.reduce(
          (max, item) => Math.max(max, item.id),
          latestIdRef.current,
        );
        setProgress(null);
        // 本轮可能落了新草稿（draft.create 已确认）——重拉草稿区。
        refreshDrafts(targetId);
      } catch (err) {
        active = false;
        setError(err instanceof Error ? err.message : String(err));
        setProgress(null);
        throw err;
      } finally {
        setBusy(false);
      }
    },
    [conversationId, refreshDrafts, t],
  );

  const submit = useCallback(async () => {
    const content = draft.trim();
    if (!content || busy) return;
    setDraft("");
    try {
      await send(content);
    } catch {
      // 失败保留输入（spec 场景「失败如实」要求可编辑重发）。
      setDraft(content);
    }
  }, [draft, busy, send]);

  /** 动作卡终态回调：产物族执行成功即重拉草稿（转正后草稿离开暂存区）。 */
  const handleActionResolved = useCallback(
    (_proposalId: number, state: WireActionState) => {
      if (state === "executed" && conversationId != null) {
        refreshDrafts(conversationId);
      }
    },
    [conversationId, refreshDrafts],
  );

  /** 草稿转正（任务 3.4）：propose wire.artifact.promote——转正经闸确认，
   *  不绕过直接执行；提议卡随 extras 渲染，客户在卡上确认。 */
  const promoteDraft = useCallback(
    async (draftItem: DraftArtifact) => {
      if (conversationId == null || promotingId != null) return;
      setPromotingId(draftItem.id);
      setPromoteError(null);
      try {
        const proposal = await proposeAction("wire.artifact.promote", {
          draft_id: draftItem.id,
        }, conversationId);
        setExtras((current) => [
          ...current,
          {
            key: `extra-${++extraSeq}`,
            proposal: {
              proposalId: proposal.proposal_id,
              actionId: proposal.action_id,
              args: { draft_id: draftItem.id },
              state: proposal.state,
              tier: proposal.tier,
              impact: proposal.impact,
            },
          },
        ]);
      } catch (err) {
        setPromoteError(
          apiErrorText(err, t("console.assistant.draft.promoteFailed")),
        );
      } finally {
        setPromotingId(null);
      }
    },
    [conversationId, promotingId, t],
  );

  /** 留痕提议 + 服务端回放行合成卡数据：服务端终态优先（回放场景），
   *  会话内刚确认的以卡内本地 state 为准（见 ActionCard）。 */
  const effectiveProposal = useCallback(
    (base: ActionCardProposal): ActionCardProposal => {
      const server = actionsById.get(base.proposalId);
      if (!server) return base;
      return {
        ...base,
        state: server.state,
        argsSummary: server.args_summary ?? base.argsSummary,
        resultSummary: server.result_summary ?? base.resultSummary,
        error: server.error ?? base.error,
      };
    },
    [actionsById],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="assistant-body">
      <div className="flex items-center gap-2 pb-2">
        <span className="text-sm font-semibold text-slate-900">
          {t("console.assistant.title")}
        </span>
        {free && (
          // 免费档能力说明徽章：不隐藏任何入口（档位校验在后端闸独立执行）。
          <span
            data-testid="assistant-free-badge"
            title={t("console.assistant.free.badgeNote")}
            className="rounded-full bg-slate-100 px-2 py-0.5 text-[10px] text-slate-600"
          >
            {t("console.assistant.free.badge")}
          </span>
        )}
        <button
          type="button"
          data-testid="assistant-new"
          onClick={resetConversation}
          className="ml-auto text-[11px] text-slate-500 hover:text-slate-800"
        >
          {t("console.assistant.newConversation")}
        </button>
      </div>

      <div
        data-testid="assistant-transcript"
        className={`flex-1 space-y-2 overflow-y-auto ${transcriptClassName}`}
      >
        {/* 本会话草稿区（事务区顶部）：草稿看得到、转正点得动（spec
            「会话草稿 SHALL 可见且可转正」）。空列表整区隐藏。 */}
        {drafts.length > 0 && (
          <div
            data-testid="assistant-drafts"
            className="rounded-lg border border-slate-200 bg-white px-3 py-2"
          >
            <p className="text-[10px] font-medium text-slate-500">
              {t("console.assistant.draft.title")}
            </p>
            <ul className="mt-1 space-y-1">
              {drafts.map((draftItem) => (
                <li
                  key={draftItem.id}
                  data-testid="assistant-draft-item"
                  className="flex items-center gap-2 text-[11px]"
                >
                  <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600">
                    {draftItem.kind}
                  </span>
                  <span
                    data-testid="assistant-draft-title"
                    className="min-w-0 flex-1 truncate text-slate-700"
                  >
                    {draftTitle(draftItem)}
                  </span>
                  <button
                    type="button"
                    data-testid="assistant-draft-promote"
                    disabled={promotingId != null}
                    onClick={() => void promoteDraft(draftItem)}
                    className="rounded border border-indigo-300 px-1.5 py-0.5 text-[10px] text-indigo-700 transition-colors hover:bg-indigo-50 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {promotingId === draftItem.id
                      ? t("console.assistant.draft.promoting")
                      : t("console.assistant.draft.promote")}
                  </button>
                </li>
              ))}
            </ul>
            {promoteError != null && (
              <p
                data-testid="assistant-draft-error"
                className="mt-1 text-[10px] text-red-600"
              >
                {promoteError}
              </p>
            )}
          </div>
        )}

        {entries.length === 0 && extras.length === 0 && !busy && (
          <p className="text-xs leading-relaxed text-slate-500">
            {t("console.assistant.empty")}
          </p>
        )}
        {entries.map((entry) => {
          if (entry.role === "action" && entry.proposal) {
            return (
              <ActionCard
                key={entry.id}
                proposal={effectiveProposal(entry.proposal)}
                conversationId={conversationId}
                onResolved={handleActionResolved}
              />
            );
          }
          return (
            <div
              key={entry.id}
              data-testid={`assistant-entry-${entry.role}`}
              className={
                entry.role === "user"
                  ? "ml-6 rounded-lg bg-slate-900 px-3 py-2 text-xs text-white"
                  : entry.role === "progress"
                    ? "text-[11px] italic text-slate-400"
                    : "mr-6 rounded-lg bg-slate-100 px-3 py-2 text-xs whitespace-pre-wrap text-slate-800"
              }
            >
              {entry.content}
            </div>
          );
        })}
        {extras.map((extra) => (
          <ActionCard
            key={extra.key}
            proposal={effectiveProposal(extra.proposal)}
            conversationId={conversationId}
            onResolved={handleActionResolved}
          />
        ))}
        {progress != null && (
          <div
            data-testid="assistant-progress"
            className="text-[11px] italic text-slate-400"
          >
            {progress}
          </div>
        )}
        {error != null && (
          <div
            data-testid="assistant-error"
            className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
          >
            {error}
          </div>
        )}
      </div>

      <div className="mt-2 border-t border-slate-200 pt-2">
        <div className="flex items-end gap-2">
          <textarea
            ref={inputRef}
            data-testid="assistant-input"
            rows={2}
            value={draft}
            placeholder={t("console.assistant.placeholder")}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit();
              }
            }}
            className="min-h-[2.5rem] flex-1 resize-none rounded-md border border-slate-300 px-2 py-1.5 text-xs focus:border-slate-500 focus:outline-none"
          />
          <button
            type="button"
            data-testid="assistant-send"
            disabled={busy || draft.trim().length === 0}
            onClick={() => void submit()}
            className="rounded-md bg-slate-900 px-3 py-1.5 text-xs text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {busy ? t("console.assistant.sending") : t("console.assistant.send")}
          </button>
        </div>
        {!hideBaseLink && (
          <div className="mt-1.5 text-right">
            <a
              data-testid="assistant-base-link"
              href={YIZUO_PLATFORM_URL}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[11px] text-slate-500 hover:text-slate-800"
            >
              {t("console.assistant.goBase")}
            </a>
          </div>
        )}
      </div>
    </div>
  );
}
