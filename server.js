// RSS 聚合服务（纯服务端）：读取 feeds.txt 中所有源 → 抓取各自最新一条 →
// 跨源去重 → 按发布时间倒序 → 输出 RSS 2.0。零第三方依赖，Node 18+（内置 fetch）。
//
// 环境变量：
//   PORT            监听端口（默认 3000）
//   CACHE_TTL       服务端刷新周期，秒（默认 60）
//   FETCH_TIMEOUT   单源抓取超时，毫秒（默认 20000）
//   FETCH_CONCURRENCY 并发抓取数（默认 12）
//   FEEDS_FILE      订阅源列表路径（默认 ./feeds.txt）

const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const zlib = require('zlib');

const PORT = parseInt(process.env.PORT || '3000', 10);
const FEEDS_FILE = process.env.FEEDS_FILE || path.join(__dirname, 'feeds.txt');
const CACHE_TTL = parseInt(process.env.CACHE_TTL || '60', 10) * 1000;
const FETCH_TIMEOUT = parseInt(process.env.FETCH_TIMEOUT || '20000', 10);
const CONCURRENCY = Math.max(1, parseInt(process.env.FETCH_CONCURRENCY || '12', 10));
// 浏览器 UA，减少被源站拦截
const UA = process.env.FEED_UA ||
  'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

// ---------------- 缓存状态 ----------------
const cache = {
  builtAt: 0,          // 上次成功刷新完成时间
  refreshing: false,
  items: [],           // 合并后的条目
  cond: {},            // url -> { etag, last_modified, item, lastOk, lastError, fails }
  stats: { ok: 0, fail: 0, total: 0, ms: 0 },
  feedEtag: '',        // 对外 feed 的 ETag（内容不变则返回 304）
  feedBody: '',
  feedGzip: null,      // 缓存的 gzip 正文
};

// ---------------- 通用工具 ----------------
// 解析 feeds.txt：返回 [{ url, name }]，name 取紧邻上一行的注释（用于阅读器显示来源）
function readFeedEntries() {
  const seen = new Set();
  const out = [];
  let pendingName = '';
  for (const raw of fs.readFileSync(FEEDS_FILE, 'utf8').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith('#')) {                       // 注释行：作为下一个地址的来源名
      const txt = line.replace(/^#+\s*/, '').trim();
      // 只取括号前的主名称，括号里通常是备注
      const clean = txt.split(/[（(]/)[0].trim();
      if (clean && !clean.startsWith('=') && !clean.startsWith('-')) pendingName = clean;
      continue;
    }
    const url = line.split('#')[0].trim();            // 也支持行尾注释
    if (!url || seen.has(url)) continue;
    seen.add(url);
    out.push({ url, name: pendingName });
    pendingName = '';
  }
  return out;
}

function readFeedUrls() {
  return readFeedEntries().map(e => e.url);
}

// 只信任合法主机名，防止 Host 头注入（页面/链接里会展示 host）
function safeHost(h) {
  const s = String(h || '').trim();
  return /^[a-zA-Z0-9._\-]+(:\d{1,5})?$/.test(s) ? s : `localhost:${PORT}`;
}

// 并发受限的 map：避免一次性打开几十个连接
async function mapLimit(items, limit, fn) {
  const results = new Array(items.length);
  let cursor = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const i = cursor++;
      if (i >= items.length) return;
      try { results[i] = { status: 'fulfilled', value: await fn(items[i], i) }; }
      catch (e) { results[i] = { status: 'rejected', reason: e }; }
    }
  });
  await Promise.all(workers);
  return results;
}

