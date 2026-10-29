package rest

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// collect 跑一次 Snapshot/Incremental 并收集全部行。
func collect(t *testing.T, run func(fn adapters.SnapshotFunc) error) []adapters.Row {
	t.Helper()
	var rows []adapters.Row
	if err := run(func(batch []adapters.Row) error {
		rows = append(rows, batch...)
		return nil
	}); err != nil {
		t.Fatalf("读取失败: %v", err)
	}
	return rows
}

func noSleep(adapter *Adapter) {
	adapter.Sleep = func(time.Duration) {}
}

// ---------------------------------------------- 单页 + 投影 + 类型推断

func TestSnapshotSinglePageWithProjection(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if got := r.Header.Get("Authorization"); got != "Bearer secret-token" {
			t.Errorf("认证头未装配: %q", got)
		}
		if got := r.Header.Get("X-Tenant"); got != "acme" {
			t.Errorf("自定义头未装配: %q", got)
		}
		_, _ = w.Write([]byte(`{"data":{"items":[
			{"id":1,"amount":12.5,"region":"东","nested":{"name":"甲"}},
			{"id":2,"amount":7,"region":"西","nested":{"name":"乙"}}
		]}}`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:      srv.URL,
		Headers:  map[string]string{"X-Tenant": "acme"},
		RootPath: "data.items",
		Projection: []Projection{
			{Path: "id", Column: "id"},
			{Path: "amount", Column: "amount"},
			{Path: "region", Column: "region"},
			{Path: "nested.name", Column: "who"},
		},
	}, &Auth{Type: "bearer", Token: "secret-token"}, 0)

	cols, err := adapter.Columns(context.Background())
	if err != nil {
		t.Fatalf("Columns: %v", err)
	}
	want := []adapters.Column{
		{Name: "id", Type: adapters.TypeInteger},
		{Name: "amount", Type: adapters.TypeReal},
		{Name: "region", Type: adapters.TypeText},
		{Name: "who", Type: adapters.TypeText},
	}
	if len(cols) != len(want) {
		t.Fatalf("列数 %d != %d: %+v", len(cols), len(want), cols)
	}
	for i := range want {
		if cols[i] != want[i] {
			t.Fatalf("列 %d = %+v，期望 %+v", i, cols[i], want[i])
		}
	}

	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Snapshot(context.Background(), fn)
	})
	if len(rows) != 2 {
		t.Fatalf("行数 %d != 2", len(rows))
	}
	if rows[0]["who"] != "甲" || rows[0]["amount"] != 12.5 {
		t.Fatalf("投影结果不符: %+v", rows[0])
	}
}

// ---------------------------------------------- 分页（page 式 / cursor 式）

func TestSnapshotPagePagination(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		page := r.URL.Query().Get("page")
		switch page {
		case "1":
			_, _ = w.Write([]byte(`[{"id":1},{"id":2}]`))
		case "2":
			_, _ = w.Write([]byte(`[{"id":3}]`))
		default:
			_, _ = w.Write([]byte(`[]`)) // 空页终止
		}
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:        srv.URL,
		RootPath:   "",
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
		Pagination: &Pagination{Mode: "page", PageParam: "page", SizeParam: "size", PageSize: 2},
	}, nil, 0)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Snapshot(context.Background(), fn)
	})
	if len(rows) != 3 {
		t.Fatalf("分页行数 %d != 3", len(rows))
	}
}

func TestSnapshotCursorPagination(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cursor := r.URL.Query().Get("cursor")
		switch cursor {
		case "":
			_, _ = w.Write([]byte(`{"items":[{"id":1}],"next":"c2"}`))
		case "c2":
			_, _ = w.Write([]byte(`{"items":[{"id":2}],"next":null}`))
		default:
			t.Errorf("意外游标 %q", cursor)
			_, _ = w.Write([]byte(`{"items":[]}`))
		}
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:        srv.URL,
		RootPath:   "items",
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
		Pagination: &Pagination{Mode: "cursor", CursorParam: "cursor", CursorPath: "next", SizeParam: "size", PageSize: 1},
	}, nil, 0)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Snapshot(context.Background(), fn)
	})
	if len(rows) != 2 {
		t.Fatalf("cursor 分页行数 %d != 2", len(rows))
	}
}

// ---------------------------------------------- 错误诚实性

func TestProjectionPathMissFailsWithSample(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"items":[{"other":1}]}`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:        srv.URL,
		RootPath:   "items",
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
	}, nil, 0)
	noSleep(adapter)
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	// 路径缺失 → 行值为 nil（NULL），不报错；这是「缺路径按 NULL」语义。
	if err != nil {
		t.Fatalf("缺路径应按 NULL 处理，实际报错: %v", err)
	}

	// root_path 不命中 → 如实失败带样例
	adapter2 := New(Config{URL: srv.URL, RootPath: "nope", Projection: []Projection{{Path: "id", Column: "id"}}}, nil, 0)
	noSleep(adapter2)
	err = adapter2.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "root_path") {
		t.Fatalf("root_path 不命中应如实失败，实际: %v", err)
	}
}

func TestTypeMismatchFailsHonestly(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[{"id":"not-a-number"}]`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:        srv.URL,
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
	}, nil, 0)
	noSleep(adapter)
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "不符") {
		t.Fatalf("类型不符应如实失败，实际: %v", err)
	}
}

func TestRetryOn429ThenFail(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusTooManyRequests)
		_, _ = w.Write([]byte(`{"error":"rate limited"}`))
	}))
	defer srv.Close()

	adapter := New(Config{URL: srv.URL, Projection: []Projection{{Path: "id", Column: "id"}}}, nil, 0)
	noSleep(adapter)
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "429") {
		t.Fatalf("429 退避后应如实失败，实际: %v", err)
	}
	if calls != retryDelays+1 {
		t.Fatalf("退避次数 %d != %d", calls, retryDelays+1)
	}
}

