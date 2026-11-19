package config

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestDefaultPath(t *testing.T) {
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	env := func(values map[string]string) func(string) string {
		return func(key string) string { return values[key] }
	}

	linuxPath, err := defaultPath("linux", env(nil))
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(home, ".config", DirName, FileName); linuxPath != want {
		t.Errorf("linux 缺省路径 = %q, want %q", linuxPath, want)
	}

	darwinPath, err := defaultPath("darwin", env(nil))
	if err != nil {
		t.Fatal(err)
	}
	if darwinPath != linuxPath {
		t.Errorf("darwin 应同 XDG 口径（契约 §5）：%q vs %q", darwinPath, linuxPath)
	}

	xdgPath, err := defaultPath("linux", env(map[string]string{"XDG_CONFIG_HOME": "/custom/cfg"}))
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join("/custom/cfg", DirName, FileName); xdgPath != want {
		t.Errorf("XDG_CONFIG_HOME 路径 = %q, want %q", xdgPath, want)
	}

	winPath, err := defaultPath("windows", env(map[string]string{"APPDATA": `C:\Users\x\AppData\Roaming`}))
	if err != nil {
		t.Fatal(err)
	}
	if want := filepath.Join(`C:\Users\x\AppData\Roaming`, DirName, FileName); winPath != want {
		t.Errorf("windows 路径 = %q, want %q", winPath, want)
	}
	if _, err := defaultPath("windows", env(nil)); err == nil {
		t.Error("APPDATA 缺失应报错")
	}
}

func TestSaveLoadRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "config.json")
	cfg := &Config{
		APIURL:       "https://wire.finddatatech.cloud",
		ConnectorKey: "fwc_secret",
		Sources: []Source{
			{Name: "erp_db", Kind: KindPostgres, DSN: "postgres://user:pass@host/db"},
			{Name: "sales_mysql", Kind: KindMySQL, DSN: "user:pass@tcp(host:3306)/sales"},
			{Name: "reports", Kind: KindFile, Path: "/data/reports"},
		},
	}
	if err := cfg.SaveTo(path); err != nil {
		t.Fatalf("SaveTo: %v", err)
	}
	loaded, err := LoadFrom(path)
	if err != nil {
		t.Fatalf("LoadFrom: %v", err)
	}
	if loaded.APIURL != cfg.APIURL || loaded.ConnectorKey != cfg.ConnectorKey || len(loaded.Sources) != 3 {
		t.Fatalf("往返不一致: %#v", loaded)
	}
	if loaded.Path() != path {
		t.Errorf("Path() = %q, want %q", loaded.Path(), path)
	}
	src, ok := loaded.Source("sales_mysql")
	if !ok || src.DSN != "user:pass@tcp(host:3306)/sales" {
		t.Fatalf("源查找失败: %#v %v", src, ok)
	}
	if names := loaded.SourceNames(); !names["erp_db"] || !names["reports"] || len(names) != 3 {
		t.Fatalf("SourceNames = %v", names)
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatalf("配置未落盘: %v", err)
	}
}

func TestSaveTightensPermissions(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("Windows 无 POSIX 权限位")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	// 预置一个 0644 的宽松文件，保存后必须收紧到 0600（契约 §5 本地安全存储）。
	if err := os.WriteFile(path, []byte(`{"api_url":"https://x"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg := &Config{APIURL: "https://wire.finddatatech.cloud", ConnectorKey: "fwc_secret"}
	if err := cfg.SaveTo(path); err != nil {
		t.Fatalf("SaveTo: %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if mode := info.Mode().Perm(); mode != 0o600 {
		t.Fatalf("配置文件权限 = %#o, want 0600", mode)
	}
	loaded, err := LoadFrom(path)
	if err != nil {
		t.Fatal(err)
	}
	if warning := loaded.PermissionWarning(); warning != "" {
		t.Errorf("0600 不应告警: %s", warning)
	}
	if err := os.Chmod(path, 0o644); err != nil {
		t.Fatal(err)
	}
	if warning := loaded.PermissionWarning(); warning == "" {
		t.Error("宽松权限应给出告警")
	}
	// 目录权限同样收紧。
	dirStat, err := os.Stat(dir)
	if err != nil {
		t.Fatal(err)
	}
	_ = dirStat
}

func TestSaveDoesNotLeakPlaintextToTempFiles(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	cfg := &Config{APIURL: "https://wire.finddatatech.cloud", ConnectorKey: "fwc_secret"}
	if err := cfg.SaveTo(path); err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 1 || entries[0].Name() != "config.json" {
		names := make([]string, 0, len(entries))
		for _, e := range entries {
			names = append(names, e.Name())
		}
		t.Fatalf("原子写入应只留正式文件，实际 %v", names)
	}
}

func TestLoadMissingFile(t *testing.T) {
	_, err := LoadFrom(filepath.Join(t.TempDir(), "nope.json"))
	if err == nil {
		t.Fatal("缺文件应报错")
	}
	if !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("错误应可用 errors.Is(fs.ErrNotExist) 判定: %v", err)
	}
}

func TestValidate(t *testing.T) {
	cases := map[string]Config{
		"缺 api_url":  {Sources: nil},
		"api_url 畸形": {APIURL: "://x"},
		"源重名": {
			APIURL: "https://x",
			Sources: []Source{
				{Name: "a", Kind: KindFile, Path: "/tmp"},
				{Name: "a", Kind: KindFile, Path: "/tmp"},
			},
		},
		"postgres 缺 dsn": {APIURL: "https://x", Sources: []Source{{Name: "a", Kind: KindPostgres}}},
		"mysql 缺 dsn":    {APIURL: "https://x", Sources: []Source{{Name: "a", Kind: KindMySQL}}},
		"file 缺 path":    {APIURL: "https://x", Sources: []Source{{Name: "a", Kind: KindFile}}},
		"未知 kind":        {APIURL: "https://x", Sources: []Source{{Name: "a", Kind: "oracle", DSN: "x"}}},
		"源缺 name":        {APIURL: "https://x", Sources: []Source{{Kind: KindFile, Path: "/tmp"}}},
	}
	for name, cfg := range cases {
		if err := cfg.Validate(); err == nil {
			t.Errorf("%s：应校验失败", name)
		}
	}
	good := Config{
		APIURL: "https://wire.finddatatech.cloud",
		Sources: []Source{
			{Name: "a", Kind: KindPostgres, DSN: "postgres://h/db"},
			{Name: "b", Kind: KindFile, Path: "/data"},
		},
	}
	if err := good.Validate(); err != nil {
		t.Errorf("合法配置不应报错: %v", err)
	}
}

func TestSavedConfigHasNoUnexpectedFields(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	cfg := &Config{APIURL: "https://x", ConnectorKey: "fwc_k", Sources: []Source{{Name: "s", Kind: KindFile, Path: "/tmp"}}}
	if err := cfg.SaveTo(path); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	for _, key := range []string{"api_url", "connector_key", "sources", "kind", "path"} {
		if !strings.Contains(text, `"`+key+`"`) {
			t.Errorf("落盘配置缺少字段 %q: %s", key, text)
		}
	}
	if strings.Contains(text, "0o600") || strings.Contains(text, "Perm") {
		t.Errorf("内部字段不应落盘: %s", text)
	}
}
