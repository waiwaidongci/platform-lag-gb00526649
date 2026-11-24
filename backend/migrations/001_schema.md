# 001 初始表结构

应用首次启动以 SQLAlchemy 模型创建表与唯一约束。现有数据表只补建缺失表，升级到修改过字段的版本需要显式迁移。

- `users` 账号角色和加盐密码摘要。
- `datasets` 不可变记录批次，以名称和 SHA-256 去重。
- `runs` 记录参数、数据批次、运行租约、分析结果、导出文件及复核决定。
- `schedules` 保存定时计划和下一到期时间。
- `deliveries` 保存站内分发回执，每次运行最多一个回执。
- `audit_events` 记录登录、数据入库、运行、复核和报表分发动作。
- `meters` 声级计台账，表号唯一，每个站台至多一台 `is_baseline` 基准表。
- `comparison_batches` 同点比测批次，`batch_key` 唯一；同版重试直接复用，不重复落库或发布。
- `comparisons` 同点比测边，记录 A−B 偏差区间 `[deviation_low, deviation_high]` 及撤回状态。
- `conversion_releases` 一次整体切换的发布版本，含当前活跃边集指纹和各组求解摘要。
- `conversion_cursors` 每个换算组当前生效版本；录入/撤回/换基准后置 `stale=1`，发布时在单事务内整体切换。