// 跨源去重：同一链接或同一归一化标题只保留第一条（时间最新的）
function normTitle(t) {
  return String(t || '')
    .replace(/[「」【】\[\]（）()“”"'‘’：:，,。.！!？?、\s|·\-—–]/g, '')
    .replace(/[a-zA-Z0-9]/g, '')
    .slice(0, 30);
}
// 标题相似度阈值：归一化后至少 8 个汉字才判重，避免误杀不同新闻
const TITLE_DUP_MIN = 8;
function dedupeItems(items) {
  const seenLink = new Set();
  const seenTitle = new Set();
  const out = [];
  let dropped = 0;
  for (const it of items) {
    const lk = (it.link || '').replace(/[#?].*$/, '');   // 去掉跟踪参数再比
    const tt = normTitle(it.title);
    if (lk && seenLink.has(lk)) { dropped++; continue; }
    if (tt.length >= TITLE_DUP_MIN && seenTitle.has(tt)) { dropped++; continue; }
    if (lk) seenLink.add(lk);
    if (tt.length >= TITLE_DUP_MIN) seenTitle.add(tt);
    out.push(it);
  }
  if (dropped) console.log(`[去重] 跨源重复 ${dropped} 条`);
  return out;
}

// ---------------- XML 解析（容错式，支持 RSS2/RDF/Atom） ----------------
function decodeEntities(s) {
  return s
    .replace(/&#x([0-9a-fA-F]+);/g, (_, h) => String.fromCodePoint(parseInt(h, 16)))
    .replace(/&#(\d+);/g, (_, d) => String.fromCodePoint(parseInt(d, 10)))
    .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&apos;/g, "'")
    .replace(/&amp;/g, '&');
}

function tagContent(xml, name) {
  const m = xml.match(new RegExp(`<${name}(?:\\s[^>]*)?>([\\s\\S]*?)<\\/${name}>`, 'i'));
  if (!m) return '';
  return decodeEntities(m[1].replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, '$1')
    .replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
}

function linkOf(entryXml) {
  let m = entryXml.match(/<link\b[^>]*\bhref=["']([^"']+)["'][^>]*>/i);   // Atom
  if (m) return decodeEntities(m[1]).trim();
  m = entryXml.match(/<link(?:\s[^>]*)?>([\s\S]*?)<\/link>/i);            // RSS
  if (m) return decodeEntities(m[1]).replace(/<[^>]+>/g, '').trim();
  return '';
}

function dateOf(entryXml) {
  for (const name of ['pubDate', 'published', 'updated', 'dc:date', 'date']) {
    const raw = tagContent(entryXml, name);
    if (!raw) continue;
    const t = new Date(raw).getTime();
    if (!Number.isNaN(t)) return { ts: t, raw };
  }
  return null;
}

function latestItem(xml) {
  const blocks = [];
  const re = /<(item|entry)(?:\s[^>]*)?>([\s\S]*?)<\/\1>/gi;
  let m;
  while ((m = re.exec(xml)) !== null) blocks.push(m[2]);
  let best = null;
  for (const b of blocks) {
    const date = dateOf(b);
    if (best === null || (date && date.ts > (best.ts || 0))) {
      best = {
        title: tagContent(b, 'title'),
        link: linkOf(b),
        description: tagContent(b, 'description') || tagContent(b, 'summary') || tagContent(b, 'content'),
        ts: date ? date.ts : 0,
        dateRaw: date ? date.raw : '',
      };
    }
  }
  return best;
}

// ---------------- 抓取单个源 ----------------
async function fetchOne(url) {
  const cached = cache.cond[url] || {};
  const headers = { 'User-Agent': UA, 'Accept': 'application/rss+xml, application/xml, text/xml, */*' };
  if (cached.etag) headers['If-None-Match'] = cached.etag;
  if (cached.last_modified) headers['If-Modified-Since'] = cached.last_modified;

  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT);
  try {
    const resp = await fetch(url, { headers, signal: ctrl.signal, redirect: 'follow' });
    const etag = resp.headers.get('etag');
    const lastModified = resp.headers.get('last-modified');

    // 304：上游无更新，直接复用缓存条目（最省流量）
    if (resp.status === 304 && cached.item) {
      cached.lastOk = new Date().toISOString();
      cached.lastError = '';
      cached.fails = 0;
      cache.cond[url] = cached;
      return cached.item;
    }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

    const body = await resp.text();
    const item = latestItem(body);
    if (!item) throw new Error('未解析到条目');
    item.src = url;                       // 记住出处，供阅读器显示来源

    cache.cond[url] = {
      etag, last_modified: lastModified, item,
      lastOk: new Date().toISOString(), lastError: '', fails: 0,
    };
    return item;
  } catch (e) {
    const cause = e.cause && (e.cause.code || e.cause.message);
    const msg = e.name === 'AbortError' ? `超时(${FETCH_TIMEOUT / 1000}s)`
      : cause ? `${e.message} (${cause})` : e.message;
    cache.cond[url] = { ...cached, lastError: msg, fails: (cached.fails || 0) + 1 };
    throw new Error(msg);
  } finally {
    clearTimeout(timer);
  }
}

// 失败源单次重试（应对上游瞬时抖动）
async function fetchWithRetry(url) {
  try {
    return await fetchOne(url);
  } catch (e) {
    await new Promise(r => setTimeout(r, 800 + Math.random() * 700));
    return await fetchOne(url);
  }
}

// ---------------- 刷新（定时 + 按需双触发） ----------------
async function refresh() {
  if (cache.refreshing) return;            // 同一时刻只跑一轮
  cache.refreshing = true;
  const startedAt = Date.now();
  try {
    const entries = readFeedEntries();
    const urls = entries.map(e => e.url);
    const nameOf = new Map(entries.map(e => [e.url, e.name]));
    if (!urls.length) throw new Error('feeds.txt 中没有有效地址');

    // 清理已被删除的源，避免 cond 无限增长
    const alive = new Set(urls);
    for (const k of Object.keys(cache.cond)) if (!alive.has(k)) delete cache.cond[k];

    const results = await mapLimit(urls, CONCURRENCY, fetchWithRetry);

    const items = [];
    let ok = 0, fail = 0;
    results.forEach((r, i) => {
      if (r.status === 'fulfilled' && r.value) {
        items.push(r.value);
        ok++;
        return;
      }
      fail++;
      const reason = r.status === 'rejected' ? r.reason.message : '无条目';
      console.error(`[源失败] ${urls[i]} -> ${reason}`);
      const cached = cache.cond[urls[i]];
      if (cached && cached.item) {
        cached.item.src = cached.item.src || urls[i];
        items.push(cached.item);                            // 回退上次成功的条目
      }
    });

    // 标注来源名（优先用 feeds.txt 注释里的中文名）
    for (const it of items) {
      it.srcName = (it.src && nameOf.get(it.src)) || (() => {
        try { return new URL(it.src).hostname.replace(/^www\./, ''); } catch (_) { return ''; }
      })();
    }
    items.sort((a, b) => (b.ts || 0) - (a.ts || 0));
    const deduped = dedupeItems(items);

    cache.items = deduped;
    cache.stats = { ok, fail, total: urls.length, ms: Date.now() - startedAt };
    cache.builtAt = Date.now();
    // 不主动清空 feedBody：请求时会重新生成，内容一致则沿用旧 ETag（客户端 304）


    console.log(`[刷新] 源 ${urls.length} 成功 ${ok} 失败 ${fail} 条目 ${deduped.length} 耗时 ${cache.stats.ms}ms`);
  } catch (e) {
    console.error('[刷新失败]', e.message);
  } finally {
    cache.refreshing = false;
  }
}

function sourceStatus() {
  return readFeedEntries().map(e => {
    const u = e.url;
    const c = cache.cond[u] || {};
    return {
      url: u,
      name: e.name,
      ok: !!c.item && !c.lastError,
      error: c.lastError || '',
      lastOk: c.lastOk || '',
      fails: c.fails || 0,
      title: c.item ? c.item.title : '',
    };
  });
}

// ---------------- RSS 输出 ----------------
const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');

function buildRss(host) {
  const self = `https://${host}/feed.xml`;
  const newest = cache.items.reduce((m, it) => Math.max(m, it.ts || 0), 0);
  const lines = [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0"><channel>',
    `<title>${esc('我的 RSS 聚合头条')}</title>`,
    `<link>${esc(self)}</link>`,
    `<description>${esc(`聚合 ${cache.stats.total} 个 RSS 源的最新头条，按发布时间倒序`)}</description>`,
    '<language>zh-CN</language>',
    `<lastBuildDate>${new Date(cache.builtAt || Date.now()).toUTCString()}</lastBuildDate>`,
    `<pubDate>${new Date(newest || Date.now()).toUTCString()}</pubDate>`,
    `<ttl>${Math.max(1, Math.round(CACHE_TTL / 60000))}</ttl>`,
    '<generator>rss-aggregator-node</generator>',
  ];
  for (const it of cache.items) {
    lines.push('<item>');
    lines.push(`<title>${esc(it.title || '(无标题)')}</title>`);
    if (it.link) lines.push(`<link>${esc(it.link)}</link>`);
    if (it.description) lines.push(`<description>${esc(it.description.slice(0, 500))}</description>`);
    if (it.dateRaw) lines.push(`<pubDate>${esc(it.dateRaw)}</pubDate>`);
    if (it.src) lines.push(`<source url="${esc(it.src)}">${esc(it.srcName || 'RSS')}</source>`);
    lines.push(`<guid isPermaLink="true">${esc(it.link || `${self}#${it.ts}`)}</guid>`);
    lines.push('</item>');
  }
  lines.push('</channel></rss>');
  return lines.join('\n');
}

// 生成 feed 正文 + ETag：内容未变则沿用旧 ETag，客户端可长期 304
function feedPayload(host) {
  const body = buildRss(host);
  if (body !== cache.feedBody) {
    cache.feedBody = body;
    cache.feedEtag = '"' + crypto.createHash('md5').update(body).digest('hex') + '"';
    cache.feedGzip = null;
    try { cache.feedGzip = zlib.gzipSync(Buffer.from(body, 'utf8')); } catch (_) {}
  }
  return { body, etag: cache.feedEtag, gzip: cache.feedGzip };
}

const htmlPage = host => {
  const next = cache.builtAt ? new Date(cache.builtAt + CACHE_TTL) : null;
  const rows = sourceStatus().map(s => `<tr><td>${esc(s.name || s.url.replace(/^https?:\/\//, '').slice(0, 42))}</td>
<td class="${s.ok ? 'ok' : 'bad'}">${s.ok ? '✓ 正常' : '✗ 失败'}</td>
<td>${esc(s.error || (s.ok ? `最近成功 ${s.lastOk ? new Date(s.lastOk).toLocaleTimeString('zh-CN') : ''}` : '等待首次抓取'))}</td></tr>`).join('');

  return `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RSS 聚合头条</title>
<style>
body{font-family:system-ui,sans-serif;max-width:760px;margin:48px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}
code{background:#f2f2f2;padding:4px 10px;border-radius:6px;display:block;word-break:break-all;user-select:all}
h1{font-size:1.5rem} h2{font-size:1.05rem;margin-top:2rem}
.muted{color:#777;font-size:.9rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}
td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left;vertical-align:top}
.ok{color:#0a7d32;white-space:nowrap}.bad{color:#c0392b;white-space:nowrap}
.dash{display:flex;gap:24px;flex-wrap:wrap;margin:16px 0}
.dash div{background:#fafafa;border:1px solid #eee;border-radius:8px;padding:10px 14px;min-width:120px}
.dash b{display:block;font-size:1.25rem}
</style></head><body>
<h1>📡 RSS 聚合头条</h1>
<p>服务端定时抓取多个 RSS 源的最新头条，合并去重后按发布时间倒序输出。将下面的地址添加到任意 RSS 阅读器订阅：</p>
<code>https://${host}/feed.xml</code>
<div class="dash">
  <div><b>${cache.items.length}</b>当前条目</div>
  <div><b>${cache.stats.ok}/${cache.stats.total}</b>源正常</div>
  <div><b>${Math.round(CACHE_TTL / 1000)}s</b>刷新周期</div>
  <div><b>${cache.stats.ms}</b>上轮耗时(ms)</div>
</div>
<p class="muted">上次刷新：${cache.builtAt ? new Date(cache.builtAt).toLocaleString('zh-CN') : '进行中…'}
${next ? ` · 下次自动刷新：${next.toLocaleTimeString('zh-CN')}` : ''}
 · 数据完全由服务端自动更新，无需任何本地设备参与</p>
<h2>各源状态</h2>
<table><tr><th>源</th><th>状态</th><th>说明</th></tr>${rows}</table>
</body></html>`;
};

// ---------------- HTTP 服务 ----------------
// 首轮抓取未完成时不直接返回 503：在超时前等待，避免客户端首次订阅空白
async function waitForFirstBuild(maxMs = 30000) {
  const deadline = Date.now() + maxMs;
  while (!cache.builtAt && Date.now() < deadline) {
    if (!cache.refreshing) refresh().catch(() => {});
    await new Promise(r => setTimeout(r, 400));
  }
  return !!cache.builtAt;
}

const server = http.createServer(async (req, res) => {
  const host = safeHost(req.headers.host || `localhost:${PORT}`);
  const urlPath = (req.url || '/').split('?')[0];
  const isHead = req.method === 'HEAD';

  // 兜底：定时器失效或长时间无请求时，请求也会触发刷新
  if (Date.now() - cache.builtAt > CACHE_TTL * 2 && !cache.refreshing) {
    refresh().catch(() => {});
  }

  if (urlPath === '/feed.xml' || urlPath === '/rss.xml') {
    if (!cache.builtAt) {
      await waitForFirstBuild();          // 首次启动：尽量等首轮抓取完成
    }
    if (!cache.builtAt) {
      res.writeHead(503, {
        'Content-Type': 'application/rss+xml; charset=utf-8',
        'Retry-After': '15',
      });
      res.end('<?xml version="1.0"?><rss version="2.0"><channel><title>首次抓取中，请稍后重试</title></channel></rss>');
      return;
    }
    const { body, etag, gzip } = feedPayload(host);
    const base = {
      // 缓存窗口小于刷新周期，避免"服务端已更新但客户端还在吃旧缓存"
      'Cache-Control': 'public, max-age=20, stale-while-revalidate=40',
      ETag: etag,
      'X-Sources': `${cache.stats.ok}/${cache.stats.total}`,
      'X-Items': String(cache.items.length),
    };
    // 客户端带 If-None-Match 且内容未变 → 304，几乎零流量
    if (req.headers['if-none-match'] === etag) {
      res.writeHead(304, base);
      res.end();
      return;
    }
    // 支持 gzip（默认已缓存好，几乎无额外开销）
    const useGzip = gzip && /\bgzip\b/.test(req.headers['accept-encoding'] || '');
    const payload = useGzip ? gzip : body;
    res.writeHead(200, {
      ...base,
      'Content-Type': 'application/rss+xml; charset=utf-8',
      'Content-Length': Buffer.byteLength(payload),
      ...(useGzip ? { 'Content-Encoding': 'gzip', Vary: 'Accept-Encoding' } : {}),
    });
    res.end(isHead ? undefined : payload);
    return;
  }

  // 自检接口（供监控/探活；注意 /healthz 被托管平台占用）
  if (urlPath === '/status.json') {
    const json = Buffer.from(JSON.stringify({
      ok: !!cache.builtAt,
      items: cache.items.length,
      sources: cache.stats,
      lastBuiltAt: cache.builtAt ? new Date(cache.builtAt).toISOString() : null,
      refreshing: cache.refreshing,
    }), 'utf8');
    res.writeHead(cache.builtAt ? 200 : 503, {
      'Content-Type': 'application/json; charset=utf-8',
      'Content-Length': json.length,
      'Cache-Control': 'no-store',
    });
    res.end(isHead ? undefined : json);
    return;
  }

  // 首页 / 其他路径
  const page = Buffer.from(htmlPage(host), 'utf8');
  res.writeHead(200, {
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Length': page.length,
    'X-Content-Type-Options': 'nosniff',
    'Cache-Control': 'no-cache',
  });
  res.end(isHead ? undefined : page);
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`RSS 聚合服务已启动，端口 ${PORT}，刷新周期 ${CACHE_TTL / 1000}s，并发 ${CONCURRENCY}`);
  // 后台定时刷新：不依赖任何请求，保证订阅随时是最新内容。
  // 采用"上一轮结束后再等剩余时间"的自适应排程，而非固定 setInterval，
  // 这样单轮抓取耗时接近（甚至偶尔超过）周期时不会整轮被跳过。
  const loop = async () => {
    const startedAt = Date.now();
    await refresh().catch(e => console.error('刷新失败:', e.message));
    const delay = Math.max(5000, CACHE_TTL - (Date.now() - startedAt));
    setTimeout(loop, delay).unref();
  };
  loop();
});
