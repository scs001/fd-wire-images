package mysql

import (
	"context"
	"database/sql"
	"os"
	"reflect"
	"testing"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// 真实 MySQL 集成测试：未设 FDWIRE_TEST_MYSQL_DSN 时跳过（CI 默认跳过）。
//
//	mysqld --initialize-insecure --datadir=/tmp/fdwire-my/data
//	mysqld --datadir=/tmp/fdwire-my/data --port=13306 --socket=/tmp/fdwire-my/mysql.sock &
//	mysql -h 127.0.0.1 -P 13306 -u root -e 'CREATE DATABASE fdwire_test'
//	FDWIRE_TEST_MYSQL_DSN='root@tcp(127.0.0.1:13306)/fdwire_test' go test ./internal/adapters/mysql/
func mysqlDSN(t *testing.T) string {
	t.Helper()
	dsn := os.Getenv("FDWIRE_TEST_MYSQL_DSN")
	if dsn == "" {
		t.Skip("未设置 FDWIRE_TEST_MYSQL_DSN，跳过真实 MySQL 集成测试")
	}
	return dsn
}

func prepareSales(t *testing.T, ctx context.Context, db *sql.DB) {
	t.Helper()
	statements := []string{
		`DROP TABLE IF EXISTS it_sales`,
		`CREATE TABLE it_sales (
			id BIGINT NOT NULL PRIMARY KEY,
			amount DECIMAL(10,2),
			region VARCHAR(64),
			flag TINYINT(1),
			updated_at DATETIME,
			note TEXT
		)`,
		`INSERT INTO it_sales (id, amount, region, flag, updated_at, note) VALUES
			(1, 12.50, '华东', 1, '2026-10-06 04:00:00', '首单'),
			(2, 7.00,  '华北', 0, '2026-10-06 05:30:00', NULL),
			(3, 3.25,  '华南', 1, '2026-10-06 06:00:00', '加急')`,
	}
	for _, stmt := range statements {
		if _, err := db.ExecContext(ctx, stmt); err != nil {
			t.Fatalf("准备数据失败: %v\n%s", err, stmt)
		}
	}
}

func TestIntegrationColumnsAndPrimaryKeys(t *testing.T) {
	dsn := mysqlDSN(t)
	ctx := context.Background()
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatalf("打开连接失败: %v", err)
	}
	defer db.Close()
	if err := db.PingContext(ctx); err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	prepareSales(t, ctx, db)

	a := New(dsn, "it_sales", 2000)
	cols, err := a.Columns(ctx)
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	want := []adapters.Column{
		{Name: "id", Type: adapters.TypeInteger},
		{Name: "amount", Type: adapters.TypeReal},
		{Name: "region", Type: adapters.TypeText},
		{Name: "flag", Type: adapters.TypeInteger},
		{Name: "updated_at", Type: adapters.TypeText},
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
}

func TestIntegrationSnapshotValues(t *testing.T) {
	dsn := mysqlDSN(t)
	ctx := context.Background()
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatalf("打开连接失败: %v", err)
	}
	defer db.Close()
	prepareSales(t, ctx, db)

	a := New(dsn, "it_sales", 2) // 批次 2 → 2 + 1
	var batches [][]adapters.Row
	if err := a.Snapshot(ctx, func(rows []adapters.Row) error {
		batches = append(batches, rows)
		return nil
	}); err != nil {
		t.Fatalf("Snapshot: %v", err)
	}
	if len(batches) != 2 || len(batches[0]) != 2 || len(batches[1]) != 1 {
		t.Fatalf("批次切分异常: %v", len(batches))
	}
	first := batches[0][0]
	if !reflect.DeepEqual(first["id"], int64(1)) {
		t.Errorf("id = %#v, want int64(1)", first["id"])
	}
	if !reflect.DeepEqual(first["amount"], 12.5) {
		t.Errorf("DECIMAL 应转数字: %#v", first["amount"])
	}
	if first["region"] != "华东" {
		t.Errorf("region = %#v", first["region"])
	}
	if !reflect.DeepEqual(first["flag"], int64(1)) {
		t.Errorf("TINYINT 应转整数: %#v", first["flag"])
	}
	if first["updated_at"] != "2026-10-06 04:00:00" {
		t.Errorf("DATETIME 按 TEXT 原样: %#v", first["updated_at"])
	}
	if batches[0][1]["note"] != nil {
		t.Errorf("NULL 应为 nil: %#v", batches[0][1]["note"])
	}
}

func TestIntegrationSchemaQualifiedAndErrors(t *testing.T) {
	dsn := mysqlDSN(t)
	ctx := context.Background()
	if _, err := New(dsn, "fdwire_test.it_sales", 2000).Columns(ctx); err != nil {
		t.Fatalf("库名限定读取失败: %v", err)
	}
	if _, err := New(dsn, "it_does_not_exist", 2000).Columns(ctx); err == nil {
		t.Fatal("表不存在应报错")
	}
	a := New("nobody:wrong@tcp(127.0.0.1:1)/nope", "t", 2000)
	if _, err := a.Columns(ctx); err == nil {
		t.Fatal("不可达地址应报错")
	}
	if err := a.Snapshot(ctx, func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("不可达地址的 Snapshot 应报错")
	}
}

func TestIntegrationIncrementalWindow(t *testing.T) {
	dsn := mysqlDSN(t)
	ctx := context.Background()
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		t.Fatalf("打开连接失败: %v", err)
	}
	defer db.Close()
	if err := db.PingContext(ctx); err != nil {
		t.Fatalf("连接失败: %v", err)
	}
	prepareSales(t, ctx, db)

	a := New(dsn, "it_sales", 2000)

	// BIGINT 水位：字符串参数隐式转换比较。
	var rows []adapters.Row
	if err := a.Incremental(ctx, "id", "1", func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Incremental(id): %v", err)
	}
	if len(rows) != 2 || rows[0]["id"] != int64(2) || rows[1]["id"] != int64(3) {
		t.Fatalf("整数水位窗口 = %#v, want ids [2 3]（ASC）", rows)
	}

	// DATETIME 水位：字符串隐式转换。
	rows = nil
	if err := a.Incremental(ctx, "updated_at", "2026-10-06 05:00:00", func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("Incremental(updated_at): %v", err)
	}
	if len(rows) != 2 || rows[0]["id"] != int64(2) || rows[1]["id"] != int64(3) {
		t.Fatalf("时间水位窗口 = %#v, want ids [2 3]", rows)
	}

	// 空窗口：水位之后的零行。
	calls := 0
	if err := a.Incremental(ctx, "updated_at", "2026-10-07 00:00:00", func([]adapters.Row) error {
		calls++
		return nil
	}); err != nil {
		t.Fatalf("Incremental(空窗口): %v", err)
	}
	if calls != 0 {
		t.Fatalf("空窗口不应回调，实际 %d 次", calls)
	}

	// 水位列不存在：应报错（驱动 Unknown column 包装）。
	if err := a.Incremental(ctx, "no_such_col", "0", func([]adapters.Row) error { return nil }); err == nil {
		t.Fatal("水位列不存在应报错")
	}
}
