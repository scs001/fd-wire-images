// record-view 纯函数测试（wire-record-view 任务 4.1/5.2）：
// - 信封组装/宽容解析（信封与本体双形态、非法条目丢弃）；
// - 草稿校验镜像（未绑中间态/键列/头部列成员与去重/段形状/limit/filters
//   形状与 {fromField} 引用/数量上限）；
// - 取数响应归一（found 双态、header/sections 收窄、expires_at null）；
// - 时间轴升序排序（字符串/数值串/空值排尾）与单元格呈现占位；
// - URL 组装（数据端点 + 公开页完整链接，t/k 分量 urlencode）。
import { describe, expect, it } from "vitest";
import {
  buildRecordViewEnvelope,
  normalizeRecordViewPayload,
  parseRecordViewSpec,
  recordCellText,
  sortRowsByDate,
  validateRecordViewSpec,
  type RecordViewSpecBody,
} from "./recordViewSpec";
import {
  buildRecordViewSharePageUrl,
  sharedRecordViewDataUrl,
} from "../api";

const EMPTY_BODY: RecordViewSpecBody = {
  datasetId: null,
  keyColumn: null,
  headerFields: [],
  sections: [],
};

const MAIN_COLUMNS = [
  { name: "批次号", type: "TEXT" },
  { name: "产品名称", type: "TEXT" },
  { name: "地块编号", type: "TEXT" },
];

const SECTION_COLUMNS = [
  { name: "日期", type: "DATE" },
  { name: "地块编号", type: "TEXT" },
  { name: "用量", type: "REAL" },
];

/** 编辑器同款 columnsOf：主数据集与段数据集两套列快照。 */
const columnsOf = (datasetId: string) =>
  datasetId === "ds-batch" ? MAIN_COLUMNS
    : datasetId === "ds-spray" ? SECTION_COLUMNS
    : null;

describe("recordViewSpec：信封与解析", () => {
  it("信封组装：{title, spec:{本体}}（saveArtifactSpec 入参形状）", () => {
    const body: RecordViewSpecBody = {
      datasetId: "ds-batch",
      keyColumn: "批次号",
      headerFields: [{ column: "产品名称", label: "产品" }],
      sections: [
        {
          title: "打药",
          datasetId: "ds-spray",
          dateColumn: "日期",
          filters: [{ column: "地块编号", op: "eq", value: { fromField: "地块编号" } }],
          limit: 100,
        },
      ],
    };
    expect(buildRecordViewEnvelope("溯源页", body)).toEqual({
      title: "溯源页",
      spec: body,
    });
  });

  it("宽容解析：信封（产物详情原样）与本体双形态；非法条目丢弃不整单失败", () => {
    const spec = {
      datasetId: "ds-batch",
      keyColumn: "批次号",
      headerFields: [
        { column: "产品名称", label: "产品" },
        { column: "" }, // 列名空 → 丢弃
        "not-an-object", // 非对象 → 丢弃
      ],
      sections: [
        {
          title: "打药",
          datasetId: "ds-spray",
          dateColumn: "日期",
          filters: [
            { column: "地块编号", op: "eq", value: { fromField: "地块编号" } },
            { column: "用量", op: "bad-op", value: 1 }, // 算子越白名单 → 丢弃
            { column: "用量", op: "gt", value: { nope: true } }, // 值形状非法 → 丢弃
          ],
        },
        { datasetId: "", dateColumn: "日期", filters: [] }, // datasetId 空 → 丢弃
      ],
    };
    const fromEnvelope = parseRecordViewSpec({ title: "t", spec });
    const fromBody = parseRecordViewSpec(spec);
    expect(fromEnvelope).toEqual({
      datasetId: "ds-batch",
      keyColumn: "批次号",
      headerFields: [{ column: "产品名称", label: "产品" }],
      sections: [
        {
          title: "打药",
          datasetId: "ds-spray",
          dateColumn: "日期",
          filters: [
            { column: "地块编号", op: "eq", value: { fromField: "地块编号" } },
          ],
        },
      ],
    });
    expect(fromBody).toEqual(fromEnvelope);
  });

  it("非 record-view 形状（无 spec 对象/非对象）返回 null（容器回退空态）", () => {
    expect(parseRecordViewSpec(null)).toBeNull();
    expect(parseRecordViewSpec("text")).toBeNull();
    expect(parseRecordViewSpec({ title: "t" })).toBeNull();
  });
});

