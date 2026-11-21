# Target-npl

本地公开内容分析与人工复核工具。支持将 MediaCrawler 的帖子、评论输出标准化，按可配置规则分析正文证据，并通过 Web、CLI 或 MCP 管理版本化任务。

此仓库只包含通用源码、脱敏测试与重新编写的中性示例。不包含使用者的筛选方向、个人模板、任务记录、密钥、登录资料或真实采集内容。七个平台的代码接入不代表已经逐个平台完成在线登录与真实业务验收。

## 工作台结构

控制端为单一前端工程 `control-web/`，分五个视图：

| 视图 | 用途 |
| --- | --- |
| 任务配置 | 目标描述、词库、平台与额度；规则试测在同一页完成 |
| 实时情报流 | 采集与筛选的实时滚动结果，可把命中原句送回试测 |
| 人物发现 | 按任意兴趣主题，从已有公开行为证据中发现账号 |
| 复核与冲突 | 同一条内容被两个账号认领时人工裁决；`J/K` 选条、`A` 接受、`R` 驳回、冲突面板 `1/2` 改判 |
| 版本与备份 | 每次保存生成不可变快照，可恢复、导入、导出 |

后端分层（标准库实现，无第三方运行时依赖）：

- `content_screening/interests/scoring.py` — 纯函数：字面证据、分句极性、覆盖率与衰减评分，不读写数据库
- `content_screening/interests/repository.py` — 全部兴趣相关 SQL
- `content_screening/interests/service.py` / `conflicts.py` / `profiles.py` — 用例编排、冲突裁决、账号读模型
- `content_screening/http_api/` — 声明式路由表（`router.py`）、领域处理器与常驻预览进程
- `content_screening/control_server.py` — HTTP 外壳：本机校验、CSRF Token、JSON 体积限制、日志与错误编号

界面默认使用低眩光浅色阅读主题，可切换柔和深色；两套主题的正文与次级文字均满足 WCAG AA（≥ 4.5:1），分值、计数与时间戳统一使用等宽数字。

## Windows 快速开始

