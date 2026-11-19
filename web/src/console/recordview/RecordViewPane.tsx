// 单记录页视图容器（wire-record-view 任务 4.1）：产物页 record-view kind 的
// 展开区（内嵌，不走路由——照 FormPane 范式）。拉取产物详情 → 查看态渲染
// RecordViewRuntime（试预览端点、console 身份、按当前版本组装）+ 未绑数据集
// 诚实态；未归档时提供「编辑」切换 RecordViewEditor；保存后同步刷新列表行。
// spec 解析宽容（非 record-view 形状回退最小空 spec）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchLifecycleArtifact,
  type Artifact,
  type ArtifactDetail,
} from "../api";
import { parseRecordViewSpec } from "./recordViewSpec";
import { RecordViewRuntime } from "./RecordViewRuntime";
import { RecordViewEditor } from "./RecordViewEditor";

export function RecordViewPane({
  artifactId,
  onSaved,
  startEditing = false,
}: {
  artifactId: number;
  onSaved: (artifact: Artifact) => void;
  /** 挂载即编辑态（空白新建的单记录页「创建即跳构建器」；缺省查看态）。 */
  startEditing?: boolean;
}) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<ArtifactDetail | null>(null);
  const [failed, setFailed] = useState(false);
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setFailed(false);
    setEditing(startEditing);
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

  if (failed) {
    return (
      <div
        data-testid="record-view-pane"
        className="mt-3 rounded-md border border-red-200 bg-red-50/60 p-4"
      >
        <p data-testid="record-view-load-error" className="text-xs text-red-700">
          {t("console.recordview.loadError")}
        </p>
      </div>
    );
  }

  if (detail == null) {
    return (
      <div
        data-testid="record-view-pane"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
      >
        <p data-testid="record-view-loading" className="text-xs text-slate-400">
          {t("console.recordview.loading")}
        </p>
      </div>
    );
  }

  // 宽容解析：非 record-view 形状回退最小空 spec，绝不白屏。
  const body =
    parseRecordViewSpec(detail.spec) ?? {
      datasetId: null,
      keyColumn: null,
      headerFields: [],
      sections: [],
    };
  const locked = detail.status === "archived";
  const bound = body.datasetId != null && !!body.keyColumn;

  return (
    <div
      data-testid="record-view-pane"
      className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("console.recordview.paneTitle")}</h2>
        {locked && (
          <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
            {t("console.recordview.lockedBadge")}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          {!editing && !locked && (
            <button
              type="button"
              data-testid="record-view-edit"
              onClick={() => setEditing(true)}
              className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.recordview.editButton")}
            </button>
          )}
        </div>
      </div>

      {editing ? (
        <RecordViewEditor
          artifactId={artifactId}
          initial={{ title: detail.title, body }}
          onSaved={(next) => {
            setDetail(next);
            onSaved(next);
          }}
          onExit={() => setEditing(false)}
        />
      ) : bound ? (
        // 查看态：示例键值选择 + 与公开页同构的取数渲染（console 身份）。
        <div className="mt-2">
          <p className="text-[11px] text-slate-400">
            {t("console.recordview.viewHint")}
          </p>
          <PreviewWithKeySelector artifactId={artifactId} />
        </div>
      ) : (
        // 未绑数据集中间态：如实提示（可保存不可分享）。
        <p
          data-testid="record-view-unbound"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.recordview.unboundBody")}
        </p>
      )}
    </div>
  );
}

/** 查看态的键值选择器 + 试预览渲染（键值下拉 ≤500 或手输；console 身份
 *  preview 端点按当前版本组装——不生成公开链接）。 */
function PreviewWithKeySelector({ artifactId }: { artifactId: number }) {
  const { t } = useTranslation();
  const [manualKey, setManualKey] = useState("");
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  return (
    <div className="mt-2">
      <div className="flex flex-wrap items-center gap-2">
        <input
          data-testid="record-view-view-key"
          value={manualKey}
          onChange={(e) => setManualKey(e.target.value)}
          placeholder={t("console.recordview.preview.keyPlaceholder")}
          className="w-48 rounded border border-slate-300 px-2 py-1 text-xs"
        />
        <button
          type="button"
          data-testid="record-view-view-preview"
          disabled={!manualKey.trim()}
          onClick={() => setPreviewKey(manualKey.trim())}
          className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {t("console.recordview.preview.button")}
        </button>
      </div>
      {previewKey != null && (
        <div className="mt-3">
          <RecordViewRuntime
            endpoint={{ kind: "preview", artifactId, recordKey: previewKey }}
          />
        </div>
      )}
    </div>
  );
}
