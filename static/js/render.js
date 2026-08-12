import { $, escapeHtml, kv, cls } from "./utils.js?v=20260808am";
import { fmt, fmtFinanceValue, parseNum } from "./format.js?v=20260808am";

export function renderQuote(q) {
  const color = cls(q.change_pct);
  const changePct =
    q.change_pct === null || q.change_pct === undefined
      ? "-"
      : `${q.change_pct > 0 ? "+" : ""}${fmt(q.change_pct)}%`;

  $("quoteCard").innerHTML = `
    <h2>行情概览</h2>
    <div class="quote-head">
      <div>
        <div class="name-lg">${escapeHtml(q.name)}（${q.code}）</div>
        <div class="quote-meta">更新：${q.update_time || "-"} · 来源：${q.source}</div>
      </div>
      <div class="price ${color}">
        ${fmt(q.price)}
        <span class="price-pct">${changePct}</span>
      </div>
    </div>
    <div class="kv">
      ${kv("今开", fmt(q.open))}
      ${kv("最高", fmt(q.high))}
      ${kv("最低", fmt(q.low))}
      ${kv("昨收", fmt(q.prev_close))}
      ${kv("成交量(手)", fmt(q.volume, 0))}
      ${kv("成交额(万)", fmt(q.amount, 0))}
      ${kv("换手率%", fmt(q.turnover_rate))}
      ${kv("市盈率", fmt(q.pe))}
      ${kv("市净率", fmt(q.pb))}
      ${kv("总市值(亿)", fmt(q.total_market_cap))}
      ${kv("流通市值(亿)", fmt(q.float_market_cap))}
      ${kv("振幅%", fmt(q.amplitude))}
    </div>
  `;
}

function shiftYear(dateStr, deltaYears) {
  if (!dateStr || dateStr.length < 10) return "";
  const y = Number(dateStr.slice(0, 4)) + deltaYears;
  if (!Number.isFinite(y)) return "";
  return `${y}${dateStr.slice(4)}`;
}

