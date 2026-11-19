package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeConfig(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestVersionCommand(t *testing.T) {
	if err := run([]string{"version"}); err != nil {
		t.Fatalf("version 子命令应成功: %v", err)
	}
}

func TestHelpAndUnknownSubcommand(t *testing.T) {
	for _, arg := range []string{"help", "-h", "--help"} {
		if err := run([]string{arg}); err != nil {
			t.Fatalf("%s 应成功: %v", arg, err)
		}
	}
	if err := run(nil); err == nil {
		t.Fatal("缺子命令应报错")
	}
	err := run([]string{"sync-everything"})
	if err == nil || !strings.Contains(err.Error(), "未知子命令") {
		t.Fatalf("未知子命令应报错: %v", err)
	}
}

func TestPairRequiresToken(t *testing.T) {
	err := run([]string{"pair"})
	if err == nil || !strings.Contains(err.Error(), "--token") {
		t.Fatalf("缺 --token 应报错: %v", err)
	}
}

func TestPairRejectsInvalidAPIURL(t *testing.T) {
	path := filepath.Join(t.TempDir(), "config.json")
	err := run([]string{"pair", "--token", "fwp_x", "--api", "http://evil.example.com", "--config", path})
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("非回环 http 应在配对时被拒（TLS 强制）: %v", err)
	}
	if _, statErr := os.Stat(path); statErr == nil {
		t.Fatal("配对失败不应写入配置")
	}
}

func TestRunRequiresPairedConfig(t *testing.T) {
	path := t.TempDir() + "/config.json"
	// 配置不存在。
	if err := run([]string{"run", "--config", path}); err == nil {
		t.Fatal("无配置应报错")
	}
	// 有配置但未配对。
	writeConfig(t, path, `{"api_url":"https://wire.finddatatech.cloud","sources":[]}`)
	err := run([]string{"run", "--config", path})
	if err == nil || !strings.Contains(err.Error(), "pair") {
		t.Fatalf("未配对应提示先配对: %v", err)
	}
}