describe("recordViewSpec：草稿校验镜像", () => {
  it("未绑数据集的最小空 spec 可保存（中间态）；携带任何配置即 unbound", () => {
    expect(validateRecordViewSpec("页", EMPTY_BODY, columnsOf)).toEqual([]);
    expect(
      validateRecordViewSpec(
        "页",
        { ...EMPTY_BODY, keyColumn: "批次号" },
        columnsOf,
      ),
    ).toEqual(["unbound"]);
    expect(
      validateRecordViewSpec(
        "页",
        { ...EMPTY_BODY, headerFields: [{ column: "产品名称" }] },
        columnsOf,
      ),
    ).toEqual(["unbound"]);
  });

  it("标题：必填与长度上限", () => {
    expect(validateRecordViewSpec("", EMPTY_BODY, columnsOf)).toEqual([
      "titleRequired",
    ]);
    expect(
      validateRecordViewSpec("x".repeat(201), EMPTY_BODY, columnsOf),
    ).toEqual(["titleTooLong"]);
  });

  it("绑定后：键列必填、列快照成员校验；headerFields 去重与 label 上限", () => {
    const base: RecordViewSpecBody = {
      datasetId: "ds-batch",
      keyColumn: null,
      headerFields: [],
      sections: [],
    };
    expect(validateRecordViewSpec("页", base, columnsOf)).toEqual([
      "keyColumnRequired",
    ]);
    expect(
      validateRecordViewSpec("页", { ...base, keyColumn: "不存在列" }, columnsOf),
    ).toEqual(["keyColumnMissing"]);
    expect(
      validateRecordViewSpec(
        "页",
        {
          ...base,
          keyColumn: "批次号",
          headerFields: [
            { column: "产品名称" },
            { column: "产品名称" }, // 重复
            { column: "不存在" }, // 不在主集列快照
            { column: "地块编号", label: "x".repeat(101) }, // label 超长
          ],
        },
        columnsOf,
      ),
    ).toEqual(
      expect.arrayContaining([
        "headerColumnMissing",
        "headerColumnDuplicate",
        "headerLabelTooLong",
      ]),
    );
  });

  it("sections：标题/数据集/日期列形状与成员、limit 1..500、filters 形状与 {fromField} 引用", () => {
    const base: RecordViewSpecBody = {
      datasetId: "ds-batch",
      keyColumn: "批次号",
      headerFields: [],
      sections: [
        {
          title: "",
          datasetId: "ds-spray",
          dateColumn: "不存在日期列",
          filters: [
            { column: "地块编号", op: "eq", value: { fromField: "不存在主列" } },
            { column: "", op: "eq", value: 1 }, // 列空
            { column: "用量", op: "in", value: [1, "a"] }, // 合法数组
          ],
          limit: 0, // 越界
        },
      ],
    };
    expect(validateRecordViewSpec("页", base, columnsOf)).toEqual(
      expect.arrayContaining([
        "sectionBad",
        "sectionDateColumnMissing",
        "sectionLimitBad",
        "filterBad",
        "fromFieldMissing",
      ]),
    );
    // 修正后通过（fromField 引用主记录列、日期列在段集列快照内）。
    const ok: RecordViewSpecBody = {
      ...base,
      sections: [
        {
          title: "打药",
          datasetId: "ds-spray",
          dateColumn: "日期",
          filters: [
            { column: "地块编号", op: "eq", value: { fromField: "地块编号" } },
            { column: "用量", op: "in", value: [1, "a"] },
          ],
          limit: 500,
        },
      ],
    };
    expect(validateRecordViewSpec("页", ok, columnsOf)).toEqual([]);
  });

  it("数量上限：headerFields ≤50、sections ≤10、filters ≤10；未知数据集跳过列成员（服务端兜底）", () => {
    const many = (n: number) =>
      Array.from({ length: n }, (_, i) => ({ column: `c${i}` }));
    const base: RecordViewSpecBody = {
      datasetId: "ds-batch",
      keyColumn: "批次号",
      headerFields: many(51) as RecordViewSpecBody["headerFields"],
      sections: [],
    };
    // 不传 columnsOf（列快照未知）：只裁数量与形状（成员校验交服务端）。
    expect(validateRecordViewSpec("页", base)).toEqual(["headerFieldsTooMany"]);
    // 未知段数据集（已删）：columnsOf 返回 null → 不查日期列成员，只查形状。
    const unknownDataset: RecordViewSpecBody = {
      ...base,
      headerFields: [],
      sections: [
        {
          title: "t",
          datasetId: "ds-deleted",
          dateColumn: "任意列",
          filters: Array.from({ length: 11 }, () => ({
            column: "c",
            op: "eq" as const,
            value: 1,
          })),
        },
      ],
    };
    expect(validateRecordViewSpec("页", unknownDataset, columnsOf)).toEqual([
      "filtersTooMany",
    ]);
  });
});

