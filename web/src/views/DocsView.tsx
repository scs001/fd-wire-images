// 文档页（#/docs）——四节：快速开始 / 计量与计费 / 数据面 REST / 需求单流程，
// 另加「我的数据与连接器」客户教程（wire-connector-distribution 任务 4）。
// 如实呈现纪律：未上线能力（cell 开通）挂「上线中 · 即将开放」徽标，不以
// 已上线口吻陈述；需求单提交已上线（wire-ux-consolidation 任务 4.1）。
// 需求单流程为三态机（submitted → in_progress → delivered，另 rejected），
// 八态工作流旧态名不再出现在客户面文案（任务 3.5 同步）。
import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { markAdminSplitDone } from "../console/auth";

/** MCP 端点占位（wire-platform URL 契约：单域 /mcp 反代 registry wire 入口）。 */
export const MCP_ENDPOINT = "https://wire.finddatatech.cloud/mcp";

const DEMAND_STEPS = [
  "docs.demands.step1",
  "docs.demands.step2",
  "docs.demands.step3",
] as const;

const UPLOAD_STEPS = [
  "docs.localData.uploadStep1",
  "docs.localData.uploadStep2",
  "docs.localData.uploadStep3",
] as const;

const CONNECTOR_STEPS = [
  "docs.localData.connectorStep1",
  "docs.localData.connectorStep2",
  "docs.localData.connectorStep3",
] as const;

/** 表单全流程步骤（wire-form-builder 任务 5.1）：创建→绑定→配字段→试填→
 *  发布分享→数据落修正历史。 */
const FORM_STEPS = [
  "docs.forms.step1",
  "docs.forms.step2",
  "docs.forms.step3",
  "docs.forms.step4",
  "docs.forms.step5",
] as const;

/** 单记录公开页步骤（wire-record-view 任务 6.1）：创建→配模板→试预览→
 *  发布分享→二维码（含实时反映口径）。 */
const RECORD_VIEW_STEPS = [
  "docs.recordView.step1",
  "docs.recordView.step2",
  "docs.recordView.step3",
  "docs.recordView.step4",
  "docs.recordView.step5",
] as const;

/** 实体语义层步骤（wire-entity-layer 任务 3.3）：声明实体→建关系→AI 构建带
 *  图谱语义→v1 边界如实说明（物化 join 非目标）。 */
const ENTITY_STEPS = [
  "docs.entities.step1",
  "docs.entities.step2",
  "docs.entities.step3",
  "docs.entities.step4",
] as const;

/** 物化连接小节步骤（wire-entity-link 任务 3.3）：双 cell 前置→LEFT JOIN
 *  语义 / 撞名 right_ 前缀 / 免费档 403 / 旧 cell 滚动窗口如实。 */
const ENTITY_MATERIALIZE_STEPS = [
  "docs.entities.materializeStep1",
  "docs.entities.materializeStep2",
] as const;

/** 常驻配置样例（deploy/README.md「连接器」段的客户面提炼；字面配置不进 i18n）。 */
const SYSTEMD_SAMPLE = `[Unit]
Description=fd-wire connector (outbound only)
After=network-online.target

[Service]
ExecStart=/usr/local/bin/fd-wire-connector run --config /etc/fd-wire-connector/config.json
Restart=always
User=fdwire

[Install]
WantedBy=multi-user.target`;

const NSSM_SAMPLE = `nssm install fd-wire-connector "C:\\Tools\\fd-wire-connector.exe" run
nssm set fd-wire-connector AppDirectory "C:\\Tools"
nssm start fd-wire-connector`;

const LAUNCHD_SAMPLE = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.finddata.fd-wire-connector</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/fd-wire-connector</string>
    <string>run</string>
  </array>
  <key>KeepAlive</key><true/>
  <key>RunAtLoad</key><true/>
