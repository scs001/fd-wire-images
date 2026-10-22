package file

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/xuri/excelize/v2"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

const csvDir = "testdata"

func TestColumnsFromCSV(t *testing.T) {
	a := New(csvDir, "sales.csv", 0)
	cols, err := a.Columns(context.Background())
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	want := []adapters.Column{
		{Name: "id", Type: adapters.TypeInteger},
		{Name: "amount", Type: adapters.TypeReal},
		{Name: "region", Type: adapters.TypeText},
		{Name: "note", Type: adapters.TypeText},
	}
	if !reflect.DeepEqual(cols, want) {
		t.Fatalf("Columns = %#v, want %#v", cols, want)
	}
	keys, err := a.PrimaryKeys(context.Background())
	if err != nil {
		t.Fatalf("PrimaryKeys: %v", err)
	}
	if len(keys) != 0 {
		t.Fatalf("文件源无主键（引擎走行哈希锚），得到 %v", keys)
	}
}

func TestSnapshotCSVTypesAndBatching(t *testing.T) {
	a := New(csvDir, "sales.csv", 2) // 批次 2：5 行 → 2/2/1
	var batches [][]adapters.Row
	err := a.Snapshot(context.Background(), func(rows []adapters.Row) error {
		batches = append(batches, rows)
		return nil
	})
	if err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(batches) != 3 || len(batches[0]) != 2 || len(batches[1]) != 2 || len(batches[2]) != 1 {
		t.Fatalf("批次切分 = %v", batchSizes(batches))
	}
	first := batches[0][0]
	if !reflect.DeepEqual(first["id"], int64(1)) {
		t.Errorf("id 应为 int64(1)，得到 %#v", first["id"])
	}
	if !reflect.DeepEqual(first["amount"], 12.5) {
		t.Errorf("amount 应为 12.5，得到 %#v", first["amount"])
	}
	if first["region"] != "华东" {
		t.Errorf("region = %#v", first["region"])
	}
	if first["note"] != "首单" {
		t.Errorf("note = %#v", first["note"])
	}
	if second := batches[0][1]; second["note"] != nil {
		t.Errorf("空单元格应为 NULL，得到 %#v", second["note"])
	}
}

func TestSnapshotReportsBrokenValue(t *testing.T) {
	// 抽样只覆盖前 200 行；第 205 行才出现的非数值必须报错，不能静默降级。
	dir := t.TempDir()
	var builder strings.Builder
	builder.WriteString("id,amount\n")
	for i := 1; i <= 204; i++ {
		fmt.Fprintf(&builder, "%d,%d.5\n", i, i)
	}
	builder.WriteString("205,oops\n")
	if err := os.WriteFile(filepath.Join(dir, "broken.csv"), []byte(builder.String()), 0o600); err != nil {
		t.Fatal(err)
	}
	a := New(dir, "broken.csv", 2000)
	err := a.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil {
		t.Fatal("样本外出现的非数值应报错")
	}
	if !strings.Contains(err.Error(), "oops") {
		t.Fatalf("错误应指明问题值: %v", err)
	}
}

func TestColumnsFromExcel(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "report.xlsx")
	f := excelize.NewFile()
	defer f.Close()
	sheet := f.GetSheetName(0)
	rows := [][]any{
		{"code", "qty", "price", "memo"},
		{"A-1", 3, 10.5, "正常"},
		{"B-2", 0, 0.25, ""},
		{"C-3", 12, 99.99, "退货"},
	}
	for r, row := range rows {
		for c, value := range row {
			cell, err := excelize.CoordinatesToCellName(c+1, r+1)
			if err != nil {
				t.Fatal(err)
			}
			if err := f.SetCellValue(sheet, cell, value); err != nil {
				t.Fatal(err)
			}
		}
	}
	if err := f.SaveAs(path); err != nil {
		t.Fatal(err)
	}

	a := New(dir, "report.xlsx", 2000)
	cols, err := a.Columns(context.Background())
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	want := []adapters.Column{
		{Name: "code", Type: adapters.TypeText},
		{Name: "qty", Type: adapters.TypeInteger},
		{Name: "price", Type: adapters.TypeReal},
		{Name: "memo", Type: adapters.TypeText},
	}
	if !reflect.DeepEqual(cols, want) {
		t.Fatalf("Excel Columns = %#v, want %#v", cols, want)
	}

	var got []adapters.Row
	if err := a.Snapshot(context.Background(), func(rows []adapters.Row) error {
		got = append(got, rows...)
		return nil
	}); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(got) != 3 {
		t.Fatalf("应读到 3 行数据，得到 %d", len(got))
	}
	if !reflect.DeepEqual(got[0]["qty"], int64(3)) || !reflect.DeepEqual(got[2]["price"], 99.99) {
		t.Fatalf("Excel 值转换异常: %#v", got[0])
	}
	if got[1]["memo"] != nil {
		t.Fatalf("空单元格应为 NULL，得到 %#v", got[1]["memo"])
	}
}

func TestFilePathGuards(t *testing.T) {
	if _, err := New(csvDir, "../etc/passwd", 0).FilePath(); err == nil {
		t.Fatal("应拒绝带路径分隔符的 source_ref")
	}
	if _, err := New(csvDir, "sub/sales.csv", 0).FilePath(); err == nil {
		t.Fatal("应拒绝子目录 source_ref")
	}
	if _, err := New(csvDir, "", 0).FilePath(); err == nil {
		t.Fatal("空 source_ref 应报错")
	}
	if _, err := New("", "sales.csv", 0).FilePath(); err == nil {
		t.Fatal("缺少本地 path 应报错")
	}
	if _, err := New(csvDir, "sales.csv", 0).FilePath(); err != nil {
		t.Fatalf("正常文件名不应报错: %v", err)
	}
}

func TestSnapshotErrors(t *testing.T) {
	ctx := context.Background()
	if err := New(csvDir, "missing.csv", 0).Snapshot(ctx, func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("文件不存在应报错")
	}
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "data.json"), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := New(dir, "data.json", 0).Snapshot(ctx, func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("不支持的文件类型应报错")
	}
	if err := New(dir, ".", 0).Snapshot(ctx, func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("目录应报错")
	}
}

func TestSnapshotSkipsBlankLines(t *testing.T) {
	dir := t.TempDir()
	content := "\xEF\xBB\xBFid,name\n1,甲\n,\n2,乙\n"
	// 首行带 BOM：应被剥掉，列名仍是 id。
	content = "\xEF\xBB\xBFid,name\n1,甲\n,\n2,乙\n"
	if err := os.WriteFile(filepath.Join(dir, "bom.csv"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	a := New(dir, "bom.csv", 0)
	cols, err := a.Columns(context.Background())
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	if cols[0].Name != "id" {
		t.Fatalf("BOM 未剥离，首列名 = %q", cols[0].Name)
	}
	var rows []adapters.Row
	if err := a.Snapshot(context.Background(), func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("全空行应跳过，得到 %d 行", len(rows))
	}
}

func batchSizes(batches [][]adapters.Row) []int {
	sizes := make([]int, 0, len(batches))
	for _, b := range batches {
		sizes = append(sizes, len(b))
	}
	return sizes
}
