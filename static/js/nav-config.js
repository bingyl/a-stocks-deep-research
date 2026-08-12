/**
 * 侧栏菜单配置。
 * 新增页面步骤：
 * 1. 在 static/pages/ 下新增 xxx.html（页面片段，不要写 html/body）
 * 2. 在下方 NAV_ITEMS 增加一项即可（不必改 index.html）
 */
export const NAV_ITEMS = [
  {
    id: "a-stock-research",
    label: "A股深研",
    title: "A股深研",
    sub: "输入代码、名称或拼音首字母，查询财务并做 AI 深度分析",
    page: "/static/pages/a-stock-research.html",
    icon: "◇",
    group: "应用",
  },
  {
    id: "research-history",
    label: "深研历史",
    title: "深研历史",
    sub: "查看已保存的分析记录，支持在线预览与 HTML 下载",
    page: "/static/pages/research-history.html",
    icon: "▣",
    group: "应用",
  },
];

export const DEFAULT_NAV_ID = "a-stock-research";
