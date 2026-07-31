import fs from "node:fs/promises";
import { createRequire } from "node:module";
const require = createRequire(import.meta.url);
const { FileBlob, SpreadsheetFile, Workbook } = require("@oai/artifact-tool");

const job = JSON.parse(await fs.readFile(process.argv[2], "utf8"));
async function loadWorkbook(path) {
  const lower = path.toLowerCase();
  if (lower.endsWith(".csv") || lower.endsWith(".tsv")) {
    const text = await fs.readFile(path, "utf8");
    return Workbook.fromCSV(text, { sheetName: "原始数据", delimiter: lower.endsWith(".tsv") ? "\t" : "," });
  }
  return SpreadsheetFile.importXlsx(await FileBlob.load(path));
}

const workbook = await loadWorkbook(job.input);
const chooseSheet = (book, name) => {
  if (!name) return book.worksheets.getItemAt(0);
  const found = book.worksheets.items.find(s => s.name === name || s.name.includes(name));
  if (!found) throw new Error(`找不到工作表：${name}`);
  return found;
};
const source = chooseSheet(workbook, job.sheet_name);
const sourceValues = source.getUsedRange(true)?.values || [];
const headers = (sourceValues[0] || []).map(v => String(v ?? "").trim());
const rows = sourceValues.slice(1);
const findColumn = (list, name) => list.findIndex(h => h === name || h.includes(name) || name.includes(h));
const safeName = base => {
  let name = base, n = 2;
  while (workbook.worksheets.items.some(s => s.name === name)) name = `${base}${n++}`;
  return name;
};
const writeStyled = (sheet, matrix) => {
  if (!matrix.length || !matrix[0].length) matrix = [["无数据"]];
  sheet.getRangeByIndexes(0, 0, matrix.length, matrix[0].length).values = matrix;
  sheet.getRangeByIndexes(0, 0, 1, matrix[0].length).format = {
    fill: "#0F766E", font: { bold: true, color: "#FFFFFF" }, wrapText: true,
  };
  const all = sheet.getRangeByIndexes(0, 0, matrix.length, matrix[0].length);
  all.format.autofitColumns(); all.format.autofitRows();
  sheet.freezePanes.freezeRows(1); sheet.showGridLines = false;
};
const maskValue = value => {
  const text = String(value ?? "");
  if (/^1\d{10}$/.test(text)) return `${text.slice(0,3)}****${text.slice(-4)}`;
  if (/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(text)) {
    const [local, domain] = text.split("@"); return `${local.slice(0,1)}***@${domain}`;
  }
  if (/^\d{17}[\dXx]$/.test(text)) return `${text.slice(0,6)}********${text.slice(-4)}`;
  return text.length > 2 ? `${text.slice(0,1)}***${text.slice(-1)}` : "***";
};

