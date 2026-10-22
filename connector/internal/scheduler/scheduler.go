// Package scheduler 负责配置轮询节奏、cron 调度、同数据集并发锁与最小间隔
// （契约 §5）：心跳与配置拉取按云端下发的 poll_interval_seconds 走；
// 白名单里 source 名在本地 sources 找不到的数据集跳过并进 heartbeat warnings。
package scheduler

import (
	"context"
	"fmt"
	"sort"
	"sync"
	"time"

	"github.com/robfig/cron/v3"

	"github.com/FindDataTechnology/fd-wire/connector/internal/client"
)

// HeartbeatFunc / ConfigFunc / SyncFunc 是调度器对外的注入点（测试可替身）。
type (
	HeartbeatFunc func(ctx context.Context) (*client.HeartbeatResponse, error)
	ConfigFunc    func(ctx context.Context) (*client.RemoteConfig, error)
	SyncFunc      func(ctx context.Context, ds client.DatasetConfig) error
)

// Scheduler 驱动心跳、配置轮询与按 cron 的全量同步。
type Scheduler struct {
	Heartbeat   HeartbeatFunc
	FetchConfig ConfigFunc
	Sync        SyncFunc

	// LocalSources 是本地配置里已定义的源名集合；云端数据集引用不到的即跳过并告警。
	LocalSources map[string]bool

	// PollInterval 是云端未下发周期时的兜底心跳/轮询周期。
	PollInterval time.Duration
	// Now 可注入时钟（测试用）。
	Now  func() time.Time
	Logf func(format string, args ...any)

	mu       sync.Mutex
	locks    map[string]bool
	lastRun  map[string]time.Time
	nextDue  map[string]time.Time
	parsed   map[string]cron.Schedule
	warnings map[string]string
	state    map[string]string
	lastCfg  *client.RemoteConfig
	wg       sync.WaitGroup
}

// New 构造调度器。
func New(hb HeartbeatFunc, cfg ConfigFunc, sync SyncFunc, localSources map[string]bool) *Scheduler {
	return &Scheduler{
		Heartbeat:    hb,
		FetchConfig:  cfg,
		Sync:         sync,
		LocalSources: localSources,
		PollInterval: client.DefaultHeartbeatInterval,
		Now:          time.Now,
		locks:        map[string]bool{},
		lastRun:      map[string]time.Time{},
		nextDue:      map[string]time.Time{},
		parsed:       map[string]cron.Schedule{},
		warnings:     map[string]string{},
		state:        map[string]string{},
	}
}

// Run 阻塞运行直到 ctx 取消，然后等待在飞同步收尾。
func (s *Scheduler) Run(ctx context.Context) error {
	interval := s.pollInterval()
	s.logf("调度启动：心跳/轮询周期 %s", interval)
	for {
		hb, err := s.Cycle(ctx)
		if err != nil && ctx.Err() == nil {
			s.logf("周期失败（下一轮重试）: %v", err)
		}
		if ctx.Err() != nil {
			s.Wait()
			return ctx.Err()
		}
		if hb != nil {
			interval = hb.Interval(s.pollInterval())
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			s.Wait()
			return ctx.Err()
		case <-timer.C:
		}
	}
}

// Cycle 执行一轮：心跳 → 配置轮询 → 启动到期同步（同步在后台跑，不阻塞心跳）。
func (s *Scheduler) Cycle(ctx context.Context) (*client.HeartbeatResponse, error) {
	var hb *client.HeartbeatResponse
	var hbErr error
	if s.Heartbeat != nil {
		hb, hbErr = s.Heartbeat(ctx)
		if hbErr != nil {
			return nil, fmt.Errorf("心跳失败: %w", hbErr)
		}
	}
	cfg, cfgErr := s.fetchConfig(ctx)
	if cfg != nil {
		// 配置拉取失败时沿用上一份配置继续调度（下一轮再试）。
		s.dispatch(ctx, cfg)
	}
	return hb, cfgErr
}

// Wait 等待在飞同步结束。
func (s *Scheduler) Wait() { s.wg.Wait() }

// SyncState 返回各数据集最近一次同步状态（进心跳上报）。
func (s *Scheduler) SyncState() map[string]string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.state) == 0 {
		return nil
	}
	out := make(map[string]string, len(s.state))
	for k, v := range s.state {
		out[k] = v
	}
	return out
}

// Warnings 返回当前诊断告警（进心跳上报，不含任何凭据）。
func (s *Scheduler) Warnings() []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.warnings) == 0 {
		return nil
	}
	out := make([]string, 0, len(s.warnings))
	for _, msg := range s.warnings {
		out = append(out, msg)
	}
	sort.Strings(out)
	return out
}