func Test4xxNoRetry(t *testing.T) {
	calls := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv.Close()

	adapter := New(Config{URL: srv.URL, Projection: []Projection{{Path: "id", Column: "id"}}}, nil, 0)
	noSleep(adapter)
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "403") {
		t.Fatalf("403 应如实失败，实际: %v", err)
	}
	if calls != 1 {
		t.Fatalf("403 不应重试，实际调用 %d 次", calls)
	}
}

func TestMaxRowsGuard(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[{"id":1},{"id":2},{"id":3}]`))
	}))
	defer srv.Close()

	adapter := New(Config{URL: srv.URL, Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}}}, nil, 0)
	noSleep(adapter)
	adapter.MaxRows = 2
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "超过上限") {
		t.Fatalf("超行数上限应如实失败，实际: %v", err)
	}
}

// ---------------------------------------------- 增量（客户端过滤 + ASC）

func TestIncrementalFiltersAndSorts(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 故意乱序 + 含旧行
		_, _ = w.Write([]byte(`[{"id":5},{"id":2},{"id":9},{"id":7}]`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:           srv.URL,
		Projection:    []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
		WatermarkPath: "id",
	}, nil, 0)
	noSleep(adapter)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Incremental(context.Background(), "id", "5", fn)
	})
	if len(rows) != 2 {
		t.Fatalf("过滤后行数 %d != 2: %+v", len(rows), rows)
	}
	if rows[0]["id"] != int64(7) || rows[1]["id"] != int64(9) {
		t.Fatalf("应按 id ASC 排序: %+v", rows)
	}
}

// 水位值经声明的响应字段路径提取（而不是投影列）：投影列叫 order_id，
// 水位路径指 raw_watermark，过滤必须按后者。
func TestIncrementalUsesDeclaredWatermarkPath(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[
			{"id":9,"meta":{"seq":90}},
			{"id":1,"meta":{"seq":10}},
			{"id":5,"meta":{"seq":50}}]`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:           srv.URL,
		Projection:    []Projection{{Path: "id", Column: "order_id", Type: "INTEGER"}},
		WatermarkPath: "meta.seq",
	}, nil, 0)
	noSleep(adapter)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		// 水位列取投影名（引擎侧按列追踪），但值由 meta.seq 提取
		return adapter.Incremental(context.Background(), "order_id", "50", fn)
	})
	if len(rows) != 1 {
		t.Fatalf("按 meta.seq 过滤后应只剩 1 行，实际 %d: %+v", len(rows), rows)
	}
	if rows[0]["order_id"] != int64(9) {
		t.Fatalf("保留行应为 id=9: %+v", rows[0])
	}
}

// 未声明水位路径 → 适配器不实现 IncrementalReader（runner 如实回退全量）。
func TestNoWatermarkPathMeansSnapshotOnly(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[{"id":1}]`))
	}))
	defer srv.Close()

	shape := Config{
		URL:        srv.URL,
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
	}
	adapter := NewFromClient(shape, nil, 0)
	if _, ok := adapter.(adapters.IncrementalReader); ok {
		t.Fatal("未声明 watermark_path 的 REST 源不应声明增量能力")
	}
	// 声明了才有
	withPath := NewFromClient(
		Config{
			URL:           srv.URL,
			Projection:    []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
			WatermarkPath: "id",
		}, nil, 0,
	)
	if _, ok := withPath.(adapters.IncrementalReader); !ok {
		t.Fatal("声明 watermark_path 的 REST 源应实现增量读取")
	}
}

func TestIncrementalZeroRowsKeepsWatermark(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`[{"id":1}]`))
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:           srv.URL,
		Projection:    []Projection{{Path: "id", Column: "id", Type: "INTEGER"}},
		WatermarkPath: "id",
	}, nil, 0)
	noSleep(adapter)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Incremental(context.Background(), "id", "99", fn)
	})
	if len(rows) != 0 {
		t.Fatalf("零新行应回调零行，实际 %d", len(rows))
	}
}

// ---------------------------------------------- 凭据纪律

func TestAuthNeverLeaksIntoErrors(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
		_, _ = w.Write([]byte(`{"detail":"boom"}`))
	}))
	defer srv.Close()

	adapter := New(Config{URL: srv.URL, Projection: []Projection{{Path: "id", Column: "id"}}},
		&Auth{Type: "bearer", Token: "super-secret-token"}, 0)
	noSleep(adapter)
	err := adapter.Snapshot(context.Background(), func([]adapters.Row) error { return nil })
	if err == nil {
		t.Fatal("应失败")
	}
	if strings.Contains(err.Error(), "super-secret-token") {
		t.Fatalf("错误文案泄漏了凭据: %v", err)
	}
}

// ---------------------------------------------- root_path 单对象包装

func TestSingleObjectRootWrapped(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"id": 42, "name": "唯一条目"})
	}))
	defer srv.Close()

	adapter := New(Config{
		URL:        srv.URL,
		Projection: []Projection{{Path: "id", Column: "id", Type: "INTEGER"}, {Path: "name", Column: "name"}},
	}, nil, 0)
	rows := collect(t, func(fn adapters.SnapshotFunc) error {
		return adapter.Snapshot(context.Background(), fn)
	})
	if len(rows) != 1 || rows[0]["name"] != "唯一条目" {
		t.Fatalf("单对象包装失败: %+v", rows)
	}
}
