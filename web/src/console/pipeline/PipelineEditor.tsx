// 管线编辑器（wire-transform-pipelines 任务 5.2）：名称/描述/基础数据集 +
// 线性步骤列表（增删改序；三类型参数表单与既有原子操作参数形状一致——
// derive=过滤/排序/限行派生 {filters,sort?,limit}；join=挂接指标
// {indicators,date_column,region_column?,default_entity?,filters?}；
// link=关联数据集 {right_dataset_id,left_keys,right_keys}）。
// 版本语义如实：编辑既有管线时改步骤或基础集 → 保存按钮旁明示
// 「步骤变更将创建新版本 vN+1」（保存后按 version_created 回号刷新徽标）；
// 仅名称/描述就地更新不动版本。viewer 403 → 只读态（资产元数据编辑器
// 同款：以服务端 403 为准如实降级，不预判角色）。
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  ConsoleApiError,
  createPipeline,
  PIPELINE_STEP_OPS,
  updatePipeline,
  type PipelineDetail,
  type PipelineStepDef,
  type PipelineStepOp,
  type PipelineUpdateResult,
} from "../api";

/** 编辑器的数据集候选（基础/右表共用一个 picker 源）。 */
export type DatasetOption = { id: string; name: string };

/** 步骤草稿：结构化小表单（derive/link）+ JSON 文本（derive filters / join
 *  整体）各占草稿字段；params 为原始底座（保存时保留未编辑键如 sort）。 */
interface StepDraft {
  name: string;
  op: PipelineStepOp;
  params: Record<string, unknown>;
  filtersText: string;
  limitText: string;
  joinText: string;
  rightDatasetId: string;
  leftKeysText: string;
  rightKeysText: string;
}

