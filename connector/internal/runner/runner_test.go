package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
	"github.com/FindDataTechnology/fd-wire/connector/internal/client"
)

type fakeAdapter struct {
	columns   []adapters.Column
	pks       []string
	batches   [][]adapters.Row
	snapErr   error
	colsErr   error
	onSnap    func()
	snapCalls int
}

func (f *fakeAdapter) Columns(context.Context) ([]adapters.Column, error) {
	return f.columns, f.colsErr
}

func (f *fakeAdapter) PrimaryKeys(context.Context) ([]string, error) { return f.pks, nil }

func (f *fakeAdapter) Snapshot(ctx context.Context, fn adapters.SnapshotFunc) error {
	f.snapCalls++
	if f.onSnap != nil {
		f.onSnap()
	}
	for _, batch := range f.batches {
		if err := fn(batch); err != nil {
			return err
		}
	}
	return f.snapErr
}

// fakeIncAdapter 在 fakeAdapter 之上实现 IncrementalReader，记录增量调用参数。
type fakeIncAdapter struct {
	*fakeAdapter
	incBatches [][]adapters.Row
	incErr     error
	incCalls   int
	incColumn  string
	incAfter   string
}

func (f *fakeIncAdapter) Incremental(ctx context.Context, column, after string, fn adapters.SnapshotFunc) error {
	f.incCalls++
	f.incColumn, f.incAfter = column, after
	for _, batch := range f.incBatches {
		if err := fn(batch); err != nil {
			return err
		}
	}
	return f.incErr
}

// incDataset 构造配了增量模式的数据集配置。
func incDataset() client.DatasetConfig {
	ds := testDataset()
	ds.SyncMode = "incremental"
	ds.WatermarkColumn = "updated_at"
	return ds
}

type mockAPI struct {
	mu           sync.Mutex
	beginCalls   int
	commitCalls  int
	abortCalls   int
	chunkRows    []int
	chunkIndexes []int
	beginBody    client.SyncBeginRequest
	commitBody   client.SyncCommitRequest
	beginStatus  int
	beginDetail  string
	commitStatus int
	commitDetail string
	syncID       string
}

func (m *mockAPI) handler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		m.mu.Lock()
		defer m.mu.Unlock()
		w.Header().Set("Content-Type", "application/json")
		switch {
		case r.URL.Path == client.PathSyncs:
			m.beginCalls++
			if err := json.NewDecoder(r.Body).Decode(&m.beginBody); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if m.beginStatus != 0 {
				w.WriteHeader(m.beginStatus)
				fmt.Fprintf(w, `{"detail":%q}`, m.beginDetail)
				return
			}
			fmt.Fprintf(w, `{"sync_id":%q}`, m.syncID)
		case strings.HasSuffix(r.URL.Path, "/chunks"):
			var body client.SyncChunkRequest
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			m.chunkRows = append(m.chunkRows, len(body.Rows))
			m.chunkIndexes = append(m.chunkIndexes, body.Index)
			fmt.Fprintf(w, `{"accepted":%d}`, len(body.Rows))
		case strings.HasSuffix(r.URL.Path, "/commit"):
			m.commitCalls++
			if err := json.NewDecoder(r.Body).Decode(&m.commitBody); err != nil {
				http.Error(w, err.Error(), http.StatusBadRequest)
				return
			}
			if m.commitStatus != 0 {
				w.WriteHeader(m.commitStatus)
				fmt.Fprintf(w, `{"detail":%q}`, m.commitDetail)
				return
			}
			fmt.Fprint(w, `{"status":"ok","rows":2500}`)
		case strings.HasSuffix(r.URL.Path, "/abort"):
			m.abortCalls++
			fmt.Fprint(w, `{"aborted":true}`)
		default:
			w.WriteHeader(http.StatusNotFound)
			fmt.Fprint(w, `{"detail":"no route"}`)
		}
	}
}

