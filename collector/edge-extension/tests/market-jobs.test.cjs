const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../market-jobs.js'), 'utf8');

function harness({ origin = 'http://127.0.0.1:8766', failUpload = false, stopUpload = false, missingReceiver = false } = {}) {
    const stored = { marketIngestToken: 'qa-private-token' }, requests = [], listeners = [], nexts = [];
    const category = (id) => ({ '类目': `类目${id}\nкатегория`, '销量': '100', '销售额': '10000',
        '类目链接': [`https://www.seerfar.cn/admin/category-detail?categoryId=${id}&platform=OZON`] });
    let page = 0, keywordPage = 0, selected = '';
    let receiverReady = !missingReceiver, injections = 0;
    const snapshot = (dataset, records, route) => ({ dataset, records,
        page_url: `https://www.seerfar.cn/admin/${route}`, captured_at: '2026-10-07T00:00:00Z' });
    const cat = () => snapshot('categories', [category(page ? '22' : '11')], 'category-search');
    const key = () => snapshot('keywords', [{ '关键词': `слово-${selected}-${keywordPage}`, '月搜热度': '1000' }], 'market');
    const context = vm.createContext({ URL, TextEncoder, AbortSignal, setTimeout: fn => setTimeout(fn, 0),
        ensureFactoryDeviceId: async () => 'qa-device',
        loadFactoryAccess: async () => ({ origin, authHeader: null }),
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (options.method === 'POST') {
                if (failUpload) throw new Error('network qa-private-token');
                if (stopUpload) stored.seerfarMarketStop = true;
            }
            return { ok: true, json: async () => ({ ok: true, inserted: options.method === 'POST' ? 1 : 0 }) };
        },
        chrome: {
            storage: { local: {
                // Match Chrome's possible property reordering across storage.
                get: async keys => Object.fromEntries(keys.map(k => [k, stored[k] === undefined ? undefined :
                    JSON.parse(JSON.stringify(stored[k], (key, value) => value && typeof value === 'object' && !Array.isArray(value)
                        ? Object.fromEntries(Object.keys(value).sort().map(k => [k, value[k]])) : value))])),
                set: async value => Object.assign(stored, structuredClone(value)),
            } },
            alarms: { create: async () => {}, clear: async () => {}, onAlarm: { addListener: () => {} } },
            runtime: { onMessage: { addListener: fn => listeners.push(fn) } },
            tabs: {
                get: async id => ({ id, status: 'complete', url: `https://www.seerfar.cn/admin/${id === 1 ? 'category-search' : 'market'}` }),
                create: async () => { keywordPage = 0; return { id: 2 }; },
                update: async () => { keywordPage = 0; },
                sendMessage: async (id, msg) => {
                    if (msg.type === 'SEERFAR_MARKET_PING') {
                        if (!receiverReady) throw new Error('Could not establish connection. Receiving end does not exist.');
                        return { ready: true, version: '0.4.31' };
                    }
                    if (msg.type === 'SEERFAR_MARKET_NEXT_PAGE') {
                        nexts.push(id);
                        if (id === 1) page++; else keywordPage++;
                        return { advanced: true, snapshot: id === 1 ? cat() : key() };
                    }
                    return id === 1 ? cat() : key();
                },
            },
            scripting: { executeScript: async ({ func, args, files }) => {
                if (files) { injections++; receiverReady = true; return [{}]; }
                if (func.name === 'selectMarketKeywordCategory') { selected = args[0]; return [{ result: { selected: true, categoryId: selected } }]; }
                return [{ result: true }];
            } },
        },
    });
    vm.runInContext(fs.readFileSync(path.join(__dirname, '../seerfar-bridge.js'), 'utf8'), context);
    vm.runInContext(source, context);
    async function settle() {
        for (let i = 0; i < 100; i++) {
            await new Promise(r => setTimeout(r, 2));
            if (stored.seerfarMarketJob?.status !== 'running') return stored.seerfarMarketJob;
        }
        throw new Error('job did not settle');
    }
    return { context, stored, requests, nexts, settle, injections: () => injections,
        start: () => vm.runInContext(`startMarketJob({tabId:1,period:'2026-09',maxPages:2,keywordPages:2,maxCategories:2})`, context),
        run: () => vm.runInContext('runMarketJob()', context),
        advanceSource: () => { page = 1; },
    };
}

test('category identities come only from real Seerfar links, never labels or foreign hosts', () => {
    const h = harness();
    const result = vm.runInContext(`marketCategoryTargets([
        {'类目':'床单 простыня','类目链接':['https://www.seerfar.cn/admin/category-detail?categoryId=12_34']},
        {'类目':'床单 простыня','类目链接':['https://www.seerfar.cn/admin/category-detail?categoryId=12_34']},
        {'类目':'床单','类目链接':['https://evil.test/admin/category-detail?categoryId=99']},
        {'类目':'枕头'}])`, h.context);
    assert.equal(result.length, 1);
    assert.equal(result[0].id, '12_34');
});

