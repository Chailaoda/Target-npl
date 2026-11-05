# WP0 基线报告：公开账号字段改造（第一轮 PR：WP0–WP3）

日期：2026-11-05 · 分支：`feature/public-account-profiles`（基于 `40ec271ccd8adb6a17450c08ee21832184f7ad28`）

## 1. 版本基线

| 组件 | 提交/版本 | 说明 |
|---|---|---|
| Target NPL | `40ec271` `Initial sanitized source distribution` | main 基线，77 个单元测试通过 |
| MediaCrawler | `68d9ef1c1da22eab75940dfd7cec259907006538` | vendor 固定提交，工作区干净 |
| 依赖声明 | `third_party/mediacrawler.json` | `source_included: false`，源码不入库 |

运行环境说明：生产运行目录为工作区根 `E:/workplace/Target-npl`（旧包名 `festival_match`）；GitHub 导出仓库位于 `tmp/github-clean-check-20261104`（新包名 `content_screening`）。**本改造全部落在 git 仓库内，不修改根目录运行文件；vendor/MediaCrawler 的 `HEAD` 保持在 `68d9ef1` 不动**（根目录运行脚本的完整性校验依赖它），受控版本通过分支 + worktree 承载。

## 2. 数据备份（本地保留，不上传 GitHub）

- 备份目录：`data/backups/20261105-wp0/`
  - `festival.sqlite3`（v2_sources 106 条 / v2_analyses 238 条 / v2_tasks 345 条 / v2_jobs 13 个批次）
  - 4 个 douyin 采集 jsonl、`tool-contract-check.jsonl`
  - `collection.json`、`control-active.json`、`mcp-client.example.json`
  - `connection.json.bak`（含 API 密钥，仅本地）

## 3. 字段许可清单（platform × field 来源矩阵）

字段来源依据 MediaCrawler `68d9ef1` 各平台 store 层源码（`store/{douyin,xhs,kuaishou,bilibili,weibo,tieba}/__init__.py`、`media_platform/tieba/help.py`、`media_platform/zhihu/help.py`）逐平台核对。上游在源头主动匿名化（`tools/user_hash.py`）：`anonymize_user_id`（sha256 截 16 位）与 `mask_nickname`（首尾各留 1 字）。

**当前字段来源矩阵（基线，均为"平台公开返回 → store 层匿名化"）：**

| 平台 | 原始账号 ID 字段 | 当前入库形态 | 昵称来源 | 主页 URL |
|---|---|---|---|---|
| 抖音 | `user_info.uid` | `creator_hash`（匿名） | `nickname`（脱敏） | 不采集 |
| 小红书 | `user.user_id` / `user_info.user_id` | `creator_hash` | `nickname`（脱敏） | 不采集 |
| B站 | `owner.mid` / `member.mid` | `creator_hash` | `name`/`uname`（脱敏） | 不采集 |
| 快手 | `author.id` / `authorId` | `creator_hash` | `name`/`authorName`（脱敏） | 不采集 |
| 微博 | `user.id` | `creator_hash` | `screen_name`（脱敏） | 不采集 |
| 贴吧 | `user.id`/`portrait` | `creator_hash` | `show_nickname`（脱敏） | 不采集 |
| 知乎 | `author.id` 等 | `creator_hash` | `name`（脱敏） | 不采集 |

**改造许可决定（WP1 执行）：**

| 字段 | 许可状态 | 依据 |
|---|---|---|
| `account_id`（原始 uid/mid/user_id） | 允许保留 | 平台搜索/详情/评论接口公开返回的作者标识，随内容正常返回 |
| `nickname`（完整） | 允许保留 | 同上，公开昵称 |
| `profile_url` | 允许保留 | 各平台公开主页格式可由账号 ID 构造（如 `space.bilibili.com/{mid}`），不请求额外接口 |
| `avatar_url` / `public_bio` | 本阶段不采集 | 需要额外的主页/用户详情请求，超出"随内容返回"范围，待 WP7 逐平台核验授权后再评估 |
| 手机号/微信号/邮箱/认证信息 | 禁止 | 方案第一节红线 |
| Cookie/Token | 禁止入库 | 现有 `xsec_token` 已在 project 层剥离，保持 |
| 私信/关注/批量互动 | 禁止 | 无对应代码路径，保持 |

**采集行为边界（不变）：** 请求频率 `request_interval_seconds`、总量 `max_requests`、`proxy_mode=direct`、验证码/登录需求即停（NEEDS_USER_CODES）、不自动登录、不绕过限制。

## 4. 平台可用性矩阵

| 平台 | 代码接入 | 离线验收 | 在线验收 |
|---|---|---|---|
| B站 | ✅ | WP1 worktree 验收 | **第一阶段端到端目标**，待用户登录后验证 |
| 小红书/抖音 | ✅ | WP1 worktree 验收 | 待验证（不在第一轮宣称可用） |
| 微博/知乎/贴吧/快手 | ✅ | WP1 worktree 验收 | 待验证 |
| 全部 | — | — | 未通过在线验收的平台一律标记 `live_acceptance: pending`，不宣称可用 |

## 5. 缓存兼容策略（WP2/WP3 核心约束）

现状：`v2_sources.source_hash` = 整条 normalize 记录的 fingerprint；`v2_analyses.analysis_key` 由 `{source_hash, policy, identity, context}` 派生。新增账号展示字段若进入 normalize 记录 → source_hash 全变 → 238 条分析缓存全部失效 → 重烧模型费用。

**对策（WP3 落实）：**
1. `normalize()` 输出拆为两部分：`semantic_fields`（进 source_hash：platform/object_type/content_id/text/root_id/parent_id/published_at/url/normalization_version/author_key…**作者标识除外**）与 `profile_fields`（不进 source_hash：nickname/avatar/profile_url 等）。
2. `accounts` 表以 `platform + account_id` 为主键，展示资料变更只 UPDATE accounts + 记 profile_history，不触碰内容分析缓存。
3. 旧匿名记录（`author_key='anonymous:…'`）读取路径保持逐字节兼容：同输入 → 同 fingerprint → 缓存命中。
4. `read_export` 的 `raw_hash` 继续基于原始行计算，不受影响。

## 6. 回滚方案

1. **代码回滚**：`git checkout main`；功能分支保留不删，数据库 schema 变更可回滚（见下）。
2. **数据库回滚**：WP3 迁移为纯增量 `CREATE TABLE IF NOT EXISTS` + `v2_sources` 增列 `semantic_hash`，提供 `scripts/db_migrate.py --rollback`：删除新增表、`DROP COLUMN semantic_hash`（SQLite 3.35+）。旧表旧列零改动，旧代码可立即在迁移后的库上运行（新列对其透明）。
3. **vendor 回滚**：受控版本在独立分支/worktree，运行时 `HEAD` 与 integrity check 不动；回滚即弃用新分支，无需任何操作。
4. **数据回滚**：真实数据全部在 `data/backups/20261105-wp0/` 有冷备份；改造过程不修改任何历史记录行（新表只增、旧表 INSERT-only）。

## 7. 验收（WP0）

- [x] 分支已建立，基线提交已记录
- [x] 77 个原有测试在基线上通过
- [x] SQLite 与配置已备份到本地，密钥不进 git
- [x] 字段清单/平台矩阵/回滚方案成文
- [ ] 未发生真实资料泄漏（交付前终检：git status 无 data/cache/output 泄漏文件）
