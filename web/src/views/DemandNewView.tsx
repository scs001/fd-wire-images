// 需求单提交表单（#/console/demands/new?indicator=<code>，wire-ux-consolidation
// 任务 4.1 自占位转正）：registered 指标「按需开通」与定价页自定义卡的导流落点。
// - 表单：需求描述（多行必填）/ 联系邮箱（必填最小形态校验）/ 公司名（选填）/
//   源地址（多行、每行一条、≥1 必填——wire-demand-generation-orchestration
//   任务 4.1）/ 公开数据声明勾选（必须勾选才能提交）；目录锚点经路由 query
//   传入：隐藏入参 indicator_code + 描述区预填「指标 {code}：」开头。
// - 提交走真实端点 POST /api/v1/demands（Bearer 登录会话；未登录提交以 401
//   如实回显并引导登录）；成功确认态承诺 1 个工作日内响应。
// - 空必填逐字段提示，不产生请求；源地址 ≥1 与声明勾选做本地预校验，服务端
//   422（FastAPI 标准错误数组）按条目映射中文文案逐条呈现（不透传英文原文）。
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  apiErrorText,
  submitDemand,
  ConsoleApiError,
  type ApiDetailItem,
  type CustomerDemand,
} from "../console/api";

/** 邮箱最小形态校验（与后端 schemas.py 的 _EMAIL_RE 同口径）。 */
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;

interface FieldErrors {
  description?: string;
  email?: string;
  sourceUrls?: string;
  asserted?: string;
}

/** 源地址多行输入 → URL 数组：逐行 trim、空行忽略（本地预校验仅查 ≥1，
 *  形态合法性由服务端逐条硬校验兜底）。 */