describe("recordViewSpec：取数响应归一", () => {
  it("found=true：header/sections 逐条收窄（label 缺省=列名、非法行丢弃、error 保留）", () => {
    const payload = normalizeRecordViewPayload({
      title: "溯源页",
      version: 3,
      expires_at: "2026-11-01T00:00:00",
      found: true,
      header: [
        { label: "产品", column: "产品名称", value: 12.5 },
        { column: "产地", value: "云南" }, // 缺 label → 列名
        "bad", // 丢弃
      ],
      sections: [
        {
          title: "打药",
          dateColumn: "日期",
          rows: [{ 日期: "2026-03-01", 用量: 1 }, "bad-row"],
          truncated: true,
        },
        {
          title: "播种",
          dateColumn: "日期",
          rows: [],
          truncated: false,
          error: "列「日期」已不在数据集列清单中",
        },
      ],
    });
    expect(payload.found).toBe(true);
    expect(payload.title).toBe("溯源页");
    expect(payload.version).toBe(3);
    expect(payload.expiresAt).toBe("2026-11-01T00:00:00");
    expect(payload.header).toEqual([
      { label: "产品", column: "产品名称", value: 12.5 },
      { label: "产地", column: "产地", value: "云南" },
    ]);
    expect(payload.sections[0]).toEqual({
      title: "打药",
      dateColumn: "日期",
      rows: [{ 日期: "2026-03-01", 用量: 1 }],
      truncated: true,
    });
    expect(payload.sections[1]?.error).toBe("列「日期」已不在数据集列清单中");
  });

  it("found=false：「无此记录」态（header/sections 空、不判错误）；expires_at 恒 string|null", () => {
    const payload = normalizeRecordViewPayload({
      title: "溯源页",
      version: 1,
      expires_at: null,
      found: false,
    });
    expect(payload.found).toBe(false);
    expect(payload.header).toEqual([]);
    expect(payload.sections).toEqual([]);
    expect(payload.expiresAt).toBeNull();
    // 非 found 真值（如缺失）一律按 false 归一。
    expect(normalizeRecordViewPayload({}).found).toBe(false);
    expect(normalizeRecordViewPayload(null).expiresAt).toBeNull();
  });
});

describe("recordViewSpec：时间轴排序与单元格呈现", () => {
  it("按日期列升序（字符串字典序/数值串数值比），空值排末尾；入参不动", () => {
    const rows = [
      { d: "2026-03-02", v: 2 },
      { d: null, v: 9 },
      { d: "2026-01-05", v: 1 },
      { d: "2026-03-01", v: 0 },
      { v: 8 },
    ];
    const sorted = sortRowsByDate(rows, "d");
    expect(sorted.map((r) => r.v)).toEqual([1, 0, 2, 9, 8]);
    // 原数组顺序不动。
    expect(rows[0]!.v).toBe(2);
    // 数值串走数值比较（10 > 9，字典序则相反）。
    const numeric = [{ d: "10" }, { d: "9" }];
    expect(sortRowsByDate(numeric, "d").map((r) => r.d)).toEqual(["9", "10"]);
  });

  it("recordCellText：null/空串 → 占位「—」，其余 String()（数字原样字符串化）", () => {
    expect(recordCellText(null)).toBe("—");
    expect(recordCellText(undefined)).toBe("—");
    expect(recordCellText("")).toBe("—");
    expect(recordCellText(12.5)).toBe("12.5");
    expect(recordCellText("云南")).toBe("云南");
    expect(recordCellText(false)).toBe("false");
  });
});

describe("record-view URL 组装（api.ts 纯函数）", () => {
  it("数据端点：t/k 进 query（供公开取数 fetch；URLSearchParams 口径）", () => {
    expect(sharedRecordViewDataUrl(11, "tok-abc", "B-2026")).toBe(
      "/api/v1/share/record-view/11?t=tok-abc&k=B-2026",
    );
  });

  it("公开页完整链接：origin + #/share/record-view/{id}，t/k 分量 urlencode", () => {
    expect(
      buildRecordViewSharePageUrl("https://wire.example", 11, "tok abc", "批次 01"),
    ).toBe(
      "https://wire.example/#/share/record-view/11?t=tok%20abc&k=%E6%89%B9%E6%AC%A1%2001",
    );
  });
});
