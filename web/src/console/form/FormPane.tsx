// 表单视图容器（wire-form-builder 任务 4.4）：产物页 form kind 的展开区
// （内嵌，不走路由）。拉取产物详情 → 查看态渲染 FormRuntime（当前版本，
// console 提交走 corrections）；未归档时提供「编辑」切换 FormEditor；保存后
// 同步刷新列表行。spec 解析宽容（非表单形状回退零字段未绑定的诚实态）。
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  fetchLifecycleArtifact,
  type Artifact,
  type ArtifactDetail,
} from "../api";
import { parseFormSpec } from "./formSpec";
import { FormRuntime } from "./FormRuntime";
import { FormEditor } from "./FormEditor";

export function FormPane({
  artifactId,
  onSaved,
  startEditing = false,
}: {
  artifactId: number;
  onSaved: (artifact: Artifact) => void;
  /** 挂载即编辑态（空白新建的表单「创建即跳构建器」；缺省查看态）。 */
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
        data-testid="form-pane"
        className="mt-3 rounded-md border border-red-200 bg-red-50/60 p-4"
      >
        <p data-testid="form-load-error" className="text-xs text-red-700">
          {t("console.form.loadError")}
        </p>
      </div>
    );
  }

  if (detail == null) {
    return (
      <div
        data-testid="form-pane"
        className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4"
      >
        <p data-testid="form-loading" className="text-xs text-slate-400">
          {t("console.form.loading")}
        </p>
      </div>
    );
  }

  // 宽容解析：非表单形状（如手工改坏的 spec）回退零字段未绑定诚实态，
  // 绝不白屏。
  const body =
    parseFormSpec(detail.spec) ?? { datasetId: null, fields: [] };
  const locked = detail.status === "archived";

  return (
    <div data-testid="form-pane" className="mt-3 rounded-md border border-slate-200 bg-slate-50/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="text-sm font-semibold">{t("console.form.paneTitle")}</h2>
        {locked && (
          <span className="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] text-slate-600">
            {t("console.form.lockedBadge")}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          {!editing && !locked && (
            <button
              type="button"
              data-testid="form-edit"
              onClick={() => setEditing(true)}
              className="rounded border border-slate-300 bg-white px-2 py-1 text-xs text-slate-600 hover:bg-slate-100"
            >
              {t("console.form.editButton")}
            </button>
          )}
        </div>
      </div>

      {editing ? (
        <FormEditor
          artifactId={artifactId}
          initial={{ title: detail.title, body }}
          onSaved={(next) => {
            setDetail(next);
            onSaved(next);
          }}
          onExit={() => setEditing(false)}
        />
      ) : (
        <div className="mt-2">
          <FormRuntime
            spec={body}
            endpoint={{
              kind: "console",
              datasetId: body.datasetId,
              // wire-form-entry-advanced：console 提交携带表单产物 id——
              // 服务端按当前版本执行全量字段校验+提交闸（corrections 的
              // artifact_id 语义）。
              artifactId,
            }}
          />
        </div>
      )}
    </div>
  );
}