func (m *mockAPI) newClient(t *testing.T) *client.Client {
	t.Helper()
	srv := httptest.NewServer(m.handler())
	t.Cleanup(srv.Close)
	c, err := client.New(srv.URL, "fwc_key", client.WithSleep(func(context.Context, time.Duration) {}))
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func testDataset() client.DatasetConfig {
	return client.DatasetConfig{Key: "sales", Source: "erp_db", SourceRef: "public.sales"}
}

func TestSyncPushesChunksAndCommits(t *testing.T) {
	rows := make([]adapters.Row, 2500)
	for i := range rows {
		rows[i] = adapters.Row{"id": int64(i)}
	}
	api := &mockAPI{syncID: "sync-1"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}},
				pks:     []string{"id"},
				batches: [][]adapters.Row{rows},
			}, nil
		},
	}
	result, err := r.Sync(context.Background(), testDataset())
	if err != nil {
		t.Fatalf("Sync: %v", err)
	}
	if result.Rows != 2500 || result.Chunks != 2 || result.CommitRows != 2500 {
		t.Fatalf("结果 = %#v", result)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.beginCalls != 1 || api.commitCalls != 1 || api.abortCalls != 0 {
		t.Fatalf("调用计数 begin=%d commit=%d abort=%d", api.beginCalls, api.commitCalls, api.abortCalls)
	}
	if fmt.Sprint(api.chunkRows) != "[2000 500]" {
		t.Fatalf("块大小 = %v, want [2000 500]（契约 §3.3 上限 2000）", api.chunkRows)
	}
	if fmt.Sprint(api.chunkIndexes) != "[0 2000]" {
		t.Fatalf("块序号 = %v, want [0 2000]", api.chunkIndexes)
	}
	if api.beginBody.DatasetKey != "sales" || api.beginBody.PKColumns[0] != "id" {
		t.Fatalf("begin 请求体 = %#v", api.beginBody)
	}
	if len(api.beginBody.Columns) != 1 || api.beginBody.Columns[0].Type != "INTEGER" {
		t.Fatalf("begin 列声明 = %#v", api.beginBody.Columns)
	}
}

func TestSyncAbortsOnSnapshotError(t *testing.T) {
	api := &mockAPI{syncID: "sync-1"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}},
				batches: [][]adapters.Row{{{"id": int64(1)}}},
				snapErr: errors.New("源库连接中断"),
			}, nil
		},
	}
	_, err := r.Sync(context.Background(), testDataset())
	if err == nil || !strings.Contains(err.Error(), "源库连接中断") {
		t.Fatalf("应报快照错误: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitCalls != 0 {
		t.Error("失败不应 commit")
	}
	if api.abortCalls != 1 {
		t.Fatalf("失败必须 abort，abort 调用 = %d", api.abortCalls)
	}
}

func TestSyncAbortsOnCommitDrift(t *testing.T) {
	api := &mockAPI{syncID: "sync-1", commitStatus: http.StatusConflict, commitDetail: "drift: 列 price 缺失"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}},
				batches: [][]adapters.Row{{{"id": int64(1)}}},
			}, nil
		},
	}
	_, err := r.Sync(context.Background(), testDataset())
	if !client.IsDrift(err) {
		t.Fatalf("commit 409 应判为漂移: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.abortCalls != 1 {
		t.Fatalf("漂移后应 abort 清理，abort 调用 = %d", api.abortCalls)
	}
}

func TestSyncDoesNotAbortWhenBeginFails(t *testing.T) {
	api := &mockAPI{syncID: "sync-1", beginStatus: http.StatusTooManyRequests, beginDetail: "quota: 存储超限"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}}}, nil
		},
	}
	_, err := r.Sync(context.Background(), testDataset())
	if !client.IsQuota(err) {
		t.Fatalf("begin 429 应判为配额超限: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.abortCalls != 0 || api.chunkRows != nil {
		t.Fatalf("begin 失败无需 abort 也无块推送: abort=%d chunks=%v", api.abortCalls, api.chunkRows)
	}
}

func TestSyncAbortsEvenWhenParentContextCancelled(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	api := &mockAPI{syncID: "sync-1"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}},
				onSnap:  cancel, // 快照中途进程收到停止信号
			}, nil
		},
	}
	_, err := r.Sync(ctx, testDataset())
	if err == nil {
		t.Fatal("ctx 取消应返回错误")
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.abortCalls != 1 {
		t.Fatalf("ctx 取消也要尽力 abort，abort 调用 = %d", api.abortCalls)
	}
}