test('production Vue VNode category selection works without DOM development hooks', async () => {
    const h = harness();
    const component = { type: { __name: 'filter-category-cascader' },
        props: { options: [{ value: '12_34', label: '床单' }], modelValue: [] },
        emit(event, value) { assert.equal(event, 'update:modelValue'); this.props.modelValue = value; } };
    let queries = 0;
    h.context.location = { pathname: '/admin/market' };
    h.context.document = {
        querySelector: () => ({ _vnode: { component: { subTree: { children: [{ component }] } } } }),
        querySelectorAll: selector => selector === 'button'
            ? [{ textContent: '查询', getClientRects: () => [1], click: () => queries++ }] : [],
    };
    const result = await vm.runInContext("selectMarketKeywordCategory('12_34')", h.context);
    assert.equal(result.selected, true);
    assert.equal(queries, 1);
    assert.equal(vm.runInContext("verifyMarketKeywordCategory('12_34')", h.context), true);
    assert.equal(vm.runInContext("verifyMarketKeywordCategory('99')", h.context), false);
    await assert.rejects(vm.runInContext("selectMarketKeywordCategory('99')", h.context), /真实类目 ID/);
    assert.equal(queries, 1);
});

test('background collection captures bounded category pages then each category keyword page', async () => {
    const h = harness();
    await h.start();
    const job = await h.settle();
    assert.equal(job.status, 'done');
    assert.equal(job.pages, 6);
    assert.equal(job.targets.length, 2);
    assert.equal(h.nexts.filter(id => id === 1).length, 1);
    assert.equal(h.nexts.filter(id => id === 2).length, 2);
    const bodies = h.requests.filter(x => x.options.method === 'POST').map(x => JSON.parse(x.options.body));
    assert.deepEqual(bodies.map(x => x.dataset), ['categories', 'categories', 'keywords', 'keywords', 'keywords', 'keywords']);
    assert.equal(bodies[0].period_kind, 'calendar_month');
    assert.equal(bodies[2].period_kind, 'rolling_30d');
    assert.equal(bodies[2].category_key, '类目11 категория');
    assert.equal(bodies[2].records[0]['采集筛选类目ID'], '11');
    assert.doesNotMatch(JSON.stringify(job), /qa-private-token/);
});

test('background refuses public HTTP before sending any token', async () => {
    const h = harness({ origin: 'http://43.132.190.110:8088' });
    await assert.rejects(h.start(), /公网 HTTP/);
    assert.equal(h.requests.length, 0);
});

test('background reconnects a missing category receiver then finishes the keyword chain', async () => {
    const h = harness({ missingReceiver: true });
    await h.start();
    const job = await h.settle();
    assert.equal(job.status, 'done');
    assert.equal(job.pages, 6);
    assert.equal(h.injections(), 1);
    assert.equal(h.nexts.length, 3);
});

test('stop during upload prevents subsequent page clicks and preserves a checkpoint', async () => {
    const h = harness({ stopUpload: true });
    await h.start();
    const job = await h.settle();
    assert.equal(job.status, 'stopped');
    assert.equal(h.nexts.length, 0);
    assert.equal(job.snapshot.dataset, 'categories');
});

test('upload failure retains the page for safe retry and never stores the token in error', async () => {
    const h = harness({ failUpload: true });
    await h.start();
    const job = await h.settle();
    assert.equal(job.status, 'error');
    assert.equal(job.snapshot.records.length, 1);
    assert.doesNotMatch(job.error, /qa-private-token/);
    assert.equal(h.nexts.length, 0);
});

test('worker recovery after a page click captures the advanced page without clicking again', async () => {
    const h = harness();
    await h.start(); await h.settle();
    const job = h.stored.seerfarMarketJob;
    const first = { '类目': '类目11\nкатегория', '销量': '100', '销售额': '10000',
        '类目链接': ['https://www.seerfar.cn/admin/category-detail?categoryId=11&platform=OZON'] };
    Object.assign(job, { status: 'running', phase: 'categories', targets: [{ id: '11', label: '类目11 категория' }],
        targetIndex: 0, page: 1, pages: 1, received: 1, inserted: 1, snapshot: null,
        needNext: true, lastSignature: vm.runInContext(`marketRecordsSignature(${JSON.stringify([first])})`, h.context) });
    const sourceClicks = h.nexts.filter(id => id === 1).length;
    h.advanceSource();
    await h.run();
    assert.equal(h.stored.seerfarMarketJob.status, 'done');
    assert.equal(h.nexts.filter(id => id === 1).length, sourceClicks);
});
