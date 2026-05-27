# Analysis — 经验可观测性标准

> 对应 AHE 论文第二层：Experience Observability。

## 目标

将大量原始 trajectory / trace 蒸馏成可消费的分析报告，让演化 Agent 能知道失败模式在哪里。

## 报告结构

analysis/
├── README.md              # 本文件
├── overview-template.md   # 概览报告模板
└── detail/
    └── task_xxx.md        # 逐任务详细分析

## Overview 报告内容
- 本轮评估摘要（pass/fail 数量、成功率）
- 失败模式聚类
- 根因分布
- 推荐优先修复的组件

## Detail 报告内容
- 任务描述
- 轨迹摘录
- 失败原因分析
- 根因定位到具体组件
- 修复建议