func TestSyncFailsWithoutTouchingAPI(t *testing.T) {
	api := &mockAPI{syncID: "sync-1"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return nil, errors.New("本地未配置源 erp_db")
		},
	}
	_, err := r.Sync(context.Background(), testDataset())
	if err == nil || !strings.Contains(err.Error(), "本地未配置源") {
		t.Fatalf("工厂错误应原样返回: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.beginCalls != 0 || api.abortCalls != 0 {
		t.Fatalf("源不可用不应发起任何 api 调用: begin=%d abort=%d", api.beginCalls, api.abortCalls)
	}
}

func TestSyncFailsOnColumnsError(t *testing.T) {
	api := &mockAPI{syncID: "sync-1"}
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return &fakeAdapter{colsErr: errors.New("连接被拒绝")}, nil
		},
	}
	_, err := r.Sync(context.Background(), testDataset())
	if err == nil || !strings.Contains(err.Error(), "读取源结构失败") {
		t.Fatalf("读取源结构失败应报错: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.beginCalls != 0 {
		t.Fatalf("读列失败不应 begin: %d", api.beginCalls)
	}
}

func TestSyncRequiresClientAndFactory(t *testing.T) {
	if _, err := (&Runner{}).Sync(context.Background(), testDataset()); err == nil {
		t.Error("缺客户端应报错")
	}
	api := &mockAPI{syncID: "s"}
	if _, err := (&Runner{Client: api.newClient(t)}).Sync(context.Background(), testDataset()); err == nil {
		t.Error("缺适配器工厂应报错")
	}
}

// captureLogs 建一个记录型 logf。
func captureLogs() (*[]string, func(string, ...any)) {
	logs := &[]string{}
	return logs, func(format string, args ...any) {
		*logs = append(*logs, fmt.Sprintf(format, args...))
	}
}

func logsContain(logs *[]string, substr string) bool {
	for _, l := range *logs {
		if strings.Contains(l, substr) {
			return true
		}
	}
	return false
}

// TestSyncIncrementalWindowPullsAndReportsWatermark 覆盖 a 态：
// 已有水位 → 走适配器增量读取（不碰 Snapshot），commit 带本轮最大水位（乱序行取最大）。
func TestSyncIncrementalWindowPullsAndReportsWatermark(t *testing.T) {
	api := &mockAPI{syncID: "sync-inc"}
	fake := &fakeIncAdapter{
		fakeAdapter: &fakeAdapter{
			columns: []adapters.Column{
				{Name: "id", Type: adapters.TypeInteger},
				{Name: "updated_at", Type: adapters.TypeText},
			},
			pks: []string{"id"},
		},
		// 乱序水位：最大值应为 03:30。
		incBatches: [][]adapters.Row{{
			{"id": int64(11), "updated_at": "2026-10-07T01:00:00Z"},
			{"id": int64(12), "updated_at": "2026-10-07T03:30:00Z"},
			{"id": int64(13), "updated_at": "2026-10-07T02:00:00Z"},
		}},
	}
	logs, logf := captureLogs()
	r := &Runner{
		Client: api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) {
			return fake, nil
		},
		Logf: logf,
	}
	ds := incDataset()
	ds.WatermarkValue = "2026-10-07T00:00:00Z"
	result, err := r.Sync(context.Background(), ds)
	if err != nil {
		t.Fatalf("Sync: %v", err)
	}
	if result.Rows != 3 || result.Chunks != 1 {
		t.Fatalf("结果 = %#v", result)
	}
	if fake.incCalls != 1 || fake.incColumn != "updated_at" || fake.incAfter != "2026-10-07T00:00:00Z" {
		t.Fatalf("增量调用 = %d (%s, %s)", fake.incCalls, fake.incColumn, fake.incAfter)
	}
	if fake.snapCalls != 0 {
		t.Fatalf("a 态不应走 Snapshot，实际调用 %d 次", fake.snapCalls)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitBody.WatermarkValue == nil || *api.commitBody.WatermarkValue != "2026-10-07T03:30:00Z" {
		t.Fatalf("commit 水位 = %#v, want 2026-10-07T03:30:00Z", api.commitBody.WatermarkValue)
	}
	if !logsContain(logs, "2026-10-07T03:30:00Z") {
		t.Fatalf("增量轮日志应带新水位: %v", *logs)
	}
}

