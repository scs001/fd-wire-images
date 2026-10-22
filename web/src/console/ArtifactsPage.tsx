// 产物页（#/console/artifacts；wire-artifacts-persistence web 面）。
// - 列表接 GET /api/v1/artifacts（持久化产物）：kind 徽标 + 状态徽标
//   （draft/published/archived）+ 版本号 + 来源会话指针；kind/status 筛选。
// - 页头「新建看板」（wire-dashboard-blank-create）：标题输入 → POST
//   /api/v1/artifacts {kind:"dashboard", title} → 新行置顶并走既有行展开
//   直接进编辑器（免 cell 免 AI 的出生路径——免费档第一块板）。
// - spec 编辑保存走 PUT（成功提示新版本号）；localStorage 退位为未保存
//   离开提示的本地缓冲（自动暂存 + 可一键丢弃）；archived 只读（服务端
//   409 文案如实呈现）。
// - 版本历史抽屉：列出历史版本、查看任一版本 spec、可复制。
// - 发布/归档/恢复操作（非法迁移 409 如实呈现）。
// - dashboard kind 卡片加「查看看板」内嵌展开区（wire-dashboard-renderer）：
//   快照渲染器查看 + 编辑器切换，保存复用下方 PUT 与版本链。
// - published dashboard 卡片加「生成分享链接」弹层（wire-dashboard-share）：
//   展示公开页完整链接（origin + #/share/dashboard/…）+ 复制 + 到期时间；
//   draft/archived/非 dashboard 不出现该入口。
//   wire-share-ttl-options：弹层内有效期三选一（7/30/90 天，缺省 30）——
//   改选即按新 ttl_days 重新生成，到期时间随选联动（响应覆盖本地预览）。
// - alert_rule 行告警执行面（wire-alert-evaluation 任务 5）：状态徽标
//   （firing 红/ok 绿/错误琥珀/未评估灰，来自 GET /alerts/states 一次性
//   map；拉取失败静默降级为全「未评估」不阻塞页面）+ 最近评估时间；
//   行展开（查看 spec）时附「告警事件」小节（最近 10 条 + 手动刷新）与
//   「试运行」按钮（POST /artifacts/{id}/alert/test——成功展示归约值与
//   触发判定，失败后端 detail 如实红条）。非 alert_rule 行零变化。
// - dashboard 行置顶默认看板（wire-personal-preferences）：「设为默认看板/
//   取消默认看板」+「默认」徽标（PUT /api/console/me/preferences）；成功经
//   onPinnedChange 回调由容器更新徽标位置，失败（如置顶已归档看板 422）
//   后端 detail 行内红字如实呈现、徽标不动。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ARTIFACT_KINDS,
  ARTIFACT_STATUSES,
  apiErrorText,
  archiveArtifact,
  createBlankAlertRule,
  createBlankDashboard,
  createBlankDoc,
  createBlankForm,
  createBlankRecordView,
  createDashboardShareLink,
  createDocShareLink,
  createFormShareLink,
  fetchArtifactVersion,
  fetchArtifactVersions,
  fetchConversations,
  fetchLifecycleArtifact,
  fetchLifecycleArtifacts,
  getAlertEvents,
  getAlertStates,
  publishArtifact,
  putPreferences,
  RECORD_VIEW_TTL_DAYS,
  restoreArtifact,
  saveArtifactSpec,
  SHARE_TTL_DAYS,
  testAlertRule,
  type AlertEvent,
  type AlertStatesMap,
  type AlertStateSummary,
  type AlertTestResult,
  type Artifact,
  type ArtifactDetail,
  type ArtifactKind,
  type ArtifactStatus,
  type ArtifactVersionDetail,
  type ArtifactVersionSummary,
  type Conversation,
  type DashboardShareLink,
  type DocShareLink,
  type RecordViewTtlDays,
  type ShareTtlDays,
} from "./api";
import { formatServerDate, truncate } from "./util";
import { DashboardPane } from "./dashboard/DashboardPane";
import { FormPane } from "./form/FormPane";
import { RecordViewPane } from "./recordview/RecordViewPane";
import { RecordViewShareDialog } from "./recordview/RecordViewShareDialog";
import { DocEditor } from "./doc/DocEditor";
import { ArtifactVisibilitySection } from "./ArtifactVisibilitySection";

const BUFFER_STORAGE_PREFIX = "wire-web-artifact-buffer-";

const KIND_BADGE_CLASS: Record<string, string> = {
  dashboard: "bg-indigo-50 text-indigo-700 ring-indigo-200",
  form: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  composite_indicator: "bg-violet-50 text-violet-700 ring-violet-200",
  research_report: "bg-sky-50 text-sky-700 ring-sky-200",
  alert_rule: "bg-rose-50 text-rose-700 ring-rose-200",
  refresh_schedule: "bg-teal-50 text-teal-700 ring-teal-200",
  "record-view": "bg-cyan-50 text-cyan-700 ring-cyan-200",
  // wire-rich-text-docs 任务 4.2：doc 徽标（琥珀——正文型产物与结构化看板分色）。
  doc: "bg-amber-50 text-amber-700 ring-amber-200",
};

const STATUS_BADGE_CLASS: Record<string, string> = {
  draft: "bg-amber-100 text-amber-700",
  published: "bg-emerald-100 text-emerald-700",
  archived: "bg-slate-200 text-slate-600",
};

// 告警执行态徽标四态（wire-alert-evaluation）：firing 红 / ok 绿 / 错误
// 琥珀 / 未评估灰。
const ALERT_STATE_BADGE_CLASS: Record<AlertBadgeState, string> = {
  firing: "bg-red-100 text-red-700",
  ok: "bg-emerald-100 text-emerald-700",
  error: "bg-amber-100 text-amber-700",
  unevaluated: "bg-slate-200 text-slate-600",
};

// 告警事件 kind 徽标四枚举（firing 红 / resolved 绿 / error 琥珀 /
// notify_failed 灰）。
const ALERT_EVENT_KIND_CLASS: Record<AlertEvent["kind"], string> = {
  firing: "bg-red-100 text-red-700",
  resolved: "bg-emerald-100 text-emerald-700",
  error: "bg-amber-100 text-amber-700",
  notify_failed: "bg-slate-200 text-slate-600",
};

/** 行内告警徽标态：states map 命中且 firing → firing；ok 且带最近错误 →
 *  error（琥珀，完整文案「错误（最近 N 时）」走 title）；ok → ok；无键 →
 *  unevaluated（从未评估）。 */
type AlertBadgeState = "firing" | "ok" | "error" | "unevaluated";

function alertBadgeOf(state: AlertStateSummary | undefined): AlertBadgeState {
  if (state == null) return "unevaluated";
  if (state.state === "firing") return "firing";
  return state.lastErrorAt != null ? "error" : "ok";
}

type StatusAction = "publish" | "archive" | "restore";

