/**
 * Turn backend error codes into a Chinese next action.
 *
 * The server keeps returning machine codes (and tests assert them); the UI must
 * never show a bare VENDOR_* string to the operator.
 */
export const ERROR_GUIDES: Record<string, { title: string; action: string }> = {
  PUBLIC_ACCOUNTS_REQUIRE_CONTROLLED_FORK: {
    title: '当前模式要求使用受控 MediaCrawler Fork',
    action: '请检查 vendor/MediaCrawler-controlled 安装，或关闭公开账号透传开关后重试。'
  },
  VENDOR_MISSING: {
    title: '未找到 MediaCrawler 依赖目录',
    action: '按 README 的“MediaCrawler 依赖”章节克隆上游基线并安装依赖后重试。'
  },
  VENDOR_DIRTY: {
    title: 'MediaCrawler 工作树不干净',
    action: '在 vendor 目录执行 git status 查看改动；恢复干净后重新采集。'
  },
  VENDOR_COMMIT_MISMATCH: {
    title: 'MediaCrawler 提交不在白名单',
    action: '检出 third_party/mediacrawler.json 声明的固定提交后重试。'
  },
  VENDOR_PYTHON_MISSING: {
    title: 'MediaCrawler 解释器缺失',
    action: '在 vendor 目录创建 .venv 并安装 requirements.txt 后重试。'
  },
  not_started_budget: {
    title: '今日采集或模型额度已达上限',
    action: '可在配置工作台调高预算上限，或等待次日 UTC 重置后再启动。'
  },
  BUDGET_EXHAUSTED: {
    title: '今日额度已用尽',
    action: '等待次日 UTC 重置，或调高 data/budget 之外的每日上限配置。'
  },
  LOGIN_REQUIRED: {
    title: '平台需要重新登录',
    action: '在“平台与范围”里点对应平台的“登录”，用项目专用浏览器完成登录。'
  },
  LOGIN_NOT_PREPARED: {
    title: '尚未准备专用浏览器会话',
    action: '先点该平台的“登录”打开专用窗口，完成后再开始采集。'
  },
  COMMENT_DATA_MISSING: {
    title: '部分帖子的评论数据缺失',
    action: '该帖已跳过并记为告警；可在实时情报流查看 warning 计数，必要时重跑。'
  }
}

export function formatErrorGuidance(raw: string): string {
  const text = String(raw || '')
  for (const [code, guide] of Object.entries(ERROR_GUIDES)) {
    if (text.includes(code)) return `${guide.title}：${guide.action}`
  }
  return text
}