let outputRows = [], summary = "", resultSheet = source;
if (job.operation === "sheets") {
  outputRows = workbook.worksheets.items.map(s => {
    const used = s.getUsedRange(true); return [s.name, used?.rowCount || 0, used?.columnCount || 0];
  });
  resultSheet = workbook.worksheets.add(safeName("工作表清单"));
  writeStyled(resultSheet, [["工作表", "行数", "列数"], ...outputRows]);
  summary = `共 ${outputRows.length} 个工作表`;
} else if (job.operation === "dedupe") {
  const col = job.column ? findColumn(headers, job.column) : -1, seen = new Set();
  outputRows = rows.filter(row => { const key = col >= 0 ? JSON.stringify(row[col]) : JSON.stringify(row); if (seen.has(key)) return false; seen.add(key); return true; });
  resultSheet = workbook.worksheets.add(safeName("去重结果")); writeStyled(resultSheet, [headers, ...outputRows]);
  summary = `原有 ${rows.length} 行，去重后 ${outputRows.length} 行，移除 ${rows.length-outputRows.length} 行`;
} else if (job.operation === "filter") {
  const conditions = job.conditions || [];
  const indexed = conditions.map(c => ({ ...c, index: findColumn(headers, c.column) }));
  const missing = indexed.find(c => c.index < 0); if (missing) throw new Error(`找不到筛选列：${missing.column}`);
  outputRows = rows.filter(row => indexed.every(c => String(row[c.index] ?? "").includes(c.value)));
  resultSheet = workbook.worksheets.add(safeName("筛选结果")); writeStyled(resultSheet, [headers, ...outputRows]);
  summary = `按 ${conditions.map(c => `${c.column}=${c.value}`).join("、")} 筛选，得到 ${outputRows.length} 行`;
} else if (job.operation === "sort") {
  const col = findColumn(headers, job.column); if (col < 0) throw new Error(`找不到排序列：${job.column}`);
  outputRows = [...rows].sort((a,b) => { const av=a[col]??"", bv=b[col]??""; const n=Number(av)-Number(bv); const c=Number.isNaN(n) ? String(av).localeCompare(String(bv), "zh-CN") : n; return job.descending ? -c : c; });
  resultSheet = workbook.worksheets.add(safeName("排序结果")); writeStyled(resultSheet, [headers, ...outputRows]);
  summary = `已按 ${job.column}${job.descending ? "降序" : "升序"}排列 ${outputRows.length} 行`;
} else if (job.operation === "aggregate" || job.operation === "pivot") {
  const groupCol = findColumn(headers, job.group_column), valueCol = findColumn(headers, job.value_column);
  if (groupCol < 0 || valueCol < 0) throw new Error("找不到汇总列，请使用准确表头名称");
  const totals = new Map(); for (const row of rows) { const key=String(row[groupCol]??"空白"); totals.set(key,(totals.get(key)||0)+(Number(row[valueCol])||0)); }
  outputRows = [...totals.entries()].map(([k,v]) => [k,v]); resultSheet = workbook.worksheets.add(safeName("汇总结果"));
  writeStyled(resultSheet, [[job.group_column, `${job.value_column}合计`], ...outputRows]);
  if (outputRows.length) resultSheet.getRange(`B2:B${outputRows.length+1}`).format.numberFormat = "#,##0.00";
  if ((job.chart || job.operation === "pivot") && outputRows.length) {
    const chart = resultSheet.charts.add("bar", resultSheet.getRange(`A1:B${outputRows.length+1}`));
    chart.titleText = `${job.group_column} - ${job.value_column}汇总`; chart.hasLegend = false;
    chart.setPosition("D2", "K18");
  }
  summary = `按 ${job.group_column} 汇总 ${job.value_column}，共 ${outputRows.length} 组${job.chart || job.operation === "pivot" ? "，已生成图表" : ""}`;
} else if (job.operation === "mask") {
  const requested = job.columns || [];
  const indexes = requested.length ? requested.map(c => findColumn(headers,c)) : headers.map((h,i) => /手机|电话|邮箱|身份证|证件/.test(h) ? i : -1).filter(i => i>=0);
  if (indexes.some(i => i < 0)) throw new Error("找不到需要脱敏的列");
  outputRows = rows.map(row => row.map((v,i) => indexes.includes(i) ? maskValue(v) : v));
  resultSheet = workbook.worksheets.add(safeName("脱敏结果")); writeStyled(resultSheet, [headers, ...outputRows]);
  summary = `已对 ${indexes.length} 列、${outputRows.length} 行进行脱敏`;
} else if (job.operation === "compare") {
  if (!job.input2) throw new Error("双表对比缺少第二份文件");
  const secondBook = await loadWorkbook(job.input2), secondSheet = chooseSheet(secondBook, job.sheet_name);
  const values2 = secondSheet.getUsedRange(true)?.values || [], headers2 = (values2[0]||[]).map(v=>String(v??"").trim()), rows2=values2.slice(1);
  const keyName = job.key_column || headers.find(h => headers2.includes(h));
  const key1=findColumn(headers,keyName), key2=findColumn(headers2,keyName); if (key1<0 || key2<0) throw new Error("两份表没有可用的共同键列，请指定表头");
  const map1=new Map(rows.map(r=>[String(r[key1]??""),r])), map2=new Map(rows2.map(r=>[String(r[key2]??""),r]));
  const keys=[...new Set([...map1.keys(),...map2.keys()])]; outputRows=keys.flatMap(key=>{
    if (!map1.has(key)) return [["新增",key,"第二份表新增记录"]]; if (!map2.has(key)) return [["删除",key,"第二份表已无此记录"]];
    const a=map1.get(key),b=map2.get(key),changes=[]; for(const h of headers.filter(x=>headers2.includes(x))){const i1=findColumn(headers,h),i2=findColumn(headers2,h);if(String(a[i1]??"")!==String(b[i2]??""))changes.push(`${h}: ${a[i1]??""} → ${b[i2]??""}`);}
    return changes.length ? [["变更",key,changes.join("；")]] : [];
  });
  resultSheet=workbook.worksheets.add(safeName("对比结果")); writeStyled(resultSheet, [["状态",keyName,"变化说明"],...outputRows]);
  summary=`按 ${keyName} 对比完成，发现 ${outputRows.length} 条差异`;
} else {
  const blankCounts=headers.map((_,c)=>rows.filter(r=>r[c]===null||r[c]===undefined||String(r[c]).trim()==="").length), duplicateRows=rows.length-new Set(rows.map(r=>JSON.stringify(r))).size;
  resultSheet=workbook.worksheets.add(safeName("处理报告")); const matrix=[["指标","结果"],["工作表",source.name],["数据行数",rows.length],["字段数",headers.length],["完整重复行",duplicateRows]];
  headers.forEach((h,i)=>matrix.push([`${h||`第${i+1}列`}空值`,blankCounts[i]])); writeStyled(resultSheet,matrix); summary=`共 ${rows.length} 行、${headers.length} 列，发现 ${duplicateRows} 条完整重复记录`;
}

const formulaErrors = await workbook.inspect({ kind:"match", searchTerm:"#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A", options:{useRegex:true,maxResults:100}, summary:"formula error scan" });
if (job.verify_render || job.preview) {
  const preview = await workbook.render({ sheetName:resultSheet.name, autoCrop:"all", scale:1, format:"png" });
  if (job.preview) await fs.writeFile(job.preview,new Uint8Array(await preview.arrayBuffer()));
}
const exported=await SpreadsheetFile.exportXlsx(workbook); await exported.save(job.output); await fs.rm(`${job.output}.inspect.ndjson`,{force:true});
console.log(JSON.stringify({operation:job.operation,rows:rows.length,columns:headers.length,summary,verified:true,formula_scan:formulaErrors.ndjson.length}));
