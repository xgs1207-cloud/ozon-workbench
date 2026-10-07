/* User-started, bounded Seerfar category -> keyword collection.
 * No Seerfar credentials or API calls: filter the normal page, then read DOM.
 * Checkpoints live in this browser only. HTTP uploads are idempotent.
 */
let marketJobRunning = false;
const MARKET_JOB_KEY = 'seerfarMarketJob';
const MARKET_STOP_KEY = 'seerfarMarketStop';
const MARKET_JOB_ALARM = 'seerfar-market-job';

function marketRecordsSignature(records) {
    // Chrome storage and message IPC may serialize object fields in different
    // orders. Property order must not be mistaken for a changed report page.
    const canonical = value => Array.isArray(value) ? value.map(canonical)
        : value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]))
        : value;
    return JSON.stringify(canonical(records || []));
}

function marketCategoryTargets(records) {
    const targets = new Map();
    for (const record of records) {
        const label = String(record['类目'] || '').replace(/\s+/g, ' ').trim();
        if (!label) continue;
        const urls = record['类目链接'] || [];
        let id = '';
        for (const value of urls) {
            try {
                const u = new URL(value);
                if (u.protocol === 'https:' && ['seerfar.cn', 'www.seerfar.cn'].includes(u.hostname)
                    && u.pathname === '/admin/category-detail') id = u.searchParams.get('categoryId') || '';
            } catch { /* An unrelated/malformed link is not a category identity. */ }
            if (id) break;
        }
        if (id && /^[\d_]+$/.test(id)) targets.set(id, { id, label });
    }
    return [...targets.values()];
}

function marketJobSummary(job) {
    if (!job) return null;
    const { status, phase, pages, received, inserted, targetIndex, targets, message, error,
        maxPages, keywordPages, maxCategories, skipped, startedAt } = job;
    return { status, phase, pages, received, inserted, targetIndex, categoryCount: targets.length,
        currentCategory: targets[targetIndex]?.label || '', message, error,
        maxPages, keywordPages, maxCategories, skipped, startedAt };
}

async function marketJobSave(job) {
    await chrome.storage.local.set({ [MARKET_JOB_KEY]: job });
}

async function marketJobRequest(path, payload, access) {
    const origin = new URL(access.origin);
    if (origin.protocol !== 'https:' && !(origin.protocol === 'http:'
        && ['127.0.0.1', 'localhost'].includes(origin.hostname)))
        throw new Error('市场数据和令牌不能通过公网 HTTP 传输');
    const stored = await chrome.storage.local.get(['marketIngestToken']);
    const token = String(stored.marketIngestToken || '');
    if (!token) throw new Error('请先验证并保存市场数据写入令牌');
    const headers = { 'X-Market-Ingest-Token': token, 'X-Factory-Device-Id': await ensureFactoryDeviceId() };
    if (access.authHeader) headers.Authorization = access.authHeader;
    if (payload) headers['Content-Type'] = 'application/json';
    try {
        const response = await fetch(access.origin + path, { method: payload ? 'POST' : 'GET',
            headers, ...(payload ? { body: JSON.stringify(payload) } : {}), signal: AbortSignal.timeout(25000) });
        const body = await response.json();
        if (!response.ok) throw new Error(typeof body.detail === 'string' ? body.detail : `工作台 HTTP ${response.status}`);
        return body;
    } catch (error) {
        throw new Error(String(error.message).split(token).join('[已隐藏令牌]'));
    }
}