export function ArtifactsPage({
  pinnedDashboardId = null,
  onPinnedChange,
  initialDashboardId = null,
}: {
  /** 已置顶为默认的看板产物 id（徽标行）；null = 未置顶。 */
  pinnedDashboardId?: number | null;
  /** 置顶/取消置顶成功回调（容器更新偏好态，徽标即时移动）。 */
  onPinnedChange?: (id: number | null) => void;
  /** 落地即展开的看板产物 id（wire-personal-preferences 置顶跳转）——仅
   *  挂载期初始化 dashboardFor 消费一次；无该 prop 的重挂载不展开。 */
  initialDashboardId?: number | null;
}) {
  const { t } = useTranslation();
  const [artifacts, setArtifacts] = useState<Artifact[] | null>(null);
  const [conversations, setConversations] = useState<Conversation[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [kindFilter, setKindFilter] = useState<ArtifactKind | "all">("all");
  const [statusFilter, setStatusFilter] = useState<ArtifactStatus | "all">("all");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  // 看板视图展开的产物 id（wire-dashboard-renderer：dashboard kind 内嵌入口）。
  // 挂载期吃下 initialDashboardId（置顶落地跳转直接展开该看板）。
  const [dashboardFor, setDashboardFor] = useState<number | null>(
    initialDashboardId,
  );
  // 表单视图展开的产物 id（wire-form-builder：form kind 内嵌入口）。
  const [formFor, setFormFor] = useState<number | null>(null);
  // 单记录页视图展开的产物 id（wire-record-view 任务 4.1：record-view kind
  // 内嵌入口——查看态试预览 + 构建器切换）。
  const [recordViewFor, setRecordViewFor] = useState<number | null>(null);
  // 文档编辑器展开的产物 id（wire-rich-text-docs 任务 4.2：doc kind 内嵌
  // 入口——分栏编辑器挂载即读写草稿层）。
  const [docFor, setDocFor] = useState<number | null>(null);
  const [historyFor, setHistoryFor] = useState<number | null>(null);
  // 分享弹层展开的产物 id（wire-dashboard-share / wire-form-builder：
  // published dashboard/form 专属——kind 随打开行分派）。
  const [shareFor, setShareFor] = useState<number | null>(null);
  const [shareKind, setShareKind] = useState<"dashboard" | "form">("dashboard");
  // 单记录页分享+二维码弹层的产物 id（wire-record-view 任务 4.3：record-view
  // 专属弹层——键值选择器/URL/QR 懒加载，不复用 ShareLinkDialog）。
  const [shareRecordViewFor, setShareRecordViewFor] = useState<number | null>(
    null,
  );
  // 文档分享弹层的产物 id（wire-rich-text-docs 任务 4.2：doc 专属极简弹层
  // ——无键值选择/二维码，仅 TTL 四档 + 复制链接；RecordViewShareDialog
  // 是 record-view 专用形状，不复用）。
  const [shareDocFor, setShareDocFor] = useState<number | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // 新建看板表单开合（wire-dashboard-blank-create：免 cell 免 AI 的出生入口）。
  const [createOpen, setCreateOpen] = useState(false);
  // 新建表单表单开合（wire-form-builder 任务 4.4：与新建看板并列）。
  const [createFormOpen, setCreateFormOpen] = useState(false);
  // 新建告警规则表单开合（wire-alert-evaluation：免 AI 的规则出生路径）。
  const [createAlertOpen, setCreateAlertOpen] = useState(false);
  // 新建单记录页表单开合（wire-record-view 任务 4.1：免 AI 的扫码页出生路径）。
  const [createRecordViewOpen, setCreateRecordViewOpen] = useState(false);
  // 新建文档表单开合（wire-rich-text-docs 任务 4.5：免 AI 的文档出生路径）。
  const [createDocOpen, setCreateDocOpen] = useState(false);
  // 刚空白创建的产物 id：其看板/表单展开区直接进编辑态（创建即跳编辑器）。
  const [freshlyCreatedId, setFreshlyCreatedId] = useState<number | null>(null);
  const [freshlyFormId, setFreshlyFormId] = useState<number | null>(null);
  // 刚空白创建的单记录页 id（创建即跳构建器）。
  const [freshlyRecordViewId, setFreshlyRecordViewId] = useState<number | null>(
    null,
  );
  // 刚空白创建的文档 id（创建即跳分栏编辑器）。
  const [freshlyDocId, setFreshlyDocId] = useState<number | null>(null);
  // 告警评估状态（wire-alert-evaluation）：进页对 alert_rule 行取一次的
  // map；null = 尚未到达（徽标暂不渲染），{} = 已到达或失败降级（无键即
  // 「未评估」——拉取失败静默降级，绝不阻塞页面）。
  const [alertStates, setAlertStates] = useState<AlertStatesMap | null>(null);
  // 置顶操作行内错误（wire-personal-preferences）：422 等 detail 按行呈现
  //（如置顶已归档看板），新操作前清空。
  const [pinError, setPinError] = useState<{
    artifactId: number;
    text: string;
  } | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setArtifacts(null);
    setAlertStates(null);
    try {
      const [rows, convs] = await Promise.all([
        fetchLifecycleArtifacts(),
        fetchConversations(),
      ]);
      setArtifacts(rows);
      setConversations(convs);
      // states 只为 alert_rule 行取一次（进页即拉；列表里一条告警规则都
      // 没有则不请求）；失败静默降级为全「未评估」。
      if (rows.some((a) => a.kind === "alert_rule")) {
        getAlertStates()
          .then((states) => setAlertStates(states))
          .catch(() => setAlertStates({}));
      }
    } catch {
      setError("artifacts");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // 执行成功事件刷新（wire-copilot-action-gate 任务 4.1，design D9）：闸
  // 执行成功后 api.ts 广播 window 级 wire:action-executed（resources 按
  // action_id 前缀映射）；产物页命中 "artifacts" 即重跑既有列表加载——只
  // 重取数据，筛选/展开等 UI 状态不动（spec：无需手动刷新整页）。
  useEffect(() => {
    const onExecuted = (event: Event) => {
      const detail = (event as CustomEvent<{ resources?: string[] }>).detail;
      if (detail?.resources?.includes("artifacts")) void load();
    };
    window.addEventListener("wire:action-executed", onExecuted);
    return () => window.removeEventListener("wire:action-executed", onExecuted);
  }, [load]);

  const conversationById = useMemo(() => {
    const map = new Map<number, Conversation>();
    conversations?.forEach((c) => map.set(c.id, c));
    return map;
  }, [conversations]);

  const visible = useMemo(
    () =>
      artifacts?.filter(
        (a) =>
          (kindFilter === "all" || a.kind === kindFilter) &&
          (statusFilter === "all" || a.status === statusFilter),
      ) ?? [],
    [artifacts, kindFilter, statusFilter],
  );

  const replaceArtifact = useCallback((next: Artifact) => {
    setArtifacts((prev) =>
      prev ? prev.map((a) => (a.id === next.id ? { ...a, ...next } : a)) : prev,
    );
  }, []);

  const runAction = useCallback(
    async (artifact: Artifact, action: StatusAction) => {
      setActionError(null);
      try {
        const caller =
          action === "publish"
            ? publishArtifact
            : action === "archive"
              ? archiveArtifact
              : restoreArtifact;
        replaceArtifact(await caller(artifact.id));
      } catch (err) {
        setActionError(
          apiErrorText(err, t("console.artifacts.actionErrorTitle")),
        );
      }
    },
    [replaceArtifact, t],
  );

  /** 置顶/取消置顶默认看板（wire-personal-preferences）：PUT 偏好
   *  pinned_dashboard_id；成功交容器（onPinnedChange）移动「默认」徽标，
   *  失败（如置顶已归档看板 422）后端 detail 行内红字、徽标不动。 */
  const handlePinToggle = useCallback(
    async (artifactId: number, next: number | null) => {
      setPinError(null);
      try {
        await putPreferences({ pinned_dashboard_id: next });
        onPinnedChange?.(next);
      } catch (err) {
        setPinError({
          artifactId,
          text: apiErrorText(err, t("console.artifacts.actionErrorTitle")),
        });
      }
    },
    [onPinnedChange, t],
  );

  /** 空白建板成功（wire-dashboard-blank-create）：新行置顶（服务端按更新
   *  时间倒序，新建即最新）+ 复位筛选保证新板可见，随即走既有「打开看板」
   *  同款行展开（dashboardFor）并直接进编辑态（freshlyCreatedId）。 */
  const handleDashboardCreated = useCallback((detail: ArtifactDetail) => {
    setArtifacts((prev) => (prev ? [detail, ...prev] : prev));
    setKindFilter("all");
    setStatusFilter("all");
    setCreateOpen(false);
    setFreshlyCreatedId(detail.id);
    setDashboardFor(detail.id);
  }, []);

  /** 空白建表单成功（wire-form-builder 任务 4.4）：与建板同款范式——置顶 +
   *  复位筛选 + 行展开（formFor）直接进构建器（freshlyFormId）。 */
  const handleFormCreated = useCallback((detail: ArtifactDetail) => {
    setArtifacts((prev) => (prev ? [detail, ...prev] : prev));
    setKindFilter("all");
    setStatusFilter("all");
    setCreateFormOpen(false);
    setFreshlyFormId(detail.id);
    setFormFor(detail.id);
  }, []);

  /** 空白建告警规则成功（wire-alert-evaluation）：置顶+复位筛选+行展开
   * （selectedId）直接进 spec 编辑器——模板壳改 dataset/阈值后保存发布。 */
  const handleAlertCreated = useCallback((detail: ArtifactDetail) => {
    setArtifacts((prev) => (prev ? [detail, ...prev] : prev));
    setKindFilter("all");
    setStatusFilter("all");
    setCreateAlertOpen(false);
    setSelectedId(detail.id);
  }, []);

  /** 空白建单记录页成功（wire-record-view 任务 4.1）：与建表单同款范式——
   * 置顶 + 复位筛选 + 行展开（recordViewFor）直接进构建器。 */
  const handleRecordViewCreated = useCallback((detail: ArtifactDetail) => {
    setArtifacts((prev) => (prev ? [detail, ...prev] : prev));
    setKindFilter("all");
    setStatusFilter("all");
    setCreateRecordViewOpen(false);
    setFreshlyRecordViewId(detail.id);
    setRecordViewFor(detail.id);
  }, []);

  /** 空白建文档成功（wire-rich-text-docs 任务 4.2）：与建表单同款范式——
   *  置顶 + 复位筛选 + 行展开（docFor）直接进分栏编辑器。 */
  const handleDocCreated = useCallback((detail: ArtifactDetail) => {
    setArtifacts((prev) => (prev ? [detail, ...prev] : prev));
    setKindFilter("all");
    setStatusFilter("all");
    setCreateDocOpen(false);
    setFreshlyDocId(detail.id);
    setDocFor(detail.id);
  }, []);

  return (
    <div>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">{t("console.artifacts.title")}</h1>
          <p className="mt-1 max-w-2xl text-xs text-slate-500">
            {t("console.artifacts.subtitle")}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {/* 空白建板入口（wire-dashboard-blank-create）：免费档无 cell 无 AI
              草稿时的唯一出生路径——标题输入 → POST → 直接进编辑器。 */}
          {!createOpen && !createFormOpen && !createAlertOpen && !createRecordViewOpen && !createDocOpen && (
            <button
              type="button"
              data-testid="create-dashboard-open"
              onClick={() => setCreateOpen(true)}
              className="rounded bg-indigo-600 px-3 py-1 text-xs text-white hover:bg-indigo-500"
            >
              {t("console.artifacts.create.button")}
            </button>
          )}
          {/* 空白建表单入口（wire-form-builder 任务 4.4）：与新建看板并列的
              出生路径——标题输入 → POST kind=form → 直接进构建器。 */}
          {!createFormOpen && !createOpen && !createAlertOpen && !createRecordViewOpen && !createDocOpen && (
            <button
              type="button"
              data-testid="create-form-open"
              onClick={() => setCreateFormOpen(true)}
              className="rounded bg-emerald-600 px-3 py-1 text-xs text-white hover:bg-emerald-500"
            >
              {t("console.artifacts.createForm.button")}
            </button>
          )}
          {/* 空白建告警规则入口（wire-alert-evaluation）：与建板/表单并列的
              规则出生路径——标题输入 → POST kind=alert_rule 模板壳 → 直接进
              spec 编辑器改 dataset/阈值 → 发布即武装评估。 */}
          {!createAlertOpen && !createOpen && !createFormOpen && !createRecordViewOpen && !createDocOpen && (
            <button
              type="button"
              data-testid="create-alert-open"
              onClick={() => setCreateAlertOpen(true)}
              className="rounded bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-500"
            >
              {t("console.artifacts.createAlert.button")}
            </button>
          )}
          {/* 空白建单记录页入口（wire-record-view 任务 4.1）：与建板/表单并列
              的扫码页出生路径——标题输入 → POST kind=record-view 最小空 spec
              → 直接进构建器选主数据集/键列配模板。 */}
          {!createRecordViewOpen && !createOpen && !createFormOpen && !createAlertOpen && !createDocOpen && (
            <button
              type="button"
              data-testid="create-recordview-open"
              onClick={() => setCreateRecordViewOpen(true)}
              className="rounded bg-cyan-700 px-3 py-1 text-xs text-white hover:bg-cyan-600"
            >
              {t("console.artifacts.createRecordView.button")}
            </button>
          )}
          {/* 空白建文档入口（wire-rich-text-docs 任务 4.5）：与建板/表单并列的
              出生路径——标题输入 → POST kind=doc 空正文最小 spec → 直接进
              分栏编辑器写作。 */}
          {!createDocOpen && !createOpen && !createFormOpen && !createAlertOpen && !createRecordViewOpen && (
            <button
              type="button"
              data-testid="create-doc-open"
              onClick={() => setCreateDocOpen(true)}
              className="rounded bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-500"
            >
              {t("console.doc.create.button", "新建文档")}
            </button>
          )}
          <label htmlFor="artifact-kind-filter" className="text-xs text-slate-500">
            {t("console.artifacts.kindFilterLabel")}
          </label>
          <select
            id="artifact-kind-filter"
            data-testid="artifact-kind-filter"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as ArtifactKind | "all")}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="all">{t("console.artifacts.kindAll")}</option>
            {ARTIFACT_KINDS.map((kind) => (
              <option key={kind} value={kind}>
                {t(`console.artifacts.kinds.${kind}`)}
              </option>
            ))}
          </select>
          <label htmlFor="artifact-status-filter" className="ml-2 text-xs text-slate-500">
            {t("console.artifacts.statusFilterLabel")}
          </label>
          <select
            id="artifact-status-filter"
            data-testid="artifact-status-filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as ArtifactStatus | "all")}
            className="rounded-md border border-slate-300 px-2 py-1 text-sm"
          >
            <option value="all">{t("console.artifacts.statusAll")}</option>
            {ARTIFACT_STATUSES.map((status) => (
              <option key={status} value={status}>
                {t(`console.artifacts.statuses.${status}`)}
              </option>
            ))}
          </select>
        </div>
      </div>

      {createOpen && (
        <CreateDashboardForm
          onCreated={handleDashboardCreated}
          onCancel={() => setCreateOpen(false)}
        />
      )}

      {createFormOpen && (
        <CreateFormForm
          onCreated={handleFormCreated}
          onCancel={() => setCreateFormOpen(false)}
        />
      )}

      {createAlertOpen && (
        <CreateAlertForm
          onCreated={handleAlertCreated}
          onCancel={() => setCreateAlertOpen(false)}
        />
      )}

      {createRecordViewOpen && (
        <CreateRecordViewForm
          onCreated={handleRecordViewCreated}
          onCancel={() => setCreateRecordViewOpen(false)}
        />
      )}

      {createDocOpen && (
        <CreateDocForm
          onCreated={handleDocCreated}
          onCancel={() => setCreateDocOpen(false)}
        />
      )}

      {error && (
        <div className="mt-4 rounded-md border border-red-200 bg-red-50 p-4" data-testid="artifacts-error">
          <p className="text-sm text-red-700">{t("console.artifacts.errorTitle")}</p>
          <button
            type="button"
            onClick={() => void load()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.artifacts.retry")}
          </button>
        </div>
      )}

      {artifacts == null && !error && (
        <p className="mt-6 text-xs text-slate-400" data-testid="artifacts-loading">
          {t("console.artifacts.loading")}
        </p>
      )}

      {artifacts != null && visible.length === 0 && !error && (
        <div className="mt-6 rounded-lg border border-dashed border-slate-300 p-8 text-center" data-testid="artifacts-empty">
          <p className="text-sm text-slate-600">{t("console.artifacts.empty")}</p>
          <p className="mt-1 text-xs text-slate-400">{t("console.artifacts.emptyHint")}</p>
        </div>
      )}

      {actionError && (
        <div
          data-testid="artifact-action-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 px-4 py-2.5 text-xs text-red-700"
        >
          {actionError}
        </div>
      )}

      {visible.length > 0 && (
        <ul data-testid="artifact-list" className="mt-4 space-y-2">
          {visible.map((artifact) => {
            const conv =
              artifact.origin_conversation_id != null
                ? conversationById.get(artifact.origin_conversation_id)
                : undefined;
            // 告警评估态（wire-alert-evaluation）：仅 alert_rule 行查 map；
            // 非 alert_rule 行零变化（不渲染告警面）。
            const alertState =
              artifact.kind === "alert_rule"
                ? alertStates?.[String(artifact.id)]
                : undefined;
            const alertBadge = alertBadgeOf(alertState);
            return (
              <li
                key={artifact.id}
                data-testid="artifact-item"
                data-artifact-id={artifact.id}
                className="rounded-lg border border-slate-200 bg-white p-4"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span
                    data-testid="artifact-kind-badge"
                    data-kind={artifact.kind}
                    className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
                      KIND_BADGE_CLASS[artifact.kind] ??
                      "bg-slate-100 text-slate-600 ring-slate-200"
                    }`}
                  >
                    {t(`console.artifacts.kinds.${artifact.kind}`, artifact.kind)}
                  </span>
                  <span
                    data-testid="artifact-status-badge"
                    data-status={artifact.status}
                    className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                      STATUS_BADGE_CLASS[artifact.status] ?? "bg-slate-100 text-slate-600"
                    }`}
                  >
                    {t(`console.artifacts.statuses.${artifact.status}`, artifact.status)}
                  </span>
                  {artifact.kind === "dashboard" &&
                    artifact.id === pinnedDashboardId && (
                      // 默认看板徽标（wire-personal-preferences）：置顶行专属。
                      <span
                        data-testid="artifact-dashboard-default"
                        className="rounded bg-indigo-100 px-1.5 py-0.5 text-[10px] font-medium text-indigo-700"
                      >
                        {t("console.preferences.defaultBadge")}
                      </span>
                    )}
                  {artifact.kind === "alert_rule" && alertStates != null && (
                    // 告警执行态徽标：states map 命中四态分色；错误态完整
                    // 文案（最近出错时间）走 title，简短文案即「错误」。
                    <span
                      data-testid="alert-state-badge"
                      data-state={alertBadge}
                      title={
                        alertBadge === "error" && alertState?.lastErrorAt != null
                          ? t("console.artifacts.alert.states.errorAt", {
                              time: formatServerDate(alertState.lastErrorAt),
                            })
                          : undefined
                      }
                      className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                        ALERT_STATE_BADGE_CLASS[alertBadge]
                      }`}
                    >
                      {t(`console.artifacts.alert.states.${alertBadge}`)}
                    </span>
                  )}
                  <span className="text-sm font-medium text-slate-800" data-testid="artifact-title">
                    {artifact.title}
                  </span>
                  <span
                    data-testid="artifact-version"
                    className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[10px] text-slate-500"
                  >
                    {t("console.artifacts.versionLabel", { version: artifact.version })}
                  </span>
                  <div className="ml-auto flex flex-wrap gap-2">
                    {artifact.kind === "dashboard" &&
                      (artifact.id === pinnedDashboardId ? (
                        // 取消置顶（wire-personal-preferences）：PUT null 清空。
                        <button
                          type="button"
                          data-testid="artifact-dashboard-unpin"
                          onClick={() => void handlePinToggle(artifact.id, null)}
                          className="rounded border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-50"
                        >
                          {t("console.preferences.unpin")}
                        </button>
                      ) : (
                        // 设为默认看板（wire-personal-preferences）：PUT 本行 id。
                        <button
                          type="button"
                          data-testid="artifact-dashboard-pin"
                          onClick={() =>
                            void handlePinToggle(artifact.id, artifact.id)
                          }
                          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                        >
                          {t("console.preferences.pin")}
                        </button>
                      ))}
                    {artifact.kind === "dashboard" && (
                      <button
                        type="button"
                        data-testid="artifact-dashboard-view"
                        onClick={() =>
                          setDashboardFor(
                            artifact.id === dashboardFor ? null : artifact.id,
                          )
                        }
                        className="rounded border border-indigo-300 px-2 py-1 text-xs text-indigo-700 hover:bg-indigo-50"
                      >
                        {t("console.dashboard.viewButton")}
                      </button>
                    )}
                    {artifact.kind === "dashboard" && artifact.status === "published" && (
                      // 分享入口分发（wire-dashboard-share）：仅 published 且
                      // kind=dashboard——draft/archived/非 dashboard 一律不渲染。
                      <button
                        type="button"
                        data-testid="artifact-share"
                        onClick={() => {
                          setShareKind("dashboard");
                          setShareFor(artifact.id);
                        }}
                        className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-700 hover:bg-sky-50"
                      >
                        {t("console.dashboard.share.button")}
                      </button>
                    )}
                    {artifact.kind === "form" && (
                      // 表单视图入口（wire-form-builder 任务 4.4）：form kind
                      // 内嵌展开 = FormRuntime + 编辑器入口。
                      <button
                        type="button"
                        data-testid="artifact-form-view"
                        onClick={() =>
                          setFormFor(
                            artifact.id === formFor ? null : artifact.id,
                          )
                        }
                        className="rounded border border-emerald-300 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50"
                      >
                        {t("console.form.viewButton")}
                      </button>
                    )}
                    {artifact.kind === "form" && artifact.status === "published" && (
                      // 表单分享入口（wire-form-builder 任务 3.1）：仅 published
                      // form——url 路径 /api/v1/share/form/…，与看板同款弹层。
                      <button
                        type="button"
                        data-testid="artifact-share"
                        onClick={() => {
                          setShareKind("form");
                          setShareFor(artifact.id);
                        }}
                        className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-700 hover:bg-sky-50"
                      >
                        {t("console.dashboard.share.button")}
                      </button>
                    )}
                    {artifact.kind === "record-view" && (
                      // 单记录页视图入口（wire-record-view 任务 4.1）：record-view
                      // kind 内嵌展开 = 查看态试预览 + 构建器入口。
                      <button
                        type="button"
                        data-testid="artifact-recordview-view"
                        onClick={() =>
                          setRecordViewFor(
                            artifact.id === recordViewFor ? null : artifact.id,
                          )
                        }
                        className="rounded border border-cyan-300 px-2 py-1 text-xs text-cyan-700 hover:bg-cyan-50"
                      >
                        {t("console.recordview.viewButton")}
                      </button>
                    )}
                    {artifact.kind === "record-view" &&
                      artifact.status === "published" && (
                        // 单记录页分享+二维码入口（wire-record-view 任务 4.3）：
                        // 仅 published record-view——专属弹层（键值选择/URL/QR）。
                        <button
                          type="button"
                          data-testid="artifact-recordview-share"
                          onClick={() => setShareRecordViewFor(artifact.id)}
                          className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-700 hover:bg-sky-50"
                        >
                          {t("console.recordview.share.button")}
                        </button>
                      )}
                    {artifact.kind === "doc" && (
                      // 文档编辑器入口（wire-rich-text-docs 任务 4.2）：doc kind
                      // 内嵌展开 = 分栏编辑器（Markdown 源 + 实时预览 + 草稿层
                      // 自动保存 + 保存版本）。
                      <button
                        type="button"
                        data-testid="artifact-doc-view"
                        onClick={() =>
                          setDocFor(artifact.id === docFor ? null : artifact.id)
                        }
                        className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-700 hover:bg-amber-50"
                      >
                        {t("console.doc.viewButton", "打开文档")}
                      </button>
                    )}
                    {artifact.kind === "doc" && artifact.status === "published" && (
                      // 文档分享入口（wire-rich-text-docs 任务 4.2/D5）：仅
                      // published doc——极简弹层（TTL 四档 + 复制链接）。
                      <button
                        type="button"
                        data-testid="artifact-doc-share"
                        onClick={() => setShareDocFor(artifact.id)}
                        className="rounded border border-sky-300 px-2 py-1 text-xs text-sky-700 hover:bg-sky-50"
                      >
                        {t("console.doc.share.button", "生成分享链接")}
                      </button>
                    )}
                    {artifact.status === "draft" && (
                      <button
                        type="button"
                        data-testid="artifact-publish"
                        onClick={() => void runAction(artifact, "publish")}
                        className="rounded border border-emerald-300 px-2 py-1 text-xs text-emerald-700 hover:bg-emerald-50"
                      >
                        {t("console.artifacts.publish")}
                      </button>
                    )}
                    {artifact.status !== "archived" && (
                      <button
                        type="button"
                        data-testid="artifact-archive"
                        onClick={() => void runAction(artifact, "archive")}
                        className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                      >
                        {t("console.artifacts.archive")}
                      </button>
                    )}
                    {artifact.status === "archived" && (
                      <button
                        type="button"
                        data-testid="artifact-restore"
                        onClick={() => void runAction(artifact, "restore")}
                        className="rounded border border-amber-300 px-2 py-1 text-xs text-amber-700 hover:bg-amber-50"
                      >
                        {t("console.artifacts.restore")}
                      </button>
                    )}
                    <button
                      type="button"
                      data-testid="artifact-history"
                      onClick={() => setHistoryFor(artifact.id)}
                      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {t("console.artifacts.history")}
                    </button>
                    <button
                      type="button"
                      data-testid="artifact-view"
                      onClick={() => setSelectedId(artifact.id === selectedId ? null : artifact.id)}
                      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
                    >
                      {t("console.artifacts.viewSpec")}
                    </button>
                  </div>
                </div>
                {pinError != null && pinError.artifactId === artifact.id && (
                  // 置顶失败行内红字（wire-personal-preferences）：后端 detail
                  //（如置顶已归档看板 422）如实呈现，徽标不动。
                  <p
                    data-testid="artifact-pin-error"
                    className="mt-1 text-[11px] text-red-600"
                  >
                    {pinError.text}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
                  <span data-testid="artifact-updated">
                    {t("console.artifacts.updatedLabel")} {formatServerDate(artifact.updated_at)}
                  </span>
                  {artifact.kind === "alert_rule" &&
                    alertState?.lastEvaluatedAt != null && (
                      // 最近评估时间（照页内既有时间惯例 formatServerDate）。
                      <>
                        <span>·</span>
                        <span data-testid="alert-last-evaluated">
                          {t("console.artifacts.alert.lastEvaluated")}{" "}
                          {formatServerDate(alertState.lastEvaluatedAt)}
                        </span>
                      </>
                    )}
                  {artifact.origin_conversation_id != null ? (
                    <>
                      <span>·</span>
                      <span>{t("console.artifacts.source")}</span>
                      <a
                        data-testid="artifact-source-link"
                        href={`#/console/chat?c=${artifact.origin_conversation_id}`}
                        className="text-slate-600 underline decoration-dotted hover:text-slate-900"
                      >
                        {conv ? conv.title : `#${artifact.origin_conversation_id}`}
                      </a>
                    </>
                  ) : (
                    <span data-testid="artifact-source-none">{t("console.artifacts.sourceNone")}</span>
                  )}
                </div>
                {artifact.id === selectedId && (
                  <SpecViewer
                    key={`viewer-${artifact.id}`}
                    artifactId={artifact.id}
                    onSaved={replaceArtifact}
                  />
                )}
                {artifact.id === selectedId && artifact.kind === "alert_rule" && (
                  // 告警执行面（wire-alert-evaluation）：alert_rule 行展开
                  // （查看 spec）时随 spec 编辑器一并呈现——事件列表 + 试运行。
                  <AlertPane key={`alert-${artifact.id}`} artifactId={artifact.id} />
                )}
                {artifact.id === dashboardFor && (
                  <DashboardPane
                    key={`dashboard-${artifact.id}`}
                    artifactId={artifact.id}
                    onSaved={replaceArtifact}
                    // 刚空白创建的板直接进编辑态（创建即跳编辑器）。
                    startEditing={artifact.id === freshlyCreatedId}
                  />
                )}
                {artifact.id === formFor && (
                  <FormPane
                    key={`form-${artifact.id}`}
                    artifactId={artifact.id}
                    onSaved={replaceArtifact}
                    // 刚空白创建的表单直接进构建器（创建即跳编辑器）。
                    startEditing={artifact.id === freshlyFormId}
                  />
                )}
                {artifact.id === recordViewFor && (
                  <RecordViewPane
                    key={`record-view-${artifact.id}`}
                    artifactId={artifact.id}
                    onSaved={replaceArtifact}
                    // 刚空白创建的单记录页直接进构建器（创建即跳编辑器）。
                    startEditing={artifact.id === freshlyRecordViewId}
                  />
                )}
                {artifact.id === docFor && (
                  // 文档分栏编辑器（wire-rich-text-docs 任务 4.2）：挂载即读
                  // 草稿层（无则回退当前版本 spec.text）；刚空白创建的文档
                  // 直接聚焦正文（创建即跳编辑器）。
                  <DocEditor
                    key={`doc-${artifact.id}`}
                    artifactId={artifact.id}
                    onSaved={replaceArtifact}
                    startEditing={artifact.id === freshlyDocId}
                  />
                )}
                {artifact.status === "published" && artifact.kind !== "doc" && (
                  // 产物公开分享选择器（wire-artifact-marketplace 5.1）：仅
                  // published 行挂载——四档可见性 + 开放模式 + 允许复制；
                  // 非私有档由服务端再校验（409 双保险）。
                  // wire-rich-text-docs 任务 4.3（design D5）：doc 不进市场/
                  // 物化，隐藏可见性设置区——服务端 422 是兜底，UI 不给入口
                  // 才能避免「看得见点不动」的假 UI。
                  <ArtifactVisibilitySection
                    key={`visibility-${artifact.id}`}
                    artifactId={artifact.id}
                    artifactStatus={artifact.status}
                    canEdit={true}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}

      {historyFor != null && (
        <VersionDrawer artifactId={historyFor} onClose={() => setHistoryFor(null)} />
      )}

      {shareFor != null && (
        <ShareLinkDialog
          key={`share-${shareFor}`}
          artifactId={shareFor}
          kind={shareKind}
          onClose={() => setShareFor(null)}
        />
      )}

      {shareRecordViewFor != null && (
        <RecordViewShareDialog
          key={`record-view-share-${shareRecordViewFor}`}
          artifactId={shareRecordViewFor}
          onClose={() => setShareRecordViewFor(null)}
        />
      )}

      {shareDocFor != null && (
        <DocShareDialog
          key={`doc-share-${shareDocFor}`}
          artifactId={shareDocFor}
          onClose={() => setShareDocFor(null)}
        />
      )}
    </div>
  );
}

/**
 * 新建看板表单（wire-dashboard-blank-create 任务 1.2）：标题输入 →
 * POST /api/v1/artifacts {kind:"dashboard", title}。空标题前端先拦（不发
 * 请求）；创建中禁用防重复提交；失败（422/网络等）后端 detail 如实呈现、
 * 表单不关可改再试；成功交 onCreated（置顶 + 展开直接进编辑器）。
 */
function CreateDashboardForm({
  onCreated,
  onCancel,
}: {
  onCreated: (artifact: ArtifactDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (creating) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError(t("console.artifacts.create.titleRequired"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      onCreated(await createBlankDashboard(trimmed));
    } catch (err) {
      setError(apiErrorText(err, t("console.artifacts.create.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="create-dashboard-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="create-dashboard-title" className="block text-xs text-slate-500">
        {t("console.artifacts.create.titleLabel")}
      </label>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          id="create-dashboard-title"
          data-testid="create-dashboard-title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setError(null);
          }}
          placeholder={t("console.artifacts.create.titlePlaceholder")}
          disabled={creating}
          className="w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <button
          type="submit"
          data-testid="create-dashboard-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.artifacts.create.creating")
            : t("console.artifacts.create.submit")}
        </button>
        <button
          type="button"
          data-testid="create-dashboard-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.artifacts.create.cancel")}
        </button>
      </div>
      {error && (
        <p data-testid="create-dashboard-error" className="mt-1.5 text-[11px] text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * 新建表单表单（wire-form-builder 任务 4.4）：与新建看板同款范式——标题
 * 输入 → POST /api/v1/artifacts {kind:"form", title}。空标题前端先拦（不发
 * 请求）；创建中禁用防重复提交；失败（422/网络等）后端 detail 如实呈现、
 * 表单不关可改再试；成功交 onCreated（置顶 + 展开直接进构建器）。
 */
function CreateFormForm({
  onCreated,
  onCancel,
}: {
  onCreated: (artifact: ArtifactDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (creating) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError(t("console.artifacts.createForm.titleRequired"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      onCreated(await createBlankForm(trimmed));
    } catch (err) {
      setError(apiErrorText(err, t("console.artifacts.createForm.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="create-form-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="create-form-title" className="block text-xs text-slate-500">
        {t("console.artifacts.createForm.titleLabel")}
      </label>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          id="create-form-title"
          data-testid="create-form-title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setError(null);
          }}
          placeholder={t("console.artifacts.createForm.titlePlaceholder")}
          disabled={creating}
          className="w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <button
          type="submit"
          data-testid="create-form-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.artifacts.createForm.creating")
            : t("console.artifacts.createForm.submit")}
        </button>
        <button
          type="button"
          data-testid="create-form-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.artifacts.createForm.cancel")}
        </button>
      </div>
      {error && (
        <p data-testid="create-form-error" className="mt-1.5 text-[11px] text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}

function CreateAlertForm({
  onCreated,
  onCancel,
}: {
  onCreated: (artifact: ArtifactDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (creating) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError(t("console.artifacts.createAlert.titleRequired"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      onCreated(await createBlankAlertRule(trimmed));
    } catch (err) {
      setError(apiErrorText(err, t("console.artifacts.createAlert.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="create-alert-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="create-alert-title" className="block text-xs text-slate-500">
        {t("console.artifacts.createAlert.titleLabel")}
      </label>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          id="create-alert-title"
          data-testid="create-alert-title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setError(null);
          }}
          placeholder={t("console.artifacts.createAlert.titlePlaceholder")}
          disabled={creating}
          className="w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <button
          type="submit"
          data-testid="create-alert-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.artifacts.createAlert.creating")
            : t("console.artifacts.createAlert.submit")}
        </button>
        <button
          type="button"
          data-testid="create-alert-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.artifacts.createAlert.cancel")}
        </button>
      </div>
      {error && (
        <p data-testid="create-alert-error" className="mt-1.5 text-[11px] text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * 新建单记录页表单（wire-record-view 任务 4.1）：与新建表单同款范式——标题
 * 输入 → POST /api/v1/artifacts {kind:"record-view", title}。空标题前端先拦
 * （不发请求）；创建中禁用防重复提交；失败（422/网络等）后端 detail 如实
 * 呈现、表单不关可改再试；成功交 onCreated（置顶 + 展开直接进构建器）。
 */
function CreateRecordViewForm({
  onCreated,
  onCancel,
}: {
  onCreated: (artifact: ArtifactDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (creating) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError(t("console.artifacts.createRecordView.titleRequired"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      onCreated(await createBlankRecordView(trimmed));
    } catch (err) {
      setError(apiErrorText(err, t("console.artifacts.createRecordView.failed")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="create-recordview-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="create-recordview-title" className="block text-xs text-slate-500">
        {t("console.artifacts.createRecordView.titleLabel")}
      </label>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          id="create-recordview-title"
          data-testid="create-recordview-title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setError(null);
          }}
          placeholder={t("console.artifacts.createRecordView.titlePlaceholder")}
          disabled={creating}
          className="w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <button
          type="submit"
          data-testid="create-recordview-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.artifacts.createRecordView.creating")
            : t("console.artifacts.createRecordView.submit")}
        </button>
        <button
          type="button"
          data-testid="create-recordview-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.artifacts.createRecordView.cancel")}
        </button>
      </div>
      {error && (
        <p data-testid="create-recordview-error" className="mt-1.5 text-[11px] text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * 新建文档表单（wire-rich-text-docs 任务 4.5）：与新建表单同款范式——标题
 * 输入 → POST /api/v1/artifacts {kind:"doc", title}。空标题前端先拦（不发
 * 请求）；创建中禁用防重复提交；失败（422/网络等）后端 detail 如实呈现、
 * 表单不关可改再试；成功交 onCreated（置顶 + 展开直接进分栏编辑器）。
 */
function CreateDocForm({
  onCreated,
  onCancel,
}: {
  onCreated: (artifact: ArtifactDetail) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [title, setTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    if (creating) return;
    const trimmed = title.trim();
    if (!trimmed) {
      setError(t("console.doc.create.titleRequired", "请先填写文档标题"));
      return;
    }
    setCreating(true);
    setError(null);
    try {
      onCreated(await createBlankDoc(trimmed));
    } catch (err) {
      setError(apiErrorText(err, t("console.doc.create.failed", "创建文档失败，请重试")));
    } finally {
      setCreating(false);
    }
  };

  return (
    <form
      data-testid="create-doc-form"
      className="mt-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(e) => {
        e.preventDefault();
        void submit();
      }}
    >
      <label htmlFor="create-doc-title" className="block text-xs text-slate-500">
        {t("console.doc.create.titleLabel", "文档标题")}
      </label>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          id="create-doc-title"
          data-testid="create-doc-title"
          value={title}
          onChange={(e) => {
            setTitle(e.target.value);
            setError(null);
          }}
          placeholder={t("console.doc.create.titlePlaceholder", "如：季度经营说明")}
          disabled={creating}
          className="w-64 rounded-md border border-slate-300 px-2 py-1 text-sm disabled:opacity-50"
        />
        <button
          type="submit"
          data-testid="create-doc-submit"
          disabled={creating}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {creating
            ? t("console.doc.create.creating", "创建中…")
            : t("console.doc.create.submit", "创建")}
        </button>
        <button
          type="button"
          data-testid="create-doc-cancel"
          disabled={creating}
          onClick={onCancel}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {t("console.doc.create.cancel", "取消")}
        </button>
      </div>
      {error && (
        <p data-testid="create-doc-error" className="mt-1.5 text-[11px] text-red-600">
          {error}
        </p>
      )}
    </form>
  );
}

/**
 * 文档分享弹层（wire-rich-text-docs 任务 4.2/D5）：doc 无键值（不像
 * record-view 需要键选择器/二维码），故极简——TTL 四档（0|7|30|90 同
 * record-view 白名单，0=不设期·印刷物）+ 复制完整公开链接。打开即生成；
 * 改选 TTL 重新生成（令牌随生成时刻钉死版本）。展示链接自构：origin +
 * /#/share/doc/{id}?t=…&v={version}（t 从响应数据端点 url 解出）。
 */
function DocShareDialog({
  artifactId,
  onClose,
}: {
  artifactId: number;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [ttlDays, setTtlDays] = useState<RecordViewTtlDays>(30);
  const [result, setResult] = useState<DocShareLink | null>(null);
  const [shareUrl, setShareUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    setResult(null);
    setShareUrl("");
    setError(null);
    setCopied(false);
    createDocShareLink(artifactId, ttlDays)
      .then((res) => {
        if (!alive) return;
        setResult(res);
        // t 从数据端点 url 的 query 解出（相对路径也能被 URL 解析出 search）。
        const token =
          new URL(res.url, window.location.origin).searchParams.get("t") ?? "";
        setShareUrl(
          `${window.location.origin}/#/share/doc/${artifactId}` +
            `?t=${encodeURIComponent(token)}&v=${res.version}`,
        );
      })
      .catch((err: unknown) => {
        // 服务端 detail 如实呈现（403 状态说明 / 503 未配置 secret 原因）。
        if (alive) {
          setError(apiErrorText(err, t("console.doc.share.failed", "生成分享链接失败，请稍后重试。")));
        }
      });
    return () => {
      alive = false;
    };
  }, [artifactId, ttlDays, t]);

  /** 到期展示：0 档（expires_at=null）呈「不设期（印刷物）」——不定死文案，
   *  复用 record-view 同语义键位。 */
  const expiresText = result
    ? result.expires_at == null
      ? t("console.doc.share.noExpiry", "不设期（印刷物档）")
      : formatServerDate(result.expires_at)
    : ttlDays === 0
      ? t("console.doc.share.noExpiry", "不设期（印刷物档）")
      : formatServerDate(
          new Date(Date.now() + ttlDays * 86_400_000).toISOString().slice(0, 19),
        );

  const copyLink = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
    } catch {
      /* 剪贴板不可用（如非安全上下文）：如实不提示已复制 */
    }
  };

  return (
    <div
      data-testid="doc-share-dialog"
      className="fixed inset-0 z-40 flex items-center justify-center p-4"
    >
      <button
        type="button"
        aria-label={t("console.dashboard.share.close")}
        data-testid="doc-share-backdrop"
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/30"
      />
      <div className="relative w-full max-w-lg rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div className="flex items-center gap-2">
          <h2 data-testid="doc-share-title" className="text-sm font-semibold">
            {t("console.doc.share.dialogTitle", "文档分享")}
          </h2>
          <button
            type="button"
            data-testid="doc-share-close"
            onClick={onClose}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.dashboard.share.close")}
          </button>
        </div>

        {error && (
          <p data-testid="doc-share-error" className="mt-4 text-xs text-red-600">
            {error}
          </p>
        )}

        {!error && (
          <div data-testid="doc-share-ttl" className="mt-4">
            <p className="text-xs font-medium text-slate-600">
              {t("console.doc.share.ttlLabel", "有效期")}
            </p>
            <div
              role="radiogroup"
              aria-label={t("console.doc.share.ttlLabel", "有效期")}
              className="mt-1.5 flex flex-wrap gap-4"
            >
                {RECORD_VIEW_TTL_DAYS.map((days) => (
                <label key={days} className="flex items-center gap-1 text-xs text-slate-700">
                  <input
                    type="radio"
                    data-testid="doc-share-ttl-option"
                    data-days={days}
                    name={`doc-share-ttl-${artifactId}`}
                    checked={ttlDays === days}
                    onChange={() => setTtlDays(days)}
                  />
                  {/* 档位文案复用 record-view 分享的既有键（同一 TTL 白名单
                      0|7|30|90、同一语义——不为同义文案重复造键）。 */}
                  {t(`console.recordview.share.ttlOptions.${days}`)}
                </label>
              ))}
            </div>
            <p className="mt-1 text-[11px] text-slate-400">
              {t("console.recordview.share.ttlNote")}
            </p>
          </div>
        )}

        {!error && result == null && (
          <p data-testid="doc-share-generating" className="mt-4 text-xs text-slate-400">
            {t("console.dashboard.share.generating")}
          </p>
        )}

        {result != null && (
          <div className="mt-4 space-y-3">
            <div>
              <p className="text-xs font-medium text-slate-600">
                {t("console.dashboard.share.urlLabel")}
              </p>
              {/* readonly 输入框承载完整链接：可选中复制、长链接不撑破布局。 */}
              <input
                data-testid="doc-share-url"
                readOnly
                value={shareUrl}
                onFocus={(e) => e.currentTarget.select()}
                className="mt-1 w-full rounded border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-[11px] text-slate-700"
              />
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  data-testid="doc-share-copy"
                  disabled={!shareUrl}
                  onClick={() => void copyLink()}
                  className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
                >
                  {t("console.doc.share.copy", "复制链接")}
                </button>
                {copied && (
                  <span data-testid="doc-share-copied" className="text-[11px] text-emerald-600">
                    {t("console.doc.share.copied", "已复制到剪贴板。")}
                  </span>
                )}
              </div>
            </div>
            <p data-testid="doc-share-expires" className="text-xs text-slate-500">
              {t("console.dashboard.share.expiresLabel")}：{expiresText}
            </p>
            <p className="text-[11px] text-slate-400">
              {t("console.dashboard.share.versionNote", { version: result.version })}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

function SpecViewer({
  artifactId,
  onSaved,
}: {
  artifactId: number;
  onSaved: (artifact: Artifact) => void;
}) {
  const { t } = useTranslation();
  const storageKey = `${BUFFER_STORAGE_PREFIX}${artifactId}`;
  const [serverSpec, setServerSpec] = useState<Record<string, unknown> | null>(null);
  const [status, setStatus] = useState<string>("draft");
  const [loadFailed, setLoadFailed] = useState(false);
  const [text, setText] = useState("");
  const [fromLocal, setFromLocal] = useState(false);
  const [invalid, setInvalid] = useState(false);
  const [saving, setSaving] = useState(false);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoadFailed(false);
    setSavedVersion(null);
    setInvalid(false);
    setServerError(null);
    fetchLifecycleArtifact(artifactId)
      .then((detail) => {
        if (!alive) return;
        setServerSpec(detail.spec);
        setStatus(detail.status);
        const original = JSON.stringify(detail.spec, null, 2);
        try {
          const buffered = localStorage.getItem(storageKey);
          if (buffered != null && buffered !== original) {
            setText(buffered);
            setFromLocal(true);
            return;
          }
        } catch {
          /* localStorage 不可用：仅展示服务端 spec */
        }
        setText(original);
        setFromLocal(false);
      })
      .catch(() => {
        if (alive) setLoadFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId, storageKey]);

  const locked = status === "archived";

  const editText = (next: string) => {
    setText(next);
    setInvalid(false);
    setSavedVersion(null);
    setServerError(null);
    // localStorage 退位为未保存离开提示的本地缓冲：编辑即自动暂存，
    // 保存到服务端成功或显式丢弃时清除。
    if (!locked) {
      try {
        localStorage.setItem(storageKey, next);
        setFromLocal(next !== (serverSpec ? JSON.stringify(serverSpec, null, 2) : next));
      } catch {
        /* 存不进去时仅本次会话可见 */
      }
    }
  };

  const saveToServer = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      setInvalid(true);
      setSavedVersion(null);
      return;
    }
    if (typeof parsed !== "object" || parsed === null) {
      setInvalid(true);
      return;
    }
    setInvalid(false);
    setSaving(true);
    setServerError(null);
    try {
      const detail = await saveArtifactSpec(artifactId, parsed as Record<string, unknown>);
      setSavedVersion(detail.version);
      setServerSpec(detail.spec);
      setStatus(detail.status);
      setText(JSON.stringify(detail.spec, null, 2));
      setFromLocal(false);
      try {
        localStorage.removeItem(storageKey);
      } catch {
        /* 清不掉时下次打开仍提示缓冲 */
      }
      onSaved(detail);
    } catch (err) {
      setServerError(apiErrorText(err, t("console.artifacts.viewer.saveFailed")));
    } finally {
      setSaving(false);
    }
  };

  const discardBuffer = () => {
    try {
      localStorage.removeItem(storageKey);
    } catch {
      /* 同上 */
    }
    if (serverSpec != null) setText(JSON.stringify(serverSpec, null, 2));
    setFromLocal(false);
    setInvalid(false);
    setSavedVersion(null);
    setServerError(null);
  };

  if (loadFailed) {
    return (
      <div data-testid="artifact-viewer" className="mt-3 rounded-md border border-red-200 bg-red-50/60 p-4">
        <p className="text-xs text-red-700">{t("console.artifacts.viewer.loadError")}</p>
      </div>
    );
  }

  if (serverSpec == null) {
    return (
      <div data-testid="artifact-viewer" className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4">
        <p className="text-xs text-slate-400">{t("console.artifacts.loading")}</p>
      </div>
    );
  }

  return (
    <div data-testid="artifact-viewer" className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("console.artifacts.viewer.title")}</h2>
        {fromLocal && (
          <span data-testid="spec-buffer-badge" className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700">
            {t("console.artifacts.viewer.bufferBadge")}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          {!locked && (
            <button
              type="button"
              data-testid="spec-save-server"
              disabled={saving}
              onClick={() => void saveToServer()}
              className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
            >
              {saving
                ? t("console.artifacts.viewer.saving")
                : t("console.artifacts.viewer.saveServer")}
            </button>
          )}
          {fromLocal && !locked && (
            <button
              type="button"
              data-testid="spec-discard-buffer"
              onClick={discardBuffer}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.artifacts.viewer.discardBuffer")}
            </button>
          )}
        </div>
      </div>
      <p className="mt-2 text-[11px] text-slate-500">
        {locked
          ? t("console.artifacts.viewer.locked")
          : t("console.artifacts.viewer.note")}
      </p>
      {savedVersion != null && (
        <p data-testid="spec-saved-version" className="mt-1 text-[11px] text-emerald-600">
          {t("console.artifacts.viewer.savedVersion", { version: savedVersion })}
        </p>
      )}
      {serverError && (
        <p data-testid="spec-server-error" className="mt-1 text-[11px] text-red-600">
          {serverError}
        </p>
      )}
      {invalid && (
        <p data-testid="spec-invalid-json" className="mt-1 text-[11px] text-red-600">
          {t("console.artifacts.viewer.invalidJson")}
        </p>
      )}
      <textarea
        data-testid="spec-editor"
        value={text}
        readOnly={locked}
        onChange={(e) => editText(e.target.value)}
        rows={Math.min(20, Math.max(8, text.split("\n").length + 1))}
        spellCheck={false}
        className="mt-2 w-full resize-y rounded-md border border-slate-300 bg-white p-3 font-mono text-xs text-slate-800 focus:outline-none focus:ring-2 focus:ring-slate-400"
      />
    </div>
  );
}

function VersionDrawer({
  artifactId,
  onClose,
}: {
  artifactId: number;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [versions, setVersions] = useState<ArtifactVersionSummary[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [selected, setSelected] = useState<ArtifactVersionDetail | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    setVersions(null);
    setFailed(false);
    setSelected(null);
    fetchArtifactVersions(artifactId)
      .then((rows) => {
        if (alive) setVersions(rows);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId]);

  const openVersion = async (version: number) => {
    setCopied(false);
    try {
      setSelected(await fetchArtifactVersion(artifactId, version));
    } catch {
      setSelected(null);
    }
  };

  const copySpec = async () => {
    if (selected == null) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(selected.spec, null, 2));
      setCopied(true);
    } catch {
      /* 剪贴板不可用（如非安全上下文）：如实不提示已复制 */
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end" data-testid="version-drawer">
      <button
        type="button"
        aria-label={t("console.artifacts.versions.close")}
        data-testid="version-drawer-backdrop"
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/30"
      />
      <aside className="relative flex h-full w-full max-w-xl flex-col overflow-y-auto border-l border-slate-200 bg-white p-5 shadow-xl">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold">{t("console.artifacts.versions.title")}</h2>
          <button
            type="button"
            data-testid="version-drawer-close"
            onClick={onClose}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.artifacts.versions.close")}
          </button>
        </div>

        {failed && (
          <p className="mt-4 text-xs text-red-600" data-testid="version-list-error">
            {t("console.artifacts.versions.fetchError")}
          </p>
        )}
        {versions == null && !failed && (
          <p className="mt-4 text-xs text-slate-400" data-testid="version-list-loading">
            {t("console.artifacts.versions.loading")}
          </p>
        )}
        {versions != null && versions.length === 0 && (
          <p className="mt-4 text-xs text-slate-400" data-testid="version-list-empty">
            {t("console.artifacts.versions.empty")}
          </p>
        )}

        <ul data-testid="version-list" className="mt-4 space-y-2">
          {versions?.map((entry) => (
            <li key={entry.version}>
              <button
                type="button"
                data-testid="version-item"
                data-version={entry.version}
                onClick={() => void openVersion(entry.version)}
                className={`w-full rounded-md border px-3 py-2 text-left text-xs ${
                  selected?.version === entry.version
                    ? "border-slate-900 bg-slate-50"
                    : "border-slate-200 hover:bg-slate-50"
                }`}
              >
                <span className="font-mono font-medium text-slate-800">
                  {t("console.artifacts.versions.entry", { version: entry.version })}
                </span>
                <span className="ml-2 text-slate-400">
                  {t("console.artifacts.versions.operatorLabel")}: {entry.operator ?? "-"}
                </span>
                <span className="ml-2 text-slate-400">{formatServerDate(entry.created_at)}</span>
              </button>
            </li>
          ))}
        </ul>

        {selected != null && (
          <div data-testid="version-detail" className="mt-4 rounded-md border border-slate-200 bg-slate-50/60 p-4">
            <div className="flex items-center gap-2">
              <h3 className="text-xs font-semibold">
                {t("console.artifacts.versions.entry", { version: selected.version })}
              </h3>
              <button
                type="button"
                data-testid="version-copy"
                onClick={() => void copySpec()}
                className="ml-auto rounded border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
              >
                {t("console.artifacts.versions.copy")}
              </button>
            </div>
            {copied && (
              <p data-testid="version-copied" className="mt-1 text-[11px] text-emerald-600">
                {t("console.artifacts.versions.copied")}
              </p>
            )}
            <pre
              data-testid="version-spec"
              className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-all rounded bg-white p-3 font-mono text-[11px] text-slate-700"
            >
              {JSON.stringify(selected.spec, null, 2)}
            </pre>
          </div>
        )}
      </aside>
    </div>
  );
}

/**
 * 分享链接弹层（wire-dashboard-share 任务 2.2；wire-form-builder 任务 3.1
 * 起 kind 分派）：打开即调 POST /api/v1/artifacts/{id}/share，展示完整公开
 * 页链接 + 复制按钮 + 到期时间。响应的 url 是**数据端点**
 * （/api/v1/share/dashboard|form/...），给接收者的展示链接必须另行构造：
 * origin + /#/share/{kind}/{id}?t=…&v={version}（t 从响应 url 的 query 里
 * 解出，版本钉死生成时刻的响应 version——form 链接路径即 /share/form/）。
 * wire-share-ttl-options 任务 2.1：有效期三选一（7/30/90 天，缺省 30）——
 * 改选即按新 ttl_days 重新生成；到期时间随选联动（生成中显示本地预览，
 * 响应到达后以 expires_at 覆盖）。
 */
function ShareLinkDialog({
  artifactId,
  kind = "dashboard",
  onClose,
}: {
  artifactId: number;
  /** 分享目标 kind：dashboard（缺省）| form（wire-form-builder）——决定
   *  生成调用与公开页链接路径段。 */
  kind?: "dashboard" | "form";
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [ttlDays, setTtlDays] = useState<ShareTtlDays>(30);
  const [result, setResult] = useState<DashboardShareLink | null>(null);
  const [shareUrl, setShareUrl] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    setResult(null);
    setShareUrl("");
    setError(null);
    setCopied(false);
    const create = kind === "form" ? createFormShareLink : createDashboardShareLink;
    create(artifactId, ttlDays)
      .then((res) => {
        if (!alive) return;
        setResult(res);
        // t 从数据端点 url 的 query 解出（相对路径也能被 URL 解析出 search）。
        const token = new URL(res.url, window.location.origin).searchParams.get("t") ?? "";
        setShareUrl(
          `${window.location.origin}/#/share/${kind}/${artifactId}` +
            `?t=${encodeURIComponent(token)}&v=${res.version}`,
        );
      })
      .catch((err: unknown) => {
        // 服务端 detail 如实呈现（403 状态说明 / 503 未配置 secret 原因）。
        if (alive) setError(apiErrorText(err, t("console.dashboard.share.generateFailed")));
      });
    return () => {
      alive = false;
    };
  }, [artifactId, kind, ttlDays, t]);

  /** 到期展示值：有响应用服务端 expires_at（覆盖本地预览），生成中按当前
   *  有效期本地预览（now + ttlDays，naive UTC 与后端同口径）。 */
  const expiresText = result
    ? formatServerDate(result.expires_at)
    : formatServerDate(
        new Date(Date.now() + ttlDays * 86_400_000).toISOString().slice(0, 19),
      );

  const copyLink = async () => {
    if (!shareUrl) return;
    try {
      await navigator.clipboard.writeText(shareUrl);
      setCopied(true);
    } catch {
      /* 剪贴板不可用（如非安全上下文）：如实不提示已复制 */
    }
  };

  return (
    <div
      data-testid="share-dialog"
      className="fixed inset-0 z-40 flex items-center justify-center p-4"
    >
      <button
        type="button"
        aria-label={t("console.dashboard.share.close")}
        data-testid="share-dialog-backdrop"
        onClick={onClose}
        className="absolute inset-0 bg-slate-900/30"
      />
      <div className="relative w-full max-w-lg rounded-lg border border-slate-200 bg-white p-5 shadow-xl">
        <div className="flex items-center gap-2">
          <h2 data-testid="share-dialog-title" className="text-sm font-semibold">
            {t("console.dashboard.share.dialogTitle")}
          </h2>
          <button
            type="button"
            data-testid="share-dialog-close"
            onClick={onClose}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {t("console.dashboard.share.close")}
          </button>
        </div>

        {error && (
          <p data-testid="share-error" className="mt-4 text-xs text-red-600">
            {error}
          </p>
        )}

        {!error && (
          <div data-testid="share-ttl" className="mt-4">
            <p className="text-xs font-medium text-slate-600">
              {t("console.dashboard.share.ttlLabel")}
            </p>
            <div
              role="radiogroup"
              aria-label={t("console.dashboard.share.ttlLabel")}
              className="mt-1.5 flex flex-wrap gap-4"
            >
              {SHARE_TTL_DAYS.map((days) => (
                <label key={days} className="flex items-center gap-1 text-xs text-slate-700">
                  <input
                    type="radio"
                    data-testid="share-ttl-option"
                    data-days={days}
                    name={`share-ttl-${artifactId}`}
                    checked={ttlDays === days}
                    onChange={() => setTtlDays(days)}
                  />
                  {t(`console.dashboard.share.ttlOptions.${days}`)}
                </label>
              ))}
            </div>
            <p className="mt-1 text-[11px] text-slate-400">
              {t("console.dashboard.share.ttlNote")}
            </p>
          </div>
        )}

        {!error && result == null && (
          <p data-testid="share-generating" className="mt-4 text-xs text-slate-400">
            {t("console.dashboard.share.generating")}
          </p>
        )}

        {result != null && (
          <div className="mt-4 space-y-3">
            <div>
              <p className="text-xs font-medium text-slate-600">
                {t("console.dashboard.share.urlLabel")}
              </p>
              {/* readonly 输入框承载完整链接：可选中复制、长链接不撑破布局。 */}
              <input
                data-testid="share-url"
                readOnly
                value={shareUrl}
                onFocus={(e) => e.currentTarget.select()}
                className="mt-1 w-full rounded border border-slate-300 bg-slate-50 px-2 py-1.5 font-mono text-[11px] text-slate-700"
              />
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  data-testid="share-copy"
                  onClick={() => void copyLink()}
                  className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700"
                >
                  {t("console.dashboard.share.copy")}
                </button>
                {copied && (
                  <span data-testid="share-copied" className="text-[11px] text-emerald-600">
                    {t("console.dashboard.share.copied")}
                  </span>
                )}
              </div>
            </div>
            <p data-testid="share-expires" className="text-xs text-slate-500">
              {t("console.dashboard.share.expiresLabel")}：{expiresText}
            </p>
            <p className="text-[11px] text-slate-400">
              {t("console.dashboard.share.versionNote", { version: result.version })}
            </p>
          </div>
        )}

        {/* 生成中（改选有效期重新生成）：本地预览到期时间随选联动。 */}
        {!error && result == null && (
          <p data-testid="share-expires" className="mt-2 text-xs text-slate-400">
            {t("console.dashboard.share.expiresLabel")}：{expiresText}
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * 告警执行面（wire-alert-evaluation 任务 5）：alert_rule 行展开时的
 * 「告警事件」小节——最近 10 条（kind 徽标四色 + 值/阈值/时间/detail
 * 摘要）带手动刷新；「试运行」按钮调 POST /artifacts/{id}/alert/test
 * （零副作用）——成功展示「归约值 X · 将触发/不触发」，失败后端 detail
 * 如实红条（422 spec 非法 / 400 查询失败等语义由服务端裁决）。
 */
function AlertPane({ artifactId }: { artifactId: number }) {
  const { t } = useTranslation();
  const [events, setEvents] = useState<AlertEvent[] | null>(null);
  const [failed, setFailed] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<AlertTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  const loadEvents = useCallback(() => {
    setEvents(null);
    setFailed(false);
    getAlertEvents(artifactId, 10)
      .then((rows) => setEvents(rows))
      .catch(() => setFailed(true));
  }, [artifactId]);

  useEffect(() => {
    setTestResult(null);
    setTestError(null);
    loadEvents();
  }, [loadEvents]);

  const runTest = async () => {
    if (testing) return;
    setTesting(true);
    setTestResult(null);
    setTestError(null);
    try {
      setTestResult(await testAlertRule(artifactId));
    } catch (err) {
      setTestError(apiErrorText(err, t("console.artifacts.alert.test.failed")));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div data-testid="alert-pane" className="mt-3 rounded-md border border-rose-200 bg-rose-50/40 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">
          {t("console.artifacts.alert.events.title")}
        </h2>
        <button
          type="button"
          data-testid="alert-events-refresh"
          onClick={loadEvents}
          className="rounded border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-600 hover:bg-slate-100"
        >
          {t("console.artifacts.alert.events.refresh")}
        </button>
        <button
          type="button"
          data-testid="alert-test-button"
          disabled={testing}
          onClick={() => void runTest()}
          className="ml-auto rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {testing
            ? t("console.artifacts.alert.test.running")
            : t("console.artifacts.alert.test.button")}
        </button>
      </div>

      {testResult != null && (
        <p data-testid="alert-test-result" className="mt-2 text-[11px] text-emerald-700">
          {t("console.artifacts.alert.test.result", {
            value: testResult.value,
            verdict: t(
              testResult.wouldFire
                ? "console.artifacts.alert.test.wouldFire"
                : "console.artifacts.alert.test.wouldNotFire",
            ),
          })}
        </p>
      )}
      {testError != null && (
        <p data-testid="alert-test-error" className="mt-2 text-[11px] text-red-600">
          {testError}
        </p>
      )}

      {failed && (
        <p data-testid="alert-events-error" className="mt-2 text-xs text-red-600">
          {t("console.artifacts.alert.events.fetchError")}
        </p>
      )}
      {events == null && !failed && (
        <p data-testid="alert-events-loading" className="mt-2 text-xs text-slate-400">
          {t("console.artifacts.alert.events.loading")}
        </p>
      )}
      {events != null && events.length === 0 && (
        <p data-testid="alert-events-empty" className="mt-2 text-xs text-slate-400">
          {t("console.artifacts.alert.events.empty")}
        </p>
      )}
      <ul data-testid="alert-events-list" className="mt-2 space-y-1.5">
        {events?.map((event) => (
          <li
            key={event.id}
            data-testid="alert-event-item"
            data-event-id={event.id}
            className="flex flex-wrap items-center gap-2 rounded border border-slate-200 bg-white px-2.5 py-1.5 text-[11px]"
          >
            <span
              data-testid="alert-event-kind"
              data-kind={event.kind}
              className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${
                ALERT_EVENT_KIND_CLASS[event.kind]
              }`}
            >
              {t(`console.artifacts.alert.kinds.${event.kind}`)}
            </span>
            <span className="font-mono text-slate-600">
              {t("console.artifacts.alert.events.valueLabel")} {event.value ?? "-"}
            </span>
            <span className="font-mono text-slate-600">
              {t("console.artifacts.alert.events.thresholdLabel")} {event.threshold ?? "-"}
            </span>
            <span className="text-slate-400">{formatServerDate(event.createdAt)}</span>
            {event.detail != null && (
              <span data-testid="alert-event-detail" className="text-slate-500">
                {truncate(event.detail, 60)}
              </span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
