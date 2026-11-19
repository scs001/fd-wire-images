package scheduler

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/client"
)

// harness 组装一个用假心跳/假配置/假同步驱动的调度器。
type harness struct {
	sched    *Scheduler
	now      time.Time
	cfg      *client.RemoteConfig
	syncErrs map[string]error
	mu       sync.Mutex
	synced   []string
	hbCalls  int
}

func newHarness(t *testing.T, sources map[string]bool, datasets ...client.DatasetConfig) *harness {
	t.Helper()
	h := &harness{
		now:      time.Date(2026, 10, 6, 10, 0, 30, 0, time.UTC),
		cfg:      &client.RemoteConfig{Datasets: datasets},
		syncErrs: map[string]error{},
	}
	h.sched = New(
		func(context.Context) (*client.HeartbeatResponse, error) {
			h.mu.Lock()
			defer h.mu.Unlock()
			h.hbCalls++
			return &client.HeartbeatResponse{PollIntervalSeconds: 60}, nil
		},
		func(context.Context) (*client.RemoteConfig, error) {
			h.mu.Lock()
			defer h.mu.Unlock()
			return h.cfg, nil
		},
		func(_ context.Context, ds client.DatasetConfig) error {
			h.mu.Lock()
			h.synced = append(h.synced, ds.Key)
			err := h.syncErrs[ds.Key]
			h.mu.Unlock()
			return err
		},
		sources,
	)
	h.sched.Now = func() time.Time { return h.now }
	return h
}

func (h *harness) cycle(t *testing.T) {
	t.Helper()
	if _, err := h.sched.Cycle(context.Background()); err != nil {
		t.Fatalf("Cycle: %v", err)
	}
	h.sched.Wait()
}

func (h *harness) syncedKeys() []string {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]string(nil), h.synced...)
}

func TestDispatchRunsDueDatasetOnce(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales", SyncCron: "0 * * * *",
	})
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 || got[0] != "sales" {
		t.Fatalf("首次见到数据集应立即同步一次，得到 %v", got)
	}
	if state := h.sched.SyncState(); state["sales"] != "ok" {
		t.Fatalf("同步状态 = %v", state)
	}
	// 30 分钟后仍不到下一个整点：不应再跑。
	h.now = h.now.Add(30 * time.Minute)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 {
		t.Fatalf("cron 未到期不应重复同步，得到 %v", got)
	}
	// 越过整点后应再次同步。
	h.now = h.now.Add(31 * time.Minute)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 2 {
		t.Fatalf("cron 到期应再次同步，得到 %v", got)
	}
}

func TestMinSyncIntervalRespected(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales", // 无 cron：只受最小间隔约束
	})
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 {
		t.Fatalf("首轮应同步，得到 %v", got)
	}
	h.now = h.now.Add(1 * time.Minute)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 {
		t.Fatalf("最小间隔 %s 内不应重复同步，得到 %v", client.DefaultMinSyncMinutes*time.Minute, got)
	}
	h.now = h.now.Add(20 * time.Minute)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 2 {
		t.Fatalf("超过最小间隔应再次同步，得到 %v", got)
	}
}

func TestDatasetLevelMinSyncOverridesGlobal(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales", MinSyncMinutes: 1,
	})
	h.cycle(t)
	h.now = h.now.Add(2 * time.Minute)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 2 {
		t.Fatalf("数据集级最小间隔（1 分钟）应生效，得到 %v", got)
	}
}

func TestConcurrentSyncOfSameDatasetIsLocked(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{}, 1)
	var mu sync.Mutex
	calls := 0
	sched := New(
		func(context.Context) (*client.HeartbeatResponse, error) { return nil, nil },
		func(context.Context) (*client.RemoteConfig, error) {
			return &client.RemoteConfig{Datasets: []client.DatasetConfig{
				{Key: "sales", Source: "erp_db", SourceRef: "public.sales"},
			}}, nil
		},
		func(context.Context, client.DatasetConfig) error {
			mu.Lock()
			calls++
			mu.Unlock()
			started <- struct{}{}
			<-release
			return nil
		},
		map[string]bool{"erp_db": true},
	)
	sched.Now = time.Now

	ctx := context.Background()
	if _, err := sched.Cycle(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(2 * time.Second):
		t.Fatal("首轮同步未启动")
	}
	// 同步仍在飞：第二轮必须被并发锁挡住。
	if _, err := sched.Cycle(ctx); err != nil {
		t.Fatal(err)
	}
	close(release)
	sched.Wait()
	mu.Lock()
	defer mu.Unlock()
	if calls != 1 {
		t.Fatalf("同一数据集并发同步应被锁住，实际启动 %d 次", calls)
	}
}

func TestMissingLocalSourceIsSkippedAndWarned(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true},
		client.DatasetConfig{Key: "sales", Source: "erp_db", SourceRef: "public.sales"},
		client.DatasetConfig{Key: "stock", Source: "missing_db", SourceRef: "public.stock"},
	)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 || got[0] != "sales" {
		t.Fatalf("源缺失的数据集应跳过，得到 %v", got)
	}
	warnings := h.sched.Warnings()
	if len(warnings) != 1 || !strings.Contains(warnings[0], "missing_db") || !strings.Contains(warnings[0], "stock") {
		t.Fatalf("心跳告警应回报缺失源: %v", warnings)
	}
	// 本地补上源后告警应消失。
	h.sched.LocalSources["missing_db"] = true
	h.cycle(t)
	if warnings := h.sched.Warnings(); len(warnings) != 0 {
		t.Fatalf("源就绪后告警应清除: %v", warnings)
	}
	// 补上源后两个数据集都应同步（首轮只跑了 sales）。
	if got := h.syncedKeys(); len(got) != 2 || got[1] != "stock" {
		t.Fatalf("补上源后 stock 应被同步，得到 %v", got)
	}
}

