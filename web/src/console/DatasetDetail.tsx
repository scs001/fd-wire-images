// 数据集详情（wire-customer-local-data 任务组 3，四块）：
// 预览网格（分页 + 行内编辑器：默认只读，「编辑」模式下双击改值/删行/加行
// 进脏队列，保存按入队顺序逐条写修正层——wire-dataset-form-editing 2.1）、
// 修正历史（悬空徽标+单条回滚）、
// 同步历史（失败原因；漂移暂停行确认列并恢复）、悬空清单（逐条回滚）。
// 溯源区块（wire-provenance-ledger 任务组 8）：档位/链完整性/锚定进度状态条、
// 倒序事件时间线（before_seq 加载更早）、证据包下载。
// 定时刷新小节（wire-refresh-scheduler 任务 4.1）：仅 derived 来源数据集在
// 详情页底部出现（间隔预设 1h/6h/24h，创建/暂停恢复/删除/状态展示）。
// 增量同步设置卡片（wire-incremental-sync）：仅连接器来源数据集——模式
// 切换（全量快照 / 增量水位，增量必须配水位列）+ 当前水位状态 + 重置水位
// （二次确认：下次同步全量重建）+ 合并口径诚实注记；PATCH sync-settings。
// CSV 导出（wire-dataset-export 任务 2.2）：头部「导出 CSV」按钮——blob 下载
// + 截断（前 10,000 行）与失败如实呈现。
// 重派生与定时重派生（wire-pipeline-actions 任务 1.2）：仅 derived + cell 档
// 显示两节——「重派生」（级联开关默认开+逐级结果呈现+完成后详情刷新）与
// 「定时重派生」（五字段 cron 登记/更新/态呈现/删除+naive UTC 诚实注记）；
// api 档（免费档）与上传集不渲染入口（端点侧 403/400 闸兜底）。
// 资产信息（wire-asset-catalog 5.2）：六字段元数据编辑（域/标签/描述/期望
// 节奏/责任人/敏感度三选）+ 生效节奏提示（api 层来自指标目录）+ 最近修改
// 留痕（可折叠）；保存走 PATCH，422 字段级回显、403 切只读态（viewer 席位
// ——前端无独立角色源，以服务端 403 为准如实降级）。
// 数据质量（wire-dataset-quality 5.3）：五项指标 + 行数趋势（echarts SVG）+
// 「指示性检查，非质量认证」口径标注常驻 + 四种如实态（正常指标 / 快照失败
// 只示原因 / 引擎不支持 / 暂无快照——绝不用旧值冒充当前值）+ 手动「立即检查」。
// 契约：openspec/changes/wire-customer-local-data/reports/interfaces.md §3.3/§4、
// openspec/changes/wire-provenance-ledger /api/v1/provenance/*、
// wire-asset-catalog / wire-dataset-quality /api/v1/assets|quality/*。
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import * as echarts from "echarts";
import {
  addCorrection,
  apiErrorText,
  deleteDataset,
  deleteDatasetSchedule,
  exportDatasetCsv,
  fetchAssetMetaEvents,
  fetchCorrections,
  fetchDataset,
  fetchDatasetAssetMeta,
  fetchDatasetQuality,
  fetchDatasetRows,
  getDatasetSchedule,
  putDatasetSchedule,
  queryDataset,
  refreshDataset,
  refreshDatasetQuality,
  updateDatasetAssetMeta,
  ConsoleApiError,
  type AssetCadenceHint,
  type AssetMetaEvent,
  type AssetSensitivity,
  type DatasetAssetMeta,
  type DatasetQuality,
  type DatasetRefreshCascadeEntry,
  type DatasetSchedule,
  type QualityFreshnessValue,
  type QualityHistoryEntry,
  type QualitySnapshot,
  type QueryFilterOp,
  fetchEvidencePackage,
  fetchProvenanceEvents,
  fetchProvenanceOverview,
  fetchProvenanceStatus,
  fetchSyncs,
  PROVENANCE_TIERS,
  resumeDataset,
  revertCorrection,
  updateProvenanceTier,
  updateSyncSettings,
  type Correction,
  type CorrectionInput,
  type Dataset,
  type DatasetRows,
  type DatasetSyncMode,
  type ProvenanceEvent,
  type ProvenanceOverview,
  type ProvenanceStatus,
  type ProvenanceTier,
  type SyncRecord,
  type SyncStatus,
} from "./api";
import { formatServerDate, truncate } from "./util";
import {
  describeEditItem,
  enqueueEdit,
  saveEditQueue,
  type EditQueueItem,
  type SaveOutcome,
} from "./editQueue";
import {
  DATASET_SCHEDULE_INTERVALS,
  RefreshScheduleSection,
} from "./RefreshScheduleSection";
import { DatasetVisibilitySection } from "./DatasetVisibilitySection";
import { DatasetQualityRulesSection } from "./DatasetQualityRulesSection";

const SYNC_BADGE_CLASS: Record<SyncStatus, string> = {
  idle: "bg-slate-100 text-slate-600 ring-slate-200",
  syncing: "bg-sky-50 text-sky-700 ring-sky-200",
  ok: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  failed: "bg-red-50 text-red-700 ring-red-200",
  paused_drift: "bg-amber-50 text-amber-700 ring-amber-200",
  paused_quota: "bg-rose-50 text-rose-700 ring-rose-200",
};

const PAGE_SIZE = 50;

/**
 * 从漂移暂停原因/同步错误文本中解析被拒绝的列名（如
 * `drift: 列 price 缺失，列 amount 类型变更` → ["price", "amount"]），
 * 供 resume 对话框逐列确认；解析不到时返回空集，由客户人工核对。
 */
export function driftColumns(reason: string | null | undefined): string[] {
  if (!reason) return [];
  const found: string[] = [];
  const patterns = [
    /列\s*([A-Za-z_][A-Za-z0-9_]*)/g,
    /column\s+([A-Za-z_][A-Za-z0-9_]*)/gi,
    /`([A-Za-z_][A-Za-z0-9_]*)`/g,
    /"([A-Za-z_][A-Za-z0-9_]*)"/g,
  ];
  for (const pattern of patterns) {
    for (const match of reason.matchAll(pattern)) {
      const name = match[1];
      if (name && !found.includes(name)) found.push(name);
    }
  }
  return found;
}

function describeCorrection(correction: Correction): string {
  if (correction.op === "update") {
    return `${correction.column ?? ""} = ${String(correction.value ?? "")}`;
  }
  if (correction.op === "delete") return correction.row_id ?? "";
  return JSON.stringify(correction.values ?? {});
}

