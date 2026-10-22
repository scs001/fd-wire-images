// 动作提议卡（wire-copilot-action-gate 任务 3.2/3.3，design D6/D8）：
// 副驾循环里 propose_action 工具的留痕渲染为专用卡片而非普通文本——
// 写操作必须经「提议-确认」闸，卡就是闸的客户面。
//
// 两面渲染：
// - pending 面（pending_confirm/pending_approval）：动作 id、目标、影响面、
//   tier 徽章 + 确认/取消；pending_approval（publish 档）显示「等待本队
//   管理员批准」而非确认按钮——审批权在 owner/admin，卡上不给假入口。
// - resolved 面（executed/failed/rejected/expired）：executed 显示
//   result_summary；failed 显示 error 并提供重试（同参数重新 propose）；
//   rejected/expired 显示对应文案。会话重载后传入终态即回放为 resolved 面。
//
// 预览即所确认（spec「看板提议先看后确认」）：wire.artifact.draft.create
// 的 dashboard kind 在同一卡内用 DashboardRenderer 渲染完整 spec；form/
// record-view 渲染结构摘要（字段清单/键列）；其余 kind 渲染 spec 键名清单
// ——任何情况下不渲染「预览暂不可用」类占位文案（spec 红线）。
//
// 合规门留白（design D8）：wire.demand.submit 的公开源 URL 与公开数据声明
// 由客户在卡上手填，未填齐确认按钮 disabled——模型给的合规取值服务端一律
// 不采纳，前端同样不预填。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  approveAction,
  proposeAction,
  rejectAction,
  type WireActionState,
  type WireActionTier,
} from "../api";
import { DashboardRenderer } from "../dashboard/DashboardRenderer";
import { parseFormSpec } from "../form/formSpec";
import { parseRecordViewSpec } from "../recordview/recordViewSpec";

/** 卡片消费的提议数据（留痕解析结果或服务端回放行，两种来源同形）。 */
export interface ActionCardProposal {
  proposalId: number;
  actionId: string;
  /** 完整提议参数（重试时原样再 propose；draft.create 的 spec 也在内）。 */
  args: Record<string, unknown>;
  state: WireActionState;
  tier?: WireActionTier;
  impact?: string;
  argsSummary?: string;
  resultSummary?: string;
  error?: string;
}

export interface ActionCardProps {
  proposal: ActionCardProposal;
  /** 重试重新 propose 需要（提议按会话归属落库）。 */
  conversationId: number | null;
  /** 终态回调（父层据此刷新草稿列表等；confirm/reject/失败重试后触发）。 */
  onResolved?: (proposalId: number, state: WireActionState) => void;
}

/** 从提议参数里取一个人类可读的「目标」：依次找常见标识键，找不到回退摘要。 */
function targetOf(proposal: ActionCardProposal): string {
  const args = proposal.args as Record<string, unknown>;
  for (const key of ["title", "name", "draft_id", "dataset_id", "code"]) {
    const value = args[key];
    if (typeof value === "string" && value.trim()) return value;
    if (typeof value === "number") return String(value);
  }
  return proposal.argsSummary ?? "";
}

