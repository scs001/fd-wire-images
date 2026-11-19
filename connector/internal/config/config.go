// Package config 读写连接器本地配置。
//
// 本地配置是客户源凭据（DSN）的唯一存放处（ADR-0004 / 契约 §5「云端零凭据」）：
// 按平台分路径、权限收紧到 0600（目录 0700）；DSN 永不上传云端。
//
// schema（契约 §5 冻结）：
//
//	{"api_url":"https://wire.finddatatech.cloud","connector_key":"fwc_...",
//	 "sources":[{"name":"erp_db","kind":"postgres","dsn":"postgres://user:pass@host/db"},
//	            {"name":"sales_mysql","kind":"mysql","dsn":"user:pass@tcp(host:3306)/sales"},
//	            {"name":"reports","kind":"file","path":"/data/reports"}]}
package config

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	// DirName 是配置目录名。
	DirName = "fd-wire-connector"
	// FileName 是配置文件名。
	FileName = "config.json"

	filePerm os.FileMode = 0o600
	dirPerm  os.FileMode = 0o700

	// KindPostgres / KindMySQL / KindFile 是本期支持的源类型（契约 §5）；
	// KindRest 是通用 REST/JSON 源（wire-rest-connector）——凭据（认证）只在
	// 本地配置，URL/分页/投影形状由云端下发（非敏感形状）。
	KindPostgres = "postgres"
	KindMySQL    = "mysql"
	KindFile     = "file"
	KindRest     = "rest"
)

// RestAuth 是 REST 源的本地认证配置（ADR-0004：凭据只存本地 0600）。
// Type=none 时不携带任何凭据头。
type RestAuth struct {
	Type     string `json:"type"`               // none | bearer | basic
	Token    string `json:"token,omitempty"`    // bearer 令牌
	Username string `json:"username,omitempty"` // basic 用户名
	Password string `json:"password,omitempty"` // basic 密码
}

// Source 是本地客户源定义。DSN（数据库）、Path（文件目录）与 RestAuth
//（REST 认证）只存在于本地配置——REST 的 URL/分页/投影形状由云端下发。
type Source struct {
	Name string    `json:"name"`
	Kind string    `json:"kind"`
	DSN  string    `json:"dsn,omitempty"`
	Path string    `json:"path,omitempty"`
	Auth *RestAuth `json:"auth,omitempty"` // 仅 kind=rest
}

// Config 是连接器本地配置。
type Config struct {
	APIURL       string   `json:"api_url"`
	ConnectorKey string   `json:"connector_key,omitempty"`
	Sources      []Source `json:"sources,omitempty"`

	path string
}

// Path 返回本配置的落盘路径（Load 之后可知）。
func (c *Config) Path() string { return c.path }

// SetPath 覆盖落盘路径（测试与 --config 使用）。
func (c *Config) SetPath(path string) { c.path = path }

// Source 按名字查找本地源。
func (c *Config) Source(name string) (Source, bool) {
	for _, s := range c.Sources {
		if s.Name == name {
			return s, true
		}
	}
	return Source{}, false
}

// SourceNames 返回本地源名集合。
func (c *Config) SourceNames() map[string]bool {
	names := make(map[string]bool, len(c.Sources))
	for _, s := range c.Sources {
		names[s.Name] = true
	}
	return names
}

// DefaultPath 返回当前平台的配置文件路径：
//   - linux / darwin：$XDG_CONFIG_HOME/fd-wire-connector/config.json，缺省 ~/.config/...（契约 §5）
//   - windows：%APPDATA%\fd-wire-connector\config.json
func DefaultPath() (string, error) {
	return defaultPath(runtime.GOOS, os.Getenv)
}

func defaultPath(goos string, getenv func(string) string) (string, error) {
	if goos == "windows" {
		base := strings.TrimSpace(getenv("APPDATA"))
		if base == "" {
			return "", errors.New("无法确定配置目录：环境变量 APPDATA 为空")
		}
		return filepath.Join(base, DirName, FileName), nil
	}
	// linux 与 darwin 同走 XDG 口径（macOS 亦用 ~/.config，契约 §5 明示）。
	if xdg := strings.TrimSpace(getenv("XDG_CONFIG_HOME")); xdg != "" {
		return filepath.Join(xdg, DirName, FileName), nil
	}
	home, err := os.UserHomeDir()
	if err != nil {
		return "", fmt.Errorf("无法确定用户主目录: %w", err)
	}
	return filepath.Join(home, ".config", DirName, FileName), nil
}

// Load 读取默认路径的配置。
func Load() (*Config, error) {
	path, err := DefaultPath()
	if err != nil {
		return nil, err
	}
	return LoadFrom(path)
}

