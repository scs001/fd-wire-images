// 看板视图容器（wire-dashboard-renderer 任务 4）：产物页 dashboard kind 的
// 展开区（内嵌，不走路由）。拉取产物详情 → 查看态渲染 DashboardRenderer；
// v1 spec 且未归档时提供「编辑」切换 DashboardEditor；保存后同步刷新列表行。
// legacy spec（旧自由 JSON）只读呈现，不提供编辑（编辑器 v1 只吃 v1 契约）。
// wire-dashboard-refresh 任务 2.1：查看态加「刷新数据」——POST refresh 落新
// 版本后重拉详情重渲染；409 进行中 / 403 档位 / 502 引擎失败如实呈现；
// archived 只读无入口；进行中 loading 禁用防重复触发。
// wire-dashboard-query-contract 任务 2.3：刷新响应按模式呈现——mode=contract
// 带逐面板结果（成功 N / 保留 M / 失败 K 统计 + 失败面板原因列表，标题按
// spec 解析）；mode=ai（或缺省）维持既有版本号提示。
// wire-refresh-scheduler 任务 4.1：查看态底部加「定时刷新」小节（间隔预设
// 6h/24h/7d）；archived 目标的调度行由调度器自动清理，不再提供管理入口。
// wire-dashboard-export 任务 1.2：查看态工具区加「导出 CSV」——整板单文件
// 客户端本地合成（快照在 spec 里，零请求零新端点）；archived 只读也可导
// （纯读不落库）；分享页/渲染器本体零导出入口（公开只读红线）。
// wire-dashboard-blank-create 任务 1.2：startEditing prop——空白建板成功后
// 展开即进编辑器（其余入口缺省查看态，行为零变化）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  ConsoleApiError,
  apiErrorText,
  fetchLifecycleArtifact,
  refreshDashboard,
  type Artifact,
  type ArtifactDetail,
  type DashboardRefreshResult,
} from "../api";
import { parseDashboardSpec } from "./spec";
import { boardToCsv } from "../csv";
import { DashboardRenderer } from "./DashboardRenderer";
import { DashboardEditor } from "./DashboardEditor";
import {
  ARTIFACT_SCHEDULE_INTERVALS,
  RefreshScheduleSection,
} from "../RefreshScheduleSection";

