# 预约与容量分析

本地演练资料记录场地、拓印台、人员能力、家庭和团体预约变更、现场签到及物料领用。人数与设备能力使用各自单位，确认记录和现场事实分开保存。

## Local checks

Python 3.9 or newer is sufficient. The repository has no third-party runtime dependencies.

Run `python3 input_audit.py` to verify the declared files and columns. Run `python3 -m unittest discover -s tests -v` to check the prepared input contract and the capacity/reconciliation rules.

The records are synthetic fixtures. Empty values and status labels retain their literal input meaning. The manifest lists file columns and the stable row identifier used by the input audit.

## 场次容量与核对分析

每次确认场次前、以及活动结束后，都可重新运行：

```bash
python3 capacity_analysis.py          # 中文文本报告:计划 / 确认 / 执行 / 下一轮估计
python3 capacity_analysis.py --json   # 机器可读结果,便于带入下一轮排班
```

工具只读不写，因此可反复重新核算名额。每个场次输出四个相互核对的视图：

- **计划**：可接待规模取安全人数、拓印台、指导人员三项上限的最小值，并点名限制因素；按 `group_token` 将共同预约整体模拟候补转正，不拆散填位。
- **确认**：以每个 `(booking_id, member_token)` 的最新 `revision` 为准（一次变更可只改部分成员），单列候补、取消及“曾确认后取消”。
- **执行**：按 `stage` 分别统计讲解、实操签到；只参加实操者记为迟到，不计入讲解环节；同时核算物料 `消耗 = 领用 - 归还`。
- **下一轮估计**：实际参与/容量利用率、人均物料消耗与满员建议备货；完成场次另给确认缺席率。

核对提示覆盖：计划状态与现场记录矛盾、已确认未到场、无确认记录的现场参与（临时加入）、无实际参与却已领用物料（缺席浪费，消耗不冲回）、现场协助但预约未登记需求、日期/外键/库存数量异常等。

口径假设（如需调整，见 `capacity_analysis.py` 顶部常量）：

- 一名参与者各占用 1 个安全名额、1 个拓印台、1 份指导能力；`requested_access=companion` 的陪同人员只占用安全名额。
- 候补按最早 `changed_at` 排序，能整体放入剩余容量的组别先转正，放不下的组别保留空位并标明受限资源。
- 签到与物料是现场事实记录，不回写预约确认；已领用物料始终保留在 `issued_units`，消耗只按 `issued - returned` 计算。
