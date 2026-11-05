# 第一轮改造变更总结（WP0–WP3）

**分支：** `feature/public-account-profiles`（基于 `main` = `40ec271`）
**范围：** WP0 安全基线、WP1 MediaCrawler 受控 fork、WP2 数据标准化、WP3 数据库与缓存分离
**不含：** 前端重构（WP6）、兴趣画像算法（WP4）、查询服务（WP5）——留待后续轮次

## 提交清单

| 提交 | 工作包 | 内容 |
|---|---|---|
| `74165fe` | WP0 | 基线报告、字段许可清单、平台可用性矩阵、回滚方案 |
| `65e8550` | WP1 | 受控 fork 登记与白名单校验、采集脚本适配 |
| `fc88e8b` | WP2 | 投影/标准化接收公开账号字段、A03/A07 修复 |
| `3661a26` | WP3 | account_repository、六表迁移、缓存分离、14 项回归测试 |

## MediaCrawler 受控版本

- fork 分支：`target-npl/public-account-fields`（worktree 位于 `vendor/mediacrawler-fork/`，git 仓库在 `vendor/MediaCrawler/.git`）
- 基线上游：`68d9ef1`；fork HEAD 相对上游 92 行纯增量
- 透传开关：环境变量 `TARGET_NPL_PUBLIC_ACCOUNTS=1`（默认关闭，关闭时 `creator_hash`/`mask_nickname` 输出与上游逐字节一致）
- 7 平台透传字段：`author_id`、`author_nickname`、`author_profile_url`（均来自平台公开内容接口本身，不发额外主页请求）
- 依赖声明：`third_party/mediacrawler.json` 登记 fork 提交；采集脚本校验改为「允许提交集合 ∈ 白名单 + 工作树必须干净」
- 许可证：上游 NON-COMMERCIAL LEARNING LICENSE 1.1 保持不变

## 关键设计决策

1. **账号键 = `platform:account_id`**（如 `bilibili:42`）。不同平台同 ID 不合并；昵称永不作为身份关联依据；无可靠 ID 时保持 unknown 分组。
2. **语义与展示分离**：公开账号的完整昵称/主页 URL 不进入标准化语义记录（`author`/`author_url` 置空），单独经 `normalize_profile()` → accounts 表。昵称或主页更新**不会**改变内容指纹，旧分析缓存零失效。
3. **旧数据逐字节兼容**：匿名记录（`creator_hash` 路径）的 normalize 输出与改造前完全一致；已用真实库副本验证 106 条 v2_sources、238 条 v2_analyses 迁移后逐条不变。
4. **迁移幂等可回滚**：`scripts/db_migrate.py --rollback` 只删新增六表，v2_* 数据不动；`workflow.connect()` 打开即自动增量迁移，CLI/控制台/MCP 全入口透明升级。
5. **运行时零影响**：根目录运行环境的 vendor HEAD（`68d9ef1`）未动，旧完整性校验照常通过。

## 验证结果

- 全量测试 91/91 通过（77 既有 + 14 新增 `tests/test_public_accounts.py`）
- 新增回归覆盖：同 ID 不同平台不合并、同昵称不同 ID 不合并、昵称/URL 更新不触发重分析、账号 ID 缺失标记 unknown、旧匿名数据导入兼容、重复采集不重复累计、A07 按平台校验来源链接、迁移回滚重放
- fork 离线验证：7 平台 × 开关两态（关闭=零公开字段；打开=ID/昵称/主页 URL 完整透传）
- 端到端链路（fork store → project → normalize）：B站账号 `bilibili:42` 作品+评论正确归属同一账号，xsec_token 不入结果
- 真实数据库副本迁移：缓存零失效、回滚→再迁移数据一致

## 未解决事项（后续轮次）

- WP4 兴趣画像模块（`interest_*` 四表已建好，逻辑未实现）
- WP5 `/api/accounts`、`/api/interests/search` 查询服务
- WP6 Web 人物工作台三视图
- B站小规模在线采集验收（方案要求端到端在线验证后再扩展平台）
- 平台字段可用性矩阵中「待在线确认」项（微博/知乎/贴吧/快手的实际字段返回情况）