// Runs in the page's MAIN world because Vue's category selection is not a
// native <select>. Inspect only this UI component's options/model, never auth,
// cookies, request clients or hidden product data. Query via the visible button.
async function selectMarketKeywordCategory(categoryId) {
    const pause = ms => new Promise(r => setTimeout(r, ms));
    const findSelector = () => {
        // Vue production does not attach __vueParentComponent to elements.
        // Follow mounted VNodes only to the category UI component; never read
        // app setup state, network clients, account data or auth properties.
        let found;
        const seen = new Set();
        const walk = (v, depth = 0) => {
            if (!v || typeof v !== 'object' || seen.has(v) || depth > 120 || found) return;
            seen.add(v);
            const c = v.component;
            if (c?.type?.__name === 'filter-category-cascader' && c.props?.options?.length) { found = c; return; }
            if (c) walk(c.subTree, depth + 1);
            if (Array.isArray(v.children)) v.children.forEach(child => walk(child, depth + 1));
        };
        walk(document.querySelector('#app')?._vnode);
        return found;
    };
    const deadline = Date.now() + 18000;
    let selector;
    while (Date.now() < deadline && !selector) {
        if (location.pathname.includes('login')) throw new Error('Seerfar 登录已失效，请在采集窗口重新登录');
        selector = findSelector();
        if (selector) break;
        for (const node of document.querySelectorAll('.el-cascader')) {
            if (!node.getClientRects().length) continue;
            for (let component = node.__vueParentComponent, depth = 0; component && depth < 12; component = component.parent, depth++) {
                if (component.type?.__name === 'filter-category-cascader' && component.props?.options?.length) {
                    selector = component;
                    break;
                }
            }
            if (selector) break;
        }
        if (!selector) await pause(300);
    }
    if (!selector) throw new Error('未找到已加载的关键词类目选择器；未查询全部市场');
    const find = nodes => {
        for (const node of nodes || []) {
            if (String(node.value) === categoryId) return node;
            const child = find(node.children);
            if (child) return child;
        }
        return null;
    };
    const option = find(selector.props.options);
    if (!option) throw new Error('关键词页没有此真实类目 ID；未使用名称猜测类目');
    selector.emit('update:modelValue', [option.value]);
    await pause(350);
    const selected = selector.props.modelValue;
    if (!Array.isArray(selected) || selected.length !== 1 || String(selected[0]) !== categoryId)
        throw new Error('类目筛选没有生效，已停止');
    const buttons = [...document.querySelectorAll('button')].filter(b => b.getClientRects().length
        && !b.disabled && b.textContent.trim() === '查询');
    if (buttons.length !== 1) throw new Error('无法唯一定位关键词查询按钮');
    buttons[0].click();
    return { categoryId, label: String(option.label || ''), selected: true };
}

async function marketJobTabMessage(tabId, message) {
    return sendSeerfarTabMessage(tabId, message);
}

async function marketJobStopped() {
    return Boolean((await chrome.storage.local.get([MARKET_STOP_KEY]))[MARKET_STOP_KEY]);
}

function verifyMarketKeywordCategory(categoryId) {
    let found;
    const seen = new Set();
    const walk = (v, depth = 0) => {
        if (!v || typeof v !== 'object' || seen.has(v) || depth > 120 || found) return;
        seen.add(v);
        const c = v.component;
        if (c?.type?.__name === 'filter-category-cascader') { found = c; return; }
        if (c) walk(c.subTree, depth + 1);
        if (Array.isArray(v.children)) v.children.forEach(child => walk(child, depth + 1));
    };
    walk(document.querySelector('#app')?._vnode);
    if (found) {
        const selected = found.props?.modelValue;
        return Array.isArray(selected) && selected.length === 1 && String(selected[0]) === categoryId;
    }
    for (const node of document.querySelectorAll('.el-cascader')) {
        if (!node.getClientRects().length) continue;
        for (let c = node.__vueParentComponent, depth = 0; c && depth < 12; c = c.parent, depth++) {
            if (c.type?.__name !== 'filter-category-cascader') continue;
            const selected = c.props?.modelValue;
            return Array.isArray(selected) && selected.length === 1 && String(selected[0]) === categoryId;
        }
    }
    return false;
}

async function prepareMarketKeywordTab(job) {
    const url = job.seerfarOrigin + '/admin/market';
    if (!job.keywordTab) {
        const tab = await chrome.tabs.create({ url: 'about:blank', active: false });
        job.keywordTab = tab.id;
        await marketJobSave(job);
        await new Promise(r => setTimeout(r, 300));
    }
    await chrome.tabs.update(job.keywordTab, { url });
    const end = Date.now() + 20000;
    while (Date.now() < end) {
        if (await marketJobStopped()) return null;
        const tab = await chrome.tabs.get(job.keywordTab);
        if (tab.status === 'complete' && /\/admin\/login/.test(tab.url || ''))
            throw new Error('Seerfar 登录已失效，请在采集窗口重新登录');
        if (tab.status === 'complete' && new URL(tab.url || 'about:blank').pathname === '/admin/market') break;
        await new Promise(r => setTimeout(r, 300));
    }
    const target = job.targets[job.targetIndex];
    const [result] = await chrome.scripting.executeScript({ target: { tabId: job.keywordTab },
        world: 'MAIN', func: selectMarketKeywordCategory, args: [target.id] });
    if (result?.error) throw new Error(result.error.message || '类目选择器执行失败');
    if (!result?.result?.selected || result.result.categoryId !== target.id)
        throw new Error('类目关键词筛选未核验，已停止');
    const snapshot = await marketJobTabMessage(job.keywordTab, { type: 'SEERFAR_MARKET_WAIT', dataset: 'keywords' });
    if (!snapshot?.records?.length) throw new Error(snapshot?.reason || '此类目未返回可采集关键词');
    return snapshot;
}

