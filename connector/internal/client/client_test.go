package client

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

// noSleep 让重试测试免等待。
func noSleep() Option { return WithSleep(func(context.Context, time.Duration) {}) }

func writeJSON(t *testing.T, w http.ResponseWriter, status int, payload any) {
	t.Helper()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if payload != nil {
		if err := json.NewEncoder(w).Encode(payload); err != nil {
			t.Fatal(err)
		}
	}
}

func TestNewEnforcesHTTPS(t *testing.T) {
	for _, bad := range []string{"http://wire.example.com", "http://10.0.0.5:8080", "ftp://x", "wire.example.com", ""} {
		if _, err := New(bad, ""); err == nil {
			t.Errorf("api_url %q 应被拒绝（契约 §5 TLS 强制）", bad)
		}
	}
	for _, good := range []string{"https://wire.finddatatech.cloud", "http://127.0.0.1:8080", "http://localhost:8080"} {
		if _, err := New(good, ""); err != nil {
			t.Errorf("api_url %q 应可用: %v", good, err)
		}
	}
	c, err := New("https://wire.example.com/", "fwc_k")
	if err != nil {
		t.Fatal(err)
	}
	if c.APIURL() != "https://wire.example.com" {
		t.Errorf("基地址应去掉尾斜杠: %q", c.APIURL())
	}
}

func TestPairFlow(t *testing.T) {
	var gotPath, gotAuth string
	var gotBody PairRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotAuth = r.URL.Path, r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("解码请求: %v", err)
		}
		writeJSON(t, w, http.StatusOK, map[string]any{"connector_key": "fwc_abc123", "connector_id": "conn-1"})
	}))
	defer srv.Close()

	resp, err := Pair(context.Background(), srv.URL, "fwp_token", "host-1", "sha-1234567", noSleep())
	if err != nil {
		t.Fatalf("Pair: %v", err)
	}
	if resp.ConnectorKey != "fwc_abc123" || resp.ConnectorID != "conn-1" {
		t.Fatalf("Pair 响应 = %#v", resp)
	}
	if gotPath != PathPair {
		t.Errorf("配对路径 = %q, want %q", gotPath, PathPair)
	}
	if gotAuth != "" {
		t.Errorf("配对请求不应携带既有 key，实际 %q", gotAuth)
	}
	if gotBody.Token != "fwp_token" || gotBody.Name != "host-1" || gotBody.Version != "sha-1234567" {
		t.Errorf("配对请求体 = %#v", gotBody)
	}
}

func TestPairRejectsBadTokenAndBadResponse(t *testing.T) {
	if _, err := Pair(context.Background(), "https://x", "not-a-token", "n", "v"); err == nil {
		t.Error("非 fwp_ 令牌应在本地拒绝")
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusOK, map[string]any{"connector_key": "bogus", "connector_id": "c"})
	}))
	defer srv.Close()
	if _, err := Pair(context.Background(), srv.URL, "fwp_t", "n", "v", noSleep()); err == nil {
		t.Error("响应 key 前缀非法应报错")
	}
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusUnauthorized, map[string]any{"detail": "配对令牌无效或已过期"})
	}))
	defer srv2.Close()
	_, err := Pair(context.Background(), srv2.URL, "fwp_t", "n", "v", noSleep())
	if err == nil || !strings.Contains(err.Error(), "配对令牌无效或已过期") {
		t.Fatalf("401 应如实带出 detail: %v", err)
	}
}