export function DatasetDetail({
  datasetId,
  onBack,
}: {
  datasetId: string;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const [dataset, setDataset] = useState<Dataset | null>(null);
  const [error, setError] = useState(false);
  const [corrections, setCorrections] = useState<Correction[] | null>(null);
  const [syncs, setSyncs] = useState<SyncRecord[] | null>(null);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  // CSV 导出（wire-dataset-export 任务 2.2）：下载中禁用防重复；
  // truncated=服务端 X-Export-Truncated（截断至前 10,000 行）如实提示。
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exportTruncated, setExportTruncated] = useState(false);
  // 敏感度镜像（wire-dataset-marketplace 6.1）：AssetMetaSection 加载/保存
  // 上报；数据开放选择器据此禁用 market/directed 两档（敏感档硬闸）。
  const [sensitivity, setSensitivity] = useState<AssetSensitivity>("常规");

  const loadDataset = useCallback(async () => {
    setError(false);
    try {
      setDataset(await fetchDataset(datasetId));
    } catch {
      setDataset(null);
      setError(true);
    }
  }, [datasetId]);

  const loadCorrections = useCallback(async () => {
    try {
      setCorrections(await fetchCorrections(datasetId));
    } catch {
      setCorrections([]);
    }
  }, [datasetId]);

  const loadSyncs = useCallback(async () => {
    try {
      setSyncs(await fetchSyncs(datasetId));
    } catch {
      setSyncs([]);
    }
  }, [datasetId]);

  useEffect(() => {
    setDataset(null);
    void loadDataset();
  }, [loadDataset]);

  useEffect(() => {
    setCorrections(null);
    void loadCorrections();
  }, [loadCorrections]);

  useEffect(() => {
    setSyncs(null);
    void loadSyncs();
  }, [loadSyncs]);

  const afterCorrection = useCallback(() => {
    setPreviewVersion((version) => version + 1);
    void loadCorrections();
  }, [loadCorrections]);

  /** 重派生完成后：详情字段（行数/更新时间）与预览行同步刷新（同款回调：
   *  loadDataset + 预览 version 递增——与同步恢复后的刷新口径一致）。 */
  const afterRederive = useCallback(() => {
    void loadDataset();
    setPreviewVersion((version) => version + 1);
  }, [loadDataset]);

  const revert = useCallback(
    async (correctionId: number) => {
      await revertCorrection(datasetId, correctionId);
      afterCorrection();
    },
    [datasetId, afterCorrection],
  );

  const confirmDelete = async () => {
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteDataset(datasetId);
      setDeleteDialogOpen(false);
      // 回列表页（onBack 内会刷新列表，删除的数据集不再出现）。
      onBack();
    } catch (err) {
      setDeleteError(apiErrorText(err, t("console.datasets.detail.deleteError")));
    } finally {
      setDeleting(false);
    }
  };

  /** 导出 CSV：blob 经 URL.createObjectURL + a 标签触发下载后即回收；
   *  失败如实呈现（apiErrorText 取后端 detail），成功且截断时标注。 */
  const exportCsv = async () => {
    if (exporting) return;
    setExporting(true);
    setExportError(null);
    setExportTruncated(false);
    try {
      const { blob, filename, truncated } = await exportDatasetCsv(datasetId);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
      setExportTruncated(truncated);
    } catch (err) {
      setExportError(apiErrorText(err, t("console.datasets.detail.exportError")));
    } finally {
      setExporting(false);
    }
  };

  const backButton = (
    <button
      type="button"
      data-testid="detail-back"
      onClick={onBack}
      className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50"
    >
      ← {t("console.datasets.detail.back")}
    </button>
  );

  if (error) {
    return (
      <div>
        {backButton}
        <div
          data-testid="detail-error"
          className="mt-4 rounded-md border border-red-200 bg-red-50 p-4"
        >
          <p className="text-sm text-red-700">
            {t("console.datasets.detail.errorTitle")}
          </p>
          <button
            type="button"
            data-testid="detail-retry"
            onClick={() => void loadDataset()}
            className="mt-2 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.retry")}
          </button>
        </div>
      </div>
    );
  }

  if (dataset == null) {
    return (
      <div>
        {backButton}
        <p className="mt-4 text-xs text-slate-400" data-testid="detail-loading">
          {t("console.datasets.detail.loading")}
        </p>
      </div>
    );
  }

  return (
    <div data-testid="dataset-detail">
      <div className="flex flex-wrap items-center gap-2">
        {backButton}
        <h1 data-testid="detail-name" className="text-lg font-semibold">
          {dataset.name}
        </h1>
        <span
          data-testid="detail-source-badge"
          data-kind={dataset.source_kind}
          className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
            dataset.source_kind === "connector"
              ? "bg-indigo-50 text-indigo-700 ring-indigo-200"
              : dataset.source_kind === "derived"
                ? "bg-amber-50 text-amber-700 ring-amber-200"
                : dataset.source_kind === "declared"
                  ? "bg-sky-50 text-sky-700 ring-sky-200"
                  : "bg-teal-50 text-teal-700 ring-teal-200"
          }`}
        >
          {t(`console.datasets.source.${dataset.source_kind}`)}
        </span>
        <span
          data-testid="detail-sync-status"
          data-status={dataset.sync_status}
          className={`rounded-full px-2 py-0.5 text-xs ring-1 ${
            SYNC_BADGE_CLASS[dataset.sync_status] ??
            "bg-slate-100 text-slate-600 ring-slate-200"
          }`}
        >
          {t(`console.datasets.syncStatus.${dataset.sync_status}`)}
        </span>
        <button
          type="button"
          data-testid="detail-export-csv"
          disabled={exporting}
          onClick={() => void exportCsv()}
          className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {exporting
            ? t("console.datasets.detail.exporting")
            : t("console.datasets.detail.exportCsv")}
        </button>
        <button
          type="button"
          data-testid="detail-delete"
          onClick={() => {
            setDeleteError(null);
            setDeleteDialogOpen(true);
          }}
          className="rounded border border-rose-300 px-2 py-1 text-xs text-rose-700 hover:bg-rose-50"
        >
          {t("console.datasets.detail.deleteDataset")}
        </button>
      </div>
      <p className="mt-1 font-mono text-[11px] text-slate-400">{dataset.key}</p>

      {exportError && (
        <p data-testid="export-csv-error" className="mt-2 text-xs text-red-600">
          {exportError}
        </p>
      )}
      {exportTruncated && (
        <p
          data-testid="export-csv-truncated"
          className="mt-2 text-xs text-amber-700"
        >
          {t("console.datasets.detail.exportTruncated")}
        </p>
      )}

      {deleteDialogOpen && (
        <div
          data-testid="delete-dialog"
          role="dialog"
          className="mt-3 rounded-lg border border-rose-200 bg-rose-50/60 p-4"
        >
          <h3 className="text-sm font-semibold text-rose-800">
            {t("console.datasets.detail.deleteTitle")}
          </h3>
          <p data-testid="delete-warning" className="mt-1.5 text-xs text-slate-600">
            {t("console.datasets.detail.deleteWarning")}
          </p>
          {dataset.source_kind === "connector" && (
            <p
              data-testid="delete-connector-warning"
              className="mt-1 text-xs text-amber-700"
            >
              {t("console.datasets.detail.deleteConnectorWarning")}
            </p>
          )}
          {deleteError && (
            <p data-testid="delete-error" className="mt-1.5 text-xs text-red-600">
              {deleteError}
            </p>
          )}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              data-testid="delete-confirm"
              disabled={deleting}
              onClick={() => void confirmDelete()}
              className="rounded bg-rose-600 px-3 py-1 text-xs text-white hover:bg-rose-700 disabled:cursor-not-allowed disabled:bg-rose-300"
            >
              {deleting
                ? t("console.datasets.detail.deleting")
                : t("console.datasets.detail.deleteConfirm")}
            </button>
            <button
              type="button"
              data-testid="delete-cancel"
              onClick={() => setDeleteDialogOpen(false)}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
            >
              {t("console.datasets.detail.cancel")}
            </button>
          </div>
        </div>
      )}

      {/* 资产信息（wire-asset-catalog 5.2）：六字段元数据补全 + 留痕；403 时
          切只读态（viewer 席位——前端无独立角色源，以服务端口径为准）。 */}
      <AssetMetaSection
        datasetId={datasetId}
        onSensitivityChange={setSensitivity}
      />
      {/* 数据开放选择器（wire-dataset-marketplace 6.1）：四档可见性 +
          开放模式 + 定向授权名单；敏感档（sensitivity=敏感）禁 market/
          directed——闸数据源即上方资产信息的敏感度。 */}
      <DatasetVisibilitySection datasetId={datasetId} sensitivity={sensitivity} />
      {/* 数据质量（wire-dataset-quality 5.3）：口径标注常驻 + 四种如实态 +
          行数趋势 + 手动「立即检查」。 */}
      <QualityPanel datasetId={datasetId} />
      {/* 质量规则（wire-quality-rules）：客户自定义检查（指标+阈值）+
          求值结果三态（GX 契约）。写权限以服务端口径为准（403 如实回显），
          与本页既有「前端无独立角色源」纪律一致。 */}
      <DatasetQualityRulesSection datasetId={datasetId} canEdit={true} />

      <PreviewGrid
        datasetId={datasetId}
        version={previewVersion}
        onChanged={afterCorrection}
      />
      <CorrectionsPanel corrections={corrections} revert={revert} />
      <SyncsPanel
        datasetId={datasetId}
        dataset={dataset}
        syncs={syncs}
        onResumed={() => {
          void loadDataset();
          void loadSyncs();
          setPreviewVersion((version) => version + 1);
        }}
      />
      {/* 增量同步设置（wire-incremental-sync）：仅连接器来源——模式切换/
          水位列/重置水位；其他来源一律不渲染（端点侧 422 闸兜底）。 */}
      {dataset.source_kind === "connector" && (
        <SyncSettingsSection
          dataset={dataset}
          onChanged={() => void loadDataset()}
        />
      )}
      <DanglingPanel corrections={corrections} revert={revert} />
      <ProvenancePanel datasetKey={dataset.key} />
      {/* 定时刷新小节（wire-refresh-scheduler 4.1）：仅 derived 来源数据集——
          调度器按间隔自动跑 dataset_refresh（上传/连接器来源走各自的同步链路）。 */}
      {dataset.source_kind === "derived" && (
        <RefreshScheduleSection
          targetType="dataset"
          targetId={dataset.id}
          intervals={DATASET_SCHEDULE_INTERVALS}
        />
      )}
      {/* 重派生与定时重派生（wire-pipeline-actions 1.2）：仅 derived + cell 档
          ——storage_tier 容错读取（响应缺省视为 cell，与 api 侧 DatasetOut 默认
          同口径）；api 档（免费档）与上传/连接器来源一律不渲染入口。 */}
      {dataset.source_kind === "derived" &&
        (dataset as { storage_tier?: string }).storage_tier !== "api" && (
          <>
            <RederiveSection
              datasetId={dataset.id}
              onRefreshed={afterRederive}
            />
            <RederiveScheduleSection datasetId={dataset.id} />
          </>
        )}
    </div>
  );
}

function PreviewGrid({
  datasetId,
  version,
  onChanged,
}: {
  datasetId: string;
  version: number;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const [data, setData] = useState<DatasetRows | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [page, setPage] = useState(1);
  // 结构化查询（wire-customer-data-engine 6.3）：列/操作符/值 + 排序；
  // 空过滤 = 原 preview 分页路径，两态共用同一网格。
  const [filterColumn, setFilterColumn] = useState("");
  const [filterOp, setFilterOp] = useState<QueryFilterOp>("eq");
  const [filterValue, setFilterValue] = useState("");
  const [sortColumn, setSortColumn] = useState("");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("asc");
  const [queryActive, setQueryActive] = useState(false);
  // 行内编辑器（wire-dataset-form-editing 2.1）：默认只读（零编辑控件渲染）；
  // 「编辑」是叠加态——双击改值/删行/加行先进脏队列，「保存」按入队顺序
  // 逐条 addCorrection（失败逐条如实呈现，不自动重试），「放弃」清队；
  // 队列非空时离开编辑模式先确认。
  const [editMode, setEditMode] = useState(false);
  const [queue, setQueue] = useState<EditQueueItem[]>([]);
  const [editing, setEditing] = useState<{
    rowId: string;
    column: string;
    value: string;
  } | null>(null);
  const [adding, setAdding] = useState(false);
  const [newValues, setNewValues] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [saveResults, setSaveResults] = useState<SaveOutcome[] | null>(null);
  const [confirmLeave, setConfirmLeave] = useState(false);

  const columns = data?.columns.map((c) => c.name) ?? [];

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      if (queryActive) {
        const filters =
          filterColumn && filterOp !== "isnull" && filterOp !== "notnull" && filterValue !== ""
            ? [{ column: filterColumn, op: filterOp, value: filterValue }]
            : filterColumn && (filterOp === "isnull" || filterOp === "notnull")
              ? [{ column: filterColumn, op: filterOp }]
              : [];
        const sort =
          sortColumn !== "" ? [{ column: sortColumn, dir: sortDir }] : [];
        const result = await queryDataset(datasetId, {
          filters,
          sort,
          limit: PAGE_SIZE,
          offset: (page - 1) * PAGE_SIZE,
        });
        setData({
          columns: result.columns,
          rows: result.rows,
          total: result.total,
        });
      } else {
        setData(await fetchDatasetRows(datasetId, page, PAGE_SIZE));
      }
    } catch {
      setData(null);
      setLoadError(true);
    }
  }, [datasetId, page, queryActive, filterColumn, filterOp, filterValue, sortColumn, sortDir]);

  useEffect(() => {
    setData(null);
    setEditing(null);
    void load();
  }, [load, version]);

  /** 入队（update/delete/insert 同入口）；新修改进队即作废旧保存结果。 */
  const enqueue = (payload: CorrectionInput) => {
    setQueue((items) => enqueueEdit(items, payload));
    setSaveResults(null);
  };

  /** 单元格编辑确认（Enter）：update 项入队；Esc 取消不开口子。 */
  const confirmCellEdit = () => {
    if (!editing) return;
    enqueue({
      op: "update",
      row_id: editing.rowId,
      column: editing.column,
      value: editing.value,
    });
    setEditing(null);
  };

  /** 加行确认：非空字段组 insert 项入队。 */
  const enqueueInsert = () => {
    const values: Record<string, string> = {};
    for (const [key, value] of Object.entries(newValues)) {
      if (value.trim() !== "") values[key] = value;
    }
    enqueue({ op: "insert", values });
    setAdding(false);
    setNewValues({});
  };

  const resetEditing = () => {
    setEditing(null);
    setAdding(false);
    setNewValues({});
  };

  /**
   * 保存：按入队顺序逐条 addCorrection（失败隔离——单条失败不断链、不重试），
   * 逐条结果呈现；全部提交完后清队并刷新行预览与修正历史（onChanged）。
   */
  const saveQueue = async () => {
    if (saving || queue.length === 0) return;
    setSaving(true);
    const outcomes = await saveEditQueue(queue, async (payload) => {
      await addCorrection(datasetId, payload);
    });
    setSaveResults(outcomes);
    setQueue([]);
    resetEditing();
    setSaving(false);
    onChanged();
  };

  /** 放弃：清空脏队列与未完成的内联编辑（显式操作，无需再确认）。 */
  const discardQueue = () => {
    setQueue([]);
    resetEditing();
  };

  /** 切换编辑模式：队列非空时离开先确认（确认后丢弃）。 */
  const toggleEditMode = () => {
    if (editMode && queue.length > 0) {
      setConfirmLeave(true);
      return;
    }
    setEditMode((mode) => !mode);
    setSaveResults(null);
    discardQueue();
  };

  const leaveEditMode = () => {
    setConfirmLeave(false);
    setEditMode(false);
    setSaveResults(null);
    discardQueue();
  };

  const total = data?.total ?? 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 队列叠加可视化（只编辑态）：待保存 update 单元格淡黄标注、delete 行划线弱化。
  const pendingCellKeys = new Set<string>();
  const pendingDeleteRows = new Set<string>();
  for (const { payload } of queue) {
    if (payload.op === "update") {
      pendingCellKeys.add(`${payload.row_id}\u0000${payload.column}`);
    } else if (payload.op === "delete") {
      pendingDeleteRows.add(payload.row_id);
    }
  }

  return (
    <section data-testid="preview-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.previewTitle")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.detail.previewHint")}
      </p>

      {loadError && (
        <div data-testid="preview-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.errorTitle")}
          </p>
          <button
            type="button"
            data-testid="preview-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.retry")}
          </button>
        </div>
      )}

      {data == null && !loadError && (
        <p data-testid="preview-loading" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.previewLoading")}
        </p>
      )}

      {data != null && (
        <>
          {/* 结构化查询条（6.3）：列+操作符+值过滤、列排序；「应用」切服务端查询，「重置」回预览分页 */}
          <div
            data-testid="query-bar"
            className="mt-2 flex flex-wrap items-center gap-1.5 rounded-lg border border-slate-200 bg-white p-2 text-xs"
          >
            <select
              data-testid="query-filter-column"
              value={filterColumn}
              onChange={(e) => setFilterColumn(e.target.value)}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="">{t("console.datasets.query.column")}</option>
              {columns.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            <select
              data-testid="query-filter-op"
              value={filterOp}
              onChange={(e) => setFilterOp(e.target.value as QueryFilterOp)}
              disabled={!filterColumn}
              className="rounded border border-slate-300 px-1.5 py-1 disabled:opacity-40"
            >
              {(["eq","ne","gt","gte","lt","lte","in","notin","like","isnull","notnull"] as QueryFilterOp[]).map((op) => (
                <option key={op} value={op}>{op}</option>
              ))}
            </select>
            {filterOp !== "isnull" && filterOp !== "notnull" && (
              <input
                data-testid="query-filter-value"
                value={filterValue}
                onChange={(e) => setFilterValue(e.target.value)}
                placeholder={t("console.datasets.query.value")}
                disabled={!filterColumn}
                className="w-32 rounded border border-slate-300 px-1.5 py-1 disabled:opacity-40"
              />
            )}
            <span className="mx-1 text-slate-200">|</span>
            <select
              data-testid="query-sort-column"
              value={sortColumn}
              onChange={(e) => setSortColumn(e.target.value)}
              className="rounded border border-slate-300 px-1.5 py-1"
            >
              <option value="">{t("console.datasets.query.sort")}</option>
              {columns.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
            {sortColumn !== "" && (
              <button
                type="button"
                data-testid="query-sort-dir"
                onClick={() => setSortDir((d) => (d === "asc" ? "desc" : "asc"))}
                className="rounded border border-slate-300 px-1.5 py-1 font-mono"
              >
                {sortDir === "asc" ? "↑" : "↓"}
              </button>
            )}
            <button
              type="button"
              data-testid="query-apply"
              onClick={() => {
                setPage(1);
                setQueryActive(true);
              }}
              className="rounded bg-slate-900 px-2.5 py-1 text-white hover:bg-slate-700"
            >
              {t("console.datasets.query.apply")}
            </button>
            {queryActive && (
              <button
                type="button"
                data-testid="query-reset"
                onClick={() => {
                  setQueryActive(false);
                  setFilterColumn("");
                  setFilterValue("");
                  setSortColumn("");
                  setPage(1);
                }}
                className="rounded border border-slate-300 px-2.5 py-1 text-slate-600 hover:bg-slate-100"
              >
                {t("console.datasets.query.reset")}
              </button>
            )}
            {queryActive && (
              <span data-testid="query-active" className="text-slate-400">
                {t("console.datasets.query.activeNote")}
              </span>
            )}
          </div>

          {/* 编辑模式条（wire-dataset-form-editing 2.1）：默认只读零编辑控件；
              编辑态叠加 脏队列计数徽标 + 保存/放弃。 */}
          <div
            data-testid="preview-edit-bar"
            className="mt-2 flex flex-wrap items-center gap-2 text-xs"
          >
            <button
              type="button"
              data-testid="preview-edit-toggle"
              data-mode={editMode ? "edit" : "read"}
              disabled={saving}
              onClick={toggleEditMode}
              className={
                editMode
                  ? "rounded border border-slate-300 bg-slate-100 px-2.5 py-1 text-slate-700 hover:bg-slate-200 disabled:cursor-not-allowed disabled:opacity-40"
                  : "rounded bg-slate-900 px-2.5 py-1 text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              }
            >
              {editMode
                ? t("console.datasets.detail.editDone")
                : t("console.datasets.detail.editStart")}
            </button>
            {editMode && queue.length > 0 && (
              <>
                <span
                  data-testid="preview-dirty-badge"
                  className="rounded-full bg-amber-50 px-2 py-0.5 text-amber-700 ring-1 ring-amber-200"
                >
                  {t("console.datasets.detail.dirtyCount", { count: queue.length })}
                </span>
                <button
                  type="button"
                  data-testid="preview-queue-save"
                  disabled={saving}
                  onClick={() => void saveQueue()}
                  className="rounded bg-slate-900 px-2.5 py-1 text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {saving
                    ? t("console.datasets.detail.saving")
                    : t("console.datasets.detail.save")}
                </button>
                <button
                  type="button"
                  data-testid="preview-queue-discard"
                  disabled={saving}
                  onClick={discardQueue}
                  className="rounded border border-slate-300 px-2.5 py-1 text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {t("console.datasets.detail.queueDiscard")}
                </button>
              </>
            )}
          </div>

          {confirmLeave && (
            <div
              data-testid="preview-leave-dialog"
              role="dialog"
              className="mt-2 rounded-lg border border-amber-300 bg-amber-50/60 p-4"
            >
              <h3 className="text-sm font-semibold text-amber-800">
                {t("console.datasets.detail.leaveTitle")}
              </h3>
              <p
                data-testid="preview-leave-warning"
                className="mt-1 text-xs text-slate-600"
              >
                {t("console.datasets.detail.leaveHint", { count: queue.length })}
              </p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  data-testid="preview-leave-confirm"
                  onClick={leaveEditMode}
                  className="rounded bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-700"
                >
                  {t("console.datasets.detail.leaveConfirm")}
                </button>
                <button
                  type="button"
                  data-testid="preview-leave-cancel"
                  onClick={() => setConfirmLeave(false)}
                  className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-50"
                >
                  {t("console.datasets.detail.leaveCancel")}
                </button>
              </div>
            </div>
          )}

          <div className="mt-2 overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table data-testid="preview-grid" className="w-full text-xs">
              <thead>
                <tr className="bg-slate-50">
                  {data.columns.map((column) => (
                    <th
                      key={column.name}
                      data-testid="preview-header"
                      data-type={column.type}
                      className="whitespace-nowrap border-b border-slate-200 px-3 py-1.5 text-left font-medium text-slate-600"
                    >
                      {column.name}
                      <span className="ml-1.5 font-mono text-[10px] text-slate-400">
                        {column.type}
                      </span>
                    </th>
                  ))}
                  {editMode && <th className="border-b border-slate-200 px-2 py-1.5" />}
                </tr>
              </thead>
              <tbody>
                {data.rows.map((row, index) => {
                  const rowId = String(row.__row_id ?? "");
                  const rowPendingDelete =
                    editMode && pendingDeleteRows.has(rowId);
                  return (
                    <tr
                      key={rowId || index}
                      data-testid="preview-row"
                      data-row-id={rowId}
                      data-pending-delete={rowPendingDelete ? "true" : "false"}
                      className={`hover:bg-slate-50/60 ${
                        rowPendingDelete ? "text-slate-400 line-through opacity-60" : ""
                      }`}
                    >
                      {data.columns.map((column) => {
                        const isEditing =
                          editing != null &&
                          editing.rowId === rowId &&
                          editing.column === column.name;
                        const cellPending =
                          editMode &&
                          !isEditing &&
                          pendingCellKeys.has(`${rowId}\u0000${column.name}`);
                        return (
                          <td
                            key={column.name}
                            data-testid="preview-cell"
                            data-column={column.name}
                            data-row-id={rowId}
                            data-pending-edit={cellPending ? "true" : "false"}
                            onDoubleClick={() => {
                              if (editMode && rowId && !saving) {
                                setEditing({
                                  rowId,
                                  column: column.name,
                                  value:
                                    row[column.name] == null
                                      ? ""
                                      : String(row[column.name]),
                                });
                              }
                            }}
                            className={`whitespace-nowrap border-b border-slate-100 px-3 py-1 text-slate-700 ${
                              cellPending ? "bg-amber-50" : ""
                            }`}
                          >
                            {isEditing ? (
                              <input
                                data-testid="preview-edit-input"
                                data-column={column.name}
                                data-row-id={rowId}
                                value={editing.value}
                                autoFocus
                                onChange={(e) =>
                                  setEditing({
                                    ...editing,
                                    value: e.target.value,
                                  })
                                }
                                onKeyDown={(e) => {
                                  if (e.key === "Enter") confirmCellEdit();
                                  else if (e.key === "Escape") setEditing(null);
                                }}
                                className="w-28 rounded border border-slate-300 px-1 py-0.5 text-xs"
                              />
                            ) : row[column.name] == null ? (
                              ""
                            ) : (
                              String(row[column.name])
                            )}
                          </td>
                        );
                      })}
                      {editMode && (
                        <td className="border-b border-slate-100 px-2 py-1 text-right">
                          <button
                            type="button"
                            data-testid="preview-delete-row"
                            title={t("console.datasets.detail.deleteRow")}
                            disabled={!rowId || saving}
                            onClick={() => {
                              if (rowId) enqueue({ op: "delete", row_id: rowId });
                            }}
                            className="rounded border border-rose-200 px-1.5 py-0.5 text-[10px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            ✕
                          </button>
                        </td>
                      )}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {data.rows.length === 0 && (
            <p data-testid="preview-empty" className="mt-2 text-xs text-slate-400">
              {t("console.datasets.detail.previewEmpty")}
            </p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span data-testid="preview-pagination">
              {t("console.datasets.detail.pagination", {
                total,
                page,
                pages,
              })}
            </span>
            <button
              type="button"
              data-testid="preview-prev-page"
              disabled={page <= 1}
              onClick={() => setPage((current) => Math.max(1, current - 1))}
              className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t("console.datasets.detail.prev")}
            </button>
            <button
              type="button"
              data-testid="preview-next-page"
              disabled={page >= pages}
              onClick={() => setPage((current) => current + 1)}
              className="rounded border border-slate-300 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t("console.datasets.detail.next")}
            </button>

            {editMode && !adding && (
              <button
                type="button"
                data-testid="preview-add-row"
                onClick={() => {
                  setAdding(true);
                  setNewValues({});
                }}
                className="ml-auto rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-50"
              >
                {t("console.datasets.detail.addRow")}
              </button>
            )}
          </div>

          {adding && (
            <div
              data-testid="preview-add-row-panel"
              className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 p-3"
            >
              <p className="text-xs font-medium text-slate-600">
                {t("console.datasets.detail.addRowTitle")}
              </p>
              <div className="mt-2 flex flex-wrap gap-2">
                {data.columns.map((column) => (
                  <input
                    key={column.name}
                    data-testid="preview-add-row-input"
                    data-column={column.name}
                    value={newValues[column.name] ?? ""}
                    placeholder={column.name}
                    onChange={(e) =>
                      setNewValues((prev) => ({
                        ...prev,
                        [column.name]: e.target.value,
                      }))
                    }
                    className="w-32 rounded border border-slate-300 px-2 py-1 text-xs"
                  />
                ))}
              </div>
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  data-testid="preview-add-row-submit"
                  onClick={enqueueInsert}
                  className="rounded bg-slate-900 px-2 py-1 text-xs text-white hover:bg-slate-700"
                >
                  {t("console.datasets.detail.addRowEnqueue")}
                </button>
                <button
                  type="button"
                  data-testid="preview-add-row-cancel"
                  onClick={() => {
                    setAdding(false);
                    setNewValues({});
                  }}
                  className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600"
                >
                  {t("console.datasets.detail.addRowCancel")}
                </button>
              </div>
            </div>
          )}

          {/* 保存结果（wire-dataset-form-editing 2.1）：逐条呈现——成功已生效；
              失败带行上下文与服务端原因（apiErrorText 取 detail），不自动重试。 */}
          {editMode && saveResults != null && saveResults.length > 0 && (
            <div
              data-testid="preview-save-results"
              className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 p-3"
            >
              <p className="text-xs font-medium text-slate-600">
                {t("console.datasets.detail.saveResultTitle")}
              </p>
              <ul className="mt-1.5 space-y-1">
                {saveResults.map((outcome) => (
                  <li
                    key={outcome.item.seq}
                    data-testid="preview-save-result"
                    data-op={outcome.item.payload.op}
                    data-ok={outcome.ok ? "true" : "false"}
                    className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs"
                  >
                    <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600">
                      {t(
                        `console.datasets.detail.correctionOps.${outcome.item.payload.op}`,
                      )}
                    </span>
                    <span
                      data-testid="preview-save-result-context"
                      className="font-mono text-[11px] text-slate-700"
                    >
                      {describeEditItem(outcome.item.payload)}
                    </span>
                    {outcome.ok ? (
                      <span
                        data-testid="preview-save-result-status"
                        className="text-emerald-600"
                      >
                        {t("console.datasets.detail.saveResultOk")}
                      </span>
                    ) : (
                      <span
                        data-testid="preview-save-result-status"
                        className="text-red-600"
                      >
                        {t("console.datasets.detail.saveResultFailed")}：
                        {apiErrorText(
                          outcome.error,
                          t("console.datasets.detail.correctionError"),
                        )}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </section>
  );
}

function CorrectionsPanel({
  corrections,
  revert,
}: {
  corrections: Correction[] | null;
  revert: (correctionId: number) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const doRevert = async (correctionId: number) => {
    setBusyId(correctionId);
    setError(null);
    try {
      await revert(correctionId);
    } catch (err) {
      setError(apiErrorText(err, t("console.datasets.detail.revertError")));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section data-testid="corrections-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.correctionsTitle")}
      </h2>
      {error && (
        <p data-testid="corrections-op-error" className="mt-2 text-xs text-red-600">
          {error}
        </p>
      )}
      {corrections == null && (
        <p data-testid="corrections-loading" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.correctionsLoading")}
        </p>
      )}
      {corrections != null && corrections.length === 0 && (
        <p data-testid="corrections-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.correctionsEmpty")}
        </p>
      )}
      {corrections != null && corrections.length > 0 && (
        <ul data-testid="correction-list" className="mt-2 space-y-1.5">
          {corrections.map((correction) => (
            <li
              key={correction.id}
              data-testid="correction-row"
              data-op={correction.op}
              data-dangling={correction.dangling_at != null ? "true" : "false"}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
            >
              <span
                data-testid="correction-op"
                className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600"
              >
                {t(`console.datasets.detail.correctionOps.${correction.op}`)}
              </span>
              <span className="font-mono text-[11px] text-slate-700">
                {describeCorrection(correction)}
              </span>
              {correction.channel != null && correction.channel !== "console" && (
                <span
                  data-testid="correction-channel"
                  data-channel={correction.channel}
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    correction.channel === "data-key"
                      ? "bg-sky-50 text-sky-700"
                      : "bg-violet-50 text-violet-700"
                  }`}
                >
                  {t(`console.datasets.detail.channel.${correction.channel}`)}
                </span>
              )}
              {correction.dangling_at != null && (
                <span
                  data-testid="correction-dangling-badge"
                  className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700"
                >
                  {t("console.datasets.detail.dangling")}
                </span>
              )}
              {correction.reverted_at != null && (
                <span
                  data-testid="correction-reverted-badge"
                  className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600"
                >
                  {t("console.datasets.detail.reverted")}
                </span>
              )}
              <span className="text-[11px] text-slate-400">
                {correction.actor} · {formatServerDate(correction.created_at)}
              </span>
              {correction.reverted_at == null && (
                <button
                  type="button"
                  data-testid="correction-revert"
                  disabled={busyId === correction.id}
                  onClick={() => void doRevert(correction.id)}
                  className="ml-auto rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  {busyId === correction.id
                    ? t("console.datasets.detail.reverting")
                    : t("console.datasets.detail.revert")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SyncsPanel({
  datasetId,
  dataset,
  syncs,
  onResumed,
}: {
  datasetId: string;
  dataset: Dataset;
  syncs: SyncRecord[] | null;
  onResumed: () => void;
}) {
  const { t } = useTranslation();
  const [dialog, setDialog] = useState<{ reason: string; columns: string[] } | null>(
    null,
  );
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resumed, setResumed] = useState(false);

  const openResume = (fallbackReason?: string | null) => {
    const reason = dataset.paused_reason ?? fallbackReason ?? "";
    const columns = driftColumns(reason);
    setDialog({ reason, columns });
    setChecked(Object.fromEntries(columns.map((column) => [column, true])));
    setResumed(false);
  };

  const confirmResume = async () => {
    if (!dialog) return;
    setBusy(true);
    setError(null);
    try {
      await resumeDataset(
        datasetId,
        dialog.columns.filter((column) => checked[column]),
      );
      setDialog(null);
      setResumed(true);
      onResumed();
    } catch (err) {
      setError(apiErrorText(err, t("console.datasets.detail.resumeError")));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="syncs-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.syncsTitle")}
      </h2>

      {dataset.sync_status === "paused_drift" && (
        <div
          data-testid="drift-banner"
          className="mt-2 flex flex-wrap items-center gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2"
        >
          <p className="text-xs text-amber-800">
            {t("console.datasets.detail.pauseBanner")}
          </p>
          <button
            type="button"
            data-testid="drift-resume"
            onClick={() => openResume()}
            className="ml-auto rounded bg-amber-600 px-2 py-1 text-xs text-white hover:bg-amber-700"
          >
            {t("console.datasets.detail.resume")}
          </button>
        </div>
      )}

      {dataset.sync_status === "paused_quota" && (
        <p
          data-testid="quota-banner"
          className="mt-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700"
        >
          {t("console.datasets.detail.pauseQuotaBanner")}
        </p>
      )}

      {error && (
        <p data-testid="syncs-op-error" className="mt-2 text-xs text-red-600">
          {error}
        </p>
      )}
      {resumed && (
        <p data-testid="resume-done" className="mt-2 text-xs text-emerald-600">
          {t("console.datasets.detail.resumed")}
        </p>
      )}

      {syncs == null && (
        <p data-testid="syncs-loading" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.syncsLoading")}
        </p>
      )}
      {syncs != null && syncs.length === 0 && (
        <p data-testid="syncs-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.syncsEmpty")}
        </p>
      )}
      {syncs != null && syncs.length > 0 && (
        <ul data-testid="sync-list" className="mt-2 space-y-1.5">
          {syncs.map((record) => (
            <li
              key={record.id}
              data-testid="sync-row"
              data-status={record.status}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
            >
              <span
                data-testid="sync-status"
                className={`rounded px-1.5 py-0.5 text-[11px] ${
                  record.status === "ok"
                    ? "bg-emerald-50 text-emerald-700"
                    : record.status === "failed"
                      ? "bg-red-50 text-red-700"
                      : "bg-amber-50 text-amber-700"
                }`}
              >
                {t(`console.datasets.detail.syncStatuses.${record.status}`)}
              </span>
              <span data-testid="sync-rows" className="text-slate-600">
                {t("console.datasets.detail.syncRows", { count: record.rows })}
              </span>
              <span className="text-[11px] text-slate-400">
                {formatServerDate(record.finished_at ?? record.started_at)}
              </span>
              {record.error && (
                <span
                  data-testid="sync-error"
                  className="font-mono text-[11px] text-red-500"
                >
                  {record.error}
                </span>
              )}
              {record.status === "drift" && (
                <button
                  type="button"
                  data-testid="sync-resume"
                  onClick={() => openResume(record.error)}
                  className="ml-auto rounded bg-amber-600 px-2 py-0.5 text-[11px] text-white hover:bg-amber-700"
                >
                  {t("console.datasets.detail.resume")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {dialog && (
        <div
          data-testid="resume-dialog"
          role="dialog"
          className="mt-3 rounded-lg border border-amber-300 bg-amber-50/60 p-4"
        >
          <h3 className="text-sm font-semibold">
            {t("console.datasets.detail.resumeTitle")}
          </h3>
          <p className="mt-1 text-xs text-slate-600">
            {t("console.datasets.detail.resumeHint")}
          </p>
          {dialog.reason && (
            <p
              data-testid="resume-reason"
              className="mt-2 font-mono text-[11px] text-slate-500"
            >
              {dialog.reason}
            </p>
          )}
          {dialog.columns.length === 0 ? (
            <p data-testid="resume-columns-empty" className="mt-2 text-[11px] text-slate-500">
              {t("console.datasets.detail.resumeColumnsEmpty")}
            </p>
          ) : (
            <ul className="mt-2 space-y-1">
              {dialog.columns.map((column) => (
                <li key={column}>
                  <label className="flex items-center gap-2 text-xs text-slate-700">
                    <input
                      type="checkbox"
                      data-testid="resume-column"
                      data-column={column}
                      checked={checked[column] !== false}
                      onChange={(e) =>
                        setChecked((prev) => ({
                          ...prev,
                          [column]: e.target.checked,
                        }))
                      }
                    />
                    <span className="font-mono">{column}</span>
                  </label>
                </li>
              ))}
            </ul>
          )}
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              data-testid="resume-confirm"
              disabled={busy}
              onClick={() => void confirmResume()}
              className="rounded bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-700 disabled:bg-amber-300"
            >
              {busy
                ? t("console.datasets.detail.resuming")
                : t("console.datasets.detail.resumeConfirm")}
            </button>
            <button
              type="button"
              data-testid="resume-cancel"
              onClick={() => setDialog(null)}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600"
            >
              {t("console.datasets.detail.resumeCancel")}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

// ── 增量同步设置（wire-incremental-sync）──────────────────────────────

/** 增量同步卡片（仅连接器来源，渲染门禁在 DatasetDetail）：模式切换——
 * 全量快照（snapshot）/ 增量水位（incremental，必须配水位列，下拉选项 =
 * 数据集列清单）；当前状态——模式 / 水位列 / 水位值（字符串原样、长值截断
 * +title 全文，未同步注记首次基线）；重置水位——二次确认（下次同步全量
 * 重建）后 PATCH {reset_watermark: true}，只清水位值；诚实口径小字——增量
 * 按主键合并、源端删除不传播。保存/重置失败（含 422）如实呈现后端 detail，
 * 不崩卡片；成功后 onChanged 重拉详情（手动 fetch 模式，与页内其余小节同款）。 */
function SyncSettingsSection({
  dataset,
  onChanged,
}: {
  dataset: Dataset;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  // 响应缺省容错：sync_mode 不带按 snapshot（旧后端与全量口径一致）。
  const currentMode: DatasetSyncMode = dataset.sync_mode ?? "snapshot";
  const [mode, setMode] = useState<DatasetSyncMode>(currentMode);
  const [watermarkColumn, setWatermarkColumn] = useState(
    dataset.watermark_column ?? "",
  );
  const [saving, setSaving] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [resetConfirm, setResetConfirm] = useState(false);
  const [resetting, setResetting] = useState(false);

  // 详情重拉后（保存/重置成功、外部刷新）把本地草稿同步回服务端态。
  useEffect(() => {
    setMode(dataset.sync_mode ?? "snapshot");
    setWatermarkColumn(dataset.watermark_column ?? "");
  }, [dataset.id, dataset.sync_mode, dataset.watermark_column]);

  // 客户端闸：增量未选水位列 → 保存禁用（服务端 422「增量必须配水位列」兜底）。
  const incrementalWithoutColumn =
    mode === "incremental" && watermarkColumn === "";

  const save = async () => {
    if (saving || resetting || incrementalWithoutColumn) return;
    setSaving(true);
    setOpError(null);
    try {
      await updateSyncSettings(
        dataset.id,
        mode === "incremental"
          ? { sync_mode: "incremental", watermark_column: watermarkColumn }
          : // 切回 snapshot：水位字段由服务端清空，客户端只发模式。
            { sync_mode: "snapshot" },
      );
      onChanged();
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.datasets.detail.syncSettings.error")),
      );
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    if (resetting) return;
    setResetting(true);
    setOpError(null);
    try {
      await updateSyncSettings(dataset.id, { reset_watermark: true });
      setResetConfirm(false);
      onChanged();
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.datasets.detail.syncSettings.error")),
      );
    } finally {
      setResetting(false);
    }
  };

  const watermarkValue = dataset.watermark_value ?? null;

  return (
    <section data-testid="sync-settings-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.syncSettings.title")}
      </h2>
      {/* 合并口径诚实注记：增量按主键合并、源端删除不传播、重置回全量。 */}
      <p
        data-testid="sync-honesty-note"
        className="mt-1 max-w-2xl text-xs text-slate-500"
      >
        {t("console.datasets.detail.syncSettings.honestyNote")}
      </p>

      {opError && (
        <p data-testid="sync-settings-error" className="mt-2 text-xs text-red-600">
          {opError}
        </p>
      )}

      {/* 当前状态：模式 / 水位列 / 水位值（原样字符串，长值截断 + title；
          未同步注记「首次将建立基线」）。 */}
      <div
        data-testid="sync-settings-status"
        className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs"
      >
        <span
          data-testid="sync-status-mode"
          data-mode={currentMode}
          className="text-slate-600"
        >
          <span className="text-slate-400">
            {t("console.datasets.detail.syncSettings.statusModeLabel")}：
          </span>
          {t(`console.datasets.detail.syncSettings.modes.${currentMode}`)}
        </span>
        <span
          data-testid="sync-status-column"
          data-column={dataset.watermark_column ?? ""}
          className="font-mono text-[11px] text-slate-700"
        >
          <span className="font-sans text-slate-400">
            {t("console.datasets.detail.syncSettings.statusColumnLabel")}：
          </span>
          {dataset.watermark_column ??
            t("console.datasets.detail.syncSettings.statusNone")}
        </span>
        <span data-testid="sync-status-value" className="text-slate-600">
          <span className="text-slate-400">
            {t("console.datasets.detail.syncSettings.statusValueLabel")}：
          </span>
          {watermarkValue == null ? (
            currentMode === "incremental" ? (
              <span data-testid="sync-not-synced" className="text-slate-400">
                {t("console.datasets.detail.syncSettings.notSynced")}
              </span>
            ) : (
              t("console.datasets.detail.syncSettings.statusNone")
            )
          ) : (
            <span
              data-testid="sync-status-value-text"
              title={watermarkValue}
              className="font-mono text-[11px] text-slate-700"
            >
              {truncate(watermarkValue, 32)}
            </span>
          )}
        </span>
        {/* 重置水位：增量已建立基线（水位值非空）才有意义；二次确认后 PATCH。 */}
        {currentMode === "incremental" && watermarkValue != null && (
          <button
            type="button"
            data-testid="sync-reset-button"
            disabled={resetting || saving}
            onClick={() => setResetConfirm(true)}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {resetting
              ? t("console.datasets.detail.syncSettings.resetting")
              : t("console.datasets.detail.syncSettings.reset")}
          </button>
        )}
      </div>

      {/* 编辑面：模式切换 + 水位列选择（增量时出现，选项 = 数据集列清单）。 */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-xs">
        <label className="flex items-center gap-1.5 text-slate-600">
          <span className="text-slate-500">
            {t("console.datasets.detail.syncSettings.modeLabel")}
          </span>
          <select
            data-testid="sync-mode-select"
            data-mode={mode}
            value={mode}
            disabled={saving || resetting}
            onChange={(e) => setMode(e.target.value as DatasetSyncMode)}
            className="rounded border border-slate-300 px-1.5 py-1 text-xs"
          >
            <option value="snapshot">
              {t("console.datasets.detail.syncSettings.modes.snapshot")}
            </option>
            <option value="incremental">
              {t("console.datasets.detail.syncSettings.modes.incremental")}
            </option>
          </select>
        </label>
        {mode === "incremental" && (
          <label className="flex items-center gap-1.5 text-slate-600">
            <span className="text-slate-500">
              {t("console.datasets.detail.syncSettings.watermarkLabel")}
            </span>
            <select
              data-testid="watermark-column-select"
              value={watermarkColumn}
              disabled={saving || resetting}
              onChange={(e) => setWatermarkColumn(e.target.value)}
              className="rounded border border-slate-300 px-1.5 py-1 font-mono text-xs"
            >
              <option value="">
                {t("console.datasets.detail.syncSettings.watermarkPlaceholder")}
              </option>
              {dataset.columns.map((column) => (
                <option key={column.name} value={column.name}>
                  {column.name}
                </option>
              ))}
            </select>
          </label>
        )}
        <button
          type="button"
          data-testid="sync-settings-save"
          disabled={saving || resetting || incrementalWithoutColumn}
          onClick={() => void save()}
          className="ml-auto rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {saving
            ? t("console.datasets.detail.syncSettings.saving")
            : t("console.datasets.detail.syncSettings.save")}
        </button>
      </div>

      {resetConfirm && (
        <div
          data-testid="sync-reset-dialog"
          role="dialog"
          className="mt-3 rounded-lg border border-amber-300 bg-amber-50/60 p-4"
        >
          <h3 className="text-sm font-semibold text-amber-800">
            {t("console.datasets.detail.syncSettings.resetTitle")}
          </h3>
          <p data-testid="sync-reset-warning" className="mt-1 text-xs text-slate-600">
            {t("console.datasets.detail.syncSettings.resetWarning")}
          </p>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              data-testid="sync-reset-confirm"
              disabled={resetting}
              onClick={() => void reset()}
              className="rounded bg-amber-600 px-3 py-1 text-xs text-white hover:bg-amber-700 disabled:bg-amber-300"
            >
              {resetting
                ? t("console.datasets.detail.syncSettings.resetting")
                : t("console.datasets.detail.syncSettings.resetConfirm")}
            </button>
            <button
              type="button"
              data-testid="sync-reset-cancel"
              disabled={resetting}
              onClick={() => setResetConfirm(false)}
              className="rounded border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
            >
              {t("console.datasets.detail.syncSettings.resetCancel")}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function DanglingPanel({
  corrections,
  revert,
}: {
  corrections: Correction[] | null;
  revert: (correctionId: number) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dangling = (corrections ?? []).filter(
    (correction) => correction.dangling_at != null && correction.reverted_at == null,
  );

  const doRevert = async (correctionId: number) => {
    setBusyId(correctionId);
    setError(null);
    try {
      await revert(correctionId);
    } catch (err) {
      setError(apiErrorText(err, t("console.datasets.detail.revertError")));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section data-testid="dangling-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.danglingTitle")}
      </h2>
      <p className="mt-1 text-xs text-slate-500">
        {t("console.datasets.detail.danglingHint")}
      </p>
      {error && (
        <p data-testid="dangling-op-error" className="mt-2 text-xs text-red-600">
          {error}
        </p>
      )}
      {corrections == null && (
        <p data-testid="dangling-loading" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.correctionsLoading")}
        </p>
      )}
      {corrections != null && dangling.length === 0 && (
        <p data-testid="dangling-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.detail.danglingEmpty")}
        </p>
      )}
      {dangling.length > 0 && (
        <ul data-testid="dangling-list" className="mt-2 space-y-1.5">
          {dangling.map((correction) => (
            <li
              key={correction.id}
              data-testid="dangling-item"
              data-op={correction.op}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-amber-200 bg-amber-50/50 px-3 py-2 text-xs"
            >
              <span
                data-testid="dangling-badge"
                className="rounded bg-amber-100 px-1.5 py-0.5 text-[10px] text-amber-700"
              >
                {t("console.datasets.detail.dangling")}
              </span>
              <span className="font-mono text-[11px] text-slate-700">
                {describeCorrection(correction)}
              </span>
              {correction.channel != null && correction.channel !== "console" && (
                <span
                  data-testid="correction-channel"
                  data-channel={correction.channel}
                  className={`rounded px-1.5 py-0.5 text-[10px] ${
                    correction.channel === "data-key"
                      ? "bg-sky-50 text-sky-700"
                      : "bg-violet-50 text-violet-700"
                  }`}
                >
                  {t(`console.datasets.detail.channel.${correction.channel}`)}
                </span>
              )}
              {correction.dangling_reason && (
                <span className="text-[11px] text-slate-500">
                  {correction.dangling_reason}
                </span>
              )}
              <button
                type="button"
                data-testid="dangling-revert"
                disabled={busyId === correction.id}
                onClick={() => void doRevert(correction.id)}
                className="ml-auto rounded border border-slate-300 bg-white px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
              >
                {busyId === correction.id
                  ? t("console.datasets.detail.reverting")
                  : t("console.datasets.detail.revert")}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
// ── 重派生 + 定时重派生（wire-pipeline-actions 任务 1.2）──────────────────

/** 手动重派生小节：级联开关（默认开）+ 逐级结果列表（名称/深度/ok/failed
 *  +失败原因）+ 完成后回调刷新详情（onRefreshed）。 */
function RederiveSection({
  datasetId,
  onRefreshed,
}: {
  datasetId: string;
  onRefreshed: () => void;
}) {
  const { t } = useTranslation();
  const [cascade, setCascade] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<DatasetRefreshCascadeEntry[] | null>(
    null,
  );

  const run = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setError(null);
    setResults(null);
    try {
      const payload = await refreshDataset(datasetId, cascade);
      setResults(payload.cascade ?? []);
      // 本体已刷新：详情字段（行数/更新时间）与预览行同步重拉。
      onRefreshed();
    } catch (err) {
      // 服务端 detail（api 档 403 / 非 derived 400 等口径）如实呈现。
      setError(apiErrorText(err, t("console.datasets.detail.rederive.error")));
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <section data-testid="rederive-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.rederive.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.detail.rederive.hint")}
      </p>

      {error && (
        <p data-testid="rederive-error" className="mt-2 text-xs text-red-600">
          {error}
        </p>
      )}

      <div
        data-testid="rederive-controls"
        className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-xs"
      >
        <label className="flex items-center gap-1.5 text-slate-700">
          <input
            type="checkbox"
            data-testid="rederive-cascade"
            checked={cascade}
            disabled={refreshing}
            onChange={(e) => setCascade(e.target.checked)}
          />
          {t("console.datasets.detail.rederive.cascadeLabel")}
        </label>
        <button
          type="button"
          data-testid="rederive-submit"
          disabled={refreshing}
          onClick={() => void run()}
          className="ml-auto rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
        >
          {refreshing
            ? t("console.datasets.detail.rederive.submitting")
            : t("console.datasets.detail.rederive.submit")}
        </button>
      </div>

      {results != null && results.length > 0 && (
        <ul data-testid="rederive-cascade" className="mt-2 space-y-1.5">
          {results.map((entry) => (
            <li
              key={entry.dataset_id}
              data-testid="rederive-cascade-row"
              data-status={entry.status}
              data-depth={entry.depth}
              className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
            >
              <span
                data-testid="rederive-cascade-status"
                data-status={entry.status}
                className={`rounded px-1.5 py-0.5 text-[11px] ${
                  entry.status === "ok"
                    ? "bg-emerald-50 text-emerald-700"
                    : "bg-red-50 text-red-700"
                }`}
              >
                {entry.status === "ok"
                  ? t("console.datasets.detail.rederive.ok")
                  : t("console.datasets.detail.rederive.failed")}
              </span>
              <span className="text-slate-700">{entry.name}</span>
              <span className="text-[11px] text-slate-400">
                {t("console.datasets.detail.rederive.depth", {
                  depth: entry.depth,
                })}
              </span>
              {entry.error && (
                <span
                  data-testid="rederive-cascade-error"
                  className="font-mono text-[11px] text-red-500"
                >
                  {entry.error}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {results != null && results.length === 0 && (
        <p
          data-testid="rederive-cascade-empty"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.rederive.cascadeEmpty")}
        </p>
      )}
    </section>
  );
}

/** 定时重派生小节：五字段 cron 登记/更新 + 已登记态呈现（cron/enabled/
 *  last_run_at 本地化/last_status/last_error/last_rows）+ 删除；naive UTC
 *  口径诚实注记（客户时区换算属登记侧 v1 约定）。 */
function RederiveScheduleSection({ datasetId }: { datasetId: string }) {
  const { t } = useTranslation();
  // undefined = 加载中；null = 未登记（登记面）；对象 = 已登记态。
  const [schedule, setSchedule] = useState<DatasetSchedule | null | undefined>(
    undefined,
  );
  const [loadError, setLoadError] = useState(false);
  const [cron, setCron] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      const next = await getDatasetSchedule(datasetId);
      setSchedule(next);
      setCron(next.cron ?? "");
    } catch {
      setSchedule(undefined);
      setLoadError(true);
    }
  }, [datasetId]);

  useEffect(() => {
    setSchedule(undefined);
    void load();
  }, [load]);

  const registered = schedule != null && schedule.cron != null;

  const save = async () => {
    if (saving || cron.trim() === "") return;
    setSaving(true);
    setOpError(null);
    try {
      const next = await putDatasetSchedule(datasetId, cron.trim());
      setSchedule(next);
      setCron(next.cron ?? cron.trim());
    } catch (err) {
      // 服务端 detail（cron 非法/档位闸等）如实呈现。
      setOpError(
        apiErrorText(err, t("console.datasets.detail.rederive.schedule.opError")),
      );
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    if (deleting) return;
    setDeleting(true);
    setOpError(null);
    try {
      await deleteDatasetSchedule(datasetId);
      // 删除后回到未登记态（登记面复现）。
      setSchedule(null);
      setCron("");
    } catch (err) {
      setOpError(
        apiErrorText(err, t("console.datasets.detail.rederive.schedule.opError")),
      );
    } finally {
      setDeleting(false);
    }
  };

  return (
    <section data-testid="rederive-schedule-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.rederive.schedule.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.detail.rederive.schedule.hint")}
      </p>
      {/* naive UTC 诚实注记（v1 口径：客户时区换算属登记侧）。 */}
      <p
        data-testid="rederive-schedule-utc-note"
        className="mt-1 text-xs text-amber-700"
      >
        {t("console.datasets.detail.rederive.schedule.utcNote")}
      </p>

      {opError && (
        <p
          data-testid="rederive-schedule-error"
          className="mt-2 text-xs text-red-600"
        >
          {opError}
        </p>
      )}

      {loadError && (
        <div data-testid="rederive-schedule-load-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.rederive.schedule.loadError")}
          </p>
          <button
            type="button"
            data-testid="rederive-schedule-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.detail.rederive.schedule.retry")}
          </button>
        </div>
      )}

      {schedule === undefined && !loadError && (
        <p
          data-testid="rederive-schedule-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.rederive.schedule.loading")}
        </p>
      )}

      {schedule !== undefined && !loadError && (
        <div
          data-testid="rederive-schedule-form"
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-xs"
        >
          <label className="flex items-center gap-1.5 text-slate-600">
            <span className="text-slate-500">
              {t("console.datasets.detail.rederive.schedule.cronLabel")}
            </span>
            <input
              data-testid="rederive-cron-input"
              value={cron}
              onChange={(e) => setCron(e.target.value)}
              placeholder={t(
                "console.datasets.detail.rederive.schedule.cronPlaceholder",
              )}
              className="w-40 rounded border border-slate-300 px-2 py-1 font-mono text-xs"
            />
          </label>
          <button
            type="button"
            data-testid="rederive-schedule-save"
            disabled={saving || cron.trim() === ""}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {saving
              ? t("console.datasets.detail.rederive.schedule.saving")
              : registered
                ? t("console.datasets.detail.rederive.schedule.update")
                : t("console.datasets.detail.rederive.schedule.save")}
          </button>
        </div>
      )}

      {registered && schedule != null && (
        <div
          data-testid="rederive-schedule-row"
          data-enabled={schedule.enabled ? "true" : "false"}
          className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
        >
          <span
            data-testid="rederive-schedule-enabled"
            data-enabled={schedule.enabled ? "true" : "false"}
            className={`rounded px-1.5 py-0.5 text-[11px] ${
              schedule.enabled
                ? "bg-emerald-50 text-emerald-700"
                : "bg-slate-100 text-slate-600"
            }`}
          >
            {schedule.enabled
              ? t("console.datasets.detail.rederive.schedule.registered")
              : t("console.datasets.detail.rederive.schedule.disabled")}
          </span>
          <span
            data-testid="rederive-schedule-cron"
            className="font-mono text-[11px] text-slate-700"
          >
            {schedule.cron}
          </span>
          <span data-testid="rederive-schedule-last-run" className="text-slate-600">
            <span className="text-slate-400">
              {t("console.datasets.detail.rederive.schedule.lastRunLabel")}：
            </span>
            {schedule.last_run_at
              ? formatServerDate(schedule.last_run_at)
              : t("console.datasets.detail.rederive.schedule.never")}
          </span>
          {schedule.last_status != null && (
            <span
              data-testid="rederive-schedule-last-status"
              data-status={schedule.last_status}
              className={`rounded px-1.5 py-0.5 text-[11px] ${
                schedule.last_status === "success"
                  ? "bg-emerald-50 text-emerald-700"
                  : /^(failed|partial_failure|quota)/.test(schedule.last_status)
                    ? "bg-red-50 text-red-700"
                    : "bg-slate-100 text-slate-600"
              }`}
            >
              {t("console.datasets.detail.rederive.schedule.lastStatusLabel")}：
              {schedule.last_status}
            </span>
          )}
          {schedule.last_rows != null && (
            <span
              data-testid="rederive-schedule-last-rows"
              className="text-slate-600"
            >
              {t("console.datasets.detail.rederive.schedule.lastRowsLabel", {
                count: schedule.last_rows,
              })}
            </span>
          )}
          {schedule.last_error && (
            <span
              data-testid="rederive-schedule-last-error"
              title={t("console.datasets.detail.rederive.schedule.lastErrorLabel")}
              className="font-mono text-[11px] text-red-500"
            >
              {schedule.last_error}
            </span>
          )}
          <button
            type="button"
            data-testid="rederive-schedule-delete"
            disabled={deleting}
            onClick={() => void remove()}
            className="ml-auto rounded border border-rose-200 px-2 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {deleting
              ? t("console.datasets.detail.rederive.schedule.deleting")
              : t("console.datasets.detail.rederive.schedule.delete")}
          </button>
        </div>
      )}
    </section>
  );
}
// ── 溯源区块（wire-provenance-ledger 任务组 8）──────────────────────────

/** 已知事件类型 → eventTypes i18n 键；cell.* 前缀归并为 cell 生命周期。 */
const PROVENANCE_EVENT_TYPE_KEYS: Record<string, string> = {
  "dataset.upload.registered": "uploadRegistered",
  "dataset.correction.added": "correctionAdded",
  "dataset.correction.reverted": "correctionReverted",
  "dataset.snapshot.refreshed": "snapshotRefreshed",
  "dataset.correction.dangling": "correctionDangling",
  "dataset.dataset.deleted": "datasetDeleted",
  "ledger.epoch.sealed": "epochSealed",
};

export function provenanceEventTypeKey(type: string): string | null {
  if (PROVENANCE_EVENT_TYPE_KEYS[type]) return PROVENANCE_EVENT_TYPE_KEYS[type];
  if (type.startsWith("cell.")) return "cellLifecycle";
  return null;
}

/** 事件负载摘要：顶层键值平铺 `k=v` 取前 4 项、长值截断，尾部 +N 计余。 */
export function summarizeProvenancePayload(
  payload: Record<string, unknown> | null | undefined,
): string {
  if (payload == null || typeof payload !== "object") return "";
  const entries = Object.entries(payload);
  const shown = entries.slice(0, 4).map(([key, value]) => {
    const text =
      value == null
        ? "null"
        : typeof value === "object"
          ? truncate(JSON.stringify(value), 48)
          : truncate(String(value), 48);
    return `${key}=${text}`;
  });
  const rest = entries.length - shown.length;
  if (rest > 0) shown.push(`+${rest}`);
  return shown.join(", ");
}

function ProvenancePanel({
  datasetKey,
}: {
  datasetKey: string;
}) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<ProvenanceStatus | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [overview, setOverview] = useState<ProvenanceOverview | null>(null);
  const [tier, setTier] = useState<ProvenanceTier>("off");
  const [tierBusy, setTierBusy] = useState(false);
  const [tierError, setTierError] = useState<string | null>(null);
  const [events, setEvents] = useState<ProvenanceEvent[] | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [eventsError, setEventsError] = useState(false);
  const [loadingEarlier, setLoadingEarlier] = useState(false);
  const [evidenceBusy, setEvidenceBusy] = useState(false);
  const [evidenceError, setEvidenceError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    setStatusError(false);
    try {
      const next = await fetchProvenanceStatus(datasetKey);
      setStatus(next);
      setTier(next.tier);
    } catch {
      setStatus(null);
      setStatusError(true);
    }
  }, [datasetKey]);

  const loadEvents = useCallback(async () => {
    setEventsError(false);
    try {
      const page = await fetchProvenanceEvents(datasetKey);
      setEvents(page.events);
      setHasMore(page.has_more);
    } catch {
      setEvents(null);
      setEventsError(true);
    }
  }, [datasetKey]);

  useEffect(() => {
    setStatus(null);
    setOverview(null);
    setTierError(null);
    void loadStatus();
    fetchProvenanceOverview()
      .then(setOverview)
      .catch(() => setOverview(null));
  }, [datasetKey, loadStatus]);

  useEffect(() => {
    setEvents(null);
    setHasMore(false);
    void loadEvents();
  }, [loadEvents]);

  const changeTier = async (next: ProvenanceTier) => {
    if (tierBusy || next === tier) return;
    setTierBusy(true);
    setTierError(null);
    try {
      const result = await updateProvenanceTier(datasetKey, next);
      setTier(result.tier);
      await loadStatus();
    } catch (err) {
      setTierError(
        apiErrorText(err, t("console.datasets.detail.provenance.tierError")),
      );
    } finally {
      setTierBusy(false);
    }
  };

  const loadEarlier = async () => {
    if (events == null || events.length === 0 || loadingEarlier) return;
    const oldest = events[events.length - 1]!.seq;
    setLoadingEarlier(true);
    try {
      const page = await fetchProvenanceEvents(datasetKey, oldest);
      setEvents((prev) => [...(prev ?? []), ...page.events]);
      setHasMore(page.has_more);
    } catch {
      setEventsError(true);
    } finally {
      setLoadingEarlier(false);
    }
  };

  const downloadEvidence = async () => {
    if (evidenceBusy) return;
    setEvidenceBusy(true);
    setEvidenceError(null);
    try {
      const { blob, filename } = await fetchEvidencePackage();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(objectUrl);
    } catch (err) {
      setEvidenceError(
        apiErrorText(err, t("console.datasets.detail.provenance.evidenceError")),
      );
    } finally {
      setEvidenceBusy(false);
    }
  };

  const anchoredThrough = status?.anchored_through;
  const anchoringParts: string[] = [];
  if (anchoredThrough?.backend) {
    anchoringParts.push(anchoredThrough.backend);
    anchoringParts.push(
      anchoredThrough.epoch != null
        ? t("console.datasets.detail.provenance.anchoringEpoch", {
            epoch: anchoredThrough.epoch,
          })
        : t("console.datasets.detail.provenance.anchoringNotSealed"),
    );
    if (overview?.anchoring) {
      anchoringParts.push(
        t("console.datasets.detail.provenance.anchoringLag", {
          count: overview.anchoring.lag_events,
        }),
      );
    }
  } else {
    anchoringParts.push(t("console.datasets.detail.provenance.anchoringNone"));
  }

  return (
    <section data-testid="provenance-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.provenance.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.detail.provenance.hint")}
      </p>

      {statusError && (
        <div data-testid="provenance-status-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.provenance.statusError")}
          </p>
          <button
            type="button"
            data-testid="provenance-status-retry"
            onClick={() => void loadStatus()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.detail.provenance.retry")}
          </button>
        </div>
      )}

      {status == null && !statusError && (
        <p
          data-testid="provenance-status-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.provenance.statusLoading")}
        </p>
      )}

      {status != null && (
        <div
          data-testid="provenance-status-bar"
          className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-xs"
        >
          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">
              {t("console.datasets.detail.provenance.tierLabel")}
            </span>
            <select
              data-testid="provenance-tier-select"
              data-tier={tier}
              value={tier}
              disabled={tierBusy}
              onChange={(e) => void changeTier(e.target.value as ProvenanceTier)}
              className="rounded border border-slate-300 px-1.5 py-1 text-xs disabled:bg-slate-100"
            >
              {PROVENANCE_TIERS.map((tierId) => (
                <option key={tierId} value={tierId}>
                  {t(`console.datasets.detail.provenance.tiers.${tierId}`)}
                </option>
              ))}
            </select>
          </label>
          <span
            data-testid="provenance-integrity"
            data-ok={status.integrity.ok ? "true" : "false"}
            className={`rounded-full px-2 py-0.5 ring-1 ${
              status.integrity.ok
                ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                : "bg-red-50 text-red-700 ring-red-200"
            }`}
          >
            {status.integrity.ok
              ? t("console.datasets.detail.provenance.integrityOk")
              : t("console.datasets.detail.provenance.integrityBroken")}
            <span className="ml-1 text-[10px] opacity-70">
              {t("console.datasets.detail.provenance.integrityChecked", {
                count: status.integrity.checked,
              })}
            </span>
          </span>
          <span
            data-testid="provenance-anchoring"
            data-backend={anchoredThrough?.backend ?? ""}
            data-epoch={anchoredThrough?.epoch ?? ""}
            className="text-slate-600"
          >
            <span className="text-slate-400">
              {t("console.datasets.detail.provenance.anchoringLabel")}：
            </span>
            {anchoringParts.join(" · ")}
          </span>
          {status.note ? (
            <span
              data-testid="provenance-note"
              className="rounded bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700"
            >
              {status.note}
            </span>
          ) : status.first_event_ts ? (
            <span
              data-testid="provenance-first-event"
              className="text-[11px] text-slate-400"
            >
              {t("console.datasets.detail.provenance.firstEvent", {
                time: formatServerDate(status.first_event_ts),
              })}
            </span>
          ) : null}
          <button
            type="button"
            data-testid="provenance-evidence-download"
            disabled={evidenceBusy}
            onClick={() => void downloadEvidence()}
            className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {evidenceBusy
              ? t("console.datasets.detail.provenance.evidenceDownloading")
              : t("console.datasets.detail.provenance.evidenceDownload")}
          </button>
        </div>
      )}

      {tierError && (
        <p data-testid="provenance-tier-error" className="mt-2 text-xs text-red-600">
          {tierError}
        </p>
      )}
      {evidenceError && (
        <p
          data-testid="provenance-evidence-error"
          className="mt-2 text-xs text-red-600"
        >
          {evidenceError}
        </p>
      )}

      <h3 className="mt-4 text-sm font-medium">
        {t("console.datasets.detail.provenance.timelineTitle")}
      </h3>
      {eventsError && (
        <div data-testid="provenance-events-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.provenance.timelineError")}
          </p>
          <button
            type="button"
            data-testid="provenance-events-retry"
            onClick={() => void loadEvents()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.detail.provenance.retry")}
          </button>
        </div>
      )}
      {events == null && !eventsError && (
        <p
          data-testid="provenance-timeline-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.provenance.timelineLoading")}
        </p>
      )}
      {events != null && events.length === 0 && !eventsError && (
        <p
          data-testid="provenance-timeline-empty"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.provenance.timelineEmpty")}
        </p>
      )}
      {events != null && events.length > 0 && (
        <ul data-testid="provenance-timeline" className="mt-2 space-y-1.5">
          {events.map((event) => {
            const typeKey = provenanceEventTypeKey(event.type);
            return (
              <li
                key={event.event_id || event.seq}
                data-testid="provenance-event"
                data-type={event.type}
                className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded border border-slate-200 bg-white px-3 py-2 text-xs"
              >
                <span
                  data-testid="provenance-event-type"
                  className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-600"
                >
                  {typeKey
                    ? t(`console.datasets.detail.provenance.eventTypes.${typeKey}`)
                    : event.type}
                </span>
                <span
                  data-testid="provenance-event-payload"
                  className="font-mono text-[11px] text-slate-700"
                >
                  {summarizeProvenancePayload(event.payload)}
                </span>
                <span
                  data-testid="provenance-event-actor"
                  className="text-[11px] text-slate-500"
                >
                  {event.actor.id}
                </span>
                <span
                  data-testid="provenance-event-ts"
                  className="text-[11px] text-slate-400"
                >
                  {formatServerDate(event.ts_platform)}
                </span>
                <span
                  data-testid="provenance-event-seq"
                  className="ml-auto font-mono text-[10px] text-slate-400"
                >
                  #{event.seq}
                </span>
              </li>
            );
          })}
        </ul>
      )}
      {hasMore && (
        <button
          type="button"
          data-testid="provenance-load-earlier"
          disabled={loadingEarlier}
          onClick={() => void loadEarlier()}
          className="mt-2 rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {loadingEarlier
            ? t("console.datasets.detail.provenance.loadingEarlier")
            : t("console.datasets.detail.provenance.loadEarlier")}
        </button>
      )}
    </section>
  );
}
// ── 资产信息（wire-asset-catalog 任务 5.2）──────────────────────────────