function parseSourceUrls(text: string): string[] {
  return text
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** 服务端 422 校验错误（冻结契约 loc 形状）→ 中文逐条文案。
 *  - loc=("body","source_urls",<索引>) → 第 N 条源地址不合法；
 *  - loc=("body","source_urls") → 源地址整体不合法；
 *  - loc=("body","public_data_asserted") → 声明缺失；
 *  - 其他条目收敛为一条通用文案；绝不透传英文 msg 原文照抄。 */
function validationErrorLines(err: unknown, t: Translate): string[] | null {
  if (
    !(err instanceof ConsoleApiError) ||
    err.status !== 422 ||
    !err.detailItems ||
    err.detailItems.length === 0
  ) {
    return null;
  }
  const lines: string[] = [];
  let genericSeen = false;
  for (const item of err.detailItems) {
    const line = detailItemLine(item, t);
    if (line === null) {
      if (!genericSeen) {
        genericSeen = true;
        lines.push(t("demandNew.errorValidationGeneric"));
      }
      continue;
    }
    if (!lines.includes(line)) lines.push(line);
  }
  if (lines.length === 0) lines.push(t("demandNew.errorValidationGeneric"));
  return lines;
}

/** 单条 422 条目文案（未知条目返回 null → 由调用方收敛为通用文案）。 */
function detailItemLine(item: ApiDetailItem, t: Translate): string | null {
  const loc = item.loc ?? [];
  if (loc[0] !== "body") return null;
  const field = loc[1];
  if (field === "source_urls") {
    const index = loc.slice(2).find((v) => typeof v === "number");
    return typeof index === "number"
      ? t("demandNew.errorSourceUrlEntry", { index: index + 1 })
      : t("demandNew.errorSourceUrls");
  }
  if (field === "public_data_asserted") {
    return t("demandNew.assertRequired");
  }
  return null;
}

export function DemandNewView({ indicator }: { indicator: string | null }) {
  const { t } = useTranslation();
  const anchor = indicator?.trim() ? indicator.trim() : null;
  const [description, setDescription] = useState(
    anchor ? t("demandNew.descriptionPrefill", { code: anchor }) : "",
  );
  const [contactEmail, setContactEmail] = useState("");
  const [company, setCompany] = useState("");
  const [sourceUrlsText, setSourceUrlsText] = useState("");
  const [asserted, setAsserted] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [submitting, setSubmitting] = useState(false);
  const [needSignIn, setNeedSignIn] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitErrorLines, setSubmitErrorLines] = useState<string[] | null>(null);
  const [created, setCreated] = useState<CustomerDemand | null>(null);

  const submit = async () => {
    // 逐字段校验：任一必填为空/未勾选即拒绝，不发请求（源地址 ≥1 为本地
    // 预校验；逐条 URL 形态合法性由服务端 422 兜底）。
    const sourceUrls = parseSourceUrls(sourceUrlsText);
    const nextErrors: FieldErrors = {};
    if (!description.trim()) nextErrors.description = t("demandNew.descriptionRequired");
    if (!contactEmail.trim()) nextErrors.email = t("demandNew.emailRequired");
    else if (!EMAIL_RE.test(contactEmail.trim()))
      nextErrors.email = t("demandNew.emailInvalid");
    if (sourceUrls.length === 0)
      nextErrors.sourceUrls = t("demandNew.sourceUrlsRequired");
    if (!asserted) nextErrors.asserted = t("demandNew.assertRequired");
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;

    setSubmitting(true);
    setSubmitError(null);
    setSubmitErrorLines(null);
    setNeedSignIn(false);
    try {
      const demand = await submitDemand({
        description: description.trim(),
        contact_email: contactEmail.trim(),
        source_urls: sourceUrls,
        public_data_asserted: true,
        ...(company.trim() ? { company: company.trim() } : {}),
        ...(anchor ? { indicator_code: anchor } : {}),
      });
      setCreated(demand);
    } catch (err) {
      if (err instanceof ConsoleApiError && err.status === 401) {
        setNeedSignIn(true);
      } else {
        // 422 校验错误数组 → 逐条中文文案；其余错误走 detail/兜底文案。
        const lines = validationErrorLines(err, t);
        setSubmitErrorLines(lines);
        setSubmitError(apiErrorText(err, t("demandNew.errorTitle")));
      }
    } finally {
      setSubmitting(false);
    }
  };

  if (created) {
    return (
      <div className="max-w-xl mx-auto py-12">
        <div
          data-testid="demand-success"
          className="rounded-lg border border-emerald-200 bg-emerald-50/60 p-8 text-center"
        >
          <h1 className="text-xl font-semibold text-emerald-900">
            {t("demandNew.successTitle")}
          </h1>
          <p className="mt-3 text-sm text-emerald-800">
            {t("demandNew.successBody")}
          </p>
          <p className="mt-2 font-mono text-xs text-emerald-700" data-testid="demand-success-code">
            {t("demandNew.successCode", { code: created.code })}
          </p>
          <div className="mt-6 flex flex-wrap items-center justify-center gap-3">
            <a
              href="#/console/demands"
              data-testid="demand-success-view-mine"
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 transition-colors"
            >
              {t("demandNew.viewMine")}
            </a>
            <a
              href="#/catalog"
              className="rounded-md border border-slate-300 bg-white px-4 py-2 text-sm hover:bg-slate-100 transition-colors"
            >
              {t("demandNew.back")}
            </a>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-xl mx-auto py-12">
      <div className="rounded-lg border border-slate-200 bg-white p-8">
        <h1 className="text-xl font-semibold">{t("demandNew.title")}</h1>
        <p className="mt-2 text-sm text-slate-600">{t("demandNew.intro")}</p>
        {anchor && (
          <div className="mt-4 flex items-center gap-2 text-sm">
            <span className="text-slate-500">{t("demandNew.prefilled")}</span>
            <code
              data-testid="demand-prefilled-code"
              className="rounded bg-amber-50 px-2 py-0.5 font-mono text-xs ring-1 ring-amber-200"
            >
              {anchor}
            </code>
          </div>
        )}

        <form
          className="mt-6 space-y-5"
          onSubmit={(e) => {
            e.preventDefault();
            if (!submitting) void submit();
          }}
          noValidate
        >
          <div>
            <label htmlFor="demand-description" className="block text-sm font-medium text-slate-700">
              {t("demandNew.descriptionLabel")}
            </label>
            <textarea
              id="demand-description"
              data-testid="demand-description"
              rows={4}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("demandNew.descriptionPlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
            {errors.description && (
              <p data-testid="demand-description-error" className="mt-1 text-xs text-red-600">
                {errors.description}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="demand-email" className="block text-sm font-medium text-slate-700">
              {t("demandNew.emailLabel")}
            </label>
            <input
              id="demand-email"
              data-testid="demand-email"
              type="text"
              value={contactEmail}
              onChange={(e) => setContactEmail(e.target.value)}
              placeholder={t("demandNew.emailPlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
            {errors.email && (
              <p data-testid="demand-email-error" className="mt-1 text-xs text-red-600">
                {errors.email}
              </p>
            )}
          </div>

          <div>
            <label htmlFor="demand-company" className="block text-sm font-medium text-slate-700">
              {t("demandNew.companyLabel")}
            </label>
            <input
              id="demand-company"
              data-testid="demand-company"
              type="text"
              value={company}
              onChange={(e) => setCompany(e.target.value)}
              placeholder={t("demandNew.companyPlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
          </div>

          <div>
            <label htmlFor="demand-source-urls" className="block text-sm font-medium text-slate-700">
              {t("demandNew.sourceUrlsLabel")}
            </label>
            <textarea
              id="demand-source-urls"
              data-testid="demand-source-urls"
              rows={3}
              value={sourceUrlsText}
              onChange={(e) => setSourceUrlsText(e.target.value)}
              placeholder={t("demandNew.sourceUrlsPlaceholder")}
              className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 font-mono text-xs focus:border-slate-500 focus:outline-none"
            />
            <p className="mt-1 text-xs text-slate-500">
              {t("demandNew.sourceUrlsHint")}
            </p>
            {errors.sourceUrls && (
              <p data-testid="demand-source-urls-error" className="mt-1 text-xs text-red-600">
                {errors.sourceUrls}
              </p>
            )}
          </div>

          <div className="rounded-md border border-slate-200 bg-slate-50 px-4 py-3">
            <p className="text-sm font-medium text-slate-700">
              {t("demandNew.assertTitle")}
            </p>
            <label className="mt-1.5 flex items-start gap-2 text-sm text-slate-700">
              <input
                type="checkbox"
                data-testid="demand-public-assert"
                checked={asserted}
                onChange={(e) => setAsserted(e.target.checked)}
                className="mt-0.5 h-4 w-4"
              />
              <span>{t("demandNew.assertText")}</span>
            </label>
            {errors.asserted && (
              <p data-testid="demand-public-assert-error" className="mt-1 text-xs text-red-600">
                {errors.asserted}
              </p>
            )}
          </div>

          {needSignIn && (
            <div
              data-testid="demand-signin-required"
              className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800"
            >
              <p>{t("demandNew.signInRequired")}</p>
              <a
                href="#/login"
                className="mt-2 inline-block rounded-md border border-amber-300 bg-white px-3 py-1 text-xs text-amber-800 hover:bg-amber-100"
              >
                {t("demandNew.signInCta")}
              </a>
            </div>
          )}
          {!needSignIn && submitError && (
            <div
              data-testid="demand-submit-error"
              className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
            >
              {submitErrorLines ? (
                <ul className="list-disc space-y-1 pl-4">
                  {submitErrorLines.map((line, index) => (
                    <li key={index} data-testid="demand-submit-error-item">
                      {line}
                    </li>
                  ))}
                </ul>
              ) : (
                submitError
              )}
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              type="submit"
              data-testid="demand-submit"
              disabled={submitting}
              className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 transition-colors"
            >
              {submitting ? t("demandNew.submitting") : t("demandNew.submit")}
            </button>
            <a
              href="#/catalog"
              className="text-sm text-slate-500 underline hover:text-slate-900"
            >
              {t("demandNew.back")}
            </a>
          </div>
        </form>
      </div>
    </div>
  );
}
