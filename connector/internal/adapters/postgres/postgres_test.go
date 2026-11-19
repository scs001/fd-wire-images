package postgres

import (
	"context"
	"math/big"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgtype"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

func TestSchemaTableParsing(t *testing.T) {
	cases := []struct {
		ref        string
		wantSchema string
		wantTable  string
		wantErr    bool
	}{
		{"sales", "", "sales", false},
		{"public.sales", "public", "sales", false},
		{"a.b.c", "", "", true},
		{"", "", "", true},
	}
	for _, c := range cases {
		schema, table, err := New("postgres://h/db", c.ref, 0).schemaTable()
		if c.wantErr {
			if err == nil {
				t.Errorf("%q 应报错", c.ref)
			}
			continue
		}
		if err != nil || schema != c.wantSchema || table != c.wantTable {
			t.Errorf("%q → (%q, %q, %v)", c.ref, schema, table, err)
		}
	}
}

func TestColumnsFailsFastWithoutTable(t *testing.T) {
	a := New("postgres://user:pass@127.0.0.1:1/db", "", 0)
	if _, err := a.Columns(context.Background()); err == nil {
		t.Fatal("缺 source_ref 不应尝试连接（立即报错）")
	}
	if err := a.Snapshot(context.Background(), func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("缺 source_ref 的 Snapshot 应报错")
	}
}

func TestQuoteQualifiedEscapesIdentifiers(t *testing.T) {
	if got := quoteQualified("public", "sales"); got != `"public"."sales"` {
		t.Errorf("quoteQualified = %s", got)
	}
	if got := quoteQualified("", "sales"); got != `"sales"` {
		t.Errorf("无 schema 时只引表名: %s", got)
	}
	// 标识符整体加引号、内部双引号加倍：注入尝试只能落成一个（不存在的）安全标识符。
	injected := quoteQualified("public", `sales"; DROP TABLE x; --`)
	if injected != `"public"."sales""; DROP TABLE x; --"` {
		t.Errorf("标识符转义结果异常: %s", injected)
	}
}

func TestNormalizeValue(t *testing.T) {
	when := time.Date(2026, 10, 6, 12, 0, 0, 0, time.FixedZone("CST", 8*3600))
	if got := NormalizeValue(when, adapters.TypeText); got != "2026-10-06T04:00:00Z" {
		t.Errorf("时间应转 UTC ISO8601: %#v", got)
	}
	if got := NormalizeValue([]byte("abc"), adapters.TypeText); got != "abc" {
		t.Errorf("bytea → string 失败: %#v", got)
	}
	if got := NormalizeValue(nil, adapters.TypeText); got != nil {
		t.Errorf("NULL 应保持 nil: %#v", got)
	}
	if got := NormalizeValue(int64(5), adapters.TypeInteger); got != int64(5) {
		t.Errorf("int8 → int64 失败: %#v", got)
	}
	if got := NormalizeValue(true, adapters.TypeText); got != "true" {
		t.Errorf("布尔按 TEXT 处理: %#v", got)
	}
	uuid := [16]byte{0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef, 0x01, 0x23, 0x45, 0x67, 0x89, 0xab, 0xcd, 0xef}
	if got := NormalizeValue(uuid, adapters.TypeText); got != "01234567-89ab-cdef-0123-456789abcdef" {
		t.Errorf("uuid → 字符串失败: %#v", got)
	}
	// NUMERIC 按目标三型转数字，而不是落成驱动结构体文本。
	numeric := pgtype.Numeric{Int: big.NewInt(1250), Exp: -2, Valid: true}
	if got := NormalizeValue(numeric, adapters.TypeReal); got != 12.5 {
		t.Errorf("numeric → REAL 失败: %#v", got)
	}
	if got := NormalizeValue(numeric, adapters.TypeInteger); got != int64(12) {
		t.Errorf("numeric → INTEGER 失败: %#v", got)
	}
	if got := NormalizeValue(numeric, adapters.TypeText); got == nil || got == "" {
		t.Errorf("numeric → TEXT 应有文本值: %#v", got)
	}
}

func TestNewDefaultsBatchSize(t *testing.T) {
	if got := New("dsn", "t", 0).BatchSize; got != 2000 {
		t.Errorf("默认批次 = %d, want 2000", got)
	}
	if got := New("dsn", "t", 500).BatchSize; got != 500 {
		t.Errorf("批次应可覆盖: %d", got)
	}
}

func TestIncrementalQueryShape(t *testing.T) {
	cases := []struct {
		schema, table, column, native string
		want                          string
	}{
		{"", "sales", "updated_at", "timestamptz",
			`SELECT * FROM "sales" WHERE "updated_at" > CAST($1 AS "timestamptz") ORDER BY "updated_at"`},
		{"public", "sales", "id", "int8",
			`SELECT * FROM "public"."sales" WHERE "id" > CAST($1 AS "int8") ORDER BY "id"`},
		{"public", "sales", "amount", "numeric",
			`SELECT * FROM "public"."sales" WHERE "amount" > CAST($1 AS "numeric") ORDER BY "amount"`},
	}
	for _, c := range cases {
		if got := incrementalQuery(c.schema, c.table, c.column, c.native); got != c.want {
			t.Errorf("incrementalQuery = %s\nwant %s", got, c.want)
		}
	}
	// 注入尝试只能落成一个（不存在的）被引号包裹的安全标识符。
	injected := incrementalQuery("public", "sales", `id"; DROP TABLE x; --`, "int8")
	if injected != `SELECT * FROM "public"."sales" WHERE "id""; DROP TABLE x; --" > CAST($1 AS "int8") ORDER BY "id""; DROP TABLE x; --"` {
		t.Errorf("水位列注入转义异常: %s", injected)
	}
}

func TestNativeTypeOf(t *testing.T) {
	defs := []columnDef{
		{Name: "id", Native: "int8"},
		{Name: "updated_at", Native: "timestamptz"},
	}
	if got, ok := nativeTypeOf(defs, "updated_at"); !ok || got != "timestamptz" {
		t.Errorf("nativeTypeOf(updated_at) = %q, %v", got, ok)
	}
	if _, ok := nativeTypeOf(defs, "nope"); ok {
		t.Error("不存在的列应返回 false")
	}
}

func TestIncrementalFailsFastWithoutTable(t *testing.T) {
	a := New("postgres://user:pass@127.0.0.1:1/db", "", 0)
	if err := a.Incremental(context.Background(), "id", "0", func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("缺 source_ref 的 Incremental 应立即报错（不连库）")
	}
}