// TestSyncIncrementalFirstSyncBaseline 覆盖 b 态：云端无水位 → 全量建基线，
// commit 带全量最大水位。
func TestSyncIncrementalFirstSyncBaseline(t *testing.T) {
	api := &mockAPI{syncID: "sync-base"}
	fake := &fakeIncAdapter{fakeAdapter: &fakeAdapter{
		columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}, {Name: "updated_at", Type: adapters.TypeText}},
		pks:     []string{"id"},
		batches: [][]adapters.Row{{
			{"id": int64(1), "updated_at": "2026-10-06T04:00:00Z"},
			{"id": int64(3), "updated_at": "2026-10-06T06:00:00Z"},
			{"id": int64(2), "updated_at": "2026-10-06T05:00:00Z"}, // 乱序
		}},
	}}
	logs, logf := captureLogs()
	r := &Runner{
		Client:  api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
		Logf:    logf,
	}
	result, err := r.Sync(context.Background(), incDataset()) // WatermarkValue 为空
	if err != nil {
		t.Fatalf("Sync: %v", err)
	}
	if result.Rows != 3 {
		t.Fatalf("结果 = %#v", result)
	}
	if fake.snapCalls != 1 || fake.incCalls != 0 {
		t.Fatalf("b 态应走 Snapshot（%d 次）且不碰 Incremental（%d 次）", fake.snapCalls, fake.incCalls)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitBody.WatermarkValue == nil || *api.commitBody.WatermarkValue != "2026-10-06T06:00:00Z" {
		t.Fatalf("基线水位 = %#v, want 2026-10-06T06:00:00Z", api.commitBody.WatermarkValue)
	}
	if !logsContain(logs, "全量建基线") {
		t.Fatalf("b 态日志应说明建基线: %v", *logs)
	}
}

// TestSyncIncrementalEmptyWindowKeepsWatermark 覆盖 a 态零行：增量窗口无新行，
// commit 不带水位（云端水位不动）。
func TestSyncIncrementalEmptyWindowKeepsWatermark(t *testing.T) {
	api := &mockAPI{syncID: "sync-empty"}
	fake := &fakeIncAdapter{fakeAdapter: &fakeAdapter{
		columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}, {Name: "updated_at", Type: adapters.TypeText}},
	}}
	r := &Runner{
		Client:  api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
	}
	ds := incDataset()
	ds.WatermarkValue = "2026-10-07T00:00:00Z"
	result, err := r.Sync(context.Background(), ds)
	if err != nil {
		t.Fatalf("Sync: %v", err)
	}
	if result.Rows != 0 || result.Chunks != 0 {
		t.Fatalf("零行结果 = %#v", result)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitCalls != 1 {
		t.Fatalf("零行窗口也应 commit，实际 %d 次", api.commitCalls)
	}
	if api.commitBody.WatermarkValue != nil {
		t.Fatalf("零行不应上报水位: %#v", *api.commitBody.WatermarkValue)
	}
}

// TestSyncIncrementalEmptyTableBaselineNoWatermark 覆盖 b 态零行：空表基线
// 不带水位（云端水位保持空，下次仍全量）。
func TestSyncIncrementalEmptyTableBaselineNoWatermark(t *testing.T) {
	api := &mockAPI{syncID: "sync-empty"}
	fake := &fakeIncAdapter{fakeAdapter: &fakeAdapter{
		columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}, {Name: "updated_at", Type: adapters.TypeText}},
	}}
	r := &Runner{
		Client:  api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
	}
	if _, err := r.Sync(context.Background(), incDataset()); err != nil {
		t.Fatalf("Sync: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitBody.WatermarkValue != nil {
		t.Fatalf("空表基线不应上报水位: %#v", *api.commitBody.WatermarkValue)
	}
}