func TestFailedSyncIsWarnedAndClearedOnSuccess(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales",
	})
	h.syncErrs["sales"] = errors.New("连接被拒绝")
	h.cycle(t)
	if state := h.sched.SyncState(); state["sales"] != "failed" {
		t.Fatalf("失败状态 = %v", state)
	}
	warnings := h.sched.Warnings()
	if len(warnings) != 1 || !strings.Contains(warnings[0], "连接被拒绝") {
		t.Fatalf("失败原因应进心跳告警: %v", warnings)
	}
	delete(h.syncErrs, "sales")
	h.now = h.now.Add(30 * time.Minute)
	h.cycle(t)
	if warnings := h.sched.Warnings(); len(warnings) != 0 {
		t.Fatalf("恢复成功后告警应清除: %v", warnings)
	}
	if state := h.sched.SyncState(); state["sales"] != "ok" {
		t.Fatalf("状态应回到 ok: %v", state)
	}
}

func TestDisabledAndRemovedDatasets(t *testing.T) {
	disabled := false
	h := newHarness(t, map[string]bool{"erp_db": true},
		client.DatasetConfig{Key: "sales", Source: "erp_db", SourceRef: "public.sales", SyncEnabled: &disabled},
		client.DatasetConfig{Key: "stock", Source: "missing_db", SourceRef: "public.stock"},
	)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 0 {
		t.Fatalf("停用数据集不应同步: %v", got)
	}
	// 白名单移除后，残留告警应被清掉。
	h.cfg = &client.RemoteConfig{Datasets: []client.DatasetConfig{}}
	h.cycle(t)
	if warnings := h.sched.Warnings(); len(warnings) != 0 {
		t.Fatalf("白名单移除后不应残留告警: %v", warnings)
	}
	if state := h.sched.SyncState(); len(state) != 0 {
		t.Fatalf("白名单移除后不应残留状态: %v", state)
	}
}

func TestInvalidCronIsWarnedNotFatal(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true},
		client.DatasetConfig{Key: "sales", Source: "erp_db", SourceRef: "public.sales", SyncCron: "not a cron"},
		client.DatasetConfig{Key: "stock", Source: "erp_db", SourceRef: "public.stock"},
	)
	h.cycle(t)
	if got := h.syncedKeys(); len(got) != 1 || got[0] != "stock" {
		t.Fatalf("非法 cron 只应跳过该数据集，得到 %v", got)
	}
	found := false
	for _, w := range h.sched.Warnings() {
		if strings.Contains(w, "调度表达式") {
			found = true
		}
	}
	if !found {
		t.Fatalf("非法 cron 应告警: %v", h.sched.Warnings())
	}
}

func TestConfigFetchFailureKeepsLastConfig(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales",
	})
	h.cycle(t)
	// 之后配置拉取一直失败：应沿用上一份配置并继续调度。
	var fail bool
	h.sched.FetchConfig = func(context.Context) (*client.RemoteConfig, error) {
		if fail {
			return nil, errors.New("网络不可达")
		}
		return h.cfg, nil
	}
	fail = true
	h.now = h.now.Add(30 * time.Minute)
	if _, err := h.sched.Cycle(context.Background()); err == nil {
		t.Fatal("配置拉取失败应回报错误")
	}
	h.sched.Wait()
	if got := h.syncedKeys(); len(got) != 2 {
		t.Fatalf("应沿用上一份配置继续同步，得到 %v", got)
	}
}

func TestHeartbeatFailureIsReported(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales",
	})
	h.sched.Heartbeat = func(context.Context) (*client.HeartbeatResponse, error) {
		return nil, errors.New("401 连接器 key 已吊销")
	}
	if _, err := h.sched.Cycle(context.Background()); err == nil || !strings.Contains(err.Error(), "心跳失败") {
		t.Fatalf("心跳失败应回报: %v", err)
	}
	if got := h.syncedKeys(); len(got) != 0 {
		t.Fatalf("心跳失败时本轮不再调度（推送同样无法送达）：%v", got)
	}
}

func TestRunStopsOnContextCancel(t *testing.T) {
	h := newHarness(t, map[string]bool{"erp_db": true}, client.DatasetConfig{
		Key: "sales", Source: "erp_db", SourceRef: "public.sales",
	})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- h.sched.Run(ctx) }()
	time.Sleep(50 * time.Millisecond)
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("停止应返回 context.Canceled: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("Run 未随 ctx 取消退出")
	}
}

func TestPollIntervalFallback(t *testing.T) {
	s := New(nil, nil, nil, nil)
	if got := s.pollInterval(); got != client.DefaultHeartbeatInterval {
		t.Fatalf("缺省轮询周期 = %s, want %s（契约 §5）", got, client.DefaultHeartbeatInterval)
	}
	s.PollInterval = 5 * time.Second
	if got := s.pollInterval(); got != 5*time.Second {
		t.Fatalf("人工覆盖周期 = %s", got)
	}
}
