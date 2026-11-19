// Command fd-wire-connector 是柏讯出站连接器（契约 §5）：
// 单二进制驻客户内网，只发起出站连接（零入站开孔）——
// 配对换 key → 心跳与配置轮询 → 按云端调度把白名单源的全量快照推给 wire api。
//
// 客户源凭据只存在本地配置文件（0600），云端零凭据（ADR-0004）。
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"runtime"
	"strings"
	"syscall"
	"time"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters/file"
	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters/rest"
	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters/mysql"
	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters/postgres"
	"github.com/FindDataTechnology/fd-wire/connector/internal/client"
	"github.com/FindDataTechnology/fd-wire/connector/internal/config"
	"github.com/FindDataTechnology/fd-wire/connector/internal/runner"
	"github.com/FindDataTechnology/fd-wire/connector/internal/scheduler"
)

// version 由构建注入：-ldflags "-X main.version=sha-<sha>"（CI 注入 git sha）。
var version = "dev"

const usageText = `fd-wire-connector —— 柏讯出站连接器（只发起出站连接，无需入站开孔）

用法:
  fd-wire-connector pair --token fwp_xxx [--api https://wire.finddatatech.cloud] [--name 机器名] [--config 路径]
  fd-wire-connector run  [--config 路径] [--once] [--heartbeat-interval 60s]
  fd-wire-connector version

子命令:
  pair     用控制台「配对向导」生成的一次性令牌换取连接器 key，写入本地配置（0600）
  run      常驻运行：心跳 + 配置轮询 + 按云端调度全量同步（--once 跑一轮后退出）
  version  打印版本

配置默认路径:
  Linux/macOS  $XDG_CONFIG_HOME/fd-wire-connector/config.json（缺省 ~/.config/fd-wire-connector/config.json）
  Windows      %APPDATA%\fd-wire-connector\config.json
客户源凭据（DSN）只存在该文件，永不上传云端。
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintf(os.Stderr, "错误: %v\n", err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Fprint(os.Stderr, usageText)
		return errors.New("缺少子命令")
	}
	switch args[0] {
	case "pair":
		return cmdPair(args[1:])
	case "run":
		return cmdRun(args[1:])
	case "version", "--version", "-v":
		fmt.Printf("fd-wire-connector %s (%s/%s)\n", version, runtime.GOOS, runtime.GOARCH)
		return nil
	case "help", "-h", "--help":
		fmt.Print(usageText)
		return nil
	default:
		fmt.Fprint(os.Stderr, usageText)
		return fmt.Errorf("未知子命令 %q", args[0])
	}
}

// cmdPair 用一次性令牌换取连接器 key 并落盘（唯一存 key 的地方）。
func cmdPair(args []string) error {
	fs := flag.NewFlagSet("pair", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	token := fs.String("token", "", "控制台生成的一次性配对令牌（fwp_ 开头）")
	apiURL := fs.String("api", "", "wire api 基地址（缺省沿用本地配置里的 api_url）")
	name := fs.String("name", "", "连接器显示名（缺省为机器名）")
	configPath := fs.String("config", "", "本地配置文件路径（缺省按平台）")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if strings.TrimSpace(*token) == "" {
		return errors.New("缺少 --token（控制台「配对向导」生成，15 分钟内有效、单次使用）")
	}

	path, err := resolveConfigPath(*configPath)
	if err != nil {
		return err
	}
	cfg := &config.Config{}
	if existing, err := config.LoadFrom(path); err == nil {
		cfg = existing // 已配对过：保留既有 sources 与 api_url
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("读取既有配置失败（未覆盖）: %w", err)
	}
	if strings.TrimSpace(*apiURL) != "" {
		cfg.APIURL = strings.TrimSpace(*apiURL)
	}
	if cfg.APIURL == "" {
		return errors.New("缺少 --api：首次配对需给出 wire api 基地址（如 https://wire.finddatatech.cloud）")
	}
	displayName := strings.TrimSpace(*name)
	if displayName == "" {
		if hostname, err := os.Hostname(); err == nil {
			displayName = hostname
		} else {
			displayName = "fd-wire-connector"
		}
	}

	ctx, cancel := signalContext()
	defer cancel()
	resp, err := client.Pair(ctx, cfg.APIURL, *token, displayName, version, client.WithVersion(version))
	if err != nil {
		return fmt.Errorf("配对失败: %w", err)
	}
	cfg.ConnectorKey = resp.ConnectorKey
	cfg.SetPath(path)
	if err := cfg.Save(); err != nil {
		return err
	}
	fmt.Printf("配对成功：connector_id=%s\n", resp.ConnectorID)
	fmt.Printf("连接器 key 已写入 %s（0600，内含凭据，勿拷贝外传）\n", path)
	fmt.Printf("下一步：在本地配置 sources 段补上客户源（%s），然后运行 fd-wire-connector run\n", "postgres/mysql/file")
	return nil
}

// cmdRun 常驻运行心跳、配置轮询与按调度同步。
func cmdRun(args []string) error {
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	fs.SetOutput(os.Stderr)
	configPath := fs.String("config", "", "本地配置文件路径（缺省按平台）")
	once := fs.Bool("once", false, "只执行一轮（心跳 + 配置轮询 + 到期同步）后退出，用于冒烟")
	heartbeatInterval := fs.Duration("heartbeat-interval", client.DefaultHeartbeatInterval, "心跳/配置轮询周期（云端可下发覆盖）")
	verbose := fs.Bool("verbose", false, "打印每步排障日志")
	if err := fs.Parse(args); err != nil {
		return err
	}

	path, err := resolveConfigPath(*configPath)
	if err != nil {
		return err
	}
	cfg, err := config.LoadFrom(path)
	if err != nil {
		return err
	}
	if cfg.ConnectorKey == "" {
		return fmt.Errorf("配置 %s 尚无 connector_key：先运行 fd-wire-connector pair --token fwp_...", path)
	}
	if warning := cfg.PermissionWarning(); warning != "" {
		logf("%s", warning)
	}
	api, err := client.New(cfg.APIURL, cfg.ConnectorKey, client.WithVersion(version))
	if err != nil {
		return err
	}
	logf("连接器启动：version=%s api=%s 本地源=%d 个 配置文件=%s", version, api.APIURL(), len(cfg.Sources), path)

	syncRunner := &runner.Runner{
		Client:  api,
		Factory: adapterFactory(cfg),
		Logf:    logf,
	}
	var sched *scheduler.Scheduler
	sendHeartbeat := func(ctx context.Context) (*client.HeartbeatResponse, error) {
		resp, err := api.Heartbeat(ctx, client.HeartbeatRequest{
			Version:   version,
			SyncState: sched.SyncState(),
			Warnings:  sched.Warnings(),
		})
		if err != nil {
			return nil, err
		}
		if *verbose {
			logf("心跳已上报（下一轮询周期 %ds）", resp.PollIntervalSeconds)
		}
		return resp, nil
	}
	sched = scheduler.New(sendHeartbeat, api.FetchConfig, func(ctx context.Context, ds client.DatasetConfig) error {
		result, err := syncRunner.Sync(ctx, ds)
		if err != nil {
			return err
		}
		if *verbose {
			logf("数据集 %s：%d 行 %d 块已提交", ds.Key, result.Rows, result.Chunks)
		}
		return nil
	}, cfg.SourceNames())
	sched.PollInterval = *heartbeatInterval
	sched.Logf = logf

	ctx, cancel := signalContext()
	defer cancel()
	if *once {
		hb, err := sched.Cycle(ctx)
		if err != nil {
			return err
		}
		sched.Wait()
		// 收尾心跳：把本轮同步产生的状态与告警一并带出去。常驻模式下这由
		// 下一轮心跳承担（心跳上报的是上一轮结果，标准监控口径），--once 只有一轮，
		// 因此这里显式补一次，让单轮冒烟可自证。
		if _, err := sendHeartbeat(ctx); err != nil {
			logf("收尾心跳失败: %v", err)
		}
		logf("单轮完成：心跳周期 %s，告警 %d 条", hb.Interval(*heartbeatInterval), len(sched.Warnings()))
		return nil
	}
	if err := sched.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
		return err
	}
	logf("已停止")
	return nil
}

// adapterFactory 把云端数据集映射到本地源适配器（新源型在此注册，核心不动）。
// restShapeFromClient 把云端下发的 REST 形状转为适配器入参（空形状=422
// 语义的登记缺失——连接器侧如实报错）。
func restShapeFromClient(rc *client.RestConfig) (rest.Config, error) {
	if rc == nil || strings.TrimSpace(rc.URL) == "" {
		return rest.Config{}, fmt.Errorf("rest 源缺少云端形状（source_config）——请重新登记数据集")
	}
	projections := make([]rest.Projection, 0, len(rc.Projection))
	for _, proj := range rc.Projection {
		projections = append(projections, rest.Projection{
			Path: proj.Path, Column: proj.Column, Type: proj.Type,
		})
	}
	var pagination *rest.Pagination
	if rc.Pagination != nil {
		pagination = &rest.Pagination{
			Mode: rc.Pagination.Mode, PageParam: rc.Pagination.PageParam,
			SizeParam: rc.Pagination.SizeParam, StartPage: rc.Pagination.StartPage,
			CursorParam: rc.Pagination.CursorParam, CursorPath: rc.Pagination.CursorPath,
			PageSize: rc.Pagination.PageSize, MaxPages: rc.Pagination.MaxPages,
		}
	}
	return rest.Config{
		URL: rc.URL, Method: rc.Method, Headers: rc.Headers,
		RootPath: rc.RootPath, Projection: projections,
		Pagination: pagination, WatermarkPath: rc.WatermarkPath,
	}, nil
}

// restAuthFromConfig 本地认证 → 适配器认证（凭据不出本进程边界）。
func restAuthFromConfig(auth *config.RestAuth) *rest.Auth {
	if auth == nil {
		return nil
	}
	return &rest.Auth{
		Type: auth.Type, Token: auth.Token,
		Username: auth.Username, Password: auth.Password,
	}
}

func adapterFactory(cfg *config.Config) runner.AdapterFactory {
	return func(ds client.DatasetConfig) (adapters.Adapter, error) {
		src, ok := cfg.Source(ds.Source)
		if !ok {
			return nil, fmt.Errorf("数据集 %s：本地 sources 未配置源 %q（连接器已跳过，见心跳告警）", ds.Key, ds.Source)
		}
		switch src.Kind {
		case config.KindPostgres:
			return postgres.New(src.DSN, ds.SourceRef, client.MaxChunkRows), nil
		case config.KindMySQL:
			return mysql.New(src.DSN, ds.SourceRef, client.MaxChunkRows), nil
		case config.KindFile:
			return file.New(src.Path, ds.SourceRef, client.MaxChunkRows), nil
		case config.KindRest:
			shape, err := restShapeFromClient(ds.RestConfig)
			if err != nil {
				return nil, fmt.Errorf("数据集 %s：%w", ds.Key, err)
			}
			return rest.NewFromClient(shape, restAuthFromConfig(src.Auth), client.MaxChunkRows), nil
		default:
			return nil, fmt.Errorf("源 %s：不支持的 kind %q", src.Name, src.Kind)
		}
	}
}

func resolveConfigPath(flagValue string) (string, error) {
	if trimmed := strings.TrimSpace(flagValue); trimmed != "" {
		return trimmed, nil
	}
	return config.DefaultPath()
}

func signalContext() (context.Context, context.CancelFunc) {
	return signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
}

func logf(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "[fd-wire-connector %s] %s\n", time.Now().Format("2006-01-02T15:04:05Z07:00"), fmt.Sprintf(format, args...))
}