export function ActionCard({
  proposal,
  conversationId,
  onResolved,
}: ActionCardProps) {
  const { t } = useTranslation();
  // 本地终态覆盖：确认/取消后立即翻面，不等服务端列表重取。
  const [local, setLocal] = useState<{
    state: WireActionState;
    resultSummary?: string;
    error?: string;
  } | null>(null);
  // 重试产生的新提议（同参数重新 propose 后 pending 面回到新 id 上）。
  const [retried, setRetried] = useState<{
    proposalId: number;
    state: WireActionState;
    tier?: WireActionTier;
    impact?: string;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  // 需求单合规门两输入（D8：默认留白，绝不预填模型值）。
  const [sourceUrl, setSourceUrl] = useState("");
  const [publicClaim, setPublicClaim] = useState(false);

  // 提议 id 变化（重试/父层换卡）即复位本地态——新提议新两面。
  useEffect(() => {
    setLocal(null);
    setRetried(null);
    setBusy(false);
    setOpError(null);
    setSourceUrl("");
    setPublicClaim(false);
  }, [proposal.proposalId]);

  const proposalId = retried?.proposalId ?? proposal.proposalId;
  // 生效态三级：本地刚确认的 > 重试新提议的 > 留痕/服务端回放的。
  const state = local?.state ?? retried?.state ?? proposal.state;
  const tier = retried?.tier ?? proposal.tier;
  const impact = retried?.impact ?? proposal.impact;
  const resultSummary = local?.resultSummary ?? proposal.resultSummary;
  const failureText = local?.error ?? proposal.error;
  const pending =
    state === "pending_confirm" || state === "pending_approval";
  // 合规门只在待确认面且需求单动作上出现。
  const needsCompliance = pending && proposal.actionId === "wire.demand.submit";
  const complianceReady = sourceUrl.trim() !== "" && publicClaim;
  const target = targetOf(proposal);

  const confirm = async () => {
    if (busy || !pending) return;
    // 双保险：按钮已 disabled，这里再拦一次防编程触发。
    if (needsCompliance && !complianceReady) return;
    setBusy(true);
    setOpError(null);
    try {
      const result = await approveAction(
        proposalId,
        needsCompliance
          ? { source_url: sourceUrl.trim(), public_claim: true }
          : undefined,
      );
      setLocal({
        state: result.state,
        resultSummary: result.result_summary,
        error: result.error,
      });
      onResolved?.(proposalId, result.state);
    } catch (err) {
      // 403/422 的后端 detail 如实呈现，卡保持待确认面可改再试。
      setOpError(apiErrorText(err, t("console.assistant.action.opFailed")));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (busy || !pending) return;
    setBusy(true);
    setOpError(null);
    try {
      await rejectAction(proposalId);
      setLocal({ state: "rejected" });
      onResolved?.(proposalId, "rejected");
    } catch (err) {
      setOpError(apiErrorText(err, t("console.assistant.action.opFailed")));
    } finally {
      setBusy(false);
    }
  };

  /** 失败重试：同参数重新 propose（新提议新 id，卡回到 pending 面）。 */
  const retry = async () => {
    if (busy || conversationId == null) return;
    setBusy(true);
    setOpError(null);
    try {
      const fresh = await proposeAction(
        proposal.actionId,
        proposal.args,
        conversationId,
      );
      setRetried({
        proposalId: fresh.proposal_id,
        state: fresh.state,
        tier: fresh.tier,
        impact: fresh.impact,
      });
      setLocal(null);
    } catch (err) {
      setOpError(apiErrorText(err, t("console.assistant.action.opFailed")));
    } finally {
      setBusy(false);
    }
  };

  const stateLabel = t(`console.assistant.action.state.${state}`);

  return (
    <div
      data-testid="action-card"
      data-state={state}
      data-proposal-id={proposalId}
      className="mr-4 rounded-lg border border-indigo-200 bg-indigo-50/40 px-3 py-2.5 text-xs"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          data-testid="action-id"
          className="rounded bg-white px-1.5 py-0.5 font-mono text-[10px] text-indigo-700 ring-1 ring-indigo-200"
        >
          {proposal.actionId}
        </span>
        {tier != null && (
          <span
            data-testid="action-tier"
            data-tier={tier}
            className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-medium text-slate-600"
          >
            {t(`console.assistant.action.tier.${tier}`)}
          </span>
        )}
        <span
          data-testid="action-state"
          className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
            state === "executed"
              ? "bg-emerald-100 text-emerald-700"
              : state === "failed"
                ? "bg-red-100 text-red-700"
                : "bg-amber-100 text-amber-700"
          }`}
        >
          {stateLabel}
        </span>
      </div>

      {target && (
        <p data-testid="action-target" className="mt-1.5 font-medium text-slate-800">
          {target}
        </p>
      )}
      {impact && (
        <p data-testid="action-impact" className="mt-1 text-slate-600">
          <span className="text-slate-400">
            {t("console.assistant.action.impactLabel")}
          </span>
          {impact}
        </p>
      )}

      <ActionPreview actionId={proposal.actionId} args={proposal.args} />

      {pending && (
        <div className="mt-2">
          {state === "pending_approval" ? (
            // publish 档：审批权在 owner/admin，卡上只如实说明在等谁。
            <p
              data-testid="action-waiting"
              className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-[11px] text-amber-700"
            >
              {t("console.assistant.action.waitingApproval")}
            </p>
          ) : (
            <>
              {needsCompliance && (
                <div
                  data-testid="action-compliance"
                  className="mt-1 space-y-1.5 rounded border border-amber-200 bg-amber-50/60 p-2"
                >
                  <label
                    htmlFor={`action-compliance-url-${proposalId}`}
                    className="block text-[11px] text-slate-600"
                  >
                    {t("console.assistant.action.compliance.sourceUrlLabel")}
                  </label>
                  <input
                    id={`action-compliance-url-${proposalId}`}
                    data-testid="action-compliance-url"
                    type="url"
                    value={sourceUrl}
                    placeholder={t(
                      "console.assistant.action.compliance.sourceUrlPlaceholder",
                    )}
                    onChange={(event) => setSourceUrl(event.target.value)}
                    className="w-full rounded border border-slate-300 bg-white px-2 py-1 text-xs"
                  />
                  <label className="flex items-start gap-1.5 text-[11px] text-slate-600">
                    <input
                      data-testid="action-compliance-claim"
                      type="checkbox"
                      checked={publicClaim}
                      onChange={(event) => setPublicClaim(event.target.checked)}
                      className="mt-0.5"
                    />
                    {t("console.assistant.action.compliance.publicClaim")}
                  </label>
                  {!complianceReady && (
                    <p
                      data-testid="action-compliance-hint"
                      className="text-[10px] text-amber-600"
                    >
                      {t("console.assistant.action.compliance.required")}
                    </p>
                  )}
                </div>
              )}
              <div className="mt-1.5 flex items-center gap-2">
                <button
                  type="button"
                  data-testid="action-confirm"
                  disabled={busy || (needsCompliance && !complianceReady)}
                  onClick={() => void confirm()}
                  className="rounded bg-slate-900 px-2.5 py-1 text-[11px] text-white transition-colors hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {busy
                    ? t("console.assistant.action.approving")
                    : t("console.assistant.action.confirm")}
                </button>
                <button
                  type="button"
                  data-testid="action-cancel"
                  disabled={busy}
                  onClick={() => void cancel()}
                  className="rounded border border-slate-300 px-2.5 py-1 text-[11px] text-slate-600 transition-colors hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t("console.assistant.action.cancel")}
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {!pending && (
        <div className="mt-2 space-y-1.5">
          {state === "executed" && (
            <p data-testid="action-result" className="text-slate-700">
              <span className="text-slate-400">
                {t("console.assistant.action.resultLabel")}
              </span>
              {resultSummary ?? t("console.assistant.action.resultNone")}
            </p>
          )}
          {(state === "failed" || state === "expired") && failureText && (
            <p
              data-testid="action-error-line"
              className="rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700"
            >
              {failureText}
            </p>
          )}
          {state === "failed" && (
            <button
              type="button"
              data-testid="action-retry"
              disabled={busy || conversationId == null}
              onClick={() => void retry()}
              className="rounded border border-red-300 px-2.5 py-1 text-[11px] text-red-700 transition-colors hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {t("console.assistant.action.retry")}
            </button>
          )}
          {state === "rejected" && (
            <p data-testid="action-rejected-note" className="text-slate-500">
              {t("console.assistant.action.rejectedNote")}
            </p>
          )}
          {state === "expired" && (
            <p data-testid="action-expired-note" className="text-slate-500">
              {t("console.assistant.action.expiredNote")}
            </p>
          )}
        </div>
      )}

      {opError != null && (
        <p
          data-testid="action-error"
          className="mt-1.5 rounded border border-red-200 bg-red-50 px-2 py-1 text-[11px] text-red-700"
        >
          {opError}
        </p>
      )}
    </div>
  );
}

/**
 * 草稿提议的内联预览（任务 3.3）：dashboard 用 DashboardRenderer 渲染完整
 * spec（客户确认的是「看到的样子」）；form/record-view 渲染结构摘要；其余
 * kind 渲染 spec 键名清单。结构都取不到时返回 null（卡仍有动作名/目标/
 * 影响面可确认）——绝不渲染占位文案。
 */
function ActionPreview({
  actionId,
  args,
}: {
  actionId: string;
  args: Record<string, unknown>;
}) {
  const { t } = useTranslation();
  // 预览只对草稿创建提议有意义（promote/publish 等动作无 spec 可看）。
  if (actionId !== "wire.artifact.draft.create") return null;
  const kind = typeof args.kind === "string" ? args.kind : "";
  const spec = args.spec;

  if (kind === "dashboard" && spec && typeof spec === "object") {
    // 固定高度容器：浮层宽度有限，完整看板进卡内滚动区先看后确认。
    return (
      <div
        data-testid="action-preview"
        className="mt-2 h-64 overflow-auto rounded-md border border-slate-200 bg-white p-2"
      >
        <DashboardRenderer spec={spec} />
      </div>
    );
  }

  if (kind === "form") {
    const form = parseFormSpec(spec);
    if (form && form.fields.length > 0) {
      return (
        <div
          data-testid="action-preview"
          className="mt-2 rounded-md border border-slate-200 bg-white p-2"
        >
          <p className="text-[10px] font-medium text-slate-500">
            {t("console.assistant.action.preview.fieldsLabel")}
          </p>
          <ul className="mt-1 flex flex-wrap gap-1">
            {form.fields.map((field) => (
              <li
                key={field.column}
                data-testid="action-preview-field"
                className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-700"
              >
                {field.label}
                <span className="ml-1 font-mono text-slate-400">
                  {field.input}
                </span>
              </li>
            ))}
          </ul>
        </div>
      );
    }
  }

  if (kind === "record-view") {
    const recordView = parseRecordViewSpec(spec);
    if (recordView && (recordView.keyColumn || recordView.headerFields.length > 0)) {
      return (
        <div
          data-testid="action-preview"
          className="mt-2 rounded-md border border-slate-200 bg-white p-2"
        >
          <p className="text-[10px] font-medium text-slate-500">
            {t("console.assistant.action.preview.keyColumnLabel")}
            <span className="ml-1 font-mono text-slate-700">
              {recordView.keyColumn ?? "—"}
            </span>
          </p>
          {recordView.headerFields.length > 0 && (
            <ul className="mt-1 flex flex-wrap gap-1">
              {recordView.headerFields.map((field) => (
                <li
                  key={field.column}
                  data-testid="action-preview-field"
                  className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-700"
                >
                  {field.label ?? field.column}
                </li>
              ))}
            </ul>
          )}
        </div>
      );
    }
  }

  // 结构未知（composite_indicator / research_report / alert_rule /
  // refresh_schedule 等）：spec 键名清单即结构摘要——不占位、不猜形状。
  if (spec && typeof spec === "object" && !Array.isArray(spec)) {
    const record = spec as { spec?: unknown };
    const inner =
      record.spec && typeof record.spec === "object" && !Array.isArray(record.spec)
        ? (record.spec as Record<string, unknown>)
        : (spec as Record<string, unknown>);
    const keys = Object.keys(inner);
    if (keys.length > 0) {
      return (
        <div
          data-testid="action-preview"
          className="mt-2 rounded-md border border-slate-200 bg-white p-2"
        >
          <p className="text-[10px] font-medium text-slate-500">
            {t("console.assistant.action.preview.structureLabel")}
          </p>
          <ul className="mt-1 flex flex-wrap gap-1">
            {keys.map((key) => (
              <li
                key={key}
                data-testid="action-preview-key"
                className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-700"
              >
                {key}
              </li>
            ))}
          </ul>
        </div>
      );
    }
  }
  return null;
}
