# Target-npl

本地公开内容分析与人工复核工具。支持将 MediaCrawler 的帖子、评论输出标准化，按可配置规则分析正文证据，并通过 Web、CLI 或 MCP 管理版本化任务。

此仓库只包含通用源码、脱敏测试与重新编写的中性示例。不包含使用者的筛选方向、个人模板、任务记录、密钥、登录资料或真实采集内容。七个平台的代码接入不代表已经逐个平台完成在线登录与真实业务验收。

## Windows 快速开始

需要 Python 3.12+、Git、Node.js/npm；在线采集还需要 Chrome 及独立安装的 MediaCrawler。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe scripts/init_local.py
npm --prefix control-web ci
npm --prefix control-web run build
.\.venv\Scripts\python.exe -X utf8 scripts/start_control.py --open
```

`init_local.py` 只创建缺失配置，不覆盖已有文件。生成的 `config/`、`profiles/` 均被 Git 忽略。示例关键词“示例活动”仅用于离线测试，请在本机修改后再执行采集。

Web 服务仅监听本机。任务会保存不可变版本；人工锁定字段、ETag 冲突检测、采集额度与模型调用额度仍由代码校验。地点和时间条件是内容证据的后处理，不是平台原生搜索过滤。

## MediaCrawler 依赖

MediaCrawler 源码不重复收录，也不上传它的虚拟环境、浏览器缓存、Cookie 或数据。适配器使用的上游地址与固定提交记录在 [third_party/mediacrawler.json](third_party/mediacrawler.json)。

```powershell
git clone https://github.com/NanmiCoder/MediaCrawler.git vendor/MediaCrawler
git -C vendor/MediaCrawler checkout 68d9ef1c1da22eab75940dfd7cec259907006538
```

然后按照该固定版本的上游说明，在 `vendor/MediaCrawler/.venv` 安装独立 Python 环境和依赖。适配器使用 Windows 的 `.venv/Scripts/python.exe` 路径。平台登录由使用者本人在项目专用浏览器中完成；不要导入其他应用的登录资料。

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
```

`tests/vendor_platform_offline.py` 是需要独立 MediaCrawler 环境的可选适配检查，不在默认 unittest 发现范围内。离线测试不证明各平台在线采集或模型服务可用。

运行产生的 `data/`、`output/`、`cache/`、`tmp/` 和本机模板、连接配置均留在本机。分析只应依据普通公开内容和正文证据，不推断敏感个人属性，不自动联系或互动；结果需要人工复核。