func TestHeartbeatSendsVersionAndState(t *testing.T) {
	var gotBody HeartbeatRequest
	var gotAuth string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotAuth = r.Header.Get("Authorization")
		if err := json.NewDecoder(r.Body).Decode(&gotBody); err != nil {
			t.Errorf("解码请求: %v", err)
		}
		writeJSON(t, w, http.StatusOK, map[string]any{"poll_interval_seconds": 90})
	}))
	defer srv.Close()

	c, err := New(srv.URL, "fwc_key", WithVersion("sha-abcdefg"), noSleep())
	if err != nil {
		t.Fatal(err)
	}
	resp, err := c.Heartbeat(context.Background(), HeartbeatRequest{
		SyncState: map[string]string{"sales": "ok"},
		Warnings:  []string{"数据集 x：本地 sources 未配置源 \"erp\""},
	})
	if err != nil {
		t.Fatalf("Heartbeat: %v", err)
	}
	if gotAuth != "Bearer fwc_key" {
		t.Errorf("鉴权头 = %q", gotAuth)
	}
	if gotBody.Version != "sha-abcdefg" {
		t.Errorf("心跳应带注入版本，得到 %q", gotBody.Version)
	}
	if gotBody.SyncState["sales"] != "ok" || len(gotBody.Warnings) != 1 {
		t.Errorf("心跳体 = %#v", gotBody)
	}
	if got := resp.Interval(time.Second); got != 90*time.Second {
		t.Errorf("云端周期 = %s, want 90s", got)
	}
}

func TestHeartbeatIntervalClamping(t *testing.T) {
	cases := []struct {
		seconds int
		want    time.Duration
	}{
		{0, 45 * time.Second},
		{-1, 45 * time.Second},
		{2, minPollInterval},
		{90, 90 * time.Second},
		{7200, maxPollInterval},
	}
	for _, c := range cases {
		resp := &HeartbeatResponse{PollIntervalSeconds: c.seconds}
		if got := resp.Interval(45 * time.Second); got != c.want {
			t.Errorf("Interval(%d) = %s, want %s", c.seconds, got, c.want)
		}
	}
	var nilResp *HeartbeatResponse
	if got := nilResp.Interval(0); got != DefaultHeartbeatInterval {
		t.Errorf("缺省周期 = %s, want %s", got, DefaultHeartbeatInterval)
	}
}

func TestFetchConfigAndDefaults(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != PathConfig || r.Method != http.MethodGet {
			t.Errorf("意外请求 %s %s", r.Method, r.URL.Path)
		}
		if r.Header.Get("Authorization") != "Bearer fwc_key" {
			t.Errorf("配置拉取应带连接器 key")
		}
		writeJSON(t, w, http.StatusOK, map[string]any{
			"connector_id":              "conn-1",
			"poll_interval_seconds":     60,
			"min_sync_interval_seconds": 1200,
			"limits":                    map[string]any{"max_rows": 500000},
			"datasets": []map[string]any{
				{"dataset_key": "sales", "name": "销售", "source": "erp_db", "source_ref": "public.sales", "schedule_cron": "*/15 * * * *"},
				{"dataset_key": "stock", "source": "erp_db", "source_ref": "public.stock", "sync_enabled": false},
				{"dataset_key": "promo", "source": "erp_db", "source_ref": "public.promo", "min_sync_minutes": 5},
			},
		})
	}))
	defer srv.Close()

	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := c.FetchConfig(context.Background())
	if err != nil {
		t.Fatalf("FetchConfig: %v", err)
	}
	if len(cfg.Datasets) != 3 {
		t.Fatalf("数据集数 = %d", len(cfg.Datasets))
	}
	if cfg.MinSync() != 20*time.Minute {
		t.Errorf("全局最小间隔 = %s", cfg.MinSync())
	}
	sales, ok := cfg.Dataset("sales")
	if !ok || sales.Source != "erp_db" || sales.SourceRef != "public.sales" || sales.SyncCron != "*/15 * * * *" {
		t.Fatalf("sales 数据集 = %#v", sales)
	}
	if !sales.Enabled() {
		t.Error("sync_enabled 缺省应启用")
	}
	if got := sales.MinSync(cfg.MinSync()); got != 20*time.Minute {
		t.Errorf("sales 最小间隔 = %s", got)
	}
	stock, _ := cfg.Dataset("stock")
	if stock.Enabled() {
		t.Error("sync_enabled=false 应停用")
	}
	promo, _ := cfg.Dataset("promo")
	if got := promo.MinSync(cfg.MinSync()); got != 5*time.Minute {
		t.Errorf("数据集覆盖最小间隔 = %s", got)
	}
	if _, ok := cfg.Dataset("nope"); ok {
		t.Error("不存在的 key 不应命中")
	}
	if got := (&RemoteConfig{}).MinSync(); got != DefaultMinSyncMinutes*time.Minute {
		t.Errorf("缺省最小间隔 = %s", got)
	}
}

