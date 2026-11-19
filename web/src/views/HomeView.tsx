// 官网首页（#/）——柏讯产品叙事：hero + 三条价值主张 + 双 CTA + wire 线站位一句。
// 联系开通（add-contact-intake）：CTA 原位展开站内表单卡（非跳页、非 mailto
// 占位）——提交走公开端点 POST /api/v1/contact（同源、无认证）；成功态如实
// 反馈（已收到受理承诺），400 逐字段回显（loc+code 本地化）、429 如实呈现
// 限流口径；honeypot 字段 website 在 DOM 中隐藏（自动化提交天然带空值，
// 真人不可见不可达）。目录规模数字活取 /v1/catalog/stats（千位向下取整）。
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { API_BASE, formatCatalogTotal } from "../catalog/api";
import { useCatalogStats } from "../catalog/useCatalogStats";

type ContactField = "name" | "email" | "company" | "message";

interface FieldErrors {
  name?: string;
  email?: string;
  company?: string;
  message?: string;
  form?: string;
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** 服务端 400/429 detail → 本地化文案：
 *  - 400 数组（FastAPI 风格 loc/msg，msg 为语言中性代码）→ 按字段映射；
 *  - 未知字段/代码收敛为通用文案，绝不透传原始 code 照抄。 */
function fieldErrorsFromDetail(
  detail: unknown,
  status: number,
  t: Translate,
): FieldErrors {
  const errors: FieldErrors = {};
  if (status === 429) {
    errors.form = t("home.contact.errorRateLimited");
    return errors;
  }
  if (typeof detail === "string") {
    errors.form =
      detail === "rejected"
        ? t("home.contact.errorRejected")
        : t("home.contact.errorGeneric");
    return errors;
  }
  if (!Array.isArray(detail)) {
    errors.form = t("home.contact.errorGeneric");
    return errors;
  }
  const KEY_BY_FIELD: Record<ContactField, string> = {
    name: "name",
    email: "email",
    company: "company",
    message: "message",
  };
  const KEY_BY_CODE: Record<string, string> = {
    required: "Required",
    invalid: "Invalid",
    too_long: "TooLong",
  };
  let generic = false;
  for (const item of detail) {
    const field = (item as { loc?: unknown[] })?.loc?.[1];
    const code = (item as { msg?: unknown })?.msg;
    const suffix =
      typeof code === "string" ? KEY_BY_CODE[code] : undefined;
    if (
      typeof field === "string" &&
      field in KEY_BY_FIELD &&
      suffix !== undefined
    ) {
      errors[field as ContactField] = t(
        `home.contact.errors.${KEY_BY_FIELD[field as ContactField]}${suffix}`,
      );
    } else {
      generic = true;
    }
  }
  if (generic && !Object.keys(errors).length) {
    errors.form = t("home.contact.errorGeneric");
  }
  return errors;
}

export function HomeView() {
  const { t } = useTranslation();
  const stats = useCatalogStats();
  const catalogTotal = formatCatalogTotal(stats?.registered ?? 0);

  // 联系开通表单卡（add-contact-intake）：CTA 原位展开，不跳页。
  const [contactOpen, setContactOpen] = useState(false);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [company, setCompany] = useState("");
  const [message, setMessage] = useState("");
  // honeypot：机器人在 DOM 里能看到的诱饵字段；真人对它不可见（见渲染处
  // 的 hidden 包裹）。提交带空值——非空即机器人，服务端 400 不落库。
  const [website, setWebsite] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [received, setReceived] = useState(false);

  const resetForm = () => {
    setName("");
    setEmail("");
    setCompany("");
    setMessage("");
    setWebsite("");
  };

  const submit = async () => {
    setSubmitting(true);
    setErrors({});
    try {
      const response = await fetch(`${API_BASE}/v1/contact`, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, company, message, website }),
      });
      if (response.ok) {
        // 成功态如实反馈（受理承诺），表单清空——可继续提第二份。
        setReceived(true);
        resetForm();
        return;
      }
      let detail: unknown;
      try {
        const data: unknown = await response.json();
        if (data && typeof data === "object") {
          detail = (data as { detail?: unknown }).detail;
        }
      } catch {
        /* 无 JSON body：走通用文案 */
      }
      setErrors(fieldErrorsFromDetail(detail, response.status, t));
    } catch {
      setErrors({ form: t("home.contact.errorGeneric") });
    } finally {
      setSubmitting(false);
    }
  };

  const values = [
    {
      title: catalogTotal
        ? t("home.value1Title", { total: catalogTotal })
        : t("home.value1TitleNoCount"),
      desc: t("home.value1Desc"),
    },
    { title: t("home.value2Title"), desc: t("home.value2Desc") },
    { title: t("home.value3Title"), desc: t("home.value3Desc") },
  ];

  return (
    <div>
      <section className="py-14 text-center">
        <h1 className="text-4xl font-semibold tracking-tight">
          {t("home.heroTitle")}
        </h1>
        <p className="mt-4 text-slate-500 max-w-2xl mx-auto">
          {t("home.heroSubtitle")}
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
          <a
            href="#/catalog"
            data-testid="home-cta-catalog"
            className="rounded-md bg-slate-900 text-white px-5 py-2.5 text-sm hover:bg-slate-700 transition-colors"
          >
            {t("home.ctaCatalog")}
          </a>
          <button
            type="button"
            data-testid="home-cta-contact"
            onClick={() => setContactOpen(true)}
            className="rounded-md border border-slate-300 bg-white px-5 py-2.5 text-sm hover:bg-slate-100 transition-colors"
          >
            {t("home.ctaContact")}
          </button>
        </div>
      </section>

      {contactOpen && (
        <section className="mx-auto max-w-xl">
          <div
            data-testid="home-contact-card"
            className="rounded-lg border border-slate-200 bg-white p-8"
          >
            <h2 className="text-xl font-semibold">{t("home.contact.title")}</h2>
            <p className="mt-2 text-sm text-slate-600">
              {t("home.contact.intro")}
            </p>

            {received && (
              <div
                data-testid="contact-success"
                className="mt-4 rounded-md border border-emerald-200 bg-emerald-50/60 px-4 py-3 text-sm text-emerald-800"
              >
                <p className="font-medium text-emerald-900">
                  {t("home.contact.successTitle")}
                </p>
                <p className="mt-1">{t("home.contact.successBody")}</p>
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
              {/* honeypot（机器人闸）：DOM 存在但视觉隐藏 + 不可聚焦——
                  自动化提交天然带空值，命中非空即服务端 400。 */}
              <div aria-hidden="true" className="hidden">
                <label htmlFor="contact-website">
                  {t("home.contact.honeypotLabel")}
                </label>
                <input
                  id="contact-website"
                  data-testid="contact-website"
                  name="website"
                  type="text"
                  tabIndex={-1}
                  autoComplete="off"
                  value={website}
                  onChange={(e) => setWebsite(e.target.value)}
                />
              </div>

              <div>
                <label
                  htmlFor="contact-name"
                  className="block text-sm font-medium text-slate-700"
                >
                  {t("home.contact.nameLabel")}
                </label>
                <input
                  id="contact-name"
                  data-testid="contact-name"
                  type="text"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("home.contact.namePlaceholder")}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
                {errors.name && (
                  <p
                    data-testid="contact-name-error"
                    className="mt-1 text-xs text-red-600"
                  >
                    {errors.name}
                  </p>
                )}
              </div>

              <div>
                <label
                  htmlFor="contact-email"
                  className="block text-sm font-medium text-slate-700"
                >
                  {t("home.contact.emailLabel")}
                </label>
                <input
                  id="contact-email"
                  data-testid="contact-email"
                  type="text"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder={t("home.contact.emailPlaceholder")}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
                {errors.email && (
                  <p
                    data-testid="contact-email-error"
                    className="mt-1 text-xs text-red-600"
                  >
                    {errors.email}
                  </p>
                )}
              </div>

              <div>
                <label
                  htmlFor="contact-company"
                  className="block text-sm font-medium text-slate-700"
                >
                  {t("home.contact.companyLabel")}
                </label>
                <input
                  id="contact-company"
                  data-testid="contact-company"
                  type="text"
                  value={company}
                  onChange={(e) => setCompany(e.target.value)}
                  placeholder={t("home.contact.companyPlaceholder")}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
                {errors.company && (
                  <p
                    data-testid="contact-company-error"
                    className="mt-1 text-xs text-red-600"
                  >
                    {errors.company}
                  </p>
                )}
              </div>

              <div>
                <label
                  htmlFor="contact-message"
                  className="block text-sm font-medium text-slate-700"
                >
                  {t("home.contact.messageLabel")}
                </label>
                <textarea
                  id="contact-message"
                  data-testid="contact-message"
                  rows={4}
                  value={message}
                  onChange={(e) => setMessage(e.target.value)}
                  placeholder={t("home.contact.messagePlaceholder")}
                  className="mt-1 w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
                />
                {errors.message && (
                  <p
                    data-testid="contact-message-error"
                    className="mt-1 text-xs text-red-600"
                  >
                    {errors.message}
                  </p>
                )}
              </div>

              {errors.form && (
                <div
                  data-testid="contact-submit-error"
                  className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700"
                >
                  {errors.form}
                </div>
              )}

              <button
                type="submit"
                data-testid="contact-submit"
                disabled={submitting}
                className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50 transition-colors"
              >
                {submitting
                  ? t("home.contact.submitting")
                  : t("home.contact.submit")}
              </button>
            </form>
          </div>
        </section>
      )}

      <section>
        <h2 className="text-sm font-medium text-slate-400 mb-4">
          {t("home.valueTitle")}
        </h2>
        <div className="grid gap-4 sm:grid-cols-3">
          {values.map((v) => (
            <div
              key={v.title}
              className="rounded-lg border border-slate-200 p-5"
            >
              <h3 className="font-medium" data-testid="home-value-title">
                {v.title}
              </h3>
              <p className="mt-2 text-sm text-slate-500">{v.desc}</p>
            </div>
          ))}
        </div>
      </section>
      <section className="mt-12 border-t border-slate-200 pt-6 text-center">
        <p className="text-sm text-slate-400">{t("home.lineNote")}</p>
      </section>
    </div>
  );
}