async function runMarketJob() {
    if (marketJobRunning) return;
    marketJobRunning = true;
    let job;
    try {
        job = (await chrome.storage.local.get([MARKET_JOB_KEY]))[MARKET_JOB_KEY];
        if (!job || job.status !== 'running') return;
        const access = await loadFactoryAccess();
        if (access.origin !== job.workbenchOrigin) throw new Error('工作台地址已改变，请恢复原地址再继续');
        while (job.status === 'running') {
            if (await marketJobStopped()) { job.status = 'stopped'; job.message = '已停止，已入库数据保留'; break; }
            if (job.phase === 'prepareKeywords') {
                if (job.targetIndex >= job.targets.length) { job.status = 'done'; job.message = '类目及对应关键词采集完成'; break; }
                job.message = `正在查询类目 ${job.targetIndex + 1}/${job.targets.length} 的关键词`;
                await marketJobSave(job);
                job.snapshot = await prepareMarketKeywordTab(job);
                if (!job.snapshot) continue;
                job.phase = 'keywords'; job.page = 0; job.lastSignature = ''; job.needNext = false;
                await marketJobSave(job);
            }
            const tabId = job.phase === 'categories' ? job.sourceTab : job.keywordTab;
            if (job.phase === 'keywords') {
                const [check] = await chrome.scripting.executeScript({ target: { tabId }, world: 'MAIN',
                    func: verifyMarketKeywordCategory, args: [job.targets[job.targetIndex].id] });
                if (check?.result !== true) throw new Error('关键词页的类目筛选已改变；已停止避免错关联');
            }
            if (job.needNext) {
                const current = await marketJobTabMessage(tabId, { type: 'SEERFAR_MARKET_CAPTURE' });
                const signature = marketRecordsSignature(current.records);
                if (signature === job.lastSignature) {
                    const next = await marketJobTabMessage(tabId, { type: 'SEERFAR_MARKET_NEXT_PAGE' });
                    if (next?.done) { await finishMarketJobSection(job); continue; }
                    if (!next?.advanced) throw new Error(next?.error || next?.reason || '翻页未成功');
                    job.snapshot = next.snapshot;
                } else if (current.records?.length) {
                    // Worker restarted after a successful click: capture the
                    // already-advanced page, never click an extra page.
                    job.snapshot = current;
                } else throw new Error(current.reason || '翻页后报表为空');
                job.needNext = false;
                await marketJobSave(job);
            }
            const snapshot = job.snapshot || await marketJobTabMessage(tabId, { type: 'SEERFAR_MARKET_CAPTURE' });
            const expected = job.phase === 'categories' ? 'categories' : 'keywords';
            if (snapshot.dataset !== expected || !snapshot.records?.length) throw new Error(snapshot.reason || '报表类型改变或没有有效数据');
            const signature = marketRecordsSignature(snapshot.records);
            if (signature === job.lastSignature) throw new Error('翻页数据重复，已停止避免循环');
            const now = new Date();
            const rollingMonth = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
            const payload = { ...snapshot, source: 'seerfar', capture_method: 'browser_extension',
                period: expected === 'keywords' ? rollingMonth : job.period,
                period_kind: expected === 'keywords' ? 'rolling_30d' : 'calendar_month' };
            if (expected === 'keywords') {
                payload.category_key = job.targets[job.targetIndex].label;
                payload.records = snapshot.records.map(row => ({ ...row,
                    '采集筛选类目': job.targets[job.targetIndex].label,
                    '采集筛选类目ID': job.targets[job.targetIndex].id }));
            }
            // One DOM page is <=200 rows. Split oversized pages by encoded size.
            let chunk = [], bytes = 0;
            const chunks = [];
            for (const row of payload.records) {
                const size = new TextEncoder().encode(JSON.stringify(row)).length + 1;
                if (size > 850000) throw new Error('单行数据过大，已停止');
                if (chunk.length && bytes + size > 850000) { chunks.push(chunk); chunk = []; bytes = 0; }
                chunk.push(row); bytes += size;
            }
            if (chunk.length) chunks.push(chunk);
            // Save the page before upload so interruption retries the same rows.
            job.snapshot = snapshot;
            await marketJobSave(job);
            let inserted = 0;
            for (const records of chunks) {
                if (await marketJobStopped()) break;
                const result = await marketJobRequest('/api/collector/market-snapshots', { ...payload, records }, access);
                inserted += Number(result.inserted || 0);
            }
            if (await marketJobStopped()) continue;
            job.pages++; job.page++; job.received += snapshot.records.length; job.inserted += inserted;
            job.lastSignature = signature; job.snapshot = null;
            if (expected === 'categories') {
                const found = marketCategoryTargets(snapshot.records);
                const known = new Set(job.targets.map(x => x.id));
                for (const target of found) if (!known.has(target.id) && job.targets.length < job.maxCategories) {
                    job.targets.push(target); known.add(target.id);
                }
                job.skipped += snapshot.records.filter(r => !marketCategoryTargets([r]).length).length;
            }
            job.message = `已采集 ${job.pages} 页，接收 ${job.received} 行，新入库 ${job.inserted} 行`;
            job.needNext = true;
            await marketJobSave(job);
            if (job.page >= (expected === 'categories' ? job.maxPages : job.keywordPages)) await finishMarketJobSection(job);
        }
    } catch (error) {
        if (job) { job.status = 'error'; job.error = String(error.message || '采集中断'); job.message = '已入库数据保留，可修正问题后继续'; }
    } finally {
        if (job) {
            await marketJobSave(job);
            if (job.status !== 'running') await chrome.alarms.clear(MARKET_JOB_ALARM);
        }
        marketJobRunning = false;
    }
}