// LoadFrom 读取指定路径的配置。未知字段被容忍（向前兼容云端演进）。
func LoadFrom(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("读取配置 %s 失败: %w", path, err)
	}
	cfg := &Config{}
	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("解析配置 %s 失败: %w", path, err)
	}
	cfg.path = path
	if err := cfg.Validate(); err != nil {
		return nil, fmt.Errorf("配置 %s 无效: %w", path, err)
	}
	return cfg, nil
}

// Validate 校验配置完备性。
func (c *Config) Validate() error {
	if strings.TrimSpace(c.APIURL) == "" {
		return errors.New("api_url 不能为空")
	}
	u, err := url.Parse(strings.TrimSpace(c.APIURL))
	if err != nil || u.Host == "" {
		return fmt.Errorf("api_url 无效: %q", c.APIURL)
	}
	seen := make(map[string]bool, len(c.Sources))
	for i, s := range c.Sources {
		if strings.TrimSpace(s.Name) == "" {
			return fmt.Errorf("sources[%d] 缺少 name", i)
		}
		if seen[s.Name] {
			return fmt.Errorf("sources 存在重名: %q", s.Name)
		}
		seen[s.Name] = true
		switch s.Kind {
		case KindPostgres, KindMySQL:
			if strings.TrimSpace(s.DSN) == "" {
				return fmt.Errorf("源 %s（%s）缺少 dsn", s.Name, s.Kind)
			}
		case KindFile:
			if strings.TrimSpace(s.Path) == "" {
				return fmt.Errorf("源 %s（file）缺少 path", s.Name)
			}
		case KindRest:
			if s.Auth != nil {
				switch s.Auth.Type {
				case "", "none", "bearer", "basic":
				default:
					return fmt.Errorf("源 %s（rest）认证类型 %q 不支持（可用 none/bearer/basic）", s.Name, s.Auth.Type)
				}
				if s.Auth.Type == "bearer" && strings.TrimSpace(s.Auth.Token) == "" {
					return fmt.Errorf("源 %s（rest）bearer 认证缺少 token", s.Name)
				}
				if s.Auth.Type == "basic" && strings.TrimSpace(s.Auth.Username) == "" {
					return fmt.Errorf("源 %s（rest）basic 认证缺少 username", s.Name)
				}
			}
		default:
			return fmt.Errorf("源 %s 的 kind %q 不支持（可用 postgres/mysql/file/rest）", s.Name, s.Kind)
		}
	}
	return nil
}

// Save 写回本配置（路径未知时用默认路径）。
func (c *Config) Save() error {
	if c.path == "" {
		path, err := DefaultPath()
		if err != nil {
			return err
		}
		c.path = path
	}
	return c.SaveTo(c.path)
}

// SaveTo 原子写入配置：同目录临时文件（0600）→ rename，避免半截文件；
// 目录 0700，落在已存在的宽松权限文件上也强制收紧（契约 §5「本地安全存储」）。
func (c *Config) SaveTo(path string) error {
	dir := filepath.Dir(path)
	if err := os.MkdirAll(dir, dirPerm); err != nil {
		return fmt.Errorf("创建配置目录 %s 失败: %w", dir, err)
	}
	data, err := json.MarshalIndent(c, "", "  ")
	if err != nil {
		return fmt.Errorf("序列化配置失败: %w", err)
	}
	data = append(data, '\n')

	tmp, err := os.CreateTemp(dir, ".config-*.json.tmp")
	if err != nil {
		return fmt.Errorf("创建临时配置文件失败: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		// 失败路径清理；成功后 rename 已使该文件不存在。
		_ = os.Remove(tmpName)
	}()
	if err := tmp.Chmod(filePerm); err != nil {
		tmp.Close()
		return fmt.Errorf("收紧临时配置文件权限失败: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return fmt.Errorf("写入配置失败: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("关闭临时配置文件失败: %w", err)
	}
	if err := os.Rename(tmpName, path); err != nil {
		return fmt.Errorf("落盘配置 %s 失败: %w", path, err)
	}
	c.path = path
	return nil
}

// PermissionWarning 在配置文件对同机其他用户可读时返回一句告警（unix）；Windows 返回空串。
func (c *Config) PermissionWarning() string {
	info, err := os.Stat(c.path)
	if err != nil {
		return ""
	}
	if mode := info.Mode().Perm(); mode&0o077 != 0 {
		return fmt.Sprintf("配置文件 %s 权限为 %#o，建议 chmod 600（内含客户源凭据）", c.path, mode)
	}
	return ""
}
