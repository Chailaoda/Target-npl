// Bounded public UI acquisition; only the dedicated, direct Chrome profile.
const fs = require('node:fs');
const path = require('node:path');
const { captureDouyinDom } = require('./douyin-dom.cjs');

(async () => {
  const modulePath = process.argv[2];
  const limit = Number(process.argv[3] || 10);
  if (!modulePath || !Number.isInteger(limit) || limit < 1 || limit > 20) throw Error('Usage: node script PLAYWRIGHT_CORE_PATH [limit 1..20]');
  const session = JSON.parse(fs.readFileSync('cache/douyin-browser-session.json', 'utf8').replace(/^\uFEFF/, ''));
  if (session.profile_path !== path.resolve('cache/douyin-browser') || session.proxy_mode !== 'direct' || session.debug_host !== '127.0.0.1') throw Error('Dedicated direct browser mismatch');
  const { chromium } = require(modulePath);
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${session.debug_port}`, { timeout: 10000 });
  const pages = browser.contexts().flatMap(c => c.pages()).filter(p => {
    const u = new URL(p.url());
    return u.hostname === 'www.douyin.com' && /^\/(?:jingxuan\/)?search\//.test(u.pathname);
  });
  if (pages.length !== 1) throw Error('Expected one dedicated search page');
  const page = pages[0];
  if (new URL(page.url()).searchParams.has('modal_id')) {
    await page.keyboard.press('Escape');
    await page.waitForURL(u => !u.searchParams.has('modal_id'), { timeout: 5000 });
  }
  const source = new URL(page.url());
  const snapshot = await page.locator('.search-result-card .hu36PKCP').allTextContents();
  const captions = [...new Set(snapshot.map(t => t.trim()).filter(Boolean))].slice(0, limit);
  if (!captions.length) throw Error('No supported public grid cards; no output created');
  const records = new Map(), events = [];
  let halted = '';
  for (const caption of captions) {
    try {
      const current = new URL(page.url());
      if (current.origin !== source.origin || current.pathname !== source.pathname || current.searchParams.has('modal_id')) throw Error('Search page changed; stopped');
      const card = page.locator('.search-result-card .hu36PKCP').filter({ hasText: caption });
      if (await card.count() !== 1 || (await card.innerText()).trim() !== caption) throw Error('Ambiguous or changed card; stopped');
      await card.click();
      await page.waitForURL(u => u.searchParams.has('modal_id'), { timeout: 10000 });
      await page.locator('[data-e2e="feed-active-video"] [data-e2e="video-desc"]').waitFor({ timeout: 10000 });
      const detail = await page.evaluate(captureDouyinDom);
      if (detail.layout !== 'active_post_detail' || detail.records.length !== 1 || detail.records[0].text !== caption) throw Error('Detail did not match selected caption; stopped');
      const record = detail.records[0];
      if (records.has(record.content_id) && records.get(record.content_id).text !== record.text) throw Error('Conflicting duplicate ID; stopped');
      events.push({ content_id: record.content_id, caption_matched: true, id_matched_url: true, duplicate: records.has(record.content_id) });
      records.set(record.content_id, record);
      await page.keyboard.press('Escape');
      await page.waitForURL(u => !u.searchParams.has('modal_id'), { timeout: 5000 });
    } catch (error) {
      // Do not log arbitrary browser error bodies, DOM or account panels.
      halted = error.message.split('\n')[0].slice(0, 160);
      break;
    }
  }
  if (!records.size) throw Error(halted || 'No validated records; no output created');
  const collectedAt = new Date().toISOString(), stamp = collectedAt.replace(/[:.]/g, '-');
  fs.mkdirSync('data', { recursive: true }); fs.mkdirSync('output', { recursive: true });
  const file = `data/douyin-batch-${stamp}.jsonl`;
  fs.writeFileSync(file, [...records.values()].map(r => JSON.stringify(r)).join('\n') + '\n', { encoding: 'utf8', flag: 'wx' });
  const receipt = { collected_at_utc: collectedAt, source_page: source.origin + source.pathname, method: 'public_UI_open_detail_then_DOM', loaded_caption_count: snapshot.length,
    limit, attempted: events.length + (halted ? 1 : 0), records: records.size, halted: halted || null, events,
    input_file: path.resolve(file), proxy_mode: 'direct', cookies_exported: false, pagination: false,
    link_note: '作品 ID 与当前详情 URL/DOM 一致；标准作品链接按该 ID 构造，作者链接取自公开 DOM' };
  const receiptPath = `output/douyin-batch-${stamp}.json`;
  fs.writeFileSync(receiptPath, JSON.stringify(receipt, null, 2), { encoding: 'utf8', flag: 'wx' });
  console.log(JSON.stringify({ ...receipt, receipt_file: path.resolve(receiptPath) }, null, 2));
  process.exit(0); // Leave the user's dedicated browser running.
})().catch(error => { console.error(error.message.split('\n')[0]); process.exit(1); });
