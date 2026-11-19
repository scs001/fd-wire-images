package postgres

import (
	"context"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/jackc/pgx/v5"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// 真实 PostgreSQL 集成测试：未设 FDWIRE_TEST_PG_DSN 时跳过（CI 默认跳过，
// 本地/容器化源库可用时设置该环境变量即可跑通）。
//
//	initdb -D /tmp/fdwire-pg/data -U fdwire --auth=trust
//	pg_ctl -D /tmp/fdwire-pg/data -o "-p 15432 -k /tmp/fdwire-pg" start
//	FDWIRE_TEST_PG_DSN='postgres://fdwire@127.0.0.1:15432/fdwire_test' go test ./internal/adapters/postgres/
func pgDSN(t *testing.T) string {
	t.Helper()
	dsn := os.Getenv("FDWIRE_TEST_PG_DSN")
	if dsn == "" {
		t.Skip("未设置 FDWIRE_TEST_PG_DSN，跳过真实 PostgreSQL 集成测试")
	}
	return dsn
}

func prepareSales(t *testing.T, ctx context.Context, conn *pgx.Conn) {
	t.Helper()
	statements := []string{
		`DROP TABLE IF EXISTS it_sales`,
		`CREATE TABLE it_sales (
			id BIGINT PRIMARY KEY,
			amount NUMERIC(10,2),
			region TEXT,
			flag BOOLEAN,
			updated_at TIMESTAMPTZ,
			raw BYTEA,
			note TEXT
		)`,
		`INSERT INTO it_sales (id, amount, region, flag, updated_at, raw, note) VALUES
			(1, 12.50, '华东', true,  '2026-10-06 04:00:00+00', '\x6869'::bytea, '首单'),
			(2, 7.00,  '华北', false, '2026-10-06 05:30:00+00', NULL, NULL),
			(3, 3.25,  '华南', true,  '2026-10-06 06:00:00+00', '\x61'::bytea, '加急')`,
	}
	for _, stmt := range statements {
		if _, err := conn.Exec(ctx, stmt); err != nil {
			t.Fatalf("准备数据失败: %v\n%s", err, stmt)
		}
	}
}

func TestIntegrationColumnsAndPrimaryKeys(t *testing.T) {
	dsn := pgDSN(t)
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer conn.Close(ctx)
	prepareSales(t, ctx, conn)

	a := New(dsn, "it_sales", 2000)
	cols, err := a.Columns(ctx)
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	want := []adapters.Column{
		{Name: "id", Type: adapters.TypeInteger},
		{Name: "amount", Type: adapters.TypeReal},
		{Name: "region", Type: adapters.TypeText},
		{Name: "flag", Type: adapters.TypeText},
		{Name: "updated_at", Type: adapters.TypeText},
		{Name: "raw", Type: adapters.TypeText},
		{Name: "note", Type: adapters.TypeText},
	}
	if !reflect.DeepEqual(cols, want) {
		t.Fatalf("Columns = %#v\nwant %#v", cols, want)
	}
	keys, err := a.PrimaryKeys(ctx)
	if err != nil {
		t.Fatalf("PrimaryKeys: %v", err)
	}
	if !reflect.DeepEqual(keys, []string{"id"}) {
		t.Fatalf("PrimaryKeys = %v, want [id]", keys)
	}
	// schema 限定写法同样可用。
	schemaCols, err := New(dsn, "public.it_sales", 2000).Columns(ctx)
	if err != nil || len(schemaCols) != len(want) {
		t.Fatalf("schema 限定列读取失败: %v %d", err, len(schemaCols))
	}
}

func TestIntegrationSnapshotValues(t *testing.T) {
	dsn := pgDSN(t)
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer conn.Close(ctx)
	prepareSales(t, ctx, conn)

	a := New(dsn, "it_sales", 2) // 批次 2 → 2 + 1
	var batches [][]adapters.Row
	if err := a.Snapshot(ctx, func(rows []adapters.Row) error {
		batches = append(batches, rows)
		return nil
	}); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(batches) != 2 || len(batches[0]) != 2 || len(batches[1]) != 1 {
		t.Fatalf("批次切分异常: %d/%v", len(batches), len(batches[0]))
	}
	first := batches[0][0]
	if !reflect.DeepEqual(first["id"], int64(1)) {
		t.Errorf("id = %#v, want int64(1)", first["id"])
	}
	if !reflect.DeepEqual(first["amount"], 12.5) {
		t.Errorf("NUMERIC 应转数字: %#v", first["amount"])
	}
	if first["region"] != "华东" {
		t.Errorf("region = %#v", first["region"])
	}
	if first["flag"] != "true" {
		t.Errorf("布尔按 TEXT = %#v", first["flag"])
	}
	if first["updated_at"] != "2026-10-06T04:00:00Z" {
		t.Errorf("时间应为 UTC ISO8601: %#v", first["updated_at"])
	}
	if first["raw"] != "hi" {
		t.Errorf("bytea → string: %#v", first["raw"])
	}
	second := batches[0][1]
	if second["note"] != nil || second["raw"] != nil {
		t.Errorf("NULL 应为 nil: %#v", second)
	}
}

func TestIntegrationNoPrimaryKeyTable(t *testing.T) {
	dsn := pgDSN(t)
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer conn.Close(ctx)
	if _, err := conn.Exec(ctx, `DROP TABLE IF EXISTS it_nopk; CREATE TABLE it_nopk (a TEXT, b BIGINT)`); err != nil {
		t.Fatalf("准备数据失败: %v", err)
	}
	if _, err := conn.Exec(ctx, `INSERT INTO it_nopk VALUES ('x', 1), ('y', 2)`); err != nil {
		t.Fatalf("插入失败: %v", err)
	}
	a := New(dsn, "it_nopk", 2000)
	keys, err := a.PrimaryKeys(ctx)
	if err != nil {
		t.Fatalf("PrimaryKeys: %v", err)
	}
	if len(keys) != 0 {
		t.Fatalf("无主键表应返回空（引擎走行哈希锚），得到 %v", keys)
	}
	var rows []adapters.Row
	if err := a.Snapshot(ctx, func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(rows) != 2 {
		t.Fatalf("应读到 2 行，得到 %d", len(rows))
	}
}

func TestIntegrationMissingTableIsClearError(t *testing.T) {
	dsn := pgDSN(t)
	ctx := context.Background()
	if _, err := New(dsn, "it_does_not_exist", 2000).Columns(ctx); err == nil {
		t.Fatal("表不存在应报错")
	}
	// 受限账号/不可达地址：错误应带原因且不 panic。
	a := New("postgres://nobody@127.0.0.1:1/nope", "t", 2000)
	if _, err := a.Columns(ctx); err == nil {
		t.Fatal("不可达地址应报错")
	}
	if err := a.Snapshot(ctx, func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("不可达地址的 Snapshot 应报错")
	}
}

func TestIntegrationIncrementalWindow(t *testing.T) {
	dsn := pgDSN(t)
	ctx := context.Background()
	conn, err := pgx.Connect(ctx, dsn)
	if err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	defer conn.Close(ctx)
	prepareSales(t, ctx, conn)

	a := New(dsn, "it_sales", 2000)

	// TIMESTAMPTZ 水位：CAST 后比较，只拉 05:00 之后的行且按水位 ASC。
	var rows []adapters.Row
	if err := a.Incremental(ctx, "updated_at", "2026-10-06T05:00:00Z", func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Incremental(timestamptz): %v", err)
	}
	if len(rows) != 2 || rows[0]["id"] != int64(2) || rows[1]["id"] != int64(3) {
		t.Fatalf("时间水位窗口 = %#v, want ids [2 3]（ASC）", rows)
	}
	if rows[1]["updated_at"] != "2026-10-06T06:00:00Z" {
		t.Fatalf("时间值收敛 = %#v", rows[1]["updated_at"])
	}

	// BIGINT 水位：字符串参数 CAST 到 int8。
	rows = nil
	if err := a.Incremental(ctx, "id", "1", func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Incremental(int8): %v", err)
	}
	if len(rows) != 2 || rows[0]["id"] != int64(2) || rows[1]["id"] != int64(3) {
		t.Fatalf("整数水位窗口 = %#v, want ids [2 3]", rows)
	}

	// 大整数水位（>2^53）无损：整表超过该值的行全部拉回。
	if _, err := conn.Exec(ctx, `INSERT INTO it_sales (id, amount) VALUES (9007199254740994, 1.5)`); err != nil {
		t.Fatalf("插入大整数失败: %v", err)
	}
	rows = nil
	if err := a.Incremental(ctx, "id", "9007199254740993", func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Incremental(大整数): %v", err)
	}
	if len(rows) != 1 || rows[0]["id"] != int64(9007199254740994) {
		t.Fatalf("大整数水位窗口 = %#v", rows)
	}

	// 空窗口：水位之后的零行。
	calls := 0
	if err := a.Incremental(ctx, "updated_at", "2026-10-07T00:00:00Z", func([]adapters.Row) error {
		calls++
		return nil
	}); err != nil {
		t.Fatalf("Incremental(空窗口): %v", err)
	}
	if calls != 0 {
		t.Fatalf("空窗口不应回调，实际 %d 次", calls)
	}

	// 水位列不存在：清晰的本地错误（而非驱动报错）。
	err = a.Incremental(ctx, "no_such_col", "0", func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "no_such_col") {
		t.Fatalf("水位列不存在应报清晰错误: %v", err)
	}
}