export function DashboardPane({
  artifactId,
  onSaved,
  startEditing = false,
}: {
  artifactId: number;
  onSaved: (artifact: Artifact) => void;
  /** 挂载即编辑态（空白新建的板「创建即跳编辑器」；缺省查看态）。 */
  startEditing?: boolean;
}) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  // 刷新响应原样留存（呈现文案在渲染层按 mode 派生——契约路径需逐面板
  // 统计，ai 路径只要版本号；面板标题随重拉后的 spec 解析）。
  const [refreshResult, setRefreshResult] = useState<DashboardRefreshResult | null>(
    null,
  );
  const [refreshError, setRefreshError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setFailed(false);
    setEditing(startEditing);
    setRefreshing(false);
    setRefreshResult(null);
    setRefreshError(null);
    fetchLifecycleArtifact(artifactId)
      .then((row) => {
        if (alive) setDetail(row);
      })
      .catch(() => {
        if (alive) setFailed(true);
      });
    return () => {
      alive = false;
    };
  }, [artifactId, startEditing]);

  /** 手动刷新：留存响应（提示文案按 mode 派生）并重拉详情重渲染（行版本号
   * 经 onSaved 同步）。 */
  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshResult(null);
    setRefreshError(null);
    try {
      const result = await refreshDashboard(artifactId);
      setRefreshResult(result);
      const row = await fetchLifecycleArtifact(artifactId);
      setDetail(row);
      onSaved(row);
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 409) {
        // 单飞锁：同产物刷新进行中——固定文案（后端 detail 是机口径）。
        setRefreshError(t("console.dashboard.refresh.inProgress"));
      } else {
        // 403 档位引导 / 502 引擎失败等：后端 detail 如实呈现，无 detail 兜底。
        setRefreshError(apiErrorText(err, t("console.dashboard.refresh.failed")));
      }
    } finally {
      setRefreshing(false);
    }
  };

  if (failed) {
    return (
      <div
        data-testid="dashboard-pane"
        className="mt-3 rounded-md border border-red-200 bg-red-50/60 p-4"
      >
        <p data-testid="dashboard-load-error" className="text-xs text-red-700">
          {t("console.dashboard.loadError")}
        </p>
      </div>
    );
  }

  if (detail == null) {
    return (
      <div
        data-testid="dashboard-pane"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
      >
        <p data-testid="dashboard-loading" className="text-xs text-slate-400">
          {t("console.dashboard.loading")}
        </p>
      </div>
    );
  }

  const parsed = parseDashboardSpec(detail.spec);
  const locked = detail.status === "archived";

  // 整板导出（wire-dashboard-export）：板名取 spec 内层标题（宽容解析缺省
  // 回退产物标题；legacy 无内层标题也回退产物标题）。
  const boardTitle =
    parsed.kind === "v1" && parsed.title ? parsed.title : detail.title;

  /** 整板 CSV 导出：当前 spec 的面板快照本地合成（boardToCsv，BOM 已在
   * 内容里，MIME 只标 charset）→ blob 经 createObjectURL + a 标签触发下载
   * 后即回收；文件名非法字符清洗（/ \ : 等 → _）。零请求零新端点。 */
  const exportCsv = () => {
    const csv = boardToCsv(
      boardTitle,
      parsed.kind === "v1" ? parsed.panels : null,
    );
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const objectUrl = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = objectUrl;
    anchor.download = `${boardTitle.replace(/[\\/:*?"<>|]/g, "_").trim() || `artifact-${artifactId}`}.csv`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(objectUrl);
  };

  // 刷新响应呈现（wire-dashboard-query-contract 任务 2.3）：契约路径
  // （mode=contract 且带 panels）按面板统计 + 失败原因列表（标题按当前
  // spec 的面板 id 解析，缺省回退 id 本身）；ai 路径（或缺省/兼容旧响应）
  // 维持既有版本号提示。
  const contractPanels =
    refreshResult?.mode === "contract" && Array.isArray(refreshResult.panels)
      ? refreshResult.panels
      : null;
  const refreshNotice = !refreshResult
    ? null
    : contractPanels
      ? t("console.dashboard.refresh.contractSummary", {
          version: refreshResult.version,
          refreshed: contractPanels.filter((p) => p.status === "refreshed").length,
          kept: contractPanels.filter((p) => p.status === "kept").length,
          failed: contractPanels.filter((p) => p.status === "failed").length,
        })
      : t("console.dashboard.refresh.success", { version: refreshResult.version });
  const refreshFailures = (contractPanels ?? [])
    .filter((p) => p.status === "failed")
    .map((p) => ({
      panelId: p.panel_id,
      title:
        (parsed.kind === "v1"
          ? parsed.panels.find((panel) => panel.id === p.panel_id)?.title
          : undefined) || p.panel_id,
      error: p.error ?? "",
    }));

  return (
    <div data-testid="dashboard-pane" className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("console.dashboard.paneTitle")}</h2>
        {locked && (
          <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
            {t("console.dashboard.lockedBadge")}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          {editing ? null : (
            <>
              {!locked && (
                <button
                  type="button"
                  data-testid="dashboard-refresh"
                  disabled={refreshing}
                  onClick={() => void refresh()}
                  className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {refreshing
                    ? t("console.dashboard.refresh.refreshing")
                    : t("console.dashboard.refresh.button")}
                </button>
              )}
              <button
                type="button"
                data-testid="dashboard-export-csv"
                onClick={exportCsv}
                className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
              >
                {t("console.dashboard.exportCsv")}
              </button>
              <button
                type="button"
                data-testid="dashboard-edit"
                disabled={parsed.kind !== "v1" || locked}
                onClick={() => setEditing(true)}
                title={
                  parsed.kind === "legacy"
                    ? t("console.dashboard.legacy.noEditHint")
                    : undefined
                }
                className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {t("console.dashboard.editButton")}
              </button>
            </>
          )}
        </div>
      </div>
      {refreshNotice && (
        <p data-testid="dashboard-refresh-success" className="mt-1 text-[11px] text-emerald-600">
          {refreshNotice}
        </p>
      )}
      {refreshError && (
        <p data-testid="dashboard-refresh-error" className="mt-1 text-[11px] text-red-600">
          {refreshError}
        </p>
      )}
      {refreshFailures.length > 0 && (
        <ul data-testid="dashboard-refresh-failures" className="mt-1 space-y-0.5">
          {refreshFailures.map((failure) => (
            <li
              key={failure.panelId}
              data-testid="dashboard-refresh-failure"
              className="text-[11px] text-red-600"
            >
              {t("console.dashboard.refresh.contractFailure", {
                title: failure.title,
                error: failure.error,
              })}
            </li>
          ))}
        </ul>
      )}

      {editing && parsed.kind === "v1" ? (
        <DashboardEditor
          artifactId={artifactId}
          initial={{ title: parsed.title, panels: parsed.panels }}
          onSaved={(next) => {
            setDetail(next);
            onSaved(next);
          }}
          onExit={() => setEditing(false)}
        />
      ) : (
        <>
          <div className="mt-1">
            {/* toolbar：控制台查看态启用面板工具栏（全屏/导出图/图数切换/
                复制数据）。分享页与市场渲染不传该 prop，红线靠默认值守住。 */}
            <DashboardRenderer spec={detail.spec} toolbar />
          </div>
          {/* 定时刷新小节（wire-refresh-scheduler 4.1）：查看态管理入口；
              archived 由调度器自动清理调度行，不再展示。 */}
          {!locked && (
            <RefreshScheduleSection
              targetType="artifact"
              targetId={String(artifactId)}
              intervals={ARTIFACT_SCHEDULE_INTERVALS}
            />
          )}
        </>
      )}
    </div>
  );
}
