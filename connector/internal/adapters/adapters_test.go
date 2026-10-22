package adapters

import (
	"reflect"
	"testing"
)

func TestMapSQLType(t *testing.T) {
	cases := map[string]string{
		// PostgreSQL udt_name / data_type
		"int2": TypeInteger, "int4": TypeInteger, "int8": TypeInteger,
		"serial": TypeInteger, "bigserial": TypeInteger, "oid": TypeInteger,
		"numeric": TypeReal, "float4": TypeReal, "float8": TypeReal, "money": TypeReal,
		"varchar": TypeText, "text": TypeText, "bpchar": TypeText,
		"timestamptz": TypeText, "bool": TypeText, "uuid": TypeText, "jsonb": TypeText,
		// MySQL DatabaseTypeName / information_schema.data_type
		"INT": TypeInteger, "BIGINT": TypeInteger, "TINYINT": TypeInteger,
		"MEDIUMINT": TypeInteger, "INT UNSIGNED": TypeInteger,
		"DECIMAL": TypeReal, "FLOAT": TypeReal, "DOUBLE": TypeReal,
		"VARCHAR": TypeText, "DATETIME": TypeText, "ENUM": TypeText, "JSON": TypeText,
		"DECIMAL(10,2)": TypeReal, "VARCHAR(255)": TypeText,
		// 未知类型一律保守 TEXT（契约 §1）
		"geography": TypeText, "": TypeText, "weird_type": TypeText,
	}
	for in, want := range cases {
		if got := MapSQLType(in); got != want {
			t.Errorf("MapSQLType(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestInferType(t *testing.T) {
	cases := []struct {
		sample []string
		want   string
	}{
		{[]string{"1", "2", " 3 "}, TypeInteger},
		{[]string{"1", "2.5"}, TypeReal},
		{[]string{"1.5", "-0.25", "1e3"}, TypeReal},
		{[]string{"1", "x"}, TypeText},
		{[]string{"", "", ""}, TypeText},   // 全空 → 保守 TEXT
		{nil, TypeText},                    // 无样本 → 保守 TEXT
		{[]string{"", "42"}, TypeInteger},  // 空值不影响判定
		{[]string{"1,000", "2"}, TypeText}, // 千分位不是数字
		{[]string{"2026-10-06"}, TypeText}, // 日期归 TEXT（引擎三型）
	}
	for _, c := range cases {
		if got := InferType(c.sample); got != c.want {
			t.Errorf("InferType(%v) = %q, want %q", c.sample, got, c.want)
		}
	}
}

func TestConvertString(t *testing.T) {
	intVal, err := ConvertString(" 42 ", TypeInteger)
	if err != nil || !reflect.DeepEqual(intVal, int64(42)) {
		t.Fatalf("INTEGER 转换 = %#v, %v", intVal, err)
	}
	realVal, err := ConvertString("2.5", TypeReal)
	if err != nil || !reflect.DeepEqual(realVal, 2.5) {
		t.Fatalf("REAL 转换 = %#v, %v", realVal, err)
	}
	textVal, err := ConvertString("华东", TypeText)
	if err != nil || textVal != "华东" {
		t.Fatalf("TEXT 转换 = %#v, %v", textVal, err)
	}
	for _, typ := range []string{TypeInteger, TypeReal, TypeText} {
		if v, err := ConvertString("  ", typ); err != nil || v != nil {
			t.Fatalf("空值应转 NULL：type=%s got=%#v err=%v", typ, v, err)
		}
	}
	if _, err := ConvertString("N/A", TypeInteger); err == nil {
		t.Fatal("INTEGER 列的非数字值应报错（不静默写脏数据）")
	}
}

func TestCoerce(t *testing.T) {
	if got := Coerce([]byte("42"), TypeInteger); !reflect.DeepEqual(got, int64(42)) {
		t.Errorf("[]byte → INTEGER = %#v", got)
	}
	if got := Coerce("42", TypeReal); !reflect.DeepEqual(got, float64(42)) {
		t.Errorf("string → REAL = %#v", got)
	}
	if got := Coerce(int32(7), TypeInteger); !reflect.DeepEqual(got, int64(7)) {
		t.Errorf("int32 → INTEGER = %#v", got)
	}
	if got := Coerce(true, TypeText); got != "true" {
		t.Errorf("bool → TEXT = %#v", got)
	}
	if got := Coerce([]byte("华东"), TypeText); got != "华东" {
		t.Errorf("[]byte → TEXT = %#v", got)
	}
	if got := Coerce(nil, TypeText); got != nil {
		t.Errorf("nil → %#v", got)
	}
	if got := Coerce("not-a-number", TypeInteger); got != "not-a-number" {
		t.Errorf("不可解析值应原样保留 = %#v", got)
	}
	if got := Coerce(struct{ A int }{1}, TypeText); got != "{1}" {
		t.Errorf("未知类型 → TEXT = %#v", got)
	}
}

func TestBatcherFlushes(t *testing.T) {
	var got []int
	b := NewBatcher(2, func(rows []Row) error {
		got = append(got, len(rows))
		return nil
	})
	for i := 0; i < 5; i++ {
		if err := b.Add(Row{"i": i}); err != nil {
			t.Fatal(err)
		}
	}
	if err := b.Flush(); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, []int{2, 2, 1}) {
		t.Fatalf("批次切分 = %v, want [2 2 1]", got)
	}
	if err := b.Flush(); err != nil {
		t.Fatal(err)
	}
	if len(got) != 3 {
		t.Fatalf("空缓冲不应触发回调: %v", got)
	}
	if NewBatcher(0, nil).size != 2000 {
		t.Fatalf("默认批次应为 2000（契约 §3.3）")
	}
}

func TestColumnNames(t *testing.T) {
	names := ColumnNames([]Column{{Name: "a", Type: TypeText}, {Name: "b", Type: TypeInteger}})
	if !reflect.DeepEqual(names, []string{"a", "b"}) {
		t.Fatalf("ColumnNames = %v", names)
	}
}
