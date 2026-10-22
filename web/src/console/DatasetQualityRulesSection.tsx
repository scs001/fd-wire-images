// 质量规则区块（wire-quality-rules / v2）：数据集详情页内的「质量规则」区块——
// 客户对单个数据集定义质量检查规则并查看求值结果（pass/fail/error 三态）。
// 检查目录两代，下拉 optgroup 分组呈现（v2）：
// - 计数代（v1，6 项）：row_count/null_count/null_rate/filled_rate/
//   value_violations/key_uniqueness——metric × op(五符) × threshold
//   （值域类另需 allowed 白名单），走引擎 count_only/snapshot 通道；
// - 内容代（v2，3 项）：regex(column+pattern)、col_compare(col_a+op 六符含
//   !=+col_b)、range(column+min/max 闭区间)——无 op/threshold，引擎侧逐行
//   执行（pandera 通道，较慢；旧镜像诚实 error 降级）。
// 结果字段对内容代重映射（后端契约）：observed=未达标行数、
// threshold_json=检查定义原文（regex→{pattern}；col_compare→{col_a,op,col_b}；
// range→{min,max}）——故结果行按 check.metric 分派：计数代照旧
// 「observed / threshold」数字对比，内容代只给「摘要 + 未达标 N 行 + 达标率」，
// 绝不拼 operator+数字 的假阈值。
// - 规则 CRUD：GET/POST/PATCH/DELETE /api/v1/datasets/{id}/quality-rules；
// - 立即检查：POST .../run → 用返回结果就地刷新（结果列表重拉由 run 响应承担）；
// - 结果：GET .../results?limit=20（最近在前）；规则行内徽章取该规则最新一条
//   （按 result.id 最大者——append-only，id 即次序）；
// - canEdit=false 只读：隐藏全部写操作（添加/改名/启停/删除/立即检查）；
// - 三态齐备（loading / error+重试 / empty），照 DatasetVisibilitySection 结构；
// - 请求器为 api.ts requestJson 同语义的就地轻实现（Bearer 会话头、非 2xx 抛
//   ConsoleApiError 带后端 detail）——端点为本区块专属，不动 api.ts。
// i18n：console.dataset.qualityRules.*（zh/en 键位对仗，i18nKeys.test 把关）。
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { API_BASE } from "../catalog/api";
import { ConsoleApiError, apiErrorText } from "./api";
import { loadSession } from "./auth";
import { formatServerDate, truncate } from "./util";

/** 计数代六种检查指标（v1；前四种按列求值，row_count/key_uniqueness 是全表口径）。 */
export const QUALITY_RULE_METRICS = [
  "row_count",
  "null_count",
  "null_rate",
  "filled_rate",
  "value_violations",
  "key_uniqueness",
] as const;
export type QualityRuleCountMetric = (typeof QUALITY_RULE_METRICS)[number];

/** 内容代三种检查指标（v2；逐行执行、较慢——引擎侧 pandera 通道）。 */
export const QUALITY_RULE_CONTENT_METRICS = [
  "regex",
  "col_compare",
  "range",
] as const;
export type QualityRuleContentMetric =
  (typeof QUALITY_RULE_CONTENT_METRICS)[number];

export type QualityRuleMetric =
  | QualityRuleCountMetric
  | QualityRuleContentMetric;

/** 计数代五种比较符（阈值闭区间语义由服务端求值；前端只拼表达式）。 */
export const QUALITY_RULE_OPS = ["<", "<=", "=", ">=", ">"] as const;
export type QualityRuleOp = (typeof QUALITY_RULE_OPS)[number];

/** col_compare 专用六种比较符（v2：含不等于 `!=`；与后端契约同枚举）。
 *  旧 QUALITY_RULE_OPS 保留不动——计数代五符语义不扩。 */
export const QUALITY_RULE_OPS_WITH_NE = [
  "<",
  "<=",
  "=",
  "!=",
  ">=",
  ">",
] as const;
export type QualityRuleCompareOp = (typeof QUALITY_RULE_OPS_WITH_NE)[number];

/** 内容代（v2）判定：regex/col_compare/range——表单字段与结果渲染按此分派。 */
export function isContentMetric(
  metric: QualityRuleMetric,
): metric is QualityRuleContentMetric {
  return (
    metric === "regex" || metric === "col_compare" || metric === "range"
  );
}

/** 除 row_count / key_uniqueness 外的指标都必须指定列（col_compare 用
 *  col_a/col_b 两列，无单列 column 字段）。 */
export function metricNeedsColumn(metric: QualityRuleMetric): boolean {
  return (
    metric !== "row_count" &&
    metric !== "key_uniqueness" &&
    metric !== "col_compare"
  );
}