/** 逗号/顿号分隔的键列文本 → 数组（trim + 去空）。 */
function parseKeys(text: string): string[] {
  return text
    .split(/[,，、]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

function draftFromStep(step: PipelineStepDef): StepDraft {
  const params = step.params ?? {};
  return {
    name: step.name,
    op: step.op,
    params,
    filtersText:
      params.filters != null ? JSON.stringify(params.filters, null, 2) : "",
    limitText: params.limit != null ? String(params.limit) : "",
    joinText: JSON.stringify(params, null, 2),
    rightDatasetId:
      typeof params.right_dataset_id === "string"
        ? params.right_dataset_id
        : "",
    leftKeysText: Array.isArray(params.left_keys)
      ? (params.left_keys as unknown[]).filter((v) => typeof v === "string").join(", ")
      : "",
    rightKeysText: Array.isArray(params.right_keys)
      ? (params.right_keys as unknown[]).filter((v) => typeof v === "string").join(", ")
      : "",
  };
}

function blankDraft(): StepDraft {
  return {
    name: "",
    op: "derive",
    params: {},
    filtersText: "",
    limitText: "",
    joinText: "{}",
    rightDatasetId: "",
    leftKeysText: "",
    rightKeysText: "",
  };
}

/** 草稿 → 保存形状（JSON 解析失败抛错并带步骤序号，供行内回显）。 */
function buildStep(draft: StepDraft): PipelineStepDef {
  const params: Record<string, unknown> = { ...draft.params };
  if (draft.op === "derive") {
    delete params.filters;
    delete params.limit;
    const filtersText = draft.filtersText.trim();
    if (filtersText) {
      const parsed: unknown = JSON.parse(filtersText);
      if (!Array.isArray(parsed)) {
        throw new Error("filters");
      }
      params.filters = parsed;
    }
    const limitText = draft.limitText.trim();
    if (limitText) {
      const limit = Number(limitText);
      if (!Number.isFinite(limit) || limit <= 0) {
        throw new Error("limit");
      }
      params.limit = limit;
    }
  } else if (draft.op === "join") {
    const parsed: unknown = JSON.parse(draft.joinText.trim() || "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new Error("join");
    }
    return { name: draft.name, op: draft.op, params: parsed as Record<string, unknown> };
  } else {
    delete params.right_dataset_id;
    delete params.left_keys;
    delete params.right_keys;
    params.right_dataset_id = draft.rightDatasetId;
    params.left_keys = parseKeys(draft.leftKeysText);
    params.right_keys = parseKeys(draft.rightKeysText);
  }
  return { name: draft.name, op: draft.op, params };
}

/** 步骤定义规范化比较（与假后端 stepsEqual 同口径：顺序/类型/参数）。 */
function sameSteps(a: PipelineStepDef[], b: PipelineStepDef[]): boolean {
  const norm = (steps: PipelineStepDef[]) =>
    JSON.stringify(
      steps.map((s) => ({ name: s.name, op: s.op, params: s.params ?? {} })),
    );
  return norm(a) === norm(b);
}

export function PipelineEditor({
  pipeline,
  datasets,
  onSaved,
  onCancel,
}: {
  /** null = 新建；对象 = 编辑既有（步骤草稿取当前版本）。 */
  pipeline: PipelineDetail | null;
  /** 基础数据集 / link 右表候选。 */
  datasets: DatasetOption[];
  onSaved: (result: PipelineUpdateResult) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(pipeline?.name ?? "");
  const [description, setDescription] = useState(pipeline?.description ?? "");
  const [baseId, setBaseId] = useState(pipeline?.base_dataset_id ?? "");
  const [steps, setSteps] = useState<StepDraft[]>(() =>
    pipeline
      ? (pipeline.versions.find(
            (v) => v.version === pipeline.current_version,
          )?.steps ?? []
        ).map(draftFromStep)
      : [],
  );
  const [saving, setSaving] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);
  /** 行内 JSON/参数错误：步骤序号（1 起）→ 文案。 */
  const [stepErrors, setStepErrors] = useState<Record<number, string>>({});
  // viewer 只读：以服务端 403 为准如实降级（资产元数据编辑器同款）。
  const [readOnly, setReadOnly] = useState(false);

  // 切换编辑对象时重置草稿（新建 ↔ 编辑互切）。
  const key = pipeline?.id ?? "new";
  useEffect(() => {
    setName(pipeline?.name ?? "");
    setDescription(pipeline?.description ?? "");
    setBaseId(pipeline?.base_dataset_id ?? "");
    setSteps(
      pipeline
        ? (pipeline.versions.find(
              (v) => v.version === pipeline.current_version,
            )?.steps ?? []
          ).map(draftFromStep)
        : [],
    );
    setOpError(null);
    setStepErrors({});
    setReadOnly(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const updateStep = (index: number, patch: Partial<StepDraft>) => {
    setSteps((prev) =>
      prev.map((step, i) => (i === index ? { ...step, ...patch } : step)),
    );
  };

  const move = (index: number, delta: -1 | 1) => {
    setSteps((prev) => {
      const target = index + delta;
      if (target < 0 || target >= prev.length) return prev;
      const next = [...prev];
      [next[index], next[target]] = [next[target]!, next[index]!];
      return next;
    });
  };

  // ── 版本语义预判：步骤或基础集变更 → 明示「步骤变更将创建新版本 vN+1」 ──
  const currentSteps = useMemo(
    () =>
      pipeline
        ? (pipeline.versions.find((v) => v.version === pipeline.current_version)
            ?.steps ?? [])
        : [],
    [pipeline],
  );
  const baseChanged =
    pipeline != null && baseId !== pipeline.base_dataset_id;
  const stepsDirty =
    pipeline != null &&
    (steps.length !== currentSteps.length ||
      steps.some((draft, i) => {
        try {
          return !sameSteps([buildStep(draft)], [currentSteps[i]!]);
        } catch {
          return true;
        }
      }));
  const willCreateVersion = pipeline != null && (baseChanged || stepsDirty);

  const save = async () => {
    if (saving || readOnly) return;
    setOpError(null);
    // 本地校验：名称/基础集/步骤名 + JSON 文本可解析（不合法不发请求）。
    if (!name.trim() || !baseId || steps.length === 0) {
      setOpError(t("console.datasets.pipeline.workbench.editorError"));
      return;
    }
    const errors: Record<number, string> = {};
    const built: PipelineStepDef[] = [];
    for (let i = 0; i < steps.length; i += 1) {
      const draft = steps[i]!;
      if (!draft.name.trim()) {
        errors[i + 1] = t("console.datasets.pipeline.workbench.stepNameRequired");
        continue;
      }
      try {
        built.push(buildStep(draft));
      } catch (err) {
        errors[i + 1] = `${t("console.datasets.pipeline.workbench.jsonError")}${
          (err as Error).message ? `（${(err as Error).message}）` : ""
        }`;
      }
    }
    setStepErrors(errors);
    if (Object.keys(errors).length > 0 || built.length !== steps.length) {
      return;
    }
    setSaving(true);
    try {
      const result =
        pipeline == null
          ? await createPipeline({
              name: name.trim(),
              description: description.trim() || undefined,
              base_dataset_id: baseId,
              steps: built,
            })
          : await updatePipeline(pipeline.id, {
              name: name.trim(),
              description: description.trim() || null,
              base_dataset_id: baseId,
              steps: built,
            });
      onSaved(result);
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 403) {
        // viewer 只读：切只读态（字段禁用、保存收起），不报错条。
        setReadOnly(true);
        setOpError(null);
      } else {
        setOpError(
          apiErrorText(err, t("console.datasets.pipeline.workbench.editorError")),
        );
      }
    } finally {
      setSaving(false);
    }
  };

  const inputClass =
    "rounded border border-slate-300 px-2 py-1 text-xs disabled:cursor-not-allowed disabled:bg-slate-100";

  return (
    <section
      data-testid="pipeline-editor"
      data-readonly={readOnly ? "true" : "false"}
      className="mt-2 rounded-lg border border-slate-200 bg-white p-4"
    >
      <h3
        data-testid="pipeline-editor-title"
        className="text-sm font-semibold text-slate-800"
      >
        {pipeline == null
          ? t("console.datasets.pipeline.workbench.editorTitleNew")
          : t("console.datasets.pipeline.workbench.editorTitleEdit")}
      </h3>

      {opError && (
        <p
          data-testid="pipeline-editor-error"
          className="mt-2 text-xs text-red-600"
        >
          {opError}
        </p>
      )}
      {readOnly && (
        <p
          data-testid="pipeline-editor-readonly"
          className="mt-2 rounded border border-amber-200 bg-amber-50/60 px-2 py-1 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.workbench.readOnlyNote")}
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-start gap-3">
        <label className="flex w-56 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.nameLabel")}
          </span>
          <input
            data-testid="pipeline-editor-name"
            value={name}
            disabled={readOnly || saving}
            onChange={(e) => setName(e.target.value)}
            placeholder={t("console.datasets.pipeline.workbench.namePlaceholder")}
            className={inputClass}
          />
        </label>
        <label className="flex w-72 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.descLabel")}
          </span>
          <input
            data-testid="pipeline-editor-desc"
            value={description}
            disabled={readOnly || saving}
            onChange={(e) => setDescription(e.target.value)}
            placeholder={t("console.datasets.pipeline.workbench.descPlaceholder")}
            className={inputClass}
          />
        </label>
        <label className="flex w-60 flex-col gap-1">
          <span className="text-xs text-slate-500">
            {t("console.datasets.pipeline.workbench.baseLabel")}
          </span>
          <select
            data-testid="pipeline-editor-base"
            value={baseId}
            disabled={readOnly || saving}
            onChange={(e) => {
              setBaseId(e.target.value);
            }}
            className={inputClass}
          >
            <option value="">
              {t("console.datasets.pipeline.workbench.basePlaceholder")}
            </option>
            {datasets.map((dataset) => (
              <option key={dataset.id} value={dataset.id}>
                {dataset.name}
              </option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-4 flex items-center justify-between">
        <h4 className="text-xs font-medium text-slate-600">
          {t("console.datasets.pipeline.workbench.stepsTitle")}
        </h4>
        <button
          type="button"
          data-testid="pipeline-add-step"
          disabled={readOnly || saving}
          onClick={() => setSteps((prev) => [...prev, blankDraft()])}
          className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t("console.datasets.pipeline.workbench.addStep")}
        </button>
      </div>
      <p className="mt-1 text-[11px] text-slate-400">
        {t("console.datasets.pipeline.workbench.stepsHint")}
      </p>

      <ol data-testid="pipeline-steps" className="mt-2 space-y-3">
        {steps.map((step, index) => (
          <li
            key={index}
            data-testid="pipeline-step"
            data-index={index}
            data-op={step.op}
            className="rounded-lg border border-slate-200 bg-slate-50/60 p-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-900 text-[11px] font-medium text-white">
                {index + 1}
              </span>
              <input
                data-testid="pipeline-step-name"
                value={step.name}
                disabled={readOnly || saving}
                onChange={(e) => updateStep(index, { name: e.target.value })}
                placeholder={t(
                  "console.datasets.pipeline.workbench.stepNamePlaceholder",
                )}
                className={`w-44 ${inputClass}`}
              />
              <select
                data-testid="pipeline-step-op"
                value={step.op}
                disabled={readOnly || saving}
                onChange={(e) =>
                  updateStep(index, {
                    op: e.target.value as PipelineStepOp,
                  })
                }
                className={inputClass}
              >
                {PIPELINE_STEP_OPS.map((op) => (
                  <option key={op} value={op}>
                    {t(`console.datasets.pipeline.workbench.opLabels.${op}`)}
                  </option>
                ))}
              </select>
              <span className="ml-auto flex items-center gap-1">
                <button
                  type="button"
                  data-testid="pipeline-step-up"
                  disabled={readOnly || saving || index === 0}
                  onClick={() => move(index, -1)}
                  title={t("console.datasets.pipeline.workbench.moveUp")}
                  className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ↑
                </button>
                <button
                  type="button"
                  data-testid="pipeline-step-down"
                  disabled={readOnly || saving || index === steps.length - 1}
                  onClick={() => move(index, 1)}
                  title={t("console.datasets.pipeline.workbench.moveDown")}
                  className="rounded border border-slate-300 px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ↓
                </button>
                <button
                  type="button"
                  data-testid="pipeline-step-remove"
                  disabled={readOnly || saving}
                  onClick={() =>
                    setSteps((prev) => prev.filter((_, i) => i !== index))
                  }
                  title={t("console.datasets.pipeline.workbench.removeStep")}
                  className="rounded border border-rose-200 px-1.5 py-0.5 text-[11px] text-rose-600 hover:bg-rose-50 disabled:cursor-not-allowed disabled:opacity-30"
                >
                  ✕
                </button>
              </span>
            </div>

            {/* 参数表单：三类型各按既有原子操作参数形状。 */}
            <div className="mt-2 flex flex-wrap items-start gap-3">
              {step.op === "derive" && (
                <>
                  <label className="flex w-28 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.limitLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-limit"
                      value={step.limitText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { limitText: e.target.value })
                      }
                      placeholder="1000"
                      inputMode="numeric"
                      className={inputClass}
                    />
                  </label>
                  <label className="flex w-80 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.filtersLabel")}
                    </span>
                    <textarea
                      data-testid="pipeline-step-filters"
                      value={step.filtersText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { filtersText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.filtersPlaceholder",
                      )}
                      rows={3}
                      className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px] disabled:cursor-not-allowed disabled:bg-slate-100"
                    />
                    <span className="text-[11px] text-slate-400">
                      {t("console.datasets.pipeline.workbench.filtersHint")}
                    </span>
                  </label>
                </>
              )}
              {step.op === "join" && (
                <label className="flex w-96 flex-col gap-1">
                  <span className="text-[11px] text-slate-500">
                    {t("console.datasets.pipeline.workbench.paramsLabel")}
                  </span>
                  <textarea
                    data-testid="pipeline-step-json"
                    value={step.joinText}
                    disabled={readOnly || saving}
                    onChange={(e) =>
                      updateStep(index, { joinText: e.target.value })
                    }
                    rows={6}
                    className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px] disabled:cursor-not-allowed disabled:bg-slate-100"
                  />
                  <span className="text-[11px] text-slate-400">
                    {t("console.datasets.pipeline.workbench.joinHint")}
                  </span>
                </label>
              )}
              {step.op === "link" && (
                <>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkRightLabel")}
                    </span>
                    <select
                      data-testid="pipeline-step-right"
                      value={step.rightDatasetId}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { rightDatasetId: e.target.value })
                      }
                      className={inputClass}
                    >
                      <option value="">
                        {t("console.datasets.pipeline.workbench.basePlaceholder")}
                      </option>
                      {datasets.map((dataset) => (
                        <option key={dataset.id} value={dataset.id}>
                          {dataset.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkLeftKeysLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-left-keys"
                      value={step.leftKeysText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { leftKeysText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.keysPlaceholder",
                      )}
                      className={inputClass}
                    />
                  </label>
                  <label className="flex w-56 flex-col gap-1">
                    <span className="text-[11px] text-slate-500">
                      {t("console.datasets.pipeline.workbench.linkRightKeysLabel")}
                    </span>
                    <input
                      data-testid="pipeline-step-right-keys"
                      value={step.rightKeysText}
                      disabled={readOnly || saving}
                      onChange={(e) =>
                        updateStep(index, { rightKeysText: e.target.value })
                      }
                      placeholder={t(
                        "console.datasets.pipeline.workbench.keysPlaceholder",
                      )}
                      className={inputClass}
                    />
                  </label>
                  <span className="mt-4 text-[11px] text-slate-400">
                    {t("console.datasets.pipeline.workbench.linkHint")}
                  </span>
                </>
              )}
            </div>
            {stepErrors[index + 1] && (
              <p
                data-testid="pipeline-step-json-error"
                data-seq={index + 1}
                className="mt-1 text-[11px] text-red-600"
              >
                {stepErrors[index + 1]}
              </p>
            )}
          </li>
        ))}
      </ol>
      {steps.length === 0 && (
        <p data-testid="pipeline-steps-empty" className="mt-2 text-xs text-slate-400">
          {t("console.datasets.pipeline.workbench.stepsEmpty")}
        </p>
      )}

      {/* 版本语义明示：改步骤/基础集 → 保存即追加新版本（append-only）。 */}
      {willCreateVersion && !readOnly && (
        <p
          data-testid="pipeline-version-confirm"
          className="mt-3 rounded border border-amber-200 bg-amber-50/60 px-2.5 py-1.5 text-xs text-amber-700"
        >
          {t("console.datasets.pipeline.workbench.versionConfirm", {
            next: (pipeline?.current_version ?? 0) + 1,
          })}
        </p>
      )}

      <div className="mt-3 flex items-center gap-2">
        {!readOnly && (
          <button
            type="button"
            data-testid="pipeline-editor-save"
            disabled={saving}
            onClick={() => void save()}
            className="rounded bg-slate-900 px-3 py-1.5 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            {saving
              ? t("console.datasets.pipeline.workbench.saving")
              : t("console.datasets.pipeline.workbench.save")}
          </button>
        )}
        <button
          type="button"
          data-testid="pipeline-editor-cancel"
          disabled={saving}
          onClick={onCancel}
          className="rounded border border-slate-300 px-3 py-1.5 text-xs text-slate-600 hover:bg-slate-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {t("console.datasets.pipeline.workbench.cancel")}
        </button>
      </div>
    </section>
  );
}
