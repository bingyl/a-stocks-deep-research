import { escapeHtml } from "./utils.js?v=20260808am";

export const fmt = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? "-" : Number(v).toFixed(digits);

const MONEY_KEYS = new Set([
  "归母净利润",
  "营业总收入",
  "营业成本",
  "净利润",
  "扣非净利润",
  "经营现金流量净额",
  "股东权益合计(净资产)",
  "商誉",
]);

const PCT_KEYS = new Set([
  "净资产收益率(ROE)",
  "总资产报酬率(ROA)",
  "毛利率",
  "销售净利率",
  "营业利润率",
  "资产负债率",
  "营业总收入增长率",
  "归属母公司净利润增长率",
  "换手率%",
  "振幅%",
]);

const RATIO_KEYS = new Set([
  "流动比率",
  "速动比率",
  "权益乘数",
  "产权比率",
  "现金比率",
  "应收账款周转率",
  "存货周转率",
  "总资产周转率",
  "市盈率",
  "市净率",
]);

const PER_SHARE_KEYS = new Set(["基本每股收益", "每股净资产", "每股现金流"]);

export function parseNum(v) {
  if (v === null || v === undefined || v === "" || v === "-") return null;
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  const n = Number(String(v).replace(/,/g, "").trim());
  return Number.isFinite(n) ? n : null;
}

export function fmtMoney(yuan, digits = 2) {
  const n = parseNum(yuan);
  if (n === null) return "-";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(digits)}亿`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(digits)}万`;
  return `${sign}${abs.toFixed(digits)}元`;
}

export function fmtFinanceValue(key, value) {
  const label = String(key || "").trim();
  const n = parseNum(value);
  if (n === null) {
    if (value === null || value === undefined || value === "") return "-";
    return escapeHtml(String(value));
  }

  // 明确百分数（避免「资产负债率」被金额规则误伤）
  if (
    label === "资产负债率" ||
    label.includes("资产负债率") ||
    PCT_KEYS.has(label) ||
    /负债率$/.test(label) ||
    /增长率|毛利率|净利率|利润率|收益率|报酬率/.test(label)
  ) {
    return `${n.toFixed(2)}%`;
  }

  // 无量纲比率（流动比率等）
  if (RATIO_KEYS.has(label) || /比率$|周转率$|乘数$/.test(label)) {
    return n.toFixed(2);
  }

  // 其余以「率」结尾的默认按百分比
  if (/率$/.test(label)) {
    return `${n.toFixed(2)}%`;
  }

  if (PER_SHARE_KEYS.has(label) || /每股/.test(label)) {
    return `${n.toFixed(2)}元`;
  }

  if (
    MONEY_KEYS.has(label) ||
    (/收入|成本|利润|现金流|净资产|商誉|资产合计/.test(label) && !/率|比率/.test(label))
  ) {
    if (Math.abs(n) > 0 && Math.abs(n) < 1000 && /亿/.test(label)) {
      return `${n.toFixed(2)}亿`;
    }
    return fmtMoney(n);
  }
  if (Math.abs(n) >= 1e6) return fmtMoney(n);
  return n.toFixed(2);
}
