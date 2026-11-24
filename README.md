# 预约与容量分析

本地演练资料记录场地、拓印台、人员能力、家庭和团体预约变更、现场签到及物料领用。人数与设备能力使用各自单位，确认记录和现场事实分开保存。

## Local checks

Python 3.9 or newer is sufficient. The repository has no third-party runtime dependencies.

Run `python3 input_audit.py` to verify the declared files and columns. Run `python3 -m unittest discover -s tests -v` to check the prepared input contract.

The records are synthetic fixtures. Empty values and status labels retain their literal input meaning. The manifest lists file columns and the stable row identifier used by the input audit.
