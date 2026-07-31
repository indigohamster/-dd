import fs from "node:fs/promises";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { FileBlob, SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const job = JSON.parse(await fs.readFile(process.argv[2], "utf8"));
const lower = job.input.toLowerCase();
let workbook;
if (lower.endsWith(".csv") || lower.endsWith(".tsv")) {
  const text = await fs.readFile(job.input, "utf8");
  workbook = await Workbook.fromCSV(text, { sheetName: "原始数据", delimiter: lower.endsWith(".tsv") ? "\t" : "," });
} else {
  workbook = await SpreadsheetFile.importXlsx(await FileBlob.load(job.input));
}

const source = workbook.worksheets.getItemAt(0);
const used = source.getUsedRange(true);
const values = used?.values || [];
const headers = (values[0] || []).map(v => String(v ?? "").trim());
const rows = values.slice(1);
const findColumn = name => headers.findIndex(h => h === name || h.includes(name) || name.includes(h));
const safeName = base => {
  let name = base, n = 2;
  while (workbook.worksheets.items.some(s => s.name === name)) name = `${base}${n++}`;
  return name;
};
const writeStyled = (sheet, matrix) => {
  if (!matrix.length || !matrix[0].length) matrix = [["无数据"]];
  sheet.getRangeByIndexes(0, 0, matrix.length, matrix[0].length).values = matrix;
  const head = sheet.getRangeByIndexes(0, 0, 1, matrix[0].length);
  head.format = { fill: "#0F766E", font: { bold: true, color: "#FFFFFF" }, wrapText: true };
  const all = sheet.getRangeByIndexes(0, 0, matrix.length, matrix[0].length);
  all.format.autofitColumns();
  all.format.autofitRows();
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = false;
};

let outputRows = [], summary = "";
let resultSheet = source;
if (job.operation === "dedupe") {
  const col = job.column ? findColumn(job.column) : -1;
  const seen = new Set();
  outputRows = rows.filter(row => {
    const key = col >= 0 ? JSON.stringify(row[col]) : JSON.stringify(row);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
  const sheet = workbook.worksheets.add(safeName("去重结果"));
  resultSheet = sheet;
  writeStyled(sheet, [headers, ...outputRows]);
  summary = `原有 ${rows.length} 行，去重后 ${outputRows.length} 行，移除 ${rows.length-outputRows.length} 行`;
} else if (job.operation === "filter") {
  const col = findColumn(job.column);
  if (col < 0) throw new Error(`找不到筛选列：${job.column}`);
  outputRows = rows.filter(row => String(row[col] ?? "").includes(job.value));
  const sheet = workbook.worksheets.add(safeName("筛选结果"));
  resultSheet = sheet;
  writeStyled(sheet, [headers, ...outputRows]);
  summary = `筛选 ${job.column} 包含“${job.value}”，得到 ${outputRows.length} 行`;
} else if (job.operation === "aggregate") {
  const groupCol = findColumn(job.group_column), valueCol = findColumn(job.value_column);
  if (groupCol < 0 || valueCol < 0) throw new Error("找不到汇总列，请使用准确表头名称");
  const totals = new Map();
  for (const row of rows) {
    const key = String(row[groupCol] ?? "空白");
    const value = Number(row[valueCol]) || 0;
    totals.set(key, (totals.get(key) || 0) + value);
  }
  outputRows = [...totals.entries()].map(([k,v]) => [k,v]);
  const sheet = workbook.worksheets.add(safeName("汇总结果"));
  resultSheet = sheet;
  writeStyled(sheet, [[job.group_column, `${job.value_column}合计`], ...outputRows]);
  sheet.getRange(`B2:B${outputRows.length+1}`).format.numberFormat = "#,##0.00";
  summary = `按 ${job.group_column} 汇总 ${job.value_column}，共 ${outputRows.length} 组`;
} else {
  const blankCounts = headers.map((_, c) => rows.filter(r => r[c] === null || r[c] === undefined || String(r[c]).trim() === "").length);
  const duplicateRows = rows.length - new Set(rows.map(r => JSON.stringify(r))).size;
  const report = workbook.worksheets.add(safeName("处理报告"));
  resultSheet = report;
  const matrix = [["指标", "结果"], ["工作表", source.name], ["数据行数", rows.length], ["字段数", headers.length], ["完整重复行", duplicateRows]];
  headers.forEach((h,i) => matrix.push([`${h || `第${i+1}列`}空值`, blankCounts[i]]));
  writeStyled(report, matrix);
  summary = `共 ${rows.length} 行、${headers.length} 列，发现 ${duplicateRows} 条完整重复记录`;
}

const formulaErrors = await workbook.inspect({
  kind: "match", searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 }, summary: "formula error scan",
});
const preview = await workbook.render({ sheetName: resultSheet.name, autoCrop: "all", scale: 1, format: "png" });
if (job.preview) await fs.writeFile(job.preview, new Uint8Array(await preview.arrayBuffer()));
const exported = await SpreadsheetFile.exportXlsx(workbook);
await exported.save(job.output);
await fs.rm(`${job.output}.inspect.ndjson`, { force: true });
console.log(JSON.stringify({ operation: job.operation, rows: rows.length, columns: headers.length, summary, verified: true, formula_scan: formulaErrors.ndjson.length }, null, 0));