/** 计数代检查体（v1 契约，逐字段不变）。 */
export type QualityRuleCountCheck =
  | { metric: "row_count"; op: QualityRuleOp; threshold: number }
  | {
      metric: "null_count" | "null_rate" | "filled_rate";
      column: string;
      op: QualityRuleOp;
      threshold: number;
    }
  | {
      metric: "value_violations";
      column: string;
      op: QualityRuleOp;
      threshold: number;
      /** 2-32 个。 */
      allowed: string[];
    }
  | { metric: "key_uniqueness"; op: QualityRuleOp; threshold: number };

/** 内容代检查体（v2 契约）：regex/col_compare/range——无 op/threshold。 */
export type QualityRuleContentCheck =
  | { metric: "regex"; column: string; pattern: string }
  | {
      metric: "col_compare";
      col_a: string;
      /** 六比较符（含 !=）。 */
      op: QualityRuleCompareOp;
      col_b: string;
    }
  | { metric: "range"; column: string; min: number; max: number };

export type QualityRuleCheck = QualityRuleCountCheck | QualityRuleContentCheck;

/** 内容代检查判定（结果行渲染按此分派：内容代无假阈值对比）。 */
export function isContentCheck(
  check: QualityRuleCheck,
): check is QualityRuleContentCheck {
  return (
    check.metric === "regex" ||
    check.metric === "col_compare" ||
    check.metric === "range"
  );
}

/** 提交前轻量校验错误码（内容代三 kind 的成形性检查）。 */
export type QualityRuleFormError =
  | "sameColumn"
  | "rangeOrder"
  | "patternRequired";

/** 错误码 → i18n 键（zh/en 键位对仗由 i18nKeys.test 把关）。 */
const FORM_ERROR_KEYS: Record<QualityRuleFormError, string> = {
  sameColumn: "console.dataset.qualityRules.sameColumnError",
  rangeOrder: "console.dataset.qualityRules.rangeOrderError",
  patternRequired: "console.dataset.qualityRules.patternRequired",
};

/** 内容代提交前校验（轻量、不为难用户）：
 *  col_compare 两列同名、range min > max、regex pattern 空 → 阻止提交。
 *  返回 null 表示通过。 */
export function validateContentCheckForm(input: {
  metric: QualityRuleContentMetric;
  column: string;
  pattern: string;
  colA: string;
  colB: string;
  minText: string;
  maxText: string;
}): QualityRuleFormError | null {
  if (input.metric === "regex" && input.pattern.trim() === "") {
    return "patternRequired";
  }
  if (input.metric === "col_compare") {
    const a = input.colA.trim();
    const b = input.colB.trim();
    if (a !== "" && a === b) return "sameColumn";
  }
  if (input.metric === "range") {
    const min = Number(input.minText);
    const max = Number(input.maxText);
    if (
      input.minText.trim() !== "" &&
      input.maxText.trim() !== "" &&
      Number.isFinite(min) &&
      Number.isFinite(max) &&
      min > max
    ) {
      return "rangeOrder";
    }
  }
  return null;
}

export type QualityRule = {
  id: string;
  dataset_id: string;
  name: string | null;
  enabled: boolean;
  check: QualityRuleCheck;
  updated_by: string | null;
  updated_at: string | null;
};

export type QualityRuleResultStatus = "pass" | "fail" | "error";

export type QualityRuleResult = {
  id: number;
  rule_id: string;
  status: QualityRuleResultStatus;
  element_count: number | null;
  unexpected_count: number | null;
  pass_rate: number | null;
  /** 计数代=实测值；内容代（v2）=未达标行数（与 unexpected_count 同值）。 */
  observed: number | null;
  /** 检查定义原文（内容代 result 行的 threshold_json 映射；计数代同形回显）。 */
  threshold: Record<string, unknown> | null;
  channel: "query" | "snapshot" | "pandera";
  basis_at: string | null;
  error_text: string | null;
  created_at: string;
};

/** 人话化检查表达式（语言中立，原始口径拼出）：
 * - 计数代：`null_rate(age) <= 0.05`（value_violations 追加 ∈ {白名单}）；
 * - 内容代（v2）：regex → `regex(code): ^\d{3}$`；
 *   col_compare → `end >= start`；range → `20 ≤ age ≤ 40`
 *   ——均为检查定义原文摘要，不拼 operator + 数字的假阈值。 */