// TestSyncIncrementalFallsBackWhenAdapterLacksSupport 覆盖 c 态：
// 源不支持增量读取（如文件源）→ 回退全量 Snapshot、日志说明原因、不带水位。
func TestSyncIncrementalFallsBackWhenAdapterLacksSupport(t *testing.T) {
	for _, wm := range []string{"", "2026-10-07T00:00:00Z"} {
		t.Run("watermark="+wm, func(t *testing.T) {
			api := &mockAPI{syncID: "sync-fb"}
			fake := &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}, {Name: "updated_at", Type: adapters.TypeText}},
				batches: [][]adapters.Row{{{"id": int64(1), "updated_at": "2026-10-07T09:00:00Z"}}},
			}
			logs, logf := captureLogs()
			r := &Runner{
				Client:  api.newClient(t),
				Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
				Logf:    logf,
			}
			ds := incDataset()
			ds.WatermarkValue = wm
			result, err := r.Sync(context.Background(), ds)
			if err != nil {
				t.Fatalf("回退全量应照常成功: %v", err)
			}
			if result.Rows != 1 || fake.snapCalls != 1 {
				t.Fatalf("回退应走 Snapshot 推送全量行: %#v snap=%d", result, fake.snapCalls)
			}
			if !logsContain(logs, "不支持增量") {
				t.Fatalf("日志应说明回退原因: %v", *logs)
			}
			api.mu.Lock()
			defer api.mu.Unlock()
			if api.commitBody.WatermarkValue != nil {
				t.Fatalf("c 态不上报新水位（云端水位不动）: %#v", *api.commitBody.WatermarkValue)
			}
		})
	}
}

// TestSyncSnapshotModeNeverTouchesIncremental 覆盖 d 态：
// 非 inc（""|"snapshot"）→ 完全现状：Snapshot 推送、commit 不带水位。
func TestSyncSnapshotModeNeverTouchesIncremental(t *testing.T) {
	for _, mode := range []string{"", "snapshot"} {
		t.Run("mode="+mode, func(t *testing.T) {
			api := &mockAPI{syncID: "sync-snap"}
			fake := &fakeIncAdapter{fakeAdapter: &fakeAdapter{
				columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}},
				batches: [][]adapters.Row{{{"id": int64(1)}}},
			}}
			r := &Runner{
				Client:  api.newClient(t),
				Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
			}
			ds := testDataset()
			ds.SyncMode = mode
			ds.WatermarkColumn = "updated_at" // 配了水位列也不影响全量判定
			ds.WatermarkValue = "2026-10-07T00:00:00Z"
			if _, err := r.Sync(context.Background(), ds); err != nil {
				t.Fatalf("Sync: %v", err)
			}
			if fake.snapCalls != 1 || fake.incCalls != 0 {
				t.Fatalf("全量模式应只走 Snapshot（%d）不碰 Incremental（%d）", fake.snapCalls, fake.incCalls)
			}
			api.mu.Lock()
			defer api.mu.Unlock()
			if api.commitBody.WatermarkValue != nil {
				t.Fatalf("全量模式 commit 不应带水位: %#v", *api.commitBody.WatermarkValue)
			}
		})
	}
}

// TestSyncIncrementalAbortsOnError 增量读取失败同样走 abort 清理。
func TestSyncIncrementalAbortsOnError(t *testing.T) {
	api := &mockAPI{syncID: "sync-inc"}
	fake := &fakeIncAdapter{fakeAdapter: &fakeAdapter{
		columns: []adapters.Column{{Name: "id", Type: adapters.TypeInteger}, {Name: "updated_at", Type: adapters.TypeText}},
	}}
	fake.incErr = errors.New("源库连接中断")
	r := &Runner{
		Client:  api.newClient(t),
		Factory: func(client.DatasetConfig) (adapters.Adapter, error) { return fake, nil },
	}
	ds := incDataset()
	ds.WatermarkValue = "2026-10-07T00:00:00Z"
	_, err := r.Sync(context.Background(), ds)
	if err == nil || !strings.Contains(err.Error(), "源库连接中断") {
		t.Fatalf("应报增量读取错误: %v", err)
	}
	api.mu.Lock()
	defer api.mu.Unlock()
	if api.commitCalls != 0 || api.abortCalls != 1 {
		t.Fatalf("失败不应 commit 且必须 abort: commit=%d abort=%d", api.commitCalls, api.abortCalls)
	}
}