</dict></plist>`;

function ComingSoonBadge() {
  const { t } = useTranslation();
  return (
    <span
      data-testid="coming-soon-badge"
      className="inline-flex items-center rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-xs text-amber-700"
    >
      {t("badge.comingSoon")}
    </span>
  );
}

function StatusRow({ label }: { label: string }) {
  return (
    <li className="flex flex-wrap items-center gap-2">
      <span className="text-sm text-slate-600">{label}</span>
      <ComingSoonBadge />
    </li>
  );
}

const DATA_PLANE_ENDPOINTS: ReadonlyArray<{
  method: string;
  path: string;
  purposeKey: string;
}> = [
  { method: "POST", path: "/api/v1/datasets", purposeKey: "docs.dataPlane.purposeCreate" },
  { method: "POST", path: "/api/v1/datasets/{id}/query", purposeKey: "docs.dataPlane.purposeQuery" },
  { method: "POST", path: "/api/v1/datasets/{id}/aggregate", purposeKey: "docs.dataPlane.purposeAggregate" },
  { method: "POST", path: "/api/v1/datasets/{id}/rows:batchWrite", purposeKey: "docs.dataPlane.purposeWrite" },
  { method: "POST", path: "/api/v1/datasets/{id}/columns", purposeKey: "docs.dataPlane.purposeColumn" },
  { method: "POST", path: "/api/v1/datasets/{id}/derive", purposeKey: "docs.dataPlane.purposeDerive" },
  { method: "POST", path: "/api/v1/datasets/{id}/join", purposeKey: "docs.dataPlane.purposeJoin" },
  { method: "GET", path: "/api/v1/datasets/{id}/lineage", purposeKey: "docs.dataPlane.purposeLineage" },
];

export function DocsView() {
  const { t } = useTranslation();

  // 深链锚点（wire-connector-distribution 任务 4.2）：配对向导链到
  // #/docs?section=local-data——hash 路由不吃 #anchor，进页后手动定位。
  useEffect(() => {
    if (window.location.hash.includes("section=local-data")) {
      document
        .getElementById("local-data")
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, []);

  const meteringPoints = [
    { title: t("docs.metering.rowsTitle"), desc: t("docs.metering.rowsDesc") },
    {
      title: t("docs.metering.exploreTitle"),
      desc: t("docs.metering.exploreDesc"),
    },
    {
      title: t("docs.metering.alertTitle"),
      desc: t("docs.metering.alertDesc"),
    },
    {
      title: t("docs.metering.hardStopTitle"),
      desc: t("docs.metering.hardStopDesc"),
    },
  ];

  return (
    <div className="space-y-12">
      <header>
        <h1 className="text-3xl font-semibold tracking-tight">
          {t("docs.title")}
        </h1>
        <p className="mt-2 text-sm text-slate-500">{t("docs.subtitle")}</p>
      </header>

      {/* 一、快速开始 */}
      <section className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.quickStart.title")}</h2>
        <div className="rounded-lg border border-slate-200 p-5 space-y-4">
          <div>
            <h3 className="font-medium">{t("docs.quickStart.mcpTitle")}</h3>
            <p className="mt-1 text-sm text-slate-500">
              {t("docs.quickStart.mcpDesc")}
            </p>
            <p className="mt-2 text-xs text-slate-400">
              {t("docs.quickStart.mcpEndpointLabel")}
            </p>
            <code
              data-testid="mcp-endpoint"
              className="mt-1 inline-block rounded bg-slate-50 px-2 py-1 font-mono text-xs ring-1 ring-slate-200"
            >
              {MCP_ENDPOINT}
            </code>
          </div>
          <div className="border-t border-slate-100 pt-4">
            <h3 className="font-medium">{t("docs.quickStart.keyTitle")}</h3>
            <p className="mt-1 text-sm text-slate-500">
              {t("docs.quickStart.keyDesc")}
            </p>
            <p className="mt-2 text-xs text-slate-400">
              {t("docs.quickStart.keyExampleLabel")}
            </p>
            <code className="mt-1 inline-block rounded bg-slate-50 px-2 py-1 font-mono text-xs ring-1 ring-slate-200">
              Authorization: Bearer &lt;wire-api-key&gt;
            </code>
            <div className="mt-3">
              {/* 控制台直达（wire-admin-console-routing 任务 3.3）：保持裸
                  #/console——它是唯一会走偏好落地序的入口（置顶看板 >
                  landing_tab），对普通客户与管理员语义一致。管理员点击时置
                  分流标记（onClick），避免这次显式进入被角色分流改跳后台；
                  不以显式子路径绕开分流，那会一并剥夺普通客户的偏好落地。 */}
              <a
                href="#/console"
                data-testid="docs-quick-start-console-link"
                onClick={markAdminSplitDone}
                className="text-sm text-slate-600 underline hover:text-slate-900"
              >
                {t("docs.quickStart.consoleLink")}
              </a>
            </div>
          </div>
        </div>
      </section>

      {/* 二、计量与计费 */}
      <section className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.metering.title")}</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          {meteringPoints.map((p) => (
            <div key={p.title} className="rounded-lg border border-slate-200 p-5">
              <h3 className="font-medium">{p.title}</h3>
              <p className="mt-2 text-sm text-slate-500">{p.desc}</p>
            </div>
          ))}
        </div>
        <a
          href="#/pricing"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.metering.pricingNote")}
        </a>
      </section>

      {/* 四、数据面 REST API（wire-customer-data-engine 7.3）：端点族 + 持钥纪律 */}
      <section className="space-y-4" data-testid="docs-data-plane">
        <h2 className="text-xl font-semibold">{t("docs.dataPlane.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.dataPlane.intro")}
        </p>
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="w-full min-w-[36rem] border-collapse text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50/60 text-left text-xs text-slate-400">
                <th className="px-4 py-2 font-medium">
                  {t("docs.dataPlane.colMethod")}
                </th>
                <th className="px-4 py-2 font-medium">
                  {t("docs.dataPlane.colEndpoint")}
                </th>
                <th className="px-4 py-2 font-medium">
                  {t("docs.dataPlane.colPurpose")}
                </th>
              </tr>
            </thead>
            <tbody>
              {DATA_PLANE_ENDPOINTS.map((e) => (
                <tr key={e.path} className="border-b border-slate-100 last:border-0">
                  <td className="px-4 py-2 font-mono text-xs text-slate-500">
                    {e.method}
                  </td>
                  <td className="px-4 py-2 font-mono text-xs text-slate-800">
                    {e.path}
                  </td>
                  <td className="px-4 py-2 text-xs text-slate-500">
                    {t(e.purposeKey)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="rounded-lg border border-slate-200 p-5">
          <h3 className="font-medium">{t("docs.dataPlane.keyTitle")}</h3>
          <p className="mt-1 text-sm text-slate-500">
            {t("docs.dataPlane.keyDesc")}
          </p>
          <ul className="mt-3 space-y-1.5 text-sm text-slate-500">
            <li>{t("docs.dataPlane.rule1")}</li>
            <li>{t("docs.dataPlane.rule2")}</li>
            <li>{t("docs.dataPlane.rule3")}</li>
            <li>{t("docs.dataPlane.rule4")}</li>
          </ul>
          <a
            href="#/console/keys"
            className="mt-3 inline-block text-sm text-slate-600 underline hover:text-slate-900"
          >
            {t("docs.dataPlane.consoleLink")}
          </a>
        </div>
        <p className="max-w-3xl text-xs text-slate-400">
          {t("docs.dataPlane.mcpNote")}
        </p>
      </section>

      {/* 五、我的数据与连接器（wire-connector-distribution 任务 4）：
          上传三步 / 连接器安装三步 / 常驻方式（systemd/nssm/launchd）/
          故障排查——面向客户的口径，运维细节留在 deploy/README.md。 */}
      <section
        id="local-data"
        data-testid="docs-local-data"
        className="space-y-4"
      >
        <h2 className="text-xl font-semibold">{t("docs.localData.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.localData.intro")}
        </p>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.localData.uploadTitle")}</h3>
            <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
              {UPLOAD_STEPS.map((key) => (
                <li key={key}>{t(key)}</li>
              ))}
            </ol>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">
              {t("docs.localData.connectorTitle")}
            </h3>
            <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
              {CONNECTOR_STEPS.map((key) => (
                <li key={key}>{t(key)}</li>
              ))}
            </ol>
          </div>
        </div>

        <div
          data-testid="docs-local-data-residency"
          className="rounded-lg border border-slate-200 p-5"
        >
          <h3 className="font-medium">{t("docs.localData.residencyTitle")}</h3>
          <p className="mt-1 text-sm text-slate-500">
            {t("docs.localData.residencyIntro")}
          </p>
          <div className="mt-3 grid gap-4 lg:grid-cols-3">
            <div>
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.localData.systemdTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.localData.systemdNote")}
              </p>
              <pre className="mt-1.5 overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[11px] leading-relaxed text-slate-700 ring-1 ring-slate-200">
                {SYSTEMD_SAMPLE}
              </pre>
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.localData.nssmTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.localData.nssmNote")}
              </p>
              <pre className="mt-1.5 overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[11px] leading-relaxed text-slate-700 ring-1 ring-slate-200">
                {NSSM_SAMPLE}
              </pre>
            </div>
            <div>
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.localData.launchdTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.localData.launchdNote")}
              </p>
              <pre className="mt-1.5 overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[11px] leading-relaxed text-slate-700 ring-1 ring-slate-200">
                {LAUNCHD_SAMPLE}
              </pre>
            </div>
          </div>
        </div>

        <div
          data-testid="docs-local-data-troubleshooting"
          className="rounded-lg border border-slate-200 p-5"
        >
          <h3 className="font-medium">
            {t("docs.localData.troubleshootTitle")}
          </h3>
          <ul className="mt-3 space-y-1.5 text-sm text-slate-500">
            <li>{t("docs.localData.troubleshootWarnings")}</li>
            <li>{t("docs.localData.troubleshootDrift")}</li>
            <li>{t("docs.localData.troubleshootRows")}</li>
            <li>{t("docs.localData.troubleshootRevoked")}</li>
          </ul>
        </div>

        <a
          href="#/console/datasets"
          data-testid="docs-local-data-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.localData.consoleLink")}
        </a>
      </section>

      {/* 五b、资产目录与数据质量（wire-asset-catalog）：数据集自动进目录
          无需注册 / 「我的数据」筛选 / 质量快照如实口径（指示性检查，
          非质量认证）/ 质量指标在专属引擎内计算——信任点按客户口径措辞。 */}
      <section id="assets" data-testid="docs-assets" className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.assets.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.assets.intro")}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.assets.assetTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.assets.assetDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.assets.filterTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.assets.filterDesc")}
            </p>
          </div>
          <div
            data-testid="docs-assets-quality"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">{t("docs.assets.qualityTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.assets.qualityDesc")}
            </p>
            <ul className="mt-3 space-y-1.5 text-sm text-slate-500">
              <li>{t("docs.assets.qualityStates")}</li>
              <li>{t("docs.assets.qualityFreshness")}</li>
              {/* 质量规则（wire-quality-rules）：客户自定义合格线 */}
              <li data-testid="docs-assets-quality-rules">
                {t("docs.assets.qualityRules")}
              </li>
            </ul>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.assets.computeTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.assets.computeDesc")}
            </p>
          </div>
        </div>
        <a
          href="#/console/datasets"
          data-testid="docs-assets-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.assets.consoleLink")}
        </a>
      </section>

      {/* 五c、变换管线（wire-transform-pipelines 任务 7.1）：什么是管线
          （线性步骤链：派生→挂指标→关联）/ 版本语义（步骤变更新版本、
          旧版本可查）/ 输出语义（同版本重跑原地刷新、换版本新输出）/
          失败隔离（中途失败前面保留、后面不执行）/ 调度（cron 管线级）/
          计量如实（每步行数照常计入配额）——口径与控制台界面一致。 */}
      <section
        id="transform-pipelines"
        data-testid="docs-transform-pipelines"
        className="space-y-4"
      >
        <h2 className="text-xl font-semibold">
          {t("docs.transformPipelines.title")}
        </h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.transformPipelines.intro")}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">
              {t("docs.transformPipelines.pipelineTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.pipelineDesc")}
            </p>
          </div>
          <div
            data-testid="docs-transform-pipelines-version"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">
              {t("docs.transformPipelines.versionTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.versionDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">
              {t("docs.transformPipelines.outputTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.outputDesc")}
            </p>
          </div>
          <div
            data-testid="docs-transform-pipelines-failure"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">
              {t("docs.transformPipelines.failureTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.failureDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">
              {t("docs.transformPipelines.scheduleTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.scheduleDesc")}
            </p>
          </div>
          <div
            data-testid="docs-transform-pipelines-metering"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">
              {t("docs.transformPipelines.meteringTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.transformPipelines.meteringDesc")}
            </p>
          </div>
        </div>
        <a
          href="#/console/datasets"
          data-testid="docs-transform-pipelines-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.transformPipelines.consoleLink")}
        </a>
      </section>

      {/* 六、表单（wire-form-builder 任务 5.1）：把「收数据」做成成熟表单分享
          给他人填写的全流程教程——创建/绑定/配字段/试填/发布分享/修正历史
          回滚，面向客户的口径（照 docs.localData 的步骤条组织）。
          进阶录入（wire-form-entry-advanced 任务 6.1）：动态选项/选值回填/
          计算字段/提交闸四小节 + 公开选项端点数据暴露边界说明（zh/en）。 */}
      <section data-testid="docs-forms" className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.forms.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.forms.intro")}
        </p>
        <div className="rounded-lg border border-slate-200 p-5">
          <h3 className="font-medium">{t("docs.forms.stepsTitle")}</h3>
          <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
            {FORM_STEPS.map((key, i) => (
              <li key={key} data-testid={`docs-form-step-${i + 1}`}>
                {t(key)}
              </li>
            ))}
          </ol>
        </div>
        <div
          data-testid="docs-forms-advanced"
          className="rounded-lg border border-slate-200 p-5"
        >
          <h3 className="font-medium">{t("docs.forms.advancedTitle")}</h3>
          <div className="mt-3 grid gap-4 lg:grid-cols-2">
            <div data-testid="docs-forms-dynamic">
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.forms.dynamicTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.forms.dynamicDesc")}
              </p>
            </div>
            <div data-testid="docs-forms-fill">
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.forms.fillTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.forms.fillDesc")}
              </p>
            </div>
            <div data-testid="docs-forms-computed">
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.forms.computedTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.forms.computedDesc")}
              </p>
            </div>
            <div data-testid="docs-forms-gate">
              <h4 className="text-xs font-medium text-slate-600">
                {t("docs.forms.gateTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.forms.gateDesc")}
              </p>
            </div>
            {/* 公开选项面边界（文档红线）：全宽单列——如实的数据暴露说明。 */}
            <div
              data-testid="docs-forms-options-boundary"
              className="rounded border border-amber-100 bg-amber-50/40 p-3 lg:col-span-2"
            >
              <h4 className="text-xs font-medium text-amber-700">
                {t("docs.forms.optionsBoundaryTitle")}
              </h4>
              <p className="mt-1 text-xs text-slate-500">
                {t("docs.forms.optionsBoundaryDesc")}
              </p>
            </div>
          </div>
        </div>
        <a
          href="#/console/artifacts"
          data-testid="docs-forms-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.forms.consoleLink")}
        </a>
      </section>

      {/* 六a、单记录公开页（wire-record-view 任务 6.1）：按键展示的公开
          扫码页全流程（创建→配模板→试预览→发布分享→二维码）+ 公开实时
          读面的暴露边界如实说明（仅钉死版本声明的查询、令牌门为产物级而
          非记录级、k 可枚举、限流与缓存口径、撤销=归档即失效——文档红线：
          无「按记录隔离」的误导表述）。 */}
      <section data-testid="docs-record-view" className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.recordView.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.recordView.intro")}
        </p>
        <div className="rounded-lg border border-slate-200 p-5">
          <h3 className="font-medium">{t("docs.recordView.stepsTitle")}</h3>
          <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
            {RECORD_VIEW_STEPS.map((key, i) => (
              <li key={key} data-testid={`docs-record-view-step-${i + 1}`}>
                {t(key)}
              </li>
            ))}
          </ol>
        </div>
        <div
          data-testid="docs-record-view-boundary"
          className="rounded-lg border border-amber-100 bg-amber-50/40 p-5"
        >
          <h3 className="font-medium text-amber-700">
            {t("docs.recordView.boundaryTitle")}
          </h3>
          <p className="mt-2 text-sm text-slate-500">
            {t("docs.recordView.boundaryDesc")}
          </p>
        </div>
        <a
          href="#/console/artifacts"
          data-testid="docs-record-view-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.recordView.consoleLink")}
        </a>
      </section>

      {/* 六b、实体（wire-entity-layer 任务 3.3）：轻量实体语义层教程——声明
          实体→建关系→AI 构建自动带图谱语义（举例说明 prompt 效果）→当前
          边界如实说明。物化连接小节（wire-entity-link 任务 3.3）：v1 上线
          关系物化（LEFT JOIN / 撞名 right_ 前缀 / 免费 403 / 旧 cell 窗口）。 */}
      <section data-testid="docs-entities" className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.entities.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.entities.intro")}
        </p>
        <div className="rounded-lg border border-slate-200 p-5">
          <h3 className="font-medium">{t("docs.entities.stepsTitle")}</h3>
          <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
            {ENTITY_STEPS.map((key, i) => (
              <li key={key} data-testid={`docs-entity-step-${i + 1}`}>
                {t(key)}
              </li>
            ))}
          </ol>
        </div>
        <div
          data-testid="docs-entity-materialize"
          className="rounded-lg border border-indigo-100 bg-indigo-50/40 p-5"
        >
          <h3 className="font-medium">{t("docs.entities.materializeTitle")}</h3>
          <ol className="mt-3 list-decimal space-y-1.5 pl-5 text-sm text-slate-500">
            {ENTITY_MATERIALIZE_STEPS.map((key, i) => (
              <li key={key} data-testid={`docs-entity-materialize-step-${i + 1}`}>
                {t(key)}
              </li>
            ))}
          </ol>
        </div>
        <a
          href="#/console/entities"
          data-testid="docs-entities-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.entities.consoleLink")}
        </a>
      </section>

      {/* 六c、团队空间（wire-team-spaces v0 + v2 任务 7.1）：三角角色门禁 /
          席位与邀请（生成与兑换双闸零超额、免费档=1 引导升级）/ 多团队与
          活跃队切换（切队=切数据面；guesting owner 暂失自家 owner 面、
          切回即恢复；被撤成员指针自愈）/ 角色变更即时生效 / team_events
          审计留痕——面向客户如实口径，邮件邀请不在册不提。 */}
      <section
        id="team-spaces"
        data-testid="docs-team-spaces"
        className="space-y-4"
      >
        <h2 className="text-xl font-semibold">
          {t("docs.teamSpaces.title")}
        </h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.teamSpaces.intro")}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.teamSpaces.rolesTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.teamSpaces.rolesDesc")}
            </p>
          </div>
          <div
            data-testid="docs-team-spaces-seats"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">{t("docs.teamSpaces.seatsTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.teamSpaces.seatsDesc")}
            </p>
          </div>
          <div
            data-testid="docs-team-spaces-multi"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">{t("docs.teamSpaces.multiTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.teamSpaces.multiDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">
              {t("docs.teamSpaces.roleChangeTitle")}
            </h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.teamSpaces.roleChangeDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.teamSpaces.auditTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.teamSpaces.auditDesc")}
            </p>
          </div>
        </div>
        <a
          href="#/console/team"
          data-testid="docs-team-spaces-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.teamSpaces.consoleLink")}
        </a>
      </section>

      {/* 数据开放与市场（wire-dataset-marketplace 任务 7.1）：四档可见性 /
          默认私有 / 可撤回 / 市场浏览订阅申请 / 只读使用与计量归属。 */}
      <section
        id="data-market"
        data-testid="docs-data-market"
        className="space-y-4"
      >
        <h2 className="text-xl font-semibold">{t("docs.market.title")}</h2>
        <p className="max-w-3xl text-sm text-slate-500">
          {t("docs.market.intro")}
        </p>
        <div className="grid gap-4 sm:grid-cols-2">
          <div
            data-testid="docs-market-tiers"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">{t("docs.market.tiersTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.tiersDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.market.defaultTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.defaultDesc")}
            </p>
          </div>
          <div
            data-testid="docs-market-access"
            className="rounded-lg border border-slate-200 p-5"
          >
            <h3 className="font-medium">{t("docs.market.accessTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.accessDesc")}
            </p>
          </div>
          <div className="rounded-lg border border-slate-200 p-5">
            <h3 className="font-medium">{t("docs.market.readonlyTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.readonlyDesc")}
            </p>
          </div>
          {/* 看板与报告开放（wire-artifact-marketplace 任务 7.1）：只读看
              与可复制两种语义 + Q1 快照披露纪律（禁「仅结构不含数据」）。 */}
          <div
            data-testid="docs-market-artifacts"
            className="rounded-lg border border-slate-200 p-5 sm:col-span-2"
          >
            <h3 className="font-medium">{t("docs.market.artifactsTitle")}</h3>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.artifactsDesc")}
            </p>
            <p className="mt-2 text-sm text-slate-500">
              {t("docs.market.artifactsCopyDesc")}
            </p>
          </div>
        </div>
        <a
          href="#/console/market"
          data-testid="docs-market-console-link"
          className="inline-block text-sm text-slate-600 underline hover:text-slate-900"
        >
          {t("docs.market.consoleLink")}
        </a>
      </section>

      {/* 三、需求单流程（三态状态机步骤条） */}
      <section className="space-y-4">
        <h2 className="text-xl font-semibold">{t("docs.demands.title")}</h2>
        <p className="text-sm text-slate-500">{t("docs.demands.intro")}</p>
        <div className="rounded-lg border border-slate-200 p-5">
          <h3 className="text-sm font-medium text-slate-400">
            {t("docs.demands.stepsTitle")}
          </h3>
          <ol className="mt-4 flex flex-wrap items-start gap-y-3">
            {DEMAND_STEPS.map((key, i) => (
              <li
                key={key}
                data-testid={`demand-step-${i + 1}`}
                className="flex items-center gap-2"
              >
                <span className="flex h-6 w-6 items-center justify-center rounded-full bg-slate-900 text-xs font-medium text-white">
                  {i + 1}
                </span>
                <span className="text-sm text-slate-700">{t(key)}</span>
                {i < DEMAND_STEPS.length - 1 && (
                  <span aria-hidden className="mx-2 h-px w-6 bg-slate-300" />
                )}
              </li>
            ))}
          </ol>
          <p className="mt-4 text-xs text-slate-400">
            {t("docs.demands.rejectedNote")}
          </p>
        </div>
      </section>

      {/* 能力上线状态——如实呈现，未上线不发卡。控制台（AI 对话构建/我的数据/
          产物预览）已上线，不再挂「上线中」徽标（wire-self-serve-launch 7.3/7.4）；
          需求单提交已上线（wire-ux-consolidation 4.1），仅 cell 开通保留徽标。 */}
      <section className="space-y-3">
        <h2 className="text-sm font-medium text-slate-400">
          {t("docs.statusTitle")}
        </h2>
        <ul className="space-y-2">
          <StatusRow label={t("docs.demands.cellLabel")} />
          <li className="flex flex-wrap items-center gap-2">
            <span className="text-sm text-slate-600">
              {t("docs.statusDemandLabel")}
            </span>
          </li>
        </ul>
      </section>
    </div>
  );
}