export function describeQualityCheck(check: QualityRuleCheck): string {
  if (check.metric === "regex") {
    return `${check.metric}(${check.column}): ${check.pattern}`;
  }
  if (check.metric === "col_compare") {
    return `${check.col_a} ${check.op} ${check.col_b}`;
  }
  if (check.metric === "range") {
    return `${check.min} ≤ ${check.column} ≤ ${check.max}`;
  }
  // 计数代（row_count/key_uniqueness 全表口径无列；其余按列）；
  // value_violations 追加白名单。
  const head =
    check.metric === "row_count" || check.metric === "key_uniqueness"
      ? check.metric
      : `${check.metric}(${check.column})`;
  const base = `${head} ${check.op} ${check.threshold}`;
  if (check.metric === "value_violations" && check.allowed?.length) {
    return `${base} ∈ {${check.allowed.join(", ")}}`;
  }
  return base;
}

/** 某规则最近一次结果（结果按 id append-only，取最大 id 即最新）。 */
export function latestResultFor(
  results: QualityRuleResult[],
  ruleId: string,
): QualityRuleResult | null {
  let best: QualityRuleResult | null = null;
  for (const result of results) {
    if (result.rule_id !== ruleId) continue;
    if (best == null || result.id > best.id) best = result;
  }
  return best;
}

/** 本区块专属端点的轻量请求器（api.ts requestJson 同语义：Bearer 会话头、
 *  非 2xx 抛 ConsoleApiError（带后端 detail 文案）、204 → null）。 */
async function qualityRequest(
  path: string,
  method: "GET" | "POST" | "PATCH" | "DELETE",
  body?: unknown,
): Promise<unknown> {
  const session = loadSession();
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers: {
        Accept: "application/json",
        ...(session?.idToken
          ? { Authorization: `Bearer ${session.idToken}` }
          : {}),
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    });
  } catch (err) {
    throw new ConsoleApiError(
      `network error fetching ${path}: ${(err as Error).message}`,
    );
  }
  if (!response.ok) {
    let detail: string | undefined;
    try {
      const data: unknown = await response.json();
      if (
        data &&
        typeof data === "object" &&
        typeof (data as { detail?: unknown }).detail === "string"
      ) {
        detail = (data as { detail: string }).detail;
      }
    } catch {
      /* 无 JSON body：保留 undefined（走兜底文案） */
    }
    throw new ConsoleApiError(
      `quality rules api ${response.status} for ${path}`,
      response.status,
      detail,
    );
  }
  if (response.status === 204) return null;
  return response.json();
}

/** 三态 → i18n 键（statusPass / statusFail / statusError）。 */
const STATUS_KEYS: Record<QualityRuleResultStatus, string> = {
  pass: "console.dataset.qualityRules.statusPass",
  fail: "console.dataset.qualityRules.statusFail",
  error: "console.dataset.qualityRules.statusError",
};

/** 三态徽章配色：pass 绿 / fail 红 / error 灰。 */
function statusBadgeClass(status: QualityRuleResultStatus): string {
  if (status === "pass") {
    return "rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-700 ring-1 ring-emerald-200";
  }
  if (status === "fail") {
    return "rounded-full bg-red-50 px-2 py-0.5 text-red-700 ring-1 ring-red-200";
  }
  return "rounded-full bg-slate-100 px-2 py-0.5 text-slate-600 ring-1 ring-slate-200";
}

