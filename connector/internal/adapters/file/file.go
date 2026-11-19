// Package file 是本地 Excel/CSV 文件适配器（契约 §5）：
// 一个文件 = 一个数据集，source_ref 为文件名（不含目录），
// Excel 走 excelize/v2 流式读取、CSV 走标准库，列类型推断同 §3.4（抽样 200 行，保守 TEXT）。
package file

import (
	"bufio"
	"context"
	"encoding/csv"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/xuri/excelize/v2"

	"github.com/FindDataTechnology/fd-wire/connector/internal/adapters"
)

// inferSampleRows 是列类型推断的抽样行数（契约 §3.4）。
const inferSampleRows = 200

// Adapter 读取 <Dir>/<SourceRef> 单个文件。
type Adapter struct {
	Dir       string
	SourceRef string
	BatchSize int
}

// New 建文件适配器；BatchSize<=0 时用 2000。
func New(dir, sourceRef string, batchSize int) *Adapter {
	if batchSize <= 0 {
		batchSize = 2000
	}
	return &Adapter{Dir: dir, SourceRef: sourceRef, BatchSize: batchSize}
}

// FilePath 返回数据文件路径（source_ref 只允许纯文件名，禁止路径穿越）。
func (a *Adapter) FilePath() (string, error) {
	name := strings.TrimSpace(a.SourceRef)
	if name == "" {
		return "", errors.New("file 适配器缺少 source_ref（应为文件名）")
	}
	if strings.ContainsAny(name, `/\`) || name == "." || name == ".." {
		return "", fmt.Errorf("source_ref %q 不是纯文件名（不允许目录分隔符）", a.SourceRef)
	}
	if strings.TrimSpace(a.Dir) == "" {
		return "", fmt.Errorf("源 %s 本地配置缺少 path", name)
	}
	return filepath.Join(a.Dir, name), nil
}

// Columns 抽样推断列类型。
func (a *Adapter) Columns(ctx context.Context) ([]adapters.Column, error) {
	_, cols, err := a.infer(ctx)
	return cols, err
}

// PrimaryKeys：文件源无主键（D8：无主键行走引擎行哈希锚定）。
func (a *Adapter) PrimaryKeys(context.Context) ([]string, error) { return nil, nil }

// Snapshot 流式读取整个文件并按批次回调；每行按推断列类型转换
// （数值型解析失败即报错终止——诚实失败优于静默写脏数据）。
func (a *Adapter) Snapshot(ctx context.Context, fn adapters.SnapshotFunc) error {
	header, cols, err := a.infer(ctx)
	if err != nil {
		return err
	}
	types := make([]string, len(cols))
	for i, c := range cols {
		types[i] = c.Type
	}
	batch := adapters.NewBatcher(a.BatchSize, fn)
	err = a.eachRow(ctx, func(lineNo int, record []string) error {
		if lineNo == 1 { // 首行是表头
			return nil
		}
		if isBlankRecord(record) {
			return nil
		}
		if len(record) > len(header) {
			return fmt.Errorf("第 %d 行列数 %d 超过表头 %d", lineNo, len(record), len(header))
		}
		row := make(adapters.Row, len(header))
		for i, name := range header {
			value := "" // 短行的尾部缺失单元格按空值处理（Excel 会裁掉尾部空单元格）
			if i < len(record) {
				value = record[i]
			}
			converted, err := adapters.ConvertString(value, types[i])
			if err != nil {
				return fmt.Errorf("第 %d 行第 %d 列 %s: %w", lineNo, i+1, name, err)
			}
			row[name] = converted
		}
		return batch.Add(row)
	})
	if err != nil {
		return err
	}
	return batch.Flush()
}

// infer 读表头 + 抽样行，返回表头、推断列与全部样本行。
func (a *Adapter) infer(ctx context.Context) ([]string, []adapters.Column, error) {
	var header []string
	var sample [][]string
	err := a.eachRow(ctx, func(lineNo int, record []string) error {
		if header == nil {
			header = record
			return nil
		}
		if isBlankRecord(record) {
			return nil
		}
		sample = append(sample, record)
		if len(sample) >= inferSampleRows {
			return errStopScan
		}
		return nil
	})
	if err != nil && !errors.Is(err, errStopScan) {
		return nil, nil, err
	}
	if len(header) == 0 {
		return nil, nil, fmt.Errorf("文件缺少表头（首行为列名）")
	}
	columns := make([]adapters.Column, 0, len(header))
	for i, name := range header {
		values := make([]string, 0, len(sample))
		for _, record := range sample {
			if i < len(record) {
				values = append(values, record[i])
			}
		}
		columns = append(columns, adapters.Column{
			Name: strings.TrimSpace(name),
			Type: adapters.InferType(values),
		})
	}
	return header, columns, nil
}

var errStopScan = errors.New("sampling complete")

// eachRow 逐行回调（表头也算一行；行号从 1 起，全空行也计入行号）。
func (a *Adapter) eachRow(ctx context.Context, fn func(lineNo int, record []string) error) error {
	path, err := a.FilePath()
	if err != nil {
		return err
	}
	info, err := os.Stat(path)
	if err != nil {
		return fmt.Errorf("读取文件 %s 失败: %w", path, err)
	}
	if info.IsDir() {
		return fmt.Errorf("%s 是目录：source_ref 应为文件名", path)
	}
	switch strings.ToLower(filepath.Ext(path)) {
	case ".csv", ".tsv", ".txt":
		return a.eachCSVRow(ctx, path, fn)
	case ".xlsx", ".xlsm":
		return a.eachExcelRow(ctx, path, fn)
	default:
		return fmt.Errorf("不支持的文件类型 %s（一期支持 .csv/.tsv/.xlsx）", filepath.Ext(path))
	}
}

func (a *Adapter) eachCSVRow(ctx context.Context, path string, fn func(int, []string) error) error {
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("打开文件 %s 失败: %w", path, err)
	}
	defer f.Close()
	reader := csv.NewReader(stripBOM(f))
	reader.FieldsPerRecord = -1 // 列数由调用方校验，错误信息更具体
	reader.LazyQuotes = true
	lineNo := 0
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		record, err := reader.Read()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return fmt.Errorf("解析 %s 第 %d 行失败: %w", filepath.Base(path), lineNo+1, err)
		}
		lineNo++
		if err := fn(lineNo, record); err != nil {
			return err
		}
	}
}

func (a *Adapter) eachExcelRow(ctx context.Context, path string, fn func(int, []string) error) error {
	f, err := excelize.OpenFile(path)
	if err != nil {
		return fmt.Errorf("打开 Excel %s 失败: %w", path, err)
	}
	defer f.Close()
	sheets := f.GetSheetList()
	if len(sheets) == 0 {
		return fmt.Errorf("Excel %s 不含工作表", filepath.Base(path))
	}
	rows, err := f.Rows(sheets[0]) // 流式：大表不整份进内存
	if err != nil {
		return fmt.Errorf("读取工作表 %s 失败: %w", sheets[0], err)
	}
	defer rows.Close()
	lineNo := 0
	for rows.Next() {
		if err := ctx.Err(); err != nil {
			return err
		}
		record, err := rows.Columns()
		if err != nil {
			return fmt.Errorf("解析 %s 第 %d 行失败: %w", filepath.Base(path), lineNo+1, err)
		}
		lineNo++
		if err := fn(lineNo, record); err != nil {
			return err
		}
	}
	return rows.Error()
}

func isBlankRecord(record []string) bool {
	if len(record) == 0 {
		return true
	}
	for _, v := range record {
		if strings.TrimSpace(v) != "" {
			return false
		}
	}
	return true
}

// stripBOM 丢弃 UTF-8 BOM（Excel 导出的 CSV 常见）。
func stripBOM(r io.Reader) io.Reader {
	br := bufio.NewReader(r)
	if head, err := br.Peek(3); err == nil && head[0] == 0xEF && head[1] == 0xBB && head[2] == 0xBF {
		_, _ = br.Discard(3)
	}
	return br
}
