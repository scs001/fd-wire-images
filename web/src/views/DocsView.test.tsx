import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import "../i18n";
import i18n from "../i18n";
import { parseHash } from "../App";
import { DocsView, MCP_ENDPOINT } from "./DocsView";

beforeEach(() => i18n.changeLanguage("zh"));
afterEach(cleanup);

describe("DocsView（文档三节 · 如实呈现）", () => {
  it("三节结构：快速开始 / 计量与计费 / 需求单流程", () => {
    render(<DocsView />);

    expect(screen.getByText("快速开始")).toBeTruthy();
    expect(screen.getByText("计量与计费")).toBeTruthy();
    expect(screen.getByText("需求单流程")).toBeTruthy();
  });

  it("快速开始：MCP 端点占位 + 密钥调用示意（密钥管理指控制台）", () => {
    render(<DocsView />);

    expect(screen.getByTestId("mcp-endpoint").textContent).toBe(MCP_ENDPOINT);
    expect(screen.getByText(/控制台「密钥管理」/)).toBeTruthy();
    expect(screen.getByText(/Authorization: Bearer/)).toBeTruthy();
  });

  it("计量与计费四要点：按行 / AI 探索不计费 / 两档预警 / 硬停", () => {
    render(<DocsView />);

    expect(screen.getByText("按行计量")).toBeTruthy();
    expect(screen.getByText("AI 探索不计费")).toBeTruthy();
    expect(screen.getByText(/80% 与 95%/)).toBeTruthy();
    // 硬停语义与四档口径联动（wire-self-serve-launch 7.4）：耗尽即停、次月自动恢复
    const hardStop = screen.getByText(/fail-closed 硬停/);
    expect(hardStop.textContent).toContain("次月自动恢复");
  });

  it("计费说明与定价页联动：四档口径 + pricingNote 链接 #/pricing", () => {
    render(<DocsView />);

    const pricingNote = screen.getByText(/四档套餐（免费\/基础\/标准\/自定义）/);
    expect(pricingNote.textContent).toContain("用完即停、次月恢复");
    expect(pricingNote.closest("a")?.getAttribute("href")).toBe("#/pricing");
  });

  it("需求单三态：三个主步骤 + 拒绝分支说明（八态旧名退场）", () => {
    render(<DocsView />);

    for (const label of ["已收到", "处理中", "已交付"]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    // rejected：分支留档说明（1 个工作日响应承诺）
    expect(screen.getByText(/「已拒绝」/)).toBeTruthy();
    expect(screen.getAllByTestId(/^demand-step-/)).toHaveLength(3);
    // 旧八态名不出现在客户面文案（wire-ux-consolidation 任务 3.5）
    const pageText = document.body.textContent ?? "";
    for (const legacy of ["评估中", "已报价", "待确认", "构建中", "交付验收"]) {
      expect(pageText).not.toContain(legacy);
    }
  });

  it("「上线中」徽标收缩为一项：隔离单元开通（需求单提交已上线不再挂徽标）", () => {
    render(<DocsView />);

    // 控制台（AI 对话构建/我的数据/产物）已上线（wire-self-serve-launch 7.3/7.4）；
    // 需求单提交已上线（wire-ux-consolidation 4.1）——仅隔离单元开通如实挂徽标
    expect(screen.queryByText("控制台分析构建")).toBeNull();
    const badges = screen.getAllByTestId("coming-soon-badge");
    expect(badges.length).toBe(1);
    expect(badges[0]!.textContent).toContain("上线中");
    expect(screen.getByText(/隔离单元开通/)).toBeTruthy();
    expect(screen.getByText("需求单提交")).toBeTruthy();
  });

  // 我的数据与连接器教程（wire-connector-distribution 任务 4）：
  // 上传三步 / 连接器三步 / 常驻样例（systemd/nssm/launchd）/
  // 故障排查四条 / 控制台互链与深链锚点定位。
  it("「我的数据与连接器」：上传与连接器各三步、控制台互链", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-local-data");
    expect(section.getAttribute("id")).toBe("local-data");
    expect(section.textContent).toContain("上传三步");
    expect(section.textContent).toContain("连接器安装三步");
    // 三步各含关键动作（拖拽 / 列类型确认 / 建集；令牌 / pair / run）
    expect(section.textContent).toContain("拖入或选择文件");
    expect(section.textContent).toContain("确认列与类型");
    expect(section.textContent).toContain("命名建档");
    expect(section.textContent).toContain("配对令牌");
    expect(section.textContent).toContain("pair --token");
    expect(section.textContent).toContain("run 常驻运行");
    // 文档 → 控制台入口
    const consoleLink = screen.getByTestId("docs-local-data-console-link");
    expect(consoleLink.getAttribute("href")).toBe("#/console/datasets");
  });

  it("常驻方式样例：systemd / nssm / launchd 三平台配置齐备", () => {
    render(<DocsView />);

    const residency = screen.getByTestId("docs-local-data-residency");
    expect(residency.textContent).toContain("systemd");
    expect(residency.textContent).toContain("nssm");
    expect(residency.textContent).toContain("launchd");
    expect(residency.textContent).toContain("fd-wire-connector.service".replace(/\.service.*/, "")); // ExecStart 在场
    expect(residency.textContent).toContain("ExecStart=/usr/local/bin/fd-wire-connector run");
    expect(residency.textContent).toContain("nssm install fd-wire-connector");
    expect(residency.textContent).toContain("com.finddata.fd-wire-connector");
  });

  it("故障排查四条：心跳 warnings / schema 漂移暂停 / 行上限 / 吊销后重配对", () => {
    render(<DocsView />);

    const block = screen.getByTestId("docs-local-data-troubleshooting");
    expect(block.textContent).toContain("心跳 warnings");
    expect(block.textContent).toContain("schema 漂移暂停");
    expect(block.textContent).toContain("行上限");
    expect(block.textContent).toContain("吊销后重配对");
  });

  it("英文文案同步（docs.localData 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-local-data");
    expect(section.textContent).toContain("My data & connectors");
    expect(section.textContent).toContain("Uploading in three steps");
    expect(section.textContent).toContain("Installing a connector in three steps");
    expect(section.textContent).toContain("Troubleshooting");
    // REST 源小节（wire-rest-connector 3.2）英文同步
    expect(section.textContent).toContain("REST / JSON API sources");
    expect(section.textContent).toContain("Credentials stay local");
    i18n.changeLanguage("zh");
  });

  // REST 源小节（wire-rest-connector 任务 3.2）：配置形状 / 分页二式 /
  // 投影 / 凭据本地化 / 增量口径 / 错误如实 + 本地配置示例。
  it("「REST / JSON API 源」小节：形状/分页/投影/零凭据/增量/错误 + 示例", () => {
    render(<DocsView />);

    const block = screen.getByTestId("docs-local-data-rest");
    expect(block.textContent).toContain("REST / JSON API 源");
    expect(block.textContent).toContain("分页声明二选一");
    expect(block.textContent).toContain("字段投影");
    // 零凭据是这一节的核心承诺，原句必须在场
    expect(block.textContent).toContain("凭据本地化（零凭据上云）");
    expect(block.textContent).toContain("增量");
    expect(block.textContent).toContain("退避");

    // 本地配置示例：展示 auth 只在本地文件里
    const sample = screen.getByTestId("docs-local-data-rest-sample");
    expect(sample.textContent).toContain('"kind": "rest"');
    expect(sample.textContent).toContain('"auth"');
    expect(sample.textContent).toContain('"projection"');
    expect(sample.textContent).toContain('"pagination"');
  });

  // 资产目录与数据质量章节（wire-asset-catalog）：自动登记与元数据补全 /
  // 「我的数据」筛选 / 质量快照如实口径（原句「指示性检查，非质量认证」
  // 必须在场）/ 质量指标在专属引擎内计算（信任点）+ 双语。
  it("「资产目录与数据质量」章节：自动登记 / 筛选 / 质量快照口径 / 计算位置", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-assets");
    expect(section.getAttribute("id")).toBe("assets");
    expect(section.textContent).toContain("资产目录与数据质量");
    // 资产信息：自动登记、元数据补全、敏感度只是标注与筛选
    expect(section.textContent).toContain("无需注册");
    expect(section.textContent).toContain("业务域");
    expect(section.textContent).toContain("敏感度");
    expect(section.textContent).toContain("不影响数据的存储位置");
    // 目录筛选：维度 + 关键字搜索
    expect(section.textContent).toContain("目录筛选");
    expect(section.textContent).toContain("关键字搜索");
    // 数据质量：快照内容 / 手动检查 / 原句口径 / 四状态 / 新鲜度判定
    const quality = screen.getByTestId("docs-assets-quality");
    expect(quality.textContent).toContain("10,000 行抽样");
    expect(quality.textContent).toContain("立即检查");
    expect(quality.textContent).toContain("指示性检查，非质量认证");
    expect(quality.textContent).toContain("暂无快照");
    expect(quality.textContent).toContain("未声明的显示「未知」");
    // 计算位置：信任点措辞（专属引擎内计算，平台只保存聚合数字）
    expect(section.textContent).toContain("专属数据引擎");
    expect(section.textContent).toContain("不接触你的数据内容");
    // 文档 → 控制台入口
    const link = screen.getByTestId("docs-assets-console-link");
    expect(link.getAttribute("href")).toBe("#/console/datasets");
  });

  it("资产目录章节英文同步（docs.assets 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-assets");
    expect(section.textContent).toContain("Asset catalog & data quality");
    const quality = screen.getByTestId("docs-assets-quality");
    expect(quality.textContent).toContain(
      "an indicative check, not a quality certification",
    );
    expect(section.textContent).toContain("dedicated data engine");
    i18n.changeLanguage("zh");
  });

  // 变换管线章节（wire-transform-pipelines 任务 7.1）：线性步骤链 / 版本
  // 语义（原句必须在场）/ 输出语义（原地刷新）/ 失败隔离（原句必须在场）/
  // 调度（cron 管线级）/ 计量如实——口径与控制台管线工作台一致。
  it("「变换管线」章节：步骤链/版本语义/输出语义/失败隔离/调度/计量如实（关键句逐字断言）", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-transform-pipelines");
    expect(section.getAttribute("id")).toBe("transform-pipelines");
    expect(section.textContent).toContain("变换管线");
    // 什么是管线：三类型线性步骤链（派生→挂接指标→关联）
    expect(section.textContent).toContain("线性步骤链");
    expect(section.textContent).toContain("派生");
    expect(section.textContent).toContain("挂接指标");
    expect(section.textContent).toContain("关联数据集");
    // 版本语义（原句逐字）：步骤变更新版本、历史可查；仅改名不动版本
    const version = screen.getByTestId("docs-transform-pipelines-version");
    expect(version.textContent).toContain("版本语义");
    expect(version.textContent).toContain(
      "步骤列表的任何变更都会产生新版本，历史版本原样可查",
    );
    expect(version.textContent).toContain("仅改名称/描述不动版本");
    // 输出语义：同版本重跑原地刷新；新版本新输出
    expect(section.textContent).toContain("同版本重跑原地刷新");
    expect(section.textContent).toContain("旧版本输出以普通数据集存续");
    // 失败隔离（原句逐字）：前面保留、后面不执行、不伪造
    const failure = screen.getByTestId("docs-transform-pipelines-failure");
    expect(failure.textContent).toContain("失败隔离");
    expect(failure.textContent).toContain(
      "已成功步骤的输出保留，后续步骤不再执行",
    );
    expect(failure.textContent).toContain("绝不伪造后续结果");
    // 调度：管线级 cron（五字段 UTC）+ 暂停恢复
    expect(section.textContent).toContain("管线级 cron 调度");
    expect(section.textContent).toContain("暂停/恢复/删除");
    // 计量如实：每步行数照常计入配额
    const metering = screen.getByTestId("docs-transform-pipelines-metering");
    expect(metering.textContent).toContain("计量如实");
    expect(metering.textContent).toContain("照常计入配额与用量");
    // 文档 → 控制台入口（管线工作台在「我的数据 → 管线」）
    const link = screen.getByTestId("docs-transform-pipelines-console-link");
    expect(link.getAttribute("href")).toBe("#/console/datasets");
    expect(link.textContent).toContain("管线");
    // 五类算子 + SQL 步骤小节（wire-transform-operators 3.2 + wire-window-operator）
    const operators = screen.getByTestId("docs-transform-pipelines-operators");
    expect(operators.textContent).toContain("五类算子与 SQL 步骤");
    expect(operators.textContent).toContain("行过滤");
    expect(operators.textContent).toContain("投影重命名");
    expect(operators.textContent).toContain("分组聚合");
    expect(operators.textContent).toContain("纵向合并");
    expect(operators.textContent).toContain("窗口计算");
    expect(operators.textContent).toContain("row_number");
    // SQL 步骤的只读护栏与「试运行不落库」承诺必须在场
    expect(operators.textContent).toContain("只读 SELECT");
    expect(operators.textContent).toContain("占位");
    expect(operators.textContent).toContain("试运行");
    expect(operators.textContent).toContain("不落库");
  });

  it("变换管线章节英文同步（docs.transformPipelines 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-transform-pipelines");
    expect(section.textContent).toContain("Transform pipelines");
    const version = screen.getByTestId("docs-transform-pipelines-version");
    expect(version.textContent).toContain(
      "Any change to the step list creates a new version",
    );
    const failure = screen.getByTestId("docs-transform-pipelines-failure");
    expect(failure.textContent).toContain(
      "outputs of already-successful steps are kept and later steps do not run",
    );
    expect(section.textContent).toContain("five fields, UTC");
    expect(section.textContent).toContain("count against quota and usage");
    // 五类算子 + SQL 步骤小节英文同步
    const operators = screen.getByTestId("docs-transform-pipelines-operators");
    expect(operators.textContent).toContain("Five operators and the SQL step");
    expect(operators.textContent).toContain("window operator");
    expect(operators.textContent).toContain("read-only SELECT");
    expect(operators.textContent).toContain("nothing is persisted");
    i18n.changeLanguage("zh");
  });

  // 表单教程（wire-form-builder 任务 5.1）：五步全流程 + 控制台互链 + 双语。
  it("「表单」章节：建表单五步（创建/绑定配字段/试填/发布分享/收集回滚）+ 控制台互链", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-forms");
    expect(section.textContent).toContain("表单：把数据「收」进来");
    for (let i = 1; i <= 5; i += 1) {
      expect(screen.getByTestId(`docs-form-step-${i}`)).toBeTruthy();
    }
    expect(section.textContent).toContain("新建表单");
    expect(section.textContent).toContain("绑定数据集并配字段");
    expect(section.textContent).toContain("发布并分享");
    expect(section.textContent).toContain("收集与回滚");
    const link = screen.getByTestId("docs-forms-console-link");
    expect(link.getAttribute("href")).toBe("#/console/artifacts");
    expect(link.textContent).toContain("产物");
  });

  it("表单章节英文同步（docs.forms 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-forms");
    expect(section.textContent).toContain("Forms: bringing data in");
    expect(section.textContent).toContain("Five steps to a form");
    expect(section.textContent).toContain("Publish and share");
    expect(section.textContent).toContain("Collect and roll back");
    i18n.changeLanguage("zh");
  });

  // 实体教程（wire-entity-layer 任务 3.3）：声明→关系→AI 带图谱→v1 边界
  // 如实说明 + 物化连接小节（wire-entity-link 任务 3.3）+ 控制台互链 + 双语。
  it("「实体」章节：四步（声明/关系/AI 带图谱/边界如实）+ 物化连接小节 + 控制台互链", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-entities");
    expect(section.textContent).toContain("实体：给数据加上业务语义");
    for (let i = 1; i <= 4; i += 1) {
      expect(screen.getByTestId(`docs-entity-step-${i}`)).toBeTruthy();
    }
    expect(section.textContent).toContain("声明实体");
    expect(section.textContent).toContain("n:1/1:1 目标列选目标实体主键列；1:n 为反向声明（源列=「一」侧主键、目标列=「多」侧外键列）");
    expect(section.textContent).toContain("实体图谱");
    // prompt 效果举例在场。
    expect(section.textContent).toContain("按客户所在城市统计订单金额");
    // v1 边界如实：物化为 cell 档能力，多关系组合 join 不在 v1。
    expect(section.textContent).toContain("当前边界");
    expect(section.textContent).toContain("物化连接是隔离单元档能力");
    expect(section.textContent).toContain("多关系组合 join");
    // 物化连接小节：两步（前置 + LEFT JOIN 语义/撞名前缀/免费档 403/旧 cell）。
    const materialize = screen.getByTestId("docs-entity-materialize");
    expect(materialize.textContent).toContain(
      "物化连接：把关系变成可查的连接数据集",
    );
    for (let i = 1; i <= 2; i += 1) {
      expect(
        screen.getByTestId(`docs-entity-materialize-step-${i}`),
      ).toBeTruthy();
    }
    expect(materialize.textContent).toContain("LEFT JOIN");
    expect(materialize.textContent).toContain("right_");
    expect(materialize.textContent).toContain("403");
    expect(materialize.textContent).toContain("工具不存在");
    // 声明提案小节（wire-entity-write-surface 任务 5.1）：内置两类型 / MCP
    // 提议即回执 pending / 控制台唯一确认面 / 元数据效果（不落修正层）/
    // 免费档可用。
    const proposal = screen.getByTestId("docs-entity-proposal");
    expect(proposal.textContent).toContain("声明提案：agent 提交，owner 确认");
    for (let i = 1; i <= 3; i += 1) {
      expect(screen.getByTestId(`docs-entity-proposal-step-${i}`)).toBeTruthy();
    }
    expect(proposal.textContent).toContain("entity-declare");
    expect(proposal.textContent).toContain("entity-relation-declare");
    expect(proposal.textContent).toContain("wire_action_propose");
    expect(proposal.textContent).toContain("pending");
    expect(proposal.textContent).toContain("不落修正/覆盖层");
    expect(proposal.textContent).toContain("免费档数据集同样可以声明");
    const link = screen.getByTestId("docs-entities-console-link");
    expect(link.getAttribute("href")).toBe("#/console/entities");
    expect(link.textContent).toContain("实体");
  });

  it("实体章节英文同步（docs.entities 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-entities");
    expect(section.textContent).toContain(
      "Entities: business semantics for your data",
    );
    expect(section.textContent).toContain("Four steps into the semantic layer");
    expect(section.textContent).toContain("Current boundary");
    // 物化连接小节双语。
    const materialize = screen.getByTestId("docs-entity-materialize");
    expect(materialize.textContent).toContain("Materialize a link");
    expect(materialize.textContent).toContain("LEFT JOIN");
    expect(materialize.textContent).toContain("right_");
    expect(materialize.textContent).toContain("403");
    // 声明提案小节双语。
    const proposal = screen.getByTestId("docs-entity-proposal");
    expect(proposal.textContent).toContain("Declaration proposals");
    expect(proposal.textContent).toContain("entity-declare");
    expect(proposal.textContent).toContain("entity-relation-declare");
    expect(proposal.textContent).toContain("wire_action_propose");
    expect(proposal.textContent).toContain("corrections/overlay layer");
    expect(proposal.textContent).toContain("free-tier datasets can be declared");
    i18n.changeLanguage("zh");
  });

  // 团队空间章节（wire-team-spaces v0 + v2 任务 7.1）：三角角色门禁 /
  // 席位双闸零超额（免费档=1 引导原句在场）/ 多团队切换=切数据面
  // （guesting owner 暂失 owner 面原句在场）/ 角色变更即时生效 / 审计留痕。
  it("「团队空间」章节：角色/席位双闸/多团队切换语义/角色变更/留痕（关键句逐字断言）", () => {
    render(<DocsView />);

    const section = screen.getByTestId("docs-team-spaces");
    expect(section.getAttribute("id")).toBe("team-spaces");
    expect(section.textContent).toContain("团队空间");
    // 三角角色：owner 全权、密钥物 owner 专属、viewer 只读
    expect(section.textContent).toContain("owner 全权");
    expect(section.textContent).toContain("owner 专属");
    expect(section.textContent).toContain("viewer 仅可查看");
    // 席位：口径=成员数+1、双闸、码保活、免费档=1
    const seats = screen.getByTestId("docs-team-spaces-seats");
    expect(seats.textContent).toContain("成员数 + 1");
    expect(seats.textContent).toContain("满员即拒、无超额计费");
    expect(seats.textContent).toContain("腾出席位后仍可兑换");
    expect(seats.textContent).toContain("免费档上限 1 席");
    // 多团队：切队=切数据面、guesting owner 暂失 owner 面、自愈回落
    const multi = screen.getByTestId("docs-team-spaces-multi");
    expect(multi.textContent).toContain("切换团队即切换数据面");
    expect(multi.textContent).toContain("切回即恢复 owner 权限");
    expect(multi.textContent).toContain("会话失效");
    // 角色变更：editor↔viewer、下次请求即生效
    expect(section.textContent).toContain("下次请求即按新角色生效");
    // 文档 → 控制台入口
    const link = screen.getByTestId("docs-team-spaces-console-link");
    expect(link.getAttribute("href")).toBe("#/console/team");
  });

  it("团队空间章节英文同步（docs.teamSpaces 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const section = screen.getByTestId("docs-team-spaces");
    expect(section.textContent).toContain("Team spaces");
    const seats = screen.getByTestId("docs-team-spaces-seats");
    expect(seats.textContent).toContain("members + 1");
    expect(seats.textContent).toContain("no overage billing");
    const multi = screen.getByTestId("docs-team-spaces-multi");
    expect(multi.textContent).toContain("switching teams switches the data plane");
    expect(multi.textContent).toContain("regain owner powers");
    i18n.changeLanguage("zh");
  });

  it("路由：#/docs 解析为 docs（未登录可访问）", () => {
    expect(parseHash("#/docs")).toEqual({ name: "docs" });
  });
});

  // 进阶录入能力（wire-form-entry-advanced 任务 6.1）：四小节 + 公开选项面
  // 数据暴露边界说明，zh/en 双语。
  it("「表单」进阶小节：动态选项/选值回填/计算字段/提交闸 + 选项端点边界", () => {
    render(<DocsView />);

    const advanced = screen.getByTestId("docs-forms-advanced");
    expect(advanced.textContent).toContain("进阶录入能力");
    const dynamic = screen.getByTestId("docs-forms-dynamic");
    expect(dynamic.textContent).toContain("动态选项");
    expect(dynamic.textContent).toContain("去重");
    expect(dynamic.textContent).toContain("升序");
    expect(dynamic.textContent).toContain("三源互斥");
    const fill = screen.getByTestId("docs-forms-fill");
    expect(fill.textContent).toContain("选值回填");
    expect(fill.textContent).toContain("可改写");
    expect(fill.textContent).toContain("快照");
    const computed = screen.getByTestId("docs-forms-computed");
    expect(computed.textContent).toContain("计算字段");
    expect(computed.textContent).toContain("add_days");
    expect(computed.textContent).toContain("闰年");
    const gate = screen.getByTestId("docs-forms-gate");
    expect(gate.textContent).toContain("提交闸");
    expect(gate.textContent).toContain("artifact_id");
    expect(gate.textContent).toContain("服务端");
    const boundary = screen.getByTestId("docs-forms-options-boundary");
    expect(boundary.textContent).toContain("选项解析端点");
    expect(boundary.textContent).toContain("令牌");
    expect(boundary.textContent).toContain("限流");
  });

  it("表单进阶小节英文同步（docs.forms 双语键齐备）", () => {
    i18n.changeLanguage("en");
    render(<DocsView />);

    const advanced = screen.getByTestId("docs-forms-advanced");
    expect(advanced.textContent).toContain("Advanced entry capabilities");
    expect(screen.getByTestId("docs-forms-dynamic").textContent).toContain(
      "Dynamic options",
    );
    expect(screen.getByTestId("docs-forms-fill").textContent).toContain(
      "Fill on selection",
    );
    expect(screen.getByTestId("docs-forms-computed").textContent).toContain(
      "Computed fields",
    );
    expect(screen.getByTestId("docs-forms-gate").textContent).toContain(
      "Submit gates",
    );
    expect(
      screen.getByTestId("docs-forms-options-boundary").textContent,
    ).toContain("options endpoint");
    i18n.changeLanguage("zh");
  });
