"""投研六维分析阶段元数据（SSE / 前端共用）。"""

from __future__ import annotations

from typing import Any

# node_name -> (index, total, title, detail)
STAGE_META: dict[str, dict[str, Any]] = {
    "init_brief": {
        "index": 0,
        "total": 6,
        "title": "解析任务",
        "detail": "提取标的代码与分析侧重点",
    },
    "stage_1_fundamentals": {
        "index": 1,
        "total": 6,
        "title": "基本面画像",
        "detail": "财务概览 + 同比/环比 + 业绩预告（如有）",
    },
    "stage_2_peers": {
        "index": 2,
        "total": 6,
        "title": "股性与估值框架",
        "detail": "股性判定 + 估值线索与综合判断框架",
    },
    "stage_3_boards": {
        "index": 3,
        "total": 6,
        "title": "板块联动与同业对比",
        "detail": "行业板块涨跌联动 + 成分股业绩/PE/PB/现金流横向对比",
    },
    "stage_4_technical": {
        "index": 4,
        "total": 6,
        "title": "技术面",
        "detail": "均线/涨跌/波动等技术面辅助观察",
    },
    "stage_5_intel": {
        "index": 5,
        "total": 6,
        "title": "公司动态与情报",
        "detail": "最新公告/新闻 + 政策宏观补缺",
    },
    "stage_6_report": {
        "index": 6,
        "total": 6,
        "title": "综合报告",
        "detail": "固定大纲汇总六维结论",
    },
}

PIPELINE_SUBAGENT_NAME = "research_pipeline"