async function finishMarketJobSection(job) {
    if (job.phase === 'categories') {
        if (!job.targets.length) throw new Error('类目已入库，但未找到真实类目跳转 ID，不能自动采词');
        job.phase = 'prepareKeywords';
    } else { job.targetIndex++; job.phase = 'prepareKeywords'; }
    job.needNext = false; job.snapshot = null; job.page = 0;
    await marketJobSave(job);
}

async function startMarketJob(message) {
    const old = (await chrome.storage.local.get([MARKET_JOB_KEY]))[MARKET_JOB_KEY];
    if (marketJobRunning || old?.status === 'running') throw new Error('已有采集任务运行中，请先停止');
    for (const key of ['maxPages', 'keywordPages', 'maxCategories'])
        if (!Number.isInteger(message[key]) || message[key] < 1 || message[key] > 20) throw new Error('页数和类目数须在 1–20 之间');
    if (!/^\d{4}-(0[1-9]|1[0-2])$/.test(message.period || '')) throw new Error('请填写类目报表实际自然月');
    const tab = await chrome.tabs.get(message.tabId);
    const u = new URL(tab.url);
    if (u.protocol !== 'https:' || !['seerfar.cn', 'www.seerfar.cn'].includes(u.hostname)
        || !/^\/admin\/category-search(?:\.html)?\/?$/.test(u.pathname)) throw new Error('请从 Seerfar 类目报表启动联动采集');
    const access = await loadFactoryAccess();
    await marketJobRequest('/api/market-data/stats', null, access);
    const snapshot = await marketJobTabMessage(tab.id, { type: 'SEERFAR_MARKET_CAPTURE' });
    if (snapshot.dataset !== 'categories' || !snapshot.records?.length) throw new Error(snapshot.reason || '请先查询出类目表格');
    const job = { status: 'running', phase: 'categories', sourceTab: tab.id, keywordTab: null,
        seerfarOrigin: u.origin, workbenchOrigin: access.origin, period: message.period,
        maxPages: message.maxPages, keywordPages: message.keywordPages, maxCategories: message.maxCategories,
        targets: [], targetIndex: 0, pages: 0, page: 0, received: 0, inserted: 0, skipped: 0,
        needNext: false, lastSignature: '', snapshot, startedAt: new Date().toISOString(), message: '开始采集类目', error: '' };
    await chrome.storage.local.set({ [MARKET_JOB_KEY]: job, [MARKET_STOP_KEY]: false });
    await chrome.alarms.create(MARKET_JOB_ALARM, { periodInMinutes: 1 });
    void runMarketJob();
    return marketJobSummary(job);
}

chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === MARKET_JOB_ALARM) void runMarketJob(); });
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!['SEERFAR_CHAIN_START', 'SEERFAR_CHAIN_STATUS', 'SEERFAR_CHAIN_STOP', 'SEERFAR_CHAIN_RESUME'].includes(message?.type)) return undefined;
    (async () => {
        try {
            let job = (await chrome.storage.local.get([MARKET_JOB_KEY]))[MARKET_JOB_KEY];
            if (message.type === 'SEERFAR_CHAIN_START') return sendResponse({ ok: true, job: await startMarketJob(message) });
            if (message.type === 'SEERFAR_CHAIN_STOP') {
                await chrome.storage.local.set({ [MARKET_STOP_KEY]: true });
                if (job && !marketJobRunning) { job.status = 'stopped'; await marketJobSave(job); }
            }
            if (message.type === 'SEERFAR_CHAIN_RESUME') {
                if (!job || !['stopped', 'error'].includes(job.status)) throw new Error('没有可继续的任务');
                if (marketJobRunning) throw new Error('请等待当前任务停止后再继续');
                job.status = 'running'; job.error = '';
                await chrome.storage.local.set({ [MARKET_STOP_KEY]: false });
                await marketJobSave(job);
                await chrome.alarms.create(MARKET_JOB_ALARM, { periodInMinutes: 1 });
                void runMarketJob();
            }
            sendResponse({ ok: true, job: marketJobSummary(job) });
        } catch (error) { sendResponse({ ok: false, error: error.message }); }
    })();
    return true;
});
