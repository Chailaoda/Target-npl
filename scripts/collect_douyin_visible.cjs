// Read only the dedicated browser's already-rendered public result cards.
const fs = require('node:fs');
const path = require('node:path');
const { captureDouyinDom } = require('./douyin-dom.cjs');

(async () => {
  const modulePath = process.argv[2];
  if (!modulePath) throw Error('Provide the installed playwright-core module path');
  const { chromium } = require(modulePath);
  const session = JSON.parse(fs.readFileSync('cache/douyin-browser-session.json', 'utf8').replace(/^\uFEFF/, ''));
  if (session.profile_path !== path.resolve('cache/douyin-browser') || session.proxy_mode !== 'direct') throw Error('Dedicated direct browser session mismatch');
  const browser = await chromium.connectOverCDP(`http://127.0.0.1:${session.debug_port}`, { timeout: 10000 });
  const pages = browser.contexts().flatMap(c => c.pages()).filter(p => {
    const u = new URL(p.url());
    return u.hostname === 'www.douyin.com' && /^\/(?:jingxuan\/)?search\//.test(u.pathname);
  });
  if (pages.length !== 1) throw Error('Expected exactly one dedicated search page');
  const page = pages[0];
  const collectedAt = new Date().toISOString();
  const snapshot = await page.evaluate(captureDouyinDom);
  if (!snapshot.records.length) throw Error('No verified public cards; no output created');
  const records = [...new Map(snapshot.records.map(r => [r.content_id, r])).values()];
  const stamp = collectedAt.replace(/[:.]/g, '-');
  const file = `data/douyin-visible-${stamp}.jsonl`;
  fs.mkdirSync('data', { recursive: true });
  fs.writeFileSync(file, records.map(r => JSON.stringify(r)).join('\n') + '\n', { encoding: 'utf8', flag: 'wx' });
  const u = new URL(page.url());
  const receipt = { collected_at_utc: collectedAt, method: 'rendered_public_DOM', source_page: u.origin + u.pathname, records: records.length,
    skipped: snapshot.skipped, layout: snapshot.layout, id_matched_url: snapshot.id_matched_url || false, input_file: path.resolve(file), proxy_mode: 'direct', cookies_exported: false,
    link_note: '作品链接由页面公开 data-e2e-vid 构造；作者链接来自实际公开 DOM 并移除追踪参数；作品链接尚未逐条打开验证' };
  fs.writeFileSync('output/douyin-visible-latest.json', JSON.stringify(receipt, null, 2));
  console.log(JSON.stringify(receipt, null, 2));
  process.exit(0); // Disconnect this client; leave the user's Chrome open.
})().catch(e => { console.error(e.message); process.exit(1); });
