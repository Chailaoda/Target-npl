function captureDouyinDom() {
    const records = [], skipped = [];
    const activeNodes = [...document.querySelectorAll('[data-e2e="feed-active-video"]')];
    const modalId = new URL(location.href).searchParams.get('modal_id');
    if (modalId && activeNodes.length === 1) {
      const active = activeNodes[0];
      const id = active.getAttribute('data-e2e-vid');
      const descriptions = active.querySelectorAll('[data-e2e="video-desc"]');
      const authors = active.querySelectorAll('[data-e2e="feed-video-nickname"]');
      const link = active.querySelector('a[href*="/user/"]');
      const text = descriptions.length === 1 ? descriptions[0].innerText.trim() : '';
      const author = authors.length === 1 ? authors[0].innerText.trim().replace(/^@/, '') : '';
      if (id !== modalId || !/^\d+$/.test(id) || !text || !author || text === author) throw Error('Active post ID/caption/author did not validate');
      const authorUrl = link ? new URL(link.href) : null;
      if (authorUrl && authorUrl.hostname !== 'www.douyin.com') throw Error('Unexpected public author host');
      records.push({ platform: 'douyin', content_id: id, object_type: 'post', author, text,
        author_url: authorUrl ? authorUrl.origin + authorUrl.pathname : '', url: `https://www.douyin.com/video/${id}`,
        published_time_text: active.innerText.split('\n').map(t => t.trim()).find(t => t.startsWith('·')) || '', synthetic: false });
      return { records, skipped, layout: 'active_post_detail', id_matched_url: true };
    }
    for (const card of [...document.querySelectorAll('.search-result-card')].slice(0, 3)) {
      const ids = [...new Set([...card.querySelectorAll('[data-e2e-vid]')].map(e => e.getAttribute('data-e2e-vid')))];
      const captions = card.querySelectorAll('div.RvxQrxux');
      const caption = captions.length === 1 ? captions[0] : null;
      const author = [...card.querySelectorAll('a[href*="/user/"]')].find(a => a.innerText.trim() && !a.innerText.trim().startsWith('@'));
      if (ids.length !== 1 || !/^\d+$/.test(ids[0]) || !caption?.innerText.trim() || !author || caption.innerText.trim() === author.innerText.trim()) {
        skipped.push('Missing or ambiguous ID/caption/author'); continue;
      }
      const authorUrl = new URL(author.href);
      if (authorUrl.hostname !== 'www.douyin.com' || !authorUrl.pathname.startsWith('/user/')) { skipped.push('Invalid author URL'); continue; }
      const lines = card.innerText.split('\n').map(t => t.trim()).filter(Boolean);
      records.push({ platform: 'douyin', content_id: ids[0], object_type: 'post', author: author.innerText.trim(), author_url: authorUrl.origin + authorUrl.pathname,
        text: caption.innerText.trim(), url: `https://www.douyin.com/video/${ids[0]}`, published_time_text: lines.find(t => t.startsWith('·')) || '', synthetic: false });
    }
    return { records, skipped, layout: 'feed_cards' };
}
module.exports = { captureDouyinDom };