需要 Python 3.12+、Git、Node.js/npm；在线采集还需要 Chrome 及独立安装的 MediaCrawler。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe scripts/init_local.py
npm --prefix control-web ci
npm --prefix control-web run build
.\.venv\Scripts\python.exe -X utf8 scripts/build_release.py
.\.venv\Scripts\python.exe -X utf8 scripts/start_control.py --open
```

`init_local.py` 只创建缺失配置，不覆盖已有文件；首次运行还会显式创建一个已迁移的空数据库，之后运行改为保留现有库。运行时的预检仍然失败关闭，不会自动发明缺失或损坏的业务库。`config/`、`profiles/` 在生成后由 Git 忽略；`deployment.json` 是本仓库提供的可移植清单，`app_root`/`state_root` = `"."` 指向仓库自身。复制到任意盘符或另一台机器后，需重跑 `npm run build` 和 `scripts/build_release.py`，再执行预检。示例关键词“示例活动”仅用于离线测试，请在本机修改后再执行采集。

Web 服务仅监听本机。任务会保存不可变版本；人工锁定字段、ETag 冲突检测、采集额度与模型调用额度仍由代码校验。地点和时间条件是内容证据的后处理，不是平台原生搜索过滤。

## MediaCrawler 依赖

MediaCrawler 源码不重复收录，也不上传它的虚拟环境、浏览器缓存、Cookie 或数据。上游地址、固定提交、受控 fork 与**本地安装目录**全部登记在 [third_party/mediacrawler.json](third_party/mediacrawler.json)。三个采集入口（任务调度器 `content_screening/multi_collection.py`、抖音适配器 `scripts/mediacrawler_collect.py`、多平台适配器 `scripts/platform_collect.py`）统一通过 `content_screening/vendor_env.py` 解析依赖，**不在代码里硬编码路径**。

上游基线与受控 fork 必须安装在**两个独立目录**；fork 不得覆盖已固定的上游检出。

### A. 上游基线（匿名采集，默认）

```powershell
git clone https://github.com/NanmiCoder/MediaCrawler.git vendor/MediaCrawler
git -C vendor/MediaCrawler checkout 68d9ef1c1da22eab75940dfd7cec259907006538
python -m venv vendor/MediaCrawler/.venv
vendor/MediaCrawler/.venv/Scripts/python.exe -m pip install -r vendor/MediaCrawler/requirements.txt
```

### B. 受控 fork（公开账号字段，需先确认授权范围）

```powershell
git clone -b target-npl/public-account-fields https://github.com/Chailaoda/MediaCrawler-target-npl.git vendor/MediaCrawler-controlled
git -C vendor/MediaCrawler-controlled rev-parse HEAD
python -m venv vendor/MediaCrawler-controlled/.venv
vendor/MediaCrawler-controlled/.venv/Scripts/python.exe -m pip install -r vendor/MediaCrawler-controlled/requirements.txt
```

`rev-parse HEAD` 应为 `c15e46b15f32e5257ae139f59390fa4efa42ecba`。Linux/macOS 的解释器路径为 `<目录>/.venv/bin/python`，由声明自动选择（也可在声明的 `runtime.python_relpath` 覆盖）。运行时会校验 HEAD 在白名单内、工作树干净、解释器存在，且**调度器选择的解释器、被导入的 MediaCrawler 源码、git 完整性校验的工作树三者必须是同一目录**；调度器用 `TARGET_NPL_VENDOR_DIR` 锁定该目录并传给子进程，子进程不得自行改用其他版本。

### 运行模式与失败策略

| 环境 | 结果 |
| --- | --- |
| 只装 A，`TARGET_NPL_PUBLIC_ACCOUNTS` 未设置或 `auto` | 原版上游 + 匿名采集，允许运行 |
| 只装 A，`=1`（明确要求公开账号） | **采集前报错** `PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK`，不静默降级 |
| 装了 B，未设置 / `auto` / `=1` | 受控 fork + 按声明字段范围启用透传 |
| 装了 B，`=0` | 显式关闭，保持匿名行为 |
| 提交不在白名单 / 工作树异常 / `.venv` 缺失 | 采集前停止，返回 `VENDOR_*` 明确错误 |

```powershell
.\.venv\Scripts\python.exe -X utf8 -m content_screening.vendor_env --preflight
.\.venv\Scripts\python.exe -X utf8 -m content_screening.vendor_env --preflight --public-accounts
```

前置自检是纯离线的：只读本地文件与 git 元数据，不登录、不采集、不发起任何网络请求。**检测到 fork 只证明运行版本具备公开字段透传能力，不能替代对平台访问权限、操作者授权与登录态的确认**。在线采集前仍需人工确认平台允许相应自动化访问，并由使用者本人在项目专用浏览器完成登录；不要导入其他应用的登录资料。

MediaCrawler 自带非商业学习许可证，使用范围以其许可证为准。前端随附的上游许可证保留在 `control-web/UPSTREAM-LICENSE` 和 `control-web/MEDIACRAWLER-LICENSE`，见 [第三方说明](THIRD_PARTY_NOTICES.md)。

## 模型连接与凭证

`examples/connection.example.json` 只有公开服务地址、模型标识和项目凭证名称，没有 API Key。初始化后在本机调整 `config/connection.json`。当前代码校验火山方舟的入口与模型组合，请以服务方文档核对实际可用性。

需要模型调用时，运行 `配置方舟密钥.cmd`，在本机隐藏输入中配置自己的密钥。密钥保存在 Windows Credential Manager 的项目条目中，不进入 JSON、仓库或报告。GitHub 推送使用本机 Git/GitHub CLI 的认证，无需在项目中配置 GitHub Token。

仅离线规则处理和测试不需要任何模型或 GitHub 密钥。不要提交自己的连接配置、任务导出、规则模板或 `.env`。

## 智能体与 CLI

```powershell
.\.venv\Scripts\python.exe -X utf8 -m content_screening.task_agent platforms
.\.venv\Scripts\python.exe -X utf8 -m content_screening.task_agent call --tool screening_task_get
```

MCP 服务入口为 `scripts/task_mcp.py`；客户端示例位于 `examples/mcp-client.example.json`，使用前将 `<PROJECT_DIR>` 替换为本机绝对路径。工具与任务契约见 `schemas/`。读取任务或平台目录不会发起采集；执行任务和模型规划会使用本机已配置的服务及额度。

## 离线验证

先运行 `scripts/init_local.py`，再执行：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests
node --test tests/test_douyin_dom.cjs
npm --prefix control-web test
npm --prefix control-web run build
uvx ruff check content_screening scripts tests
```

`uvx ruff` 使用 `pyproject.toml` 中声明的语法/未定义名称门禁（E9、F63、F7、F82）。它不是运行时依赖，但合并前必须执行；本次复核发现的 `NameError` 正属于 F82 的拦截范围。

`tests/vendor_platform_offline.py` 是需要独立 MediaCrawler 环境的可选适配检查，不在默认 unittest 发现范围内。离线测试不证明各平台在线采集或模型服务可用。

运行产生的 `data/`、`output/`、`cache/`、`tmp/` 和本机模板、连接配置均留在本机。分析只应依据普通公开内容和正文证据，不推断敏感个人属性，不自动联系或互动；结果需要人工复核。