func TestFetchConfigRejectsDatasetWithoutKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusOK, map[string]any{"datasets": []map[string]any{{"source": "erp"}}})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.FetchConfig(context.Background()); err == nil {
		t.Fatal("缺少 key 的数据集应报错")
	}
}

func TestSyncThreePhaseSequence(t *testing.T) {
	var paths []string
	var chunkBodies []SyncChunkRequest
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		switch {
		case r.URL.Path == PathSyncs:
			var body SyncBeginRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("解码 begin: %v", err)
			}
			if body.DatasetKey != "sales" || len(body.Columns) != 2 || body.PKColumns[0] != "id" {
				t.Errorf("begin 请求体 = %#v", body)
			}
			writeJSON(t, w, http.StatusOK, map[string]any{"sync_id": "sync-1"})
		case strings.HasSuffix(r.URL.Path, "/chunks"):
			var body SyncChunkRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				t.Errorf("解码 chunk: %v", err)
			}
			chunkBodies = append(chunkBodies, body)
			writeJSON(t, w, http.StatusOK, map[string]any{"accepted": len(body.Rows)})
		case strings.HasSuffix(r.URL.Path, "/commit"):
			writeJSON(t, w, http.StatusOK, map[string]any{"status": "ok", "rows": 2500})
		default:
			t.Errorf("意外路径 %s", r.URL.Path)
			writeJSON(t, w, http.StatusNotFound, map[string]any{"detail": "no route"})
		}
	}))
	defer srv.Close()

	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	begin, err := c.SyncBegin(ctx, SyncBeginRequest{
		DatasetKey: "sales",
		Columns:    []Column{{Name: "id", Type: "INTEGER"}, {Name: "amount", Type: "REAL"}},
		PKColumns:  []string{"id"},
	})
	if err != nil {
		t.Fatalf("SyncBegin: %v", err)
	}
	if begin.SyncID != "sync-1" {
		t.Fatalf("sync_id = %q", begin.SyncID)
	}
	if _, err := c.SyncChunk(ctx, begin.SyncID, 0, []map[string]any{{"id": 1}, {"id": 2}}); err != nil {
		t.Fatalf("SyncChunk: %v", err)
	}
	if _, err := c.SyncChunk(ctx, begin.SyncID, 2, []map[string]any{{"id": 3}}); err != nil {
		t.Fatalf("SyncChunk: %v", err)
	}
	commit, err := c.SyncCommit(ctx, begin.SyncID, SyncCommitRequest{})
	if err != nil {
		t.Fatalf("SyncCommit: %v", err)
	}
	if commit.Rows != 2500 {
		t.Errorf("commit rows = %d", commit.Rows)
	}
	want := []string{PathSyncs, PathSyncs + "/sync-1/chunks", PathSyncs + "/sync-1/chunks", PathSyncs + "/sync-1/commit"}
	if fmt.Sprint(paths) != fmt.Sprint(want) {
		t.Fatalf("调用序列 = %v, want %v", paths, want)
	}
	if len(chunkBodies) != 2 || chunkBodies[0].Index != 0 || chunkBodies[1].Index != 2 {
		t.Fatalf("块序号 = %#v", chunkBodies)
	}
}

