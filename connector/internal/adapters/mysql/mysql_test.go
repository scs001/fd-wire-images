package mysql

import (
	"context"
	"testing"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

func TestDatabaseTableParsing(t *testing.T) {
	cases := []struct {
		ref       string
		wantDB    string
		wantTable string
		wantErr   bool
	}{
		{"sales", "", "sales", false},
		{"shop.sales", "shop", "sales", false},
		{"a.b.c", "", "", true},
		{"", "", "", true},
	}
	for _, c := range cases {
		db, table, err := New("user:pass@tcp(h:3306)/sales", c.ref, 0).databaseTable()
		if c.wantErr {
			if err == nil {
				t.Errorf("%q 应报错", c.ref)
			}
			continue
		}
		if err != nil || db != c.wantDB || table != c.wantTable {
			t.Errorf("%q → (%q, %q, %v)", c.ref, db, table, err)
		}
	}
}

func TestColumnsFailsFastWithoutTable(t *testing.T) {
	a := New("user:pass@tcp(127.0.0.1:1)/db", "", 0)
	if _, err := a.Columns(context.Background()); err == nil {
		t.Fatal("缺 source_ref 不应尝试连接（立即报错）")
	}
	if _, err := a.PrimaryKeys(context.Background()); err == nil {
		t.Fatal("缺 source_ref 的 PrimaryKeys 应报错")
	}
	if err := a.Snapshot(context.Background(), func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("缺 source_ref 的 Snapshot 应报错")
	}
}

func TestQuoteQualifiedEscapesIdentifiers(t *testing.T) {
	if got := quoteQualified("shop", "sales"); got != "`shop`.`sales`" {
		t.Errorf("quoteQualified = %s", got)
	}
	if got := quoteQualified("", "sales"); got != "`sales`" {
		t.Errorf("无库名时只引表名: %s", got)
	}
	// 标识符整体加反引号、内部反引号加倍：注入尝试只能落成一个（不存在的）安全标识符。
	injected := quoteQualified("shop", "sales`; DROP TABLE x; --")
	if injected != "`shop`.`sales``; DROP TABLE x; --`" {
		t.Errorf("标识符转义结果异常: %s", injected)
	}
}

func TestNormalizeValue(t *testing.T) {
	if got := NormalizeValue([]byte("42")); got != "42" {
		t.Errorf("raw bytes → string 失败: %#v", got)
	}
	if got := NormalizeValue(nil); got != nil {
		t.Errorf("NULL 应保持 nil: %#v", got)
	}
	if got := NormalizeValue(int64(7)); got != int64(7) {
		t.Errorf("整数应保持整数: %#v", got)
	}
}

func TestNewDefaultsBatchSize(t *testing.T) {
	if got := New("dsn", "t", -1).BatchSize; got != 2000 {
		t.Errorf("默认批次 = %d, want 2000", got)
	}
}

func TestIncrementalQueryShape(t *testing.T) {
	cases := []struct {
		database, table, column string
		want                    string
	}{
		{"", "sales", "updated_at",
			"SELECT * FROM `sales` WHERE `updated_at` > ? ORDER BY `updated_at`"},
		{"shop", "sales", "id",
			"SELECT * FROM `shop`.`sales` WHERE `id` > ? ORDER BY `id`"},
	}
	for _, c := range cases {
		if got := incrementalQuery(c.database, c.table, c.column); got != c.want {
			t.Errorf("incrementalQuery = %s\nwant %s", got, c.want)
		}
	}
	// 注入尝试只能落成一个（不存在的）被反引号包裹的安全标识符。
	injected := incrementalQuery("shop", "sales", "id`; DROP TABLE x; --")
	if injected != "SELECT * FROM `shop`.`sales` WHERE `id``; DROP TABLE x; --` > ? ORDER BY `id``; DROP TABLE x; --`" {
		t.Errorf("水位列注入转义异常: %s", injected)
	}
}

func TestIncrementalFailsFastWithoutTable(t *testing.T) {
	a := New("user:pass@tcp(127.0.0.1:1)/db", "", 0)
	if err := a.Incremental(context.Background(), "id", "0", func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("缺 source_ref 的 Incremental 应立即报错（不连库）")
	}
}
