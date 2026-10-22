// 全量页脚（门户视图用，wire-ux-consolidation 任务 1.1）：
// 组织署名（措辞对齐官网 fd-official-web footer.org/footer.line）+ GitHub +
// ICP/公安备（新窗口）+ 一行跨服务入口（只放有独立门户的活入口）。
import { useTranslation } from "react-i18next";
import {
  FOOTER_GITHUB_URL,
  FOOTER_ICP_NUMBER,
  FOOTER_ICP_URL,
  FOOTER_MPS_NUMBER,
  FOOTER_MPS_URL,
  FOOTER_SERVICE_LINKS,
} from "./footerConstants";

export function SiteFooter() {
  const { t } = useTranslation();

  return (
    <footer
      data-testid="site-footer"
      className="mx-auto max-w-5xl px-4 py-8 text-xs text-slate-400"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
        <span className="text-slate-500">{t("footer.servicesLabel")}</span>
        {FOOTER_SERVICE_LINKS.map((service) => (
          <a
            key={service.key}
            data-testid={`footer-service-${service.key}`}
            href={service.href}
            target="_blank"
            rel="noopener noreferrer"
            className="underline hover:text-slate-600"
          >
            {t(`footer.services.${service.key}`)}
          </a>
        ))}
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1">
        <span data-testid="site-footer-org">
          © {new Date().getFullYear()} {t("footer.org")} · {t("footer.tagline")}
        </span>
        <a
          data-testid="footer-github"
          href={FOOTER_GITHUB_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="underline hover:text-slate-600"
        >
          {t("footer.github")}
        </a>
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1">
        <a
          data-testid="footer-icp"
          href={FOOTER_ICP_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-slate-600"
        >
          {FOOTER_ICP_NUMBER}
        </a>
        <a
          data-testid="footer-mps"
          href={FOOTER_MPS_URL}
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-slate-600"
        >
          {FOOTER_MPS_NUMBER}
        </a>
      </div>
    </footer>
  );
}