func TestSyncChunkRejectsOversizeBlock(t *testing.T) {
	c, err := New("https://wire.example.com", "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	rows := make([]map[string]any, MaxChunkRows+1)
	if _, err := c.SyncChunk(context.Background(), "sync-1", 0, rows); err == nil {
		t.Fatalf("超过 %d 行的块应被拒绝（契约 §3.3）", MaxChunkRows)
	}
}

func TestSyncAbort(t *testing.T) {
	var gotPath string
	var gotMethod string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath, gotMethod = r.URL.Path, r.Method
		writeJSON(t, w, http.StatusOK, map[string]any{"aborted": true})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	if err := c.SyncAbort(context.Background(), "sync-9"); err != nil {
		t.Fatalf("SyncAbort: %v", err)
	}
	if gotPath != PathSyncs+"/sync-9/abort" || gotMethod != http.MethodPost {
		t.Fatalf("abort 调用 = %s %s", gotMethod, gotPath)
	}
	if err := c.SyncAbort(context.Background(), ""); err != nil {
		t.Fatalf("空 sync_id 应静默返回: %v", err)
	}
}

func TestRetryOn5xxThenSuccess(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if atomic.AddInt32(&calls, 1) == 1 {
			writeJSON(t, w, http.StatusServiceUnavailable, map[string]any{"detail": "上游暂不可用"})
			return
		}
		writeJSON(t, w, http.StatusOK, map[string]any{"poll_interval_seconds": 60})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", WithRetry(4, time.Millisecond), noSleep())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Heartbeat(context.Background(), HeartbeatRequest{}); err != nil {
		t.Fatalf("503 后重试应成功: %v", err)
	}
	if got := atomic.LoadInt32(&calls); got != 2 {
		t.Fatalf("调用次数 = %d, want 2", got)
	}
}

func TestRetryExhaustedReturnsLastError(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		writeJSON(t, w, http.StatusBadGateway, map[string]any{"detail": "坏网关"})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", WithRetry(3, time.Millisecond), noSleep())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.Heartbeat(context.Background(), HeartbeatRequest{})
	if err == nil || !strings.Contains(err.Error(), "坏网关") {
		t.Fatalf("耗尽重试应返回最后一次 5xx 原因: %v", err)
	}
	if got := atomic.LoadInt32(&calls); got != 3 {
		t.Fatalf("调用次数 = %d, want 3", got)
	}
}

func Test4xxFailsImmediately(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		writeJSON(t, w, http.StatusUnauthorized, map[string]any{"detail": "连接器 key 已吊销"})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", WithRetry(4, time.Millisecond), noSleep())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.Heartbeat(context.Background(), HeartbeatRequest{})
	if err == nil || !strings.Contains(err.Error(), "连接器 key 已吊销") {
		t.Fatalf("4xx 应立即失败并带原因: %v", err)
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("4xx 不应重试，调用次数 = %d", got)
	}
	if !IsUnauthorized(err) {
		t.Errorf("应可判定为鉴权失败: %v", err)
	}
}

func TestDriftError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusConflict, map[string]any{
			"detail": map[string]any{
				"code":        "drift",
				"message":     "schema 漂移：列 price 缺失",
				"differences": []string{"列 price 缺失"},
			},
		})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.SyncBegin(context.Background(), SyncBeginRequest{DatasetKey: "sales", Columns: []Column{{Name: "id", Type: "INTEGER"}}})
	if err == nil {
		t.Fatal("409 应报错")
	}
	if !IsDrift(err) {
		t.Fatalf("应判定为漂移: %v", err)
	}
	var drift *DriftError
	if !errors.As(err, &drift) {
		t.Fatalf("errors.As 应命中 DriftError: %v", err)
	}
	if drift.Code != "drift" || len(drift.Differences) != 1 || drift.Status != http.StatusConflict {
		t.Fatalf("漂移细节 = %#v", drift)
	}
	var apiErr *APIError
	if !errors.As(err, &apiErr) {
		t.Fatalf("DriftError 应可解出 APIError: %v", err)
	}
}

func TestDriftErrorFromPlainDetailText(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusConflict, map[string]any{"detail": "drift: 列 price 缺失"})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.SyncCommit(context.Background(), "sync-1", SyncCommitRequest{})
	if !IsDrift(err) {
		t.Fatalf("纯文本 detail 的 409 也应判为漂移: %v", err)
	}
	var drift *DriftError
	if errors.As(err, &drift) && drift.Code != "drift" {
		t.Fatalf("应从 detail 前缀解析出 code: %#v", drift)
	}
}

func TestQuotaError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusTooManyRequests, map[string]any{
			"detail": map[string]any{"code": "quota", "reason": "存储配额超限"},
		})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.SyncBegin(context.Background(), SyncBeginRequest{DatasetKey: "sales", Columns: []Column{{Name: "id", Type: "INTEGER"}}})
	if !IsQuota(err) {
		t.Fatalf("应判定为配额超限: %v", err)
	}
	var quota *QuotaError
	if errors.As(err, &quota) && quota.Reason != "存储配额超限" {
		t.Fatalf("配额细节 = %#v", quota)
	}
	if IsDrift(err) {
		t.Error("429 不应判为漂移")
	}
}