func (s *Scheduler) fetchConfig(ctx context.Context) (*client.RemoteConfig, error) {
	if s.FetchConfig == nil {
		return s.lastCfg, nil
	}
	cfg, err := s.FetchConfig(ctx)
	if err != nil {
		if last := s.lastConfig(); last != nil {
			// 配置拉取失败不中断调度：沿用上一份配置，等下一轮。
			return last, fmt.Errorf("配置拉取失败（沿用上一份配置）: %w", err)
		}
		return nil, fmt.Errorf("配置拉取失败: %w", err)
	}
	s.mu.Lock()
	s.lastCfg = cfg
	s.mu.Unlock()
	return cfg, nil
}

func (s *Scheduler) lastConfig() *client.RemoteConfig {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.lastCfg
}

// dispatch 挑出到期数据集并启动同步。
func (s *Scheduler) dispatch(ctx context.Context, cfg *client.RemoteConfig) {
	if cfg == nil || s.Sync == nil {
		return
	}
	now := s.now()
	seen := make(map[string]bool, len(cfg.Datasets))
	for _, ds := range cfg.Datasets {
		seen[ds.Key] = true
		if !ds.Enabled() {
			s.dropWarning(ds.Key)
			continue
		}
		if s.LocalSources != nil && !s.LocalSources[ds.Source] {
			s.setWarning(ds.Key, fmt.Sprintf("数据集 %s：本地 sources 未配置源 %q，已跳过（契约 §5）", ds.Key, ds.Source))
			continue
		}
		if !s.due(ds, cfg, now) {
			continue
		}
		s.launch(ctx, ds)
	}
	// 云端已移除的数据集：清掉残留告警与状态。
	s.pruneKeys(seen)
}

func (s *Scheduler) due(ds client.DatasetConfig, cfg *client.RemoteConfig, now time.Time) bool {
	schedule, ok := s.scheduleFor(ds)
	if !ok {
		return false
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if ds.SyncCron != "" {
		next, exists := s.nextDue[ds.Key]
		if !exists {
			// 首次见到该数据集即同步一次（配置落地即可见数据）。
			s.nextDue[ds.Key] = schedule.Next(now)
			return true
		}
		if now.Before(next) {
			return false
		}
		s.nextDue[ds.Key] = schedule.Next(now)
	}
	minSync := ds.MinSync(cfg.MinSync())
	if last, exists := s.lastRun[ds.Key]; exists && now.Sub(last) < minSync {
		s.logf("数据集 %s：距上次同步不足最小间隔 %s，本轮跳过", ds.Key, minSync)
		return false
	}
	return true
}

func (s *Scheduler) scheduleFor(ds client.DatasetConfig) (cron.Schedule, bool) {
	if ds.SyncCron == "" {
		return nil, true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if schedule, ok := s.parsed[ds.Key+"|"+ds.SyncCron]; ok {
		return schedule, true
	}
	schedule, err := cron.ParseStandard(ds.SyncCron)
	if err != nil {
		s.warnings[ds.Key] = fmt.Sprintf("数据集 %s：调度表达式 %q 无效，已跳过（%v）", ds.Key, ds.SyncCron, err)
		return nil, false
	}
	s.parsed[ds.Key+"|"+ds.SyncCron] = schedule
	return schedule, true
}

func (s *Scheduler) launch(ctx context.Context, ds client.DatasetConfig) {
	if !s.tryLock(ds.Key) {
		s.logf("数据集 %s：上一次同步仍在进行，本轮跳过（并发锁）", ds.Key)
		return
	}
	s.setState(ds.Key, "syncing")
	s.wg.Add(1)
	go func() {
		defer s.wg.Done()
		defer s.unlock(ds.Key)
		err := s.Sync(ctx, ds)
		s.mu.Lock()
		s.lastRun[ds.Key] = s.now()
		s.mu.Unlock()
		if err != nil {
			s.setState(ds.Key, "failed")
			s.setWarning(ds.Key, fmt.Sprintf("数据集 %s：同步失败：%v", ds.Key, err))
			s.logf("数据集 %s：同步失败: %v", ds.Key, err)
			return
		}
		s.setState(ds.Key, "ok")
		s.dropWarning(ds.Key)
	}()
}

func (s *Scheduler) tryLock(key string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.locks[key] {
		return false
	}
	s.locks[key] = true
	return true
}

func (s *Scheduler) unlock(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.locks, key)
}

func (s *Scheduler) setWarning(key, msg string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.warnings[key] = msg
}

func (s *Scheduler) dropWarning(key string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.warnings, key)
}

func (s *Scheduler) setState(key, state string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.state[key] = state
}

func (s *Scheduler) pruneKeys(seen map[string]bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key := range s.warnings {
		if !seen[key] {
			delete(s.warnings, key)
		}
	}
	for key := range s.state {
		if !seen[key] {
			delete(s.state, key)
		}
	}
}

func (s *Scheduler) pollInterval() time.Duration {
	if s.PollInterval <= 0 {
		return client.DefaultHeartbeatInterval
	}
	return s.PollInterval
}

func (s *Scheduler) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Scheduler) logf(format string, args ...any) {
	if s.Logf != nil {
		s.Logf(format, args...)
	}
}