/** 留痕变更字段（before/after 键面；展示标签走 eventFields i18n）。 */
const ASSET_META_FIELD_KEYS = [
  "domain",
  "tags",
  "description",
  "cadence_hint",
  "owner_note",
  "sensitivity",
] as const;

/** 留痕条目的变更字段清单（前后值不同的键；坏/缺失值按无变更跳过）。 */
export function assetMetaChangedFields(event: {
  before?: Record<string, unknown> | null;
  after?: Record<string, unknown> | null;
}): string[] {
  return ASSET_META_FIELD_KEYS.filter(
    (field) =>
      event.before != null &&
      event.after != null &&
      event.before[field] !== event.after[field],
  );
}

/** 标签输入草稿 → 数组（中英逗号分隔、去空白、去空项）。 */
export function parseTagsInput(text: string): string[] {
  return text
    .split(/[,，]/)
    .map((tag) => tag.trim())
    .filter(Boolean);
}

function AssetMetaSection({
  datasetId,
  onSensitivityChange,
}: {
  datasetId: string;
  /** 敏感度上报（wire-dataset-marketplace 6.1）：数据开放选择器的敏感档
   *  禁用闸数据源——元数据加载与保存后即回调最新敏感度。 */
  onSensitivityChange?: (sensitivity: AssetSensitivity) => void;
}) {
  const { t } = useTranslation();
  const [meta, setMeta] = useState<DatasetAssetMeta | null>(null);
  const [loadError, setLoadError] = useState(false);
  // 六字段草稿（cadence 空串=未声明；tags 以逗号文本编辑，提交时拆数组）。
  const [domain, setDomain] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [description, setDescription] = useState("");
  const [cadence, setCadence] = useState<"" | AssetCadenceHint>("");
  const [owner, setOwner] = useState("");
  const [sensitivity, setSensitivity] = useState<AssetSensitivity>("常规");
  const [saving, setSaving] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  // 403（viewer 只读）后置只读：控制台无独立角色端点（团队总览 owner-only），
  // 前端以服务端 PATCH 403 裁决为准如实降级，不预判角色。
  const [readOnly, setReadOnly] = useState(false);
  const [events, setEvents] = useState<AssetMetaEvent[] | null>(null);
  const [eventsOpen, setEventsOpen] = useState(false);

  const applyMeta = useCallback(
    (next: DatasetAssetMeta) => {
      setMeta(next);
      setDomain(next.domain ?? "");
      setTagsText(next.tags.join(", "));
      setDescription(next.description ?? "");
      setCadence(next.cadence_hint ?? "");
      setOwner(next.owner_note ?? "");
      setSensitivity(next.sensitivity);
      onSensitivityChange?.(next.sensitivity);
    },
    [onSensitivityChange],
  );

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      applyMeta(await fetchDatasetAssetMeta(datasetId));
    } catch {
      setMeta(null);
      setLoadError(true);
    }
  }, [datasetId, applyMeta]);

  const loadEvents = useCallback(async () => {
    try {
      const page = await fetchAssetMetaEvents(datasetId);
      setEvents(page.items);
    } catch {
      // 留痕端点不可用（旧后端/故障）→ 留痕区静默不渲染，不阻塞编辑面。
      setEvents(null);
    }
  }, [datasetId]);

  useEffect(() => {
    setMeta(null);
    setEvents(null);
    setReadOnly(false);
    setOpError(null);
    setFieldErrors({});
    void load();
    void loadEvents();
  }, [load, loadEvents]);

  const save = async () => {
    if (saving || readOnly) return;
    setSaving(true);
    setOpError(null);
    setFieldErrors({});
    setSaved(false);
    try {
      const next = await updateDatasetAssetMeta(datasetId, {
        domain: domain.trim() || null,
        tags: parseTagsInput(tagsText),
        description: description.trim() || null,
        cadence_hint: cadence || null,
        owner_note: owner.trim() || null,
        sensitivity,
      });
      applyMeta(next);
      setSaved(true);
      void loadEvents();
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 403) {
        // viewer 只读：切只读态（字段禁用、保存收起、提示如实），不报错条。
        setReadOnly(true);
        setOpError(null);
      } else if (
        err instanceof ConsoleApiError &&
        err.status === 422 &&
        err.detailItems?.length
      ) {
        // FastAPI 422 数组：按 loc 定位字段逐条回显（如非法敏感度合法清单）。
        const errors: Record<string, string> = {};
        for (const item of err.detailItems) {
          const field = item.loc?.[1];
          if (typeof field === "string" && !errors[field]) {
            errors[field] = item.msg ?? "";
          }
        }
        setFieldErrors(errors);
        setOpError(t("console.datasets.detail.asset.error"));
      } else {
        setOpError(apiErrorText(err, t("console.datasets.detail.asset.error")));
      }
    } finally {
      setSaving(false);
    }
  };

  const fieldError = (field: string) =>
    fieldErrors[field] ? (
      <span
        data-testid="asset-meta-field-error"
        data-field={field}
        className="text-[11px] text-red-600"
      >
        {fieldErrors[field]}
      </span>
    ) : null;

  const inputClass =
    "rounded border border-slate-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:bg-slate-100";

  return (
    <section data-testid="asset-meta-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.datasets.detail.asset.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.datasets.detail.asset.hint")}
      </p>

      {loadError && (
        <div data-testid="asset-meta-load-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.asset.loadError")}
          </p>
          <button
            type="button"
            data-testid="asset-meta-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.retry")}
          </button>
        </div>
      )}

      {meta == null && !loadError && (
        <p
          data-testid="asset-meta-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.asset.loading")}
        </p>
      )}

      {meta != null && !loadError && (
        <div
          data-testid="asset-meta-form"
          data-readonly={readOnly ? "true" : "false"}
          className="mt-2 rounded-lg border border-slate-200 bg-white px-3 py-3 text-xs"
        >
          <div className="flex flex-wrap items-start gap-3">
            <label className="flex w-56 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.domainLabel")}
              </span>
              <input
                data-testid="asset-meta-domain"
                value={domain}
                disabled={readOnly || saving}
                onChange={(e) => setDomain(e.target.value)}
                placeholder={t("console.datasets.detail.asset.domainPlaceholder")}
                className={inputClass}
              />
              {fieldError("domain")}
            </label>
            <label className="flex w-64 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.tagsLabel")}
              </span>
              <input
                data-testid="asset-meta-tags"
                value={tagsText}
                disabled={readOnly || saving}
                onChange={(e) => setTagsText(e.target.value)}
                placeholder={t("console.datasets.detail.asset.tagsPlaceholder")}
                className={inputClass}
              />
              {fieldError("tags")}
            </label>
            <label className="flex w-40 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.cadenceLabel")}
              </span>
              <select
                data-testid="asset-meta-cadence"
                value={cadence}
                disabled={readOnly || saving}
                onChange={(e) =>
                  setCadence(e.target.value as "" | AssetCadenceHint)
                }
                className={inputClass}
              >
                <option value="">
                  {t("console.datasets.detail.asset.cadenceValues.undeclared")}
                </option>
                {(["daily", "weekly", "monthly"] as AssetCadenceHint[]).map(
                  (value) => (
                    <option key={value} value={value}>
                      {t(`console.datasets.detail.asset.cadenceValues.${value}`)}
                    </option>
                  ),
                )}
              </select>
              {fieldError("cadence_hint")}
            </label>
            <label className="flex w-40 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.ownerLabel")}
              </span>
              <input
                data-testid="asset-meta-owner"
                value={owner}
                disabled={readOnly || saving}
                onChange={(e) => setOwner(e.target.value)}
                placeholder={t("console.datasets.detail.asset.ownerPlaceholder")}
                className={inputClass}
              />
              {fieldError("owner_note")}
            </label>
            <label className="flex w-36 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.sensitivityLabel")}
              </span>
              <select
                data-testid="asset-meta-sensitivity"
                value={sensitivity}
                disabled={readOnly || saving}
                onChange={(e) =>
                  setSensitivity(e.target.value as AssetSensitivity)
                }
                className={inputClass}
              >
                {(["常规", "内部", "敏感"] as AssetSensitivity[]).map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
              {meta.sensitivity_inherited && (
                <span
                  data-testid="asset-meta-sensitivity-inherited"
                  className="text-[11px] text-amber-700"
                >
                  {t("console.datasets.detail.asset.sensitivityInherited", {
                    level: meta.sensitivity_effective ?? meta.sensitivity,
                  })}
                </span>
              )}
              {meta.sensitivity_incomplete && (
                <span
                  data-testid="asset-meta-sensitivity-incomplete"
                  className="text-[11px] text-slate-500"
                >
                  {t("console.datasets.detail.asset.sensitivityIncomplete")}
                </span>
              )}
              {fieldError("sensitivity")}
            </label>
            <label className="flex min-w-64 flex-1 flex-col gap-1">
              <span className="text-slate-500">
                {t("console.datasets.detail.asset.descriptionLabel")}
              </span>
              <textarea
                data-testid="asset-meta-description"
                value={description}
                disabled={readOnly || saving}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t(
                  "console.datasets.detail.asset.descriptionPlaceholder",
                )}
                rows={2}
                className={`${inputClass} w-full`}
              />
              {fieldError("description")}
            </label>
          </div>

          {/* 生效节奏提示：api 层数据集自动取自指标注册表；客户未声明时展示。 */}
          {meta.cadence_effective && !cadence && (
            <p
              data-testid="asset-meta-effective-hint"
              className="mt-2 text-[11px] text-slate-400"
            >
              {t("console.datasets.detail.asset.effectiveHint", {
                cadence: t(
                  `console.datasets.detail.asset.cadenceValues.${meta.cadence_effective}`,
                ),
              })}
            </p>
          )}

          {opError && (
            <p
              data-testid="asset-meta-error"
              className="mt-2 text-xs text-red-600"
            >
              {opError}
            </p>
          )}
          {saved && !opError && (
            <p
              data-testid="asset-meta-saved"
              className="mt-2 text-xs text-emerald-600"
            >
              {t("console.datasets.detail.asset.saved")}
            </p>
          )}
          {readOnly && (
            <p
              data-testid="asset-meta-readonly"
              className="mt-2 text-xs text-amber-700"
            >
              {t("console.datasets.detail.asset.readonlyNote")}
            </p>
          )}
          {!readOnly && (
            <button
              type="button"
              data-testid="asset-meta-save"
              disabled={saving}
              onClick={() => void save()}
              className="mt-2 rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {saving
                ? t("console.datasets.detail.asset.saving")
                : t("console.datasets.detail.asset.save")}
            </button>
          )}

          {/* 最近修改留痕（append-only；最近 3 条，默认折叠）。 */}
          {events != null && events.length > 0 && (
            <div
              data-testid="asset-meta-events"
              className="mt-3 border-t border-slate-100 pt-2"
            >
              <button
                type="button"
                data-testid="asset-meta-events-toggle"
                onClick={() => setEventsOpen((open) => !open)}
                className="text-[11px] text-slate-500 underline-offset-2 hover:underline"
              >
                {eventsOpen
                  ? t("console.datasets.detail.asset.eventsHide")
                  : t("console.datasets.detail.asset.eventsShow")}
              </button>
              {eventsOpen && (
                <ul className="mt-1.5 space-y-1">
                  {events.slice(0, 3).map((event, index) => {
                    const changed = assetMetaChangedFields(event);
                    return (
                      <li
                        key={`${event.at}-${index}`}
                        data-testid="asset-meta-event"
                        className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[11px] text-slate-500"
                      >
                        <span>{formatServerDate(event.at)}</span>
                        <span>·</span>
                        <span className="font-mono">{event.actor_sub}</span>
                        {changed.length > 0 && (
                          <>
                            <span>·</span>
                            <span data-testid="asset-meta-event-changes">
                              {t(
                                "console.datasets.detail.asset.eventChanged",
                              )}
                             ：
                              {changed
                                .map((field) =>
                                  t(
                                    `console.datasets.detail.asset.eventFields.${field}`,
                                  ),
                                )
                                .join("、")}
                            </span>
                          </>
                        )}
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

// ── 数据质量（wire-dataset-quality 任务 5.3）────────────────────────────

const QUALITY_FRESHNESS_CLASS: Record<QualityFreshnessValue, string> = {
  fresh: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  stale: "bg-amber-50 text-amber-700 ring-amber-200",
  unknown: "bg-slate-100 text-slate-600 ring-slate-200",
};

/** 详情响应 freshness 容错解析：对象 {freshness,last_refresh_at} 或裸枚举
 *  字符串两种下发形态都收（并行后端形态差异不前端崩）。 */
export function parseQualityFreshness(raw: unknown): {
  freshness: QualityFreshnessValue;
  last_refresh_at: string | null;
} {
  if (raw === "fresh" || raw === "stale" || raw === "unknown") {
    return { freshness: raw, last_refresh_at: null };
  }
  if (raw && typeof raw === "object") {
    const record = raw as { freshness?: unknown; last_refresh_at?: unknown };
    const value = record.freshness;
    return {
      freshness:
        value === "fresh" || value === "stale" || value === "unknown"
          ? value
          : "unknown",
      last_refresh_at:
        typeof record.last_refresh_at === "string" && record.last_refresh_at
          ? record.last_refresh_at
          : null,
    };
  }
  return { freshness: "unknown", last_refresh_at: null };
}

/** 比率 → 百分比文本（0..1 乘 100；异常值原样保守呈现一位小数）。 */
export function formatRatePercent(rate: number): string {
  const percent = rate >= 0 && rate <= 1 ? rate * 100 : rate;
  return `${percent.toFixed(1)}%`;
}

/** 行数趋势图（echarts SVG 渲染器——测试环境无 canvas 也能跑，与
 *  DashboardRenderer.EChartBox 同生命周期纪律：init/dispose/option 重画）。 */
function QualityTrendChart({ history }: { history: QualityHistoryEntry[] }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const option = useMemo<echarts.EChartsOption>(
    () => ({
      grid: { left: 8, right: 8, top: 8, bottom: 8, containLabel: true },
      xAxis: {
        type: "category",
        data: history.map((entry) => entry.computed_at.slice(0, 10)),
        show: false,
      },
      yAxis: { type: "value", show: false },
      series: [
        {
          type: "line",
          data: history.map((entry) => entry.row_count),
          showSymbol: false,
          lineStyle: { width: 1.5, color: "#334155" },
          areaStyle: { opacity: 0.08 },
        },
      ],
    }),
    [history],
  );

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, undefined, {
      renderer: "svg",
    });
    chartRef.current = chart;
    return () => {
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.setOption(option, true);
  }, [option]);

  return <div ref={containerRef} data-testid="quality-trend" style={{ height: 72 }} />;
}

/** 触发方式已知值（未知值原样呈现——服务端口径如实）。 */
const QUALITY_TRIGGER_KEYS = ["manual", "sync", "create", "derive"] as const;

function QualityPanel({ datasetId }: { datasetId: string }) {
  const { t } = useTranslation();
  const [data, setData] = useState<DatasetQuality | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      setData(await fetchDatasetQuality(datasetId));
    } catch {
      setData(null);
      setLoadError(true);
    }
  }, [datasetId]);

  useEffect(() => {
    setData(null);
    void load();
  }, [load]);

  /** 手动「立即检查」：POST refresh 后重拉详情——新快照或如实失败记录都以
   *  详情区为准呈现；触发失败（如 viewer 403 / 引擎不可达）如实报错误条。 */
  const refresh = async () => {
    if (refreshing) return;
    setRefreshing(true);
    setRefreshError(null);
    try {
      await refreshDatasetQuality(datasetId);
      await load();
    } catch (err) {
      setRefreshError(
        apiErrorText(err, t("console.datasets.detail.quality.refreshError")),
      );
    } finally {
      setRefreshing(false);
    }
  };

  const freshness = data ? parseQualityFreshness(data.freshness) : null;
  const latest: QualitySnapshot | null = data?.latest ?? null;

  return (
    <section data-testid="quality-section" className="mt-6">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-base font-semibold">
          {t("console.datasets.detail.quality.title")}
        </h2>
        {/* 口径标注常驻（呈现如实：任何质量信息界面都含该标注）。 */}
        <span
          data-testid="quality-caption"
          className="rounded bg-slate-100 px-2 py-0.5 text-[11px] text-slate-500"
        >
          {t("console.datasets.detail.quality.caption")}
        </span>
        <button
          type="button"
          data-testid="quality-refresh"
          disabled={refreshing}
          onClick={() => void refresh()}
          className="ml-auto rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {refreshing
            ? t("console.datasets.detail.quality.refreshing")
            : t("console.datasets.detail.quality.refresh")}
        </button>
      </div>

      {refreshError && (
        <p
          data-testid="quality-refresh-error"
          className="mt-2 text-xs text-red-600"
        >
          {refreshError}
        </p>
      )}

      {loadError && (
        <div data-testid="quality-error" className="mt-2">
          <p className="text-xs text-red-600">
            {t("console.datasets.detail.quality.loadError")}
          </p>
          <button
            type="button"
            data-testid="quality-retry"
            onClick={() => void load()}
            className="mt-1 rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            {t("console.datasets.retry")}
          </button>
        </div>
      )}

      {data == null && !loadError && (
        <p
          data-testid="quality-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.datasets.detail.quality.loading")}
        </p>
      )}

      {data != null && freshness != null && (
        <>
          {/* 新鲜度 chip：按声明节奏判定（未声明=未知，不误判为过期）。 */}
          <span
            data-testid="quality-freshness"
            data-freshness={freshness.freshness}
            className={`mt-2 inline-flex items-center rounded-full px-2 py-0.5 text-xs ring-1 ${
              QUALITY_FRESHNESS_CLASS[freshness.freshness]
            }`}
          >
            {t(`console.datasets.detail.quality.freshness.${freshness.freshness}`)}
            <span className="ml-1.5 text-[10px] opacity-70">
              {t("console.datasets.detail.quality.lastRefreshLabel")}：
              {freshness.last_refresh_at
                ? formatServerDate(freshness.last_refresh_at)
                : t("console.datasets.detail.quality.lastRefreshNone")}
            </span>
          </span>

          {/* 暂无快照（与引擎不支持分开呈现——都绝不显示估计值）。 */}
          {latest == null && (
            <div
              data-testid="quality-empty"
              data-state="empty"
              className="mt-2 rounded-lg border border-dashed border-slate-300 bg-slate-50/60 px-3 py-4 text-xs text-slate-500"
            >
              <p className="font-medium text-slate-600">
                {t("console.datasets.detail.quality.emptyTitle")}
              </p>
              <p className="mt-1">
                {t("console.datasets.detail.quality.emptyHint")}
              </p>
            </div>
          )}

          {/* 引擎暂不支持（滚动版本窗口的降级态）。 */}
          {latest?.status === "unsupported" && (
            <div
              data-testid="quality-unsupported"
              data-state="unsupported"
              className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 px-3 py-4 text-xs text-slate-500"
            >
              <p className="font-medium text-slate-600">
                {t("console.datasets.detail.quality.unsupportedTitle")}
              </p>
              <p className="mt-1">
                {t("console.datasets.detail.quality.unsupportedHint")}
              </p>
            </div>
          )}

          {/* 最近一次快照失败：只示原因摘要，不显示旧指标冒充当前值。 */}
          {latest?.status === "failed" && (
            <div
              data-testid="quality-failed"
              data-state="failed"
              className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-4 text-xs"
            >
              <p className="font-medium text-red-700">
                {t("console.datasets.detail.quality.failedTitle")}
              </p>
              {latest.error_text && (
                <p
                  data-testid="quality-failed-error"
                  className="mt-1 break-all font-mono text-[11px] text-red-500"
                >
                  {truncate(latest.error_text, 160)}
                </p>
              )}
              <p className="mt-1 text-[11px] text-red-400">
                {t("console.datasets.detail.quality.failedHint")}
              </p>
            </div>
          )}

          {/* ok：五项指标如实呈现（键列未声明标「未声明键列」、抽样标注）。 */}
          {latest?.status === "ok" && (
            <div
              data-testid="quality-metrics"
              data-state="ok"
              className="mt-2 rounded-lg border border-slate-200 bg-white px-3 py-3 text-xs"
            >
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
                <span data-testid="quality-rows" className="text-slate-600">
                  <span className="text-slate-400">
                    {t("console.datasets.detail.quality.rowsLabel")}：
                  </span>
                  {latest.row_count.toLocaleString("en-US")}
                </span>
                <span
                  data-testid="quality-pk"
                  data-declared={latest.pk_declared ? "true" : "false"}
                  className="text-slate-600"
                >
                  <span className="text-slate-400">
                    {t("console.datasets.detail.quality.pkLabel")}：
                  </span>
                  {latest.pk_declared && latest.pk_unique_ratio != null
                    ? formatRatePercent(latest.pk_unique_ratio)
                    : t("console.datasets.detail.quality.pkUndeclared")}
                </span>
                <span data-testid="quality-computed" className="text-slate-500">
                  {t("console.datasets.detail.quality.computedAtLabel")}：
                  {formatServerDate(latest.computed_at)}
                </span>
                <span data-testid="quality-trigger" className="text-slate-500">
                  {t("console.datasets.detail.quality.triggeredByLabel")}：
                  {(QUALITY_TRIGGER_KEYS as readonly string[]).includes(
                    latest.triggered_by,
                  )
                    ? t(
                        `console.datasets.detail.quality.triggeredBy.${latest.triggered_by}`,
                      )
                    : latest.triggered_by}
                </span>
              </div>

              {latest.null_rates.length > 0 && (
                <div className="mt-2 overflow-x-auto">
                  <table data-testid="quality-null-rates" className="text-xs">
                    <thead>
                      <tr className="bg-slate-50">
                        <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                          {t("console.datasets.detail.quality.nullRatesColumn")}
                        </th>
                        <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                          {t("console.datasets.detail.quality.nullRatesRate")}
                        </th>
                        <th className="border-b border-slate-200 px-2 py-1 text-left font-medium text-slate-500">
                          {t(
                            "console.datasets.detail.quality.nullRatesSampled",
                          )}
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {latest.null_rates.map((entry) => (
                        <tr key={entry.column} data-testid="quality-null-rate">
                          <td className="border-b border-slate-100 px-2 py-1 font-mono text-slate-700">
                            {entry.column}
                          </td>
                          <td className="border-b border-slate-100 px-2 py-1 text-slate-700">
                            {formatRatePercent(entry.null_rate)}
                          </td>
                          <td className="border-b border-slate-100 px-2 py-1 text-slate-500">
                            {entry.sampled_rows.toLocaleString("en-US")}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              {latest.sample_limit_hit && (
                <p
                  data-testid="quality-sample-note"
                  className="mt-2 text-[11px] text-slate-400"
                >
                  {t("console.datasets.detail.quality.sampleNote")}
                </p>
              )}
            </div>
          )}

          {/* 行数趋势（≥2 个历史点才画；快照失败/暂无时不画——不用旧值冒充）。 */}
          {(latest?.status === "ok" || latest == null) &&
            data.history.length >= 2 && (
              <div className="mt-3">
                <p className="text-xs font-medium text-slate-600">
                  {t("console.datasets.detail.quality.trendTitle")}
                </p>
                <QualityTrendChart history={data.history} />
              </div>
            )}
        </>
      )}
    </section>
  );
}