/** 报告期展示：优先用后端报告类型，否则按日期推断 */
export function formatReportPeriod(rowOrDate, mode = "cumulative") {
  if (rowOrDate && typeof rowOrDate === "object") {
    const date = String(rowOrDate["报告期"] || "");
    const scope = String(rowOrDate["报告口径"] || "累计");
    const isForecast = Boolean(rowOrDate.is_forecast || rowOrDate["预告"]);
    if (/^\d{4}-\d{2}-\d{2}$/.test(date)) {
      const year = date.slice(0, 4);
      const md = date.slice(5);
      if (mode === "quarter") {
        if (scope === "累计" && md === "12-31" && !isForecast) {
          return `${year}年年报（${date}）`;
        }
        const qmap = {
          "03-31": "Q1",
          "06-30": "Q2",
          "09-30": "Q3",
          "12-31": "Q4",
        };
        const q = qmap[md];
        if (q) {
          const suffix = isForecast ? "预告" : "单季";
          // 中报预告在单季模式下标注为 H1/Q2 相关预告
          if (isForecast && md === "06-30") {
            return `${year}年半年报预告（对应 Q1–Q2 累计）（${date}）`;
          }
          if (isForecast && md === "12-31") {
            return `${year}年年报预告（${date}）`;
          }
          if (isForecast) {
            return `${year}年${q}预告（${date}）`;
          }
          return `${year}年${q}（${suffix}）（${date}）`;
        }
      } else {
        // cumulative
        const cmap = {
          "03-31": "一季报",
          "06-30": "半年报",
          "09-30": "三季报（累计）",
          "12-31": "年报",
        };
        const kind = cmap[md];
        if (kind) {
          if (isForecast) return `${year}年${kind}预告（${date}）`;
          return `${year}年${kind}（${date}）`;
        }
      }
    }
    const typ = rowOrDate["报告类型"];
    if (typ) return date ? `${typ}（${date}）` : String(typ);
    return formatReportPeriod(date, mode);
  }
  const raw = String(rowOrDate || "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(raw)) return raw || "-";
  const year = raw.slice(0, 4);
  const md = raw.slice(5);
  const typeMap = {
    "03-31": "一季报",
    "06-30": "半年报",
    "09-30": "三季报（累计）",
    "12-31": "年报",
  };
  const kind = typeMap[md] || "财报";
  return `${year}年${kind}（${raw}）`;
}

const INCOME_MODE_KEY = "incomeDisplayMode";

function getIncomeMode() {
  const saved = localStorage.getItem(INCOME_MODE_KEY);
  return saved === "quarter" || saved === "cumulative" ? saved : "cumulative";
}

function setIncomeMode(mode) {
  localStorage.setItem(INCOME_MODE_KEY, mode);
}

function periodMd(row) {
  const d = String(row?.["报告期"] || "");
  return d.length >= 10 ? d.slice(5) : "";
}

/** 按展示模式过滤利润表行 */
function filterIncomeRows(rows, mode) {
  const allowed = new Set(["03-31", "06-30", "09-30", "12-31"]);
  return (rows || []).filter((row) => {
    const md = periodMd(row);
    if (!allowed.has(md)) return false;
    const scope = String(row["报告口径"] || "累计");
    const isForecast = Boolean(row.is_forecast || row["预告"]);
    if (isForecast) return true;
    if (mode === "quarter") {
      // Q1–Q4 单季 + 年报（累计 12-31）
      if (md === "12-31" && scope === "累计") return true;
      return scope === "单季";
    }
    // 一季报 / 半年报 / 三季报累计 / 年报
    return scope === "累计";
  });
}

function renderPeriodCell(row, mode = "cumulative") {
  const title = escapeHtml(formatReportPeriod(row, mode));
  const isForecast = Boolean(row?.is_forecast || row?.["预告"]);
  if (!isForecast) return title;

  const ftype = row["预告类型"]
    ? `<span class="forecast-type">${escapeHtml(String(row["预告类型"]))}</span>`
    : "";
  const notice = row["公告日期"]
    ? `预告公告日：${escapeHtml(String(row["公告日期"]))}`
    : "业绩预告";
  const formal = row["正式披露日期"] || row["正式披露预约日"] || "";
  const status = String(row["正式披露状态"] || "");
  let formalLine = "正式业绩披露日：待交易所预约";
  if (formal) {
    formalLine =
      status === "已披露"
        ? `正式业绩已披露：${escapeHtml(String(formal))}`
        : `正式业绩预约披露：${escapeHtml(String(formal))}${
            status ? `（${escapeHtml(status)}）` : ""
          }`;
  }
  return `<div class="period-stack">
    <div class="period-title">${title}</div>
    <div class="forecast-badge">预告${ftype}</div>
    <div class="period-meta">${notice}</div>
    <div class="period-meta formal">${formalLine}</div>
  </div>`;
}

function fmtForecastValue(key, row) {
  const lower = parseNum(row[`${key}_下限`]);
  const upper = parseNum(row[`${key}_上限`]);
  if (lower !== null && upper !== null && Math.abs(lower - upper) > 1e-6) {
    return `${fmtFinanceValue(key, lower)} ~ ${fmtFinanceValue(key, upper)}`;
  }
  return fmtFinanceValue(key, row[key]);
}

function pctChange(cur, prev) {
  const a = parseNum(cur);
  const b = parseNum(prev);
  if (a === null || b === null || Math.abs(b) < 1e-12) return null;
  return ((a - b) / Math.abs(b)) * 100;
}

function trendLine(label, pct) {
  if (pct === null || pct === undefined || Number.isNaN(pct)) {
    return `<div class="cell-trend muted">${label} -</div>`;
  }
  const up = pct > 0.05;
  const down = pct < -0.05;
  const clsName = up ? "up" : down ? "down" : "flat";
  const arrow = up ? "↑" : down ? "↓" : "→";
  const sign = pct > 0 ? "+" : "";
  return `<div class="cell-trend ${clsName}">${label} ${sign}${pct.toFixed(1)}% ${arrow}</div>`;
}

function renderMetricCell(key, row, byPeriod, rows, rowIndex) {
  const isForecast = Boolean(row?.is_forecast || row?.["预告"]);
  if (isForecast) {
    const valueHtml = fmtForecastValue(key, row);
    const yoyPct = parseNum(row[`${key}_同比`]);
    return `<div class="cell-stack">
      <div class="cell-main">${valueHtml}</div>
      ${trendLine("同比", yoyPct)}
      <div class="cell-trend muted">环比 预告不适用</div>
    </div>`;
  }

  const valueHtml = fmtFinanceValue(key, row[key]);
  if (/增长率/.test(key)) {
    const n = parseNum(row[key]);
    const clsName = n > 0 ? "up" : n < 0 ? "down" : "flat";
    return `<div class="cell-stack"><div class="cell-main">${valueHtml}</div><div class="cell-trend ${clsName}">本期增速</div></div>`;
  }

  const period = String(row["报告期"] || "");
  const scope = String(row["报告口径"] || "累计");
  // 同比：同口径、去年同一报告期
  const yoyKey = `${shiftYear(period, -1)}|${scope}`;
  const yoyRow = byPeriod.get(yoyKey) || byPeriod.get(shiftYear(period, -1));
  const yoyPct = yoyRow ? pctChange(row[key], yoyRow[key]) : null;

  // 环比：表中同口径的上一期（跳过预告行）
  let prevRow = null;
  for (let i = rowIndex + 1; i < rows.length; i += 1) {
    const r = rows[i];
    if (r?.is_forecast || r?.["预告"]) continue;
    if (String(r["报告口径"] || "累计") === scope) {
      prevRow = r;
      break;
    }
  }
  const qoqPct = prevRow ? pctChange(row[key], prevRow[key]) : null;

  return `<div class="cell-stack">
    <div class="cell-main">${valueHtml}</div>
    ${trendLine("同比", yoyPct)}
    ${trendLine("环比", qoqPct)}
  </div>`;
}

function buildIncomeTableHtml(allRows, mode) {
  const all = allRows || [];
  const list = filterIncomeRows(all, mode).slice(0, 16);
  if (!list.length) {
    return `<div class="table-note">当前模式下暂无利润表数据</div>`;
  }
  const byPeriod = new Map();
  for (const row of all) {
    const date = String(row["报告期"] || "");
    const scope = String(row["报告口径"] || "累计");
    if (date) {
      byPeriod.set(`${date}|${scope}`, row);
      if (!byPeriod.has(date) || (scope === "累计" && !row.is_forecast && !row["预告"])) {
        byPeriod.set(date, row);
      }
    }
  }
  const keys = ["报告期", "营业总收入", "营业成本", "归母净利润", "净利润", "扣非净利润", "基本每股收益", "经营现金流量净额"]
    .filter((k) => k === "报告期" || list.some((r) => r[k] !== undefined && r[k] !== null));

  let html = `<div class="table-wrap"><table class="finance-table"><thead><tr>`;
  html += keys.map((k) => `<th>${escapeHtml(k)}</th>`).join("");
  html += `</tr></thead><tbody>`;

  list.forEach((row, idx) => {
    const scope = String(row["报告口径"] || "累计");
    const isForecast = Boolean(row.is_forecast || row["预告"]);
    const rowClass = isForecast
      ? "row-forecast"
      : scope === "单季"
        ? "row-single"
        : "row-cum";
    html += `<tr class="${rowClass}">`;
    for (const k of keys) {
      if (k === "报告期") {
        html += `<td class="period-cell">${renderPeriodCell(row, mode)}</td>`;
      } else {
        html += `<td>${renderMetricCell(k, row, byPeriod, list, idx)}</td>`;
      }
    }
    html += "</tr>";
  });
  html += `</tbody></table></div>`;
  const note =
    mode === "quarter"
      ? "当前为单季模式：展示 Q1–Q4 单季利润（由累计报表差分推算）及年报。带「预告」标记为业绩预告区间（非正式报表）。同比对照去年同季/同年报。"
      : "当前为累计模式：展示一季报、半年报、三季报（累计）、年报。带「预告」标记为业绩预告区间（非正式报表），并标注正式业绩预约披露日。同比对照去年同期累计。";
  html += `<div class="table-note">${note}</div>`;
  return html;
}

function renderIncomeModeSwitcher(mode) {
  const qOn = mode === "quarter" ? "is-active" : "";
  const cOn = mode === "cumulative" ? "is-active" : "";
  return `<div class="mode-switch" role="radiogroup" aria-label="报表展示模式">
    <span class="mode-switch-label">展示模式</span>
    <div class="mode-switch-track">
      <label class="mode-switch-opt ${qOn}">
        <input type="radio" name="incomeMode" value="quarter" ${mode === "quarter" ? "checked" : ""} />
        <span>单季：Q1 / Q2 / Q3 / Q4 + 年报</span>
      </label>
      <label class="mode-switch-opt ${cOn}">
        <input type="radio" name="incomeMode" value="cumulative" ${mode === "cumulative" ? "checked" : ""} />
        <span>累计：一季报 / 半年报 / 三季报 / 年报</span>
      </label>
    </div>
  </div>`;
}

function filterBalanceRows(rows, mode) {
  // 资产负债多为时点指标：两种模式都按季末/年末节点展示，标签随模式变化
  const allowed = new Set(["03-31", "06-30", "09-30", "12-31"]);
  return (rows || []).filter((row) => {
    const md = periodMd(row);
    if (!allowed.has(md)) return false;
    const scope = String(row["报告口径"] || "累计");
    return scope === "累计" || !row["报告口径"];
  });
}

const BALANCE_COLS = [
  "报告期",
  "股东权益合计(净资产)",
  "商誉",
  "资产负债率",
  "流动比率",
  "速动比率",
  "权益乘数",
  "产权比率",
  "现金比率",
];

function formatBalancePeriod(row, mode) {
  const date = String(row?.["报告期"] || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return formatReportPeriod(row, mode);
  const year = date.slice(0, 4);
  const md = date.slice(5);
  if (mode === "quarter") {
    const qmap = {
      "03-31": "Q1 时点",
      "06-30": "Q2 时点",
      "09-30": "Q3 时点",
      "12-31": "年报时点",
    };
    return `${year}年${qmap[md] || "财报"}（${date}）`;
  }
  const cmap = {
    "03-31": "一季报",
    "06-30": "半年报",
    "09-30": "三季报",
    "12-31": "年报",
  };
  return `${year}年${cmap[md] || "财报"}（${date}）`;
}

function buildBalanceTableHtml(allRows, mode) {
  const list = filterBalanceRows(allRows, mode).slice(0, 12);
  if (!list.length) {
    return `<div class="table-note">当前模式下暂无净资产与财务风险数据</div>`;
  }
  const keys = BALANCE_COLS.filter(
    (k) => k === "报告期" || list.some((r) => r[k] !== undefined && r[k] !== null)
  );
  let html = `<div class="table-wrap"><table class="finance-table"><thead><tr>`;
  html += keys.map((k) => `<th>${escapeHtml(k)}</th>`).join("");
  html += `</tr></thead><tbody>`;
  for (const row of list) {
    html += `<tr>`;
    for (const k of keys) {
      if (k === "报告期") {
        html += `<td class="period-cell">${escapeHtml(formatBalancePeriod(row, mode))}</td>`;
      } else {
        html += `<td>${fmtFinanceValue(k, row[k])}</td>`;
      }
    }
    html += `</tr>`;
  }
  html += `</tbody></table></div>`;
  html += `<div class="table-note">${
    mode === "quarter"
      ? "净资产与比率多为时点指标，单季模式下按 Q1–Q3 季末及年报时点展示（非利润差分）。"
      : "按一季报 / 半年报 / 三季报 / 年报时点展示净资产与财务风险指标。"
  }</div>`;
  return html;
}

function renderIncomeTable(rows, mode = getIncomeMode()) {
  const all = rows || [];
  if (!all.length) return "";
  return `<div id="incomeTableBody">${buildIncomeTableHtml(all, mode)}</div>`;
}

function renderBalanceTable(rows, mode = getIncomeMode()) {
  const all = rows || [];
  if (!all.length) return "";
  return `<div id="balanceTableBody">${buildBalanceTableHtml(all, mode)}</div>`;
}

function bindIncomeModeSwitcher(incomeRows, balanceRows) {
  const root = $("financeCard");
  if (!root) return;
  const syncActive = (mode) => {
    root.querySelectorAll(".mode-switch-opt").forEach((el) => {
      const input = el.querySelector('input[name="incomeMode"]');
      el.classList.toggle("is-active", Boolean(input && input.value === mode && input.checked));
    });
  };
  root.querySelectorAll('input[name="incomeMode"]').forEach((input) => {
    input.addEventListener("change", () => {
      if (!input.checked) return;
      const mode = input.value === "quarter" ? "quarter" : "cumulative";
      setIncomeMode(mode);
      syncActive(mode);
      const incomeBody = document.getElementById("incomeTableBody");
      if (incomeBody && incomeRows?.length) {
        incomeBody.innerHTML = buildIncomeTableHtml(incomeRows, mode);
      }
      const balanceBody = document.getElementById("balanceTableBody");
      if (balanceBody && balanceRows?.length) {
        balanceBody.innerHTML = buildBalanceTableHtml(balanceRows, mode);
      }
    });
  });
}

export function renderFinance(f) {
  const latest = f.latest_indicators;
  const mode = getIncomeMode();
  let html = `<h2>财务概况</h2>`;
  if (latest) {
    html += `<div style="color:var(--muted);margin-bottom:10px;">最新报告期：${escapeHtml(
      formatReportPeriod(latest.report_date)
    )}</div>`;
    html += `<div class="kv">`;
    const entries = Object.entries(latest.items || {}).slice(0, 18);
    for (const [k, v] of entries) {
      html += kv(escapeHtml(k), fmtFinanceValue(k, v));
    }
    html += `</div>`;
  } else {
    html += `<p style="color:var(--muted)">暂无财务指标</p>`;
  }

  const hasIncome = Boolean(f.income_summary && f.income_summary.length);
  const hasBalance = Boolean(f.balance_summary && f.balance_summary.length);
  if (hasIncome || hasBalance) {
    html += renderIncomeModeSwitcher(mode);
  }
  if (hasIncome) {
    html += `<h2 style="margin-top:16px;color:var(--muted);font-size:15px;">利润表摘要</h2>`;
    html += renderIncomeTable(f.income_summary, mode);
  }
  if (hasBalance) {
    html += `<h2 style="margin-top:20px;color:var(--muted);font-size:15px;">净资产与财务风险摘要</h2>`;
    html += renderBalanceTable(f.balance_summary, mode);
  }
  $("financeCard").innerHTML = html;
  if (hasIncome || hasBalance) {
    bindIncomeModeSwitcher(f.income_summary || [], f.balance_summary || []);
  }
}