export function DatasetQualityRulesSection({
  datasetId,
  canEdit,
}: {
  datasetId: string;
  /** false = 只读档（如 viewer）：隐藏全部写操作，仅展示规则与结果。 */
  canEdit: boolean;
}) {
  const { t } = useTranslation();
  const [rules, setRules] = useState<QualityRule[] | null>(null);
  const [note, setNote] = useState("");
  const [results, setResults] = useState<QualityRuleResult[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState(false); // 增/删/启停/改名互斥
  const [running, setRunning] = useState(false); // 立即检查进行中
  const [actionError, setActionError] = useState<string | null>(null);
  // 添加表单草稿（计数代字段 + 内容代 v2 字段并存；按 metric 条件渲染）
  const [metric, setMetric] = useState<QualityRuleMetric>("row_count");
  const [column, setColumn] = useState("");
  const [op, setOp] = useState<QualityRuleOp>(">=");
  const [thresholdText, setThresholdText] = useState("");
  const [allowedText, setAllowedText] = useState("");
  const [patternText, setPatternText] = useState("");
  const [colAText, setColAText] = useState("");
  const [colBText, setColBText] = useState("");
  const [compareOp, setCompareOp] = useState<QualityRuleCompareOp>(">=");
  const [minText, setMinText] = useState("");
  const [maxText, setMaxText] = useState("");
  /** 提交前轻量校验的错误码（i18n 键由 FORM_ERROR_KEYS 映射）。 */
  const [formError, setFormError] = useState<QualityRuleFormError | null>(null);
  // 行内改名
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [nameDraft, setNameDraft] = useState("");

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      // 结果拉取失败不拖垮区块（降级为空结果——规则面仍可用）。
      const [rulesResp, resultsResp] = await Promise.all([
        qualityRequest(
          `/v1/datasets/${datasetId}/quality-rules`,
          "GET",
        ) as Promise<{ items: QualityRule[]; note: string }>,
        qualityRequest(
          `/v1/datasets/${datasetId}/quality-rules/results?limit=20`,
          "GET",
        ).catch(() => null) as Promise<{ items: QualityRuleResult[] } | null>,
      ]);
      setRules(rulesResp.items);
      setNote(rulesResp.note ?? "");
      setResults(resultsResp ? resultsResp.items : []);
    } catch {
      setLoadError(true);
    }
  }, [datasetId]);

  useEffect(() => {
    setRules(null);
    setResults(null);
    void load();
  }, [load]);

  /** 立即检查：POST run → 用返回结果就地刷新（canEdit 才有入口）。 */
  const run = async () => {
    if (running || busy) return;
    setRunning(true);
    setActionError(null);
    try {
      const ran = (await qualityRequest(
        `/v1/datasets/${datasetId}/quality-rules/run`,
        "POST",
      )) as { results: QualityRuleResult[]; ranAt: string };
      setResults(ran.results);
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.dataset.qualityRules.actionError")),
      );
    } finally {
      setRunning(false);
    }
  };

  // ── 添加表单 ──（计数代 v1 字段 + 内容代 v2 字段；按 metric 条件渲染）
  const threshold = Number(thresholdText);
  const content = isContentMetric(metric);
  const needsColumn = metricNeedsColumn(metric);
  const needsAllowed = metric === "value_violations";
  const allowedValues = allowedText
    .split(/[,，]/)
    .map((value) => value.trim())
    .filter(Boolean);
  // 计数代成形性（内容代不走 op/threshold）
  const countValid =
    thresholdText.trim() !== "" &&
    Number.isFinite(threshold) &&
    (!needsColumn || column.trim() !== "") &&
    (!needsAllowed ||
      (allowedValues.length >= 2 && allowedValues.length <= 32));
  // 内容代成形性（三 kind 各自必填字段齐备）。注意 pattern 为空 / 两列同名 /
  // min>max 三类**留给提交时校验**（按钮不禁用），以便给出明确错误文案。
  const contentValid =
    (metric === "regex" && column.trim() !== "") ||
    (metric === "col_compare" &&
      colAText.trim() !== "" &&
      colBText.trim() !== "") ||
    (metric === "range" &&
      column.trim() !== "" &&
      minText.trim() !== "" &&
      Number.isFinite(Number(minText)) &&
      maxText.trim() !== "" &&
      Number.isFinite(Number(maxText)));
  const formValid = content ? contentValid : countValid;

  /** 组装 POST 载荷 check（两代各按契约字段集，绝不多发无关字段）。 */
  const buildCheck = (): QualityRuleCheck => {
    if (metric === "regex") {
      return { metric, column: column.trim(), pattern: patternText.trim() };
    }
    if (metric === "col_compare") {
      return {
        metric,
        col_a: colAText.trim(),
        op: compareOp,
        col_b: colBText.trim(),
      };
    }
    if (metric === "range") {
      return {
        metric,
        column: column.trim(),
        min: Number(minText),
        max: Number(maxText),
      };
    }
    // 计数代（v1 契约逐字段不变）
    if (metric === "row_count" || metric === "key_uniqueness") {
      return { metric, op, threshold };
    }
    if (metric === "value_violations") {
      return {
        metric,
        column: column.trim(),
        op,
        threshold,
        allowed: allowedValues,
      };
    }
    return { metric, column: column.trim(), op, threshold };
  };

  /** 表单复位（提交成功后回到初态；字段全清、metric 回 row_count）。 */
  const resetForm = () => {
    setMetric("row_count");
    setColumn("");
    setOp(">=");
    setThresholdText("");
    setAllowedText("");
    setPatternText("");
    setColAText("");
    setColBText("");
    setCompareOp(">=");
    setMinText("");
    setMaxText("");
    setFormError(null);
  };

  const addRule = async () => {
    if (busy) return;
    // 轻量校验先行（顺序性/必填语义类错误不进网络）：col_a===col_b、
    // min>max、pattern 为空 → 就地提示并阻止提交。
    const validationError = content
      ? validateContentCheckForm({
          metric,
          column,
          pattern: patternText,
          colA: colAText,
          colB: colBText,
          minText,
          maxText,
        })
      : null;
    setFormError(validationError);
    if (!formValid || validationError != null) return;
    setBusy(true);
    setActionError(null);
    try {
      await qualityRequest(`/v1/datasets/${datasetId}/quality-rules`, "POST", {
        check: buildCheck(),
      });
      // 复位 + 刷新列表（新规则来自服务端，含 id/时间戳）。
      resetForm();
      const next = (await qualityRequest(
        `/v1/datasets/${datasetId}/quality-rules`,
        "GET",
      )) as { items: QualityRule[] };
      setRules(next.items);
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.dataset.qualityRules.actionError")),
      );
    } finally {
      setBusy(false);
    }
  };

  /** 启停开关：PATCH {enabled}，响应就地替换该行。 */
  const toggleRule = async (rule: QualityRule) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const next = (await qualityRequest(
        `/v1/datasets/${datasetId}/quality-rules/${rule.id}`,
        "PATCH",
        { enabled: !rule.enabled },
      )) as QualityRule;
      setRules((current) =>
        (current ?? []).map((row) => (row.id === next.id ? next : row)),
      );
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.dataset.qualityRules.actionError")),
      );
    } finally {
      setBusy(false);
    }
  };

  /** 删除：先 window.confirm（removeConfirm 文案），确认后 DELETE。 */
  const removeRule = async (rule: QualityRule) => {
    if (busy) return;
    if (!window.confirm(t("console.dataset.qualityRules.removeConfirm"))) {
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      await qualityRequest(
        `/v1/datasets/${datasetId}/quality-rules/${rule.id}`,
        "DELETE",
      );
      setRules((current) => (current ?? []).filter((row) => row.id !== rule.id));
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.dataset.qualityRules.actionError")),
      );
    } finally {
      setBusy(false);
    }
  };

  /** 改名：PATCH {name}（空串归 null，回落到检查表达式呈现）。 */
  const saveName = async (rule: QualityRule) => {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    try {
      const next = (await qualityRequest(
        `/v1/datasets/${datasetId}/quality-rules/${rule.id}`,
        "PATCH",
        { name: nameDraft.trim() || null },
      )) as QualityRule;
      setRules((current) =>
        (current ?? []).map((row) => (row.id === next.id ? next : row)),
      );
      setRenamingId(null);
    } catch (err) {
      setActionError(
        apiErrorText(err, t("console.dataset.qualityRules.actionError")),
      );
    } finally {
      setBusy(false);
    }
  };

  if (loadError) {
    return (
      <section data-testid="quality-rules-section" className="mt-6">
        <h2 className="text-base font-semibold">
          {t("console.dataset.qualityRules.title")}
        </h2>
        <div
          data-testid="quality-rules-error"
          className="mt-2 flex items-start gap-2 rounded-md border border-red-200 bg-red-50 p-3"
        >
          <p className="text-xs text-red-700">
            {t("console.dataset.qualityRules.error")}
          </p>
          <button
            type="button"
            data-testid="quality-rules-retry"
            aria-label={t("console.dataset.qualityRules.error")}
            onClick={() => void load()}
            className="rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 hover:bg-red-100"
          >
            ↻
          </button>
        </div>
      </section>
    );
  }

  return (
    <section data-testid="quality-rules-section" className="mt-6">
      <h2 className="text-base font-semibold">
        {t("console.dataset.qualityRules.title")}
      </h2>
      <p className="mt-1 max-w-2xl text-xs text-slate-500">
        {t("console.dataset.qualityRules.subtitle")}
      </p>

      {rules == null && (
        <p
          data-testid="quality-rules-loading"
          className="mt-2 text-xs text-slate-400"
        >
          {t("console.dataset.qualityRules.loading")}
        </p>
      )}

      {rules != null && (
        <>
          {/* 操作条：立即检查（只读档隐藏）。 */}
          {canEdit && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                data-testid="quality-rules-run"
                disabled={running || busy}
                onClick={() => void run()}
                className="rounded bg-slate-900 px-3 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {running
                  ? t("console.dataset.qualityRules.running")
                  : t("console.dataset.qualityRules.runNow")}
              </button>
            </div>
          )}

          {/* 规则列表：人话化检查 + 最近结果徽章 + 启停/删除（只读档仅展示）。 */}
          {rules.length === 0 ? (
            <p
              data-testid="quality-rules-empty"
              className="mt-2 text-xs text-slate-400"
            >
              {t("console.dataset.qualityRules.empty")}
            </p>
          ) : (
            <ul className="mt-2 space-y-2">
              {rules.map((rule) => {
                const latest =
                  latestResultFor(results ?? [], rule.id) ?? null;
                const checkText = describeQualityCheck(rule.check);
                return (
                  <li
                    key={rule.id}
                    data-testid={`quality-rule-item-${rule.id}`}
                    data-enabled={rule.enabled ? "true" : "false"}
                    className="rounded-lg border border-slate-200 bg-white p-2.5 text-xs"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      {renamingId === rule.id ? (
                        <span className="flex items-center gap-1.5">
                          <input
                            data-testid="quality-rule-name-input"
                            value={nameDraft}
                            onChange={(e) => setNameDraft(e.target.value)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void saveName(rule);
                            }}
                            className="w-40 rounded border border-slate-300 px-2 py-0.5 text-[11px]"
                          />
                          <button
                            type="button"
                            data-testid="quality-rule-name-save"
                            disabled={busy}
                            onClick={() => void saveName(rule)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {t("console.dataset.qualityRules.save")}
                          </button>
                          <button
                            type="button"
                            data-testid="quality-rule-name-cancel"
                            onClick={() => setRenamingId(null)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-50"
                          >
                            {t("console.dataset.qualityRules.cancel")}
                          </button>
                        </span>
                      ) : (
                        <span className="font-mono text-[11px] text-slate-700">
                          {canEdit ? (
                            <button
                              type="button"
                              data-testid={`quality-rule-rename-${rule.id}`}
                              title={checkText}
                              onClick={() => {
                                setRenamingId(rule.id);
                                setNameDraft(rule.name ?? "");
                              }}
                              className="max-w-56 truncate text-left underline decoration-dotted underline-offset-2"
                            >
                              {rule.name ?? checkText}
                            </button>
                          ) : (
                            (rule.name ?? checkText)
                          )}
                        </span>
                      )}
                      <span className="font-mono text-[11px] text-slate-500">
                        {checkText}
                      </span>
                      {latest != null && (
                        <span
                          data-testid={`quality-rule-result-badge-${rule.id}`}
                          data-status={latest.status}
                          className={statusBadgeClass(latest.status)}
                        >
                          {t(STATUS_KEYS[latest.status])}
                          {latest.status === "error" && latest.error_text
                            ? `：${truncate(latest.error_text, 40)}`
                            : ""}
                        </span>
                      )}
                      {canEdit && (
                        <span className="ml-auto flex items-center gap-1.5">
                          <button
                            type="button"
                            data-testid={`quality-rule-toggle-${rule.id}`}
                            data-enabled={rule.enabled ? "true" : "false"}
                            disabled={busy}
                            onClick={() => void toggleRule(rule)}
                            className="rounded border border-slate-300 px-1.5 py-0.5 text-[10px] text-slate-600 hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {t(
                              rule.enabled
                                ? "console.dataset.qualityRules.enabled"
                                : "console.dataset.qualityRules.disabled",
                            )}
                          </button>
                          <button
                            type="button"
                            data-testid={`quality-rule-delete-${rule.id}`}
                            disabled={busy}
                            onClick={() => void removeRule(rule)}
                            className="rounded border border-red-200 px-1.5 py-0.5 text-[10px] text-red-600 hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-40"
                          >
                            {t("console.dataset.qualityRules.delete")}
                          </button>
                        </span>
                      )}
                    </div>
                  </li>
                );
              })}
            </ul>
          )}

          {note.trim() !== "" && (
            <p
              data-testid="quality-rules-server-note"
              className="mt-2 max-w-2xl text-[11px] text-slate-400"
            >
              {note}
            </p>
          )}

          {/* 添加表单（只读档隐藏）：metric/column/op/threshold/allowed。 */}
          {canEdit && (
            <div className="mt-3 rounded-lg border border-slate-200 bg-slate-50/60 p-3">
              <div className="flex flex-wrap items-center gap-2 text-xs">
                <label className="flex items-center gap-1.5">
                  <span className="text-slate-500">
                    {t("console.dataset.qualityRules.metricLabel")}
                  </span>
                  <select
                    data-testid="quality-rule-metric"
                    value={metric}
                    onChange={(e) => {
                      setMetric(e.target.value as QualityRuleMetric);
                      setFormError(null);
                    }}
                    className="rounded border border-slate-300 bg-white px-1.5 py-1"
                  >
                    {/* v2：两代分组（汇总检查 6 / 内容检查 3）；内容代选项
                        后缀「逐行执行，较慢」提示，选前即知代价。 */}
                    <optgroup
                      label={t(
                        "console.dataset.qualityRules.groupAggregate",
                      )}
                    >
                      {QUALITY_RULE_METRICS.map((value) => (
                        <option key={value} value={value}>
                          {t(`console.dataset.qualityRules.metric.${value}`)}
                        </option>
                      ))}
                    </optgroup>
                    <optgroup
                      label={t("console.dataset.qualityRules.groupContent")}
                    >
                      {QUALITY_RULE_CONTENT_METRICS.map((value) => (
                        <option key={value} value={value}>
                          {`${t(`console.dataset.qualityRules.metric.${value}`)} · ${t(
                            "console.dataset.qualityRules.contentSlowHint",
                          )}`}
                        </option>
                      ))}
                    </optgroup>
                  </select>
                </label>
                {needsColumn && (
                  <label className="flex items-center gap-1.5">
                    <span className="text-slate-500">
                      {t("console.dataset.qualityRules.column")}
                    </span>
                    <input
                      data-testid="quality-rule-column"
                      value={column}
                      onChange={(e) => setColumn(e.target.value)}
                      className="w-28 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                    />
                  </label>
                )}
                {metric === "regex" && (
                  <label className="flex items-center gap-1.5">
                    <span className="text-slate-500">
                      {t("console.dataset.qualityRules.pattern")}
                    </span>
                    <input
                      data-testid="quality-rule-pattern"
                      value={patternText}
                      onChange={(e) => {
                        setPatternText(e.target.value);
                        setFormError(null);
                      }}
                      placeholder={t(
                        "console.dataset.qualityRules.patternPlaceholder",
                      )}
                      className="w-56 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                    />
                  </label>
                )}
                {metric === "col_compare" && (
                  <>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.colA")}
                      </span>
                      <input
                        data-testid="quality-rule-col-a"
                        value={colAText}
                        onChange={(e) => {
                          setColAText(e.target.value);
                          setFormError(null);
                        }}
                        className="w-28 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                      />
                    </label>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.op")}
                      </span>
                      <select
                        data-testid="quality-rule-compare-op"
                        value={compareOp}
                        onChange={(e) =>
                          setCompareOp(e.target.value as QualityRuleCompareOp)
                        }
                        className="rounded border border-slate-300 bg-white px-1.5 py-1 font-mono"
                      >
                        {QUALITY_RULE_OPS_WITH_NE.map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.colB")}
                      </span>
                      <input
                        data-testid="quality-rule-col-b"
                        value={colBText}
                        onChange={(e) => {
                          setColBText(e.target.value);
                          setFormError(null);
                        }}
                        className="w-28 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                      />
                    </label>
                  </>
                )}
                {metric === "range" && (
                  <>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.min")}
                      </span>
                      <input
                        data-testid="quality-rule-min"
                        type="number"
                        step="any"
                        value={minText}
                        onChange={(e) => {
                          setMinText(e.target.value);
                          setFormError(null);
                        }}
                        className="w-24 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                      />
                    </label>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.max")}
                      </span>
                      <input
                        data-testid="quality-rule-max"
                        type="number"
                        step="any"
                        value={maxText}
                        onChange={(e) => {
                          setMaxText(e.target.value);
                          setFormError(null);
                        }}
                        className="w-24 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                      />
                    </label>
                  </>
                )}
                {!content && (
                  <>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.op")}
                      </span>
                      <select
                        data-testid="quality-rule-op"
                        value={op}
                        onChange={(e) => setOp(e.target.value as QualityRuleOp)}
                        className="rounded border border-slate-300 bg-white px-1.5 py-1 font-mono"
                      >
                        {QUALITY_RULE_OPS.map((value) => (
                          <option key={value} value={value}>
                            {value}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="flex items-center gap-1.5">
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.threshold")}
                      </span>
                      <input
                        data-testid="quality-rule-threshold"
                        type="number"
                        step="any"
                        value={thresholdText}
                        onChange={(e) => setThresholdText(e.target.value)}
                        className="w-24 rounded border border-slate-300 bg-white px-2 py-1 font-mono text-[11px]"
                      />
                    </label>
                    {needsAllowed && (
                      <label className="flex items-center gap-1.5">
                        <span className="text-slate-500">
                          {t("console.dataset.qualityRules.allowedValues")}
                        </span>
                        <input
                          data-testid="quality-rule-allowed"
                          value={allowedText}
                          onChange={(e) => setAllowedText(e.target.value)}
                          className="w-56 rounded border border-slate-300 bg-white px-2 py-1 text-[11px]"
                        />
                      </label>
                    )}
                  </>
                )}
                <button
                  type="button"
                  data-testid="quality-rule-add"
                  disabled={!formValid || busy}
                  onClick={() => void addRule()}
                  className="rounded bg-slate-900 px-2.5 py-1 text-xs text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {t("console.dataset.qualityRules.add")}
                </button>
              </div>
              {formError != null && (
                <p
                  data-testid="quality-rule-form-error"
                  className="mt-2 text-xs text-red-600"
                >
                  {t(FORM_ERROR_KEYS[formError])}
                </p>
              )}
            </div>
          )}

          {actionError && (
            <p
              data-testid="quality-rules-action-error"
              className="mt-2 text-xs text-red-600"
            >
              {actionError}
            </p>
          )}

          {/* 求值结果：状态徽章 + observed vs threshold + 计数/通过率 +
              snapshot 通道的基准时刻（basisAt）。 */}
          <h3 className="mt-4 text-sm font-semibold">
            {t("console.dataset.qualityRules.results")}
          </h3>
          {results == null ? (
            <p
              data-testid="quality-rules-results-loading"
              className="mt-1.5 text-xs text-slate-400"
            >
              {t("console.dataset.qualityRules.loading")}
            </p>
          ) : results.length === 0 ? (
            <p
              data-testid="quality-rules-results-empty"
              className="mt-1.5 text-xs text-slate-400"
            >
              {t("console.dataset.qualityRules.resultsEmpty")}
            </p>
          ) : (
            <ul
              data-testid="quality-rules-results"
              className="mt-1.5 space-y-1.5"
            >
              {results.map((result) => {
                const rule = rules.find((row) => row.id === result.rule_id);
                return (
                  <li
                    key={result.id}
                    data-testid="quality-rule-result-row"
                    data-rule-id={result.rule_id}
                    data-status={result.status}
                    data-channel={result.channel}
                    className="flex flex-wrap items-center gap-2 rounded border border-slate-100 bg-slate-50/40 px-2.5 py-1.5 text-[11px]"
                  >
                    <span
                      data-status={result.status}
                      className={statusBadgeClass(result.status)}
                    >
                      {t(STATUS_KEYS[result.status])}
                    </span>
                    <span className="font-mono text-slate-600">
                      {rule ? describeQualityCheck(rule.check) : result.rule_id}
                    </span>
                    {/* 内容代（v2）结果：observed/threshold 已在后端重映射
                        （observed=未达标行数、threshold=检查定义原文），
                        不再拼假阈值对比——摘要已在左侧，此处只补
                        「未达标 N 行 + 达标率」。计数代照旧 observed /
                        threshold 数字对比。 */}
                    {rule != null && isContentCheck(rule.check) ? (
                      result.observed != null && (
                        <span
                          data-testid="quality-rule-result-content-count"
                          className="text-slate-500"
                        >
                          {t("console.dataset.qualityRules.unexpectedCount")}：
                          {result.observed}
                        </span>
                      )
                    ) : (
                      <span
                        data-testid="quality-rule-result-observed"
                        className="text-slate-500"
                      >
                        {t("console.dataset.qualityRules.observed")}：
                        {result.observed ?? "—"}
                        {rule != null && !isContentCheck(rule.check)
                          ? ` / ${t("console.dataset.qualityRules.threshold")}：${rule.check.threshold}`
                          : ""}
                      </span>
                    )}
                    {result.element_count != null && (
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.elementCount")}：
                        {result.element_count.toLocaleString()}
                      </span>
                    )}
                    {result.unexpected_count != null &&
                      !(
                        rule != null && isContentCheck(rule.check)
                      ) && (
                        <span className="text-slate-500">
                          {t("console.dataset.qualityRules.unexpectedCount")}：
                          {result.unexpected_count}
                        </span>
                      )}
                    {result.pass_rate != null && (
                      <span className="text-slate-500">
                        {t("console.dataset.qualityRules.passRate")}：
                        {(result.pass_rate * 100).toFixed(1)}%
                      </span>
                    )}
                    {result.channel === "snapshot" &&
                      result.basis_at != null && (
                        <span
                          data-testid="quality-rule-result-basis"
                          className="text-slate-400"
                        >
                          {t("console.dataset.qualityRules.basisAt")}：
                          {formatServerDate(result.basis_at)}
                        </span>
                      )}
                    {result.status === "error" && result.error_text != null && (
                      <span className="text-slate-400">
                        {truncate(result.error_text, 60)}
                      </span>
                    )}
                    <span className="ml-auto text-slate-400">
                      {formatServerDate(result.created_at)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          <p className="mt-2 max-w-2xl text-[11px] text-slate-400">
            {t("console.dataset.qualityRules.rulesNote")}
          </p>
        </>
      )}
    </section>
  );
}