func TestBeginValidatesInput(t *testing.T) {
	c, err := New("https://wire.example.com", "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.SyncBegin(context.Background(), SyncBeginRequest{Columns: []Column{{Name: "a", Type: "TEXT"}}}); err == nil {
		t.Error("缺 dataset_key 应报错")
	}
	if _, err := c.SyncBegin(context.Background(), SyncBeginRequest{DatasetKey: "k"}); err == nil {
		t.Error("缺列定义应报错")
	}
}

func TestContextCancellationStopsRetry(t *testing.T) {
	var calls int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		atomic.AddInt32(&calls, 1)
		writeJSON(t, w, http.StatusInternalServerError, map[string]any{"detail": "boom"})
	}))
	defer srv.Close()
	ctx, cancel := context.WithCancel(context.Background())
	c, err := New(srv.URL, "fwc_key", WithRetry(5, time.Millisecond), WithSleep(func(context.Context, time.Duration) {
		cancel()
	}))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := c.Heartbeat(ctx, HeartbeatRequest{}); !errors.Is(err, context.Canceled) {
		t.Fatalf("ctx 取消应中止重试: %v", err)
	}
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("取消后不应再发请求，调用次数 = %d", got)
	}
}

// TestSyncCommitWatermarkBody 验证 commit 请求体：带指针上报水位，nil 则完全省略。
func TestSyncCommitWatermarkBody(t *testing.T) {
	var bodies []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		raw, _ := io.ReadAll(r.Body)
		bodies = append(bodies, string(raw))
		writeJSON(t, w, http.StatusOK, map[string]any{"status": "ok", "rows": 3})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	wm := "9007199254740993"
	if _, err := c.SyncCommit(ctx, "sync-1", SyncCommitRequest{WatermarkValue: &wm}); err != nil {
		t.Fatalf("SyncCommit: %v", err)
	}
	if _, err := c.SyncCommit(ctx, "sync-2", SyncCommitRequest{}); err != nil {
		t.Fatalf("SyncCommit: %v", err)
	}
	if len(bodies) != 2 {
		t.Fatalf("commit 次数 = %d", len(bodies))
	}
	if bodies[0] != `{"watermark_value":"9007199254740993"}` {
		t.Fatalf("增量 commit 体 = %s", bodies[0])
	}
	if bodies[1] != "{}" {
		t.Fatalf("全量 commit 体应为空对象: %s", bodies[1])
	}
}

// TestFetchConfigParsesIncrementalFields 验证增量三字段的下发解析。
func TestFetchConfigParsesIncrementalFields(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(t, w, http.StatusOK, map[string]any{
			"datasets": []map[string]any{
				{"dataset_key": "orders", "source": "erp_db", "source_ref": "public.orders",
					"sync_mode": "incremental", "watermark_column": "updated_at", "watermark_value": "9007199254740993"},
				{"dataset_key": "dim", "source": "erp_db", "source_ref": "public.dim",
					"sync_mode": "snapshot"},
			},
		})
	}))
	defer srv.Close()
	c, err := New(srv.URL, "fwc_key", noSleep())
	if err != nil {
		t.Fatal(err)
	}
	cfg, err := c.FetchConfig(context.Background())
	if err != nil {
		t.Fatalf("FetchConfig: %v", err)
	}
	orders, _ := cfg.Dataset("orders")
	if orders.SyncMode != "incremental" || orders.WatermarkColumn != "updated_at" || orders.WatermarkValue != "9007199254740993" {
		t.Fatalf("增量字段解析 = %#v", orders)
	}
	dim, _ := cfg.Dataset("dim")
	if dim.SyncMode != "snapshot" || dim.WatermarkColumn != "" || dim.WatermarkValue != "" {
		t.Fatalf("全量数据集不应有水位: %#v", dim)
	}
}
