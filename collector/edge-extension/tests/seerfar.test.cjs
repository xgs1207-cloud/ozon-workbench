const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const extensionRoot = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(extensionRoot, name), 'utf8');

function settingsContext(name, cutoff, chrome = {}) {
    const source = read(name);
    const context = vm.createContext({ URL, TextEncoder, btoa, chrome });
    vm.runInContext(source.slice(0, source.indexOf(cutoff)), context, { filename: name });
    return context;
}

test('the SSH tunnel address remains configured in popup and background', () => {
    for (const [name, cutoff] of [
        ['popup.js', 'const els ='],
        ['background.js', '// 返回 { origin, authHeader }'],
    ]) {
        const context = settingsContext(name, cutoff);
        assert.equal(vm.runInContext('factoryUrlOrDefault("http://127.0.0.1:8766")', context),
            'http://127.0.0.1:8766');
        assert.equal(vm.runInContext('factoryUrlOrDefault("http://localhost:8766")', context),
            'http://localhost:8766');
        assert.equal(vm.runInContext('factoryUrlOrDefault("http://127.0.0.1:8765")', context),
            'http://43.132.190.110:8088');
    }
});

test('HTTPS workbench permission is requested for only the chosen host', async () => {
    const requested = [];
    const context = settingsContext('popup.js', 'const els =', {
        permissions: {
            contains: async () => false,
            request: async ({ origins }) => { requested.push(...origins); return true; },
        },
    });
    await vm.runInContext('ensureWorkbenchHostPermission("https://workbench.example:8443")', context);
    assert.deepEqual(requested, ['https://workbench.example/*']);
    await vm.runInContext('ensureWorkbenchHostPermission("http://127.0.0.1:8766")', context);
    assert.equal(requested.length, 1);
});

test('market token cannot be sent to a public HTTP host', () => {
    const context = settingsContext('popup.js', 'const els =');
    for (const origin of ['http://43.132.190.110:8088', 'http://127.0.0.1.attacker.example']) {
        assert.equal(vm.runInContext(`isSafeMarketDestination(${JSON.stringify(origin)})`, context), false);
    }
    for (const origin of ['http://127.0.0.1:8766', 'http://localhost:8767', 'https://workbench.example']) {
        assert.equal(vm.runInContext(`isSafeMarketDestination(${JSON.stringify(origin)})`, context), true);
    }
});

test('only the market ranking page is marked as rolling 30 days', () => {
    const context = settingsContext('popup.js', 'const els =');
    assert.equal(vm.runInContext('isRollingMarketPage("https://www.seerfar.cn/admin/market")', context), true);
    assert.equal(vm.runInContext('isRollingMarketPage("https://seerfar.cn/admin/market.html?sort=hot")', context), true);
    assert.equal(vm.runInContext('isRollingMarketPage("https://www.seerfar.cn/admin/category")', context), false);
    assert.equal(vm.runInContext('isRollingMarketPage("https://attacker.example/admin/market")', context), false);
    assert.equal(vm.runInContext('marketCaptureMonth({getFullYear:()=>2026,getMonth:()=>0})', context), '2026-01');
});

function cell(text, rendered = text, assets = {}) {
    return {
        textContent: text, innerText: rendered,
        querySelectorAll: (selector) => selector === 'a[href]' ? (assets.links || [])
            : selector === 'img[src]' ? (assets.images || []) : [],
    };
}

function row(cells, visible = true) {
    return {
        textContent: cells.map((item) => item.textContent).join(' '),
        getClientRects: () => visible ? [{}] : [],
        querySelectorAll: (selector) => selector === 'td' || selector === 'th,td' ? cells : [],
    };
}

function captureContext(headers, bodyRows) {
    const rows = [row(headers.map((item) => cell(item))), ...bodyRows];
    const table = { querySelectorAll: () => rows };
    const context = vm.createContext({
        document: { querySelectorAll: () => [table] },
        location: { origin: 'https://seerfar.cn', pathname: '/admin/market.html' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: { setInterval: () => { throw new Error('unexpected automatic polling'); } },
    });
    vm.runInContext(read('seerfar-content.js'), context, { filename: 'seerfar-content.js' });
    return vm.runInContext('captureVisibleMarketTable()', context);
}

test('manual keyword capture keeps Russian and Chinese on separate lines', () => {
    const result = captureContext(['关键词', '月搜热度', '竞品数'], [
        row([cell('простыня床单', 'простыня\n床单'), cell('1 200'), cell('80')]),
    ]);
    assert.equal(result.dataset, 'keywords');
    assert.equal(result.records[0]['关键词'], 'простыня\n床单');
    assert.equal(result.records[0]['月搜热度'], '1 200');
    assert.equal(result.page_url, 'https://seerfar.cn/admin/market.html');
});

test('capture reads visible category rows only and caps one page at 200', () => {
    const body = Array.from({ length: 202 }, (_, index) => row([
        cell(`类目${index}`), cell('1000'), cell('100'),
    ], index !== 0));
    const result = captureContext(['类目', '销售额', '销量'], body);
    assert.equal(result.dataset, 'categories');
    assert.equal(result.records.length, 200);
    assert.equal(result.records[0]['类目'], '类目1');
});

test('a report with SKU plus category and revenue remains a product dataset', () => {
    const result = captureContext(['SKU', '类目', '销售额', '销量'], [
        row([cell('123456'), cell('床品'), cell('5000'), cell('20')]),
    ]);
    assert.equal(result.dataset, 'products');
    assert.equal(result.records[0].SKU, '123456');
});

test('product report preserves visible image and detail link in any column', () => {
    const result = captureContext(['SKU', '商品', '销量'], [
        row([cell('123456'), cell('保温杯', '保温杯', {
            links: [{ href: 'https://www.ozon.ru/product/123456' }],
            images: [{ src: 'https://cdn.example/cup.jpg' }],
        }), cell('80')]),
    ]);
    assert.equal(result.dataset, 'products');
    assert.equal(result.records[0]['商品'], '保温杯');
    assert.deepEqual(Array.from(result.records[0]['商品链接']), ['https://www.ozon.ru/product/123456']);
    assert.deepEqual(Array.from(result.records[0]['商品图片']), ['https://cdn.example/cup.jpg']);
});

test('unrecognized tables are rejected instead of guessed into the database', () => {
    const result = captureContext(['名称', '流量', '数量'], [
        row([cell('unknown'), cell('1000'), cell('30')]),
    ]);
    assert.equal(result.records.length, 0);
    assert.match(result.reason, /未找到匹配表格/);
});

test('Element Plus split header/body captures the market ranking and all visible columns', () => {
    const headers = ['选择', '排名', '关键词', '类目', '关键词相关商品（前10条）', '平均价格', '销量', '销售额', '月搜热度'];
    const gutter = cell('');
    gutter.className = 'gutter';
    const header = row([...headers.map((value) => cell(value)), gutter]);
    const productCell = cell('', '', {
        links: [{ href: 'https://www.ozon.ru/product/123' }],
        images: [{ src: 'https://cdn.example/product.jpg' }],
    });
    const data = row([
        cell(''), cell('1'), cell('постельное белье 2 спальное', 'постельное белье 2 спальное\n睡眠用床单2m'),
        cell('床品套件', '床品套件\nКомплект постельн'), productCell,
        cell('1 798₽'), cell('2,754'), cell('4 195 900₽'), cell('452,740'),
    ]);
    const fixedClone = row([
        cell(''), cell('1'), cell('错误的固定列克隆'), cell('床品套件'), productCell,
        cell('1 798₽'), cell('2,754'), cell('4 195 900₽'), cell('452,740'),
    ]);
    const widget = { querySelectorAll: (selector) => selector === '.el-table__body-wrapper tbody > tr'
        ? [data] : selector.includes('tr') ? [fixedClone, data] : [] };
    const headerTable = { querySelectorAll: (selector) => selector === 'tr' ? [header] : [], closest: () => widget };
    const bodyTable = { querySelectorAll: (selector) => selector === 'tr' ? [data] : [], closest: () => widget };
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector === 'table' ? [headerTable, bodyTable] : [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: {},
    });
    vm.runInContext(read('seerfar-content.js'), context);
    const result = vm.runInContext('captureVisibleMarketTable()', context);
    assert.equal(result.dataset, 'keywords');
    assert.equal(result.records.length, 1);
    assert.equal(Object.keys(result.records[0]).includes('col_9'), false);
    assert.equal(result.records[0]['关键词'], 'постельное белье 2 спальное\n睡眠用床单2m');
    assert.equal(result.records[0]['月搜热度'], '452,740');
    assert.equal(result.records[0]['销量'], '2,754');
    assert.deepEqual(Array.from(result.records[0]['关键词相关商品（前10条）链接']),
        ['https://www.ozon.ru/product/123']);
    assert.deepEqual(Array.from(result.records[0]['关键词相关商品（前10条）图片']),
        ['https://cdn.example/product.jpg']);
    assert.equal(result.page_url, 'https://www.seerfar.cn/admin/market');
});

test('Element Plus next page waits for changed records and stops when disabled', async () => {
    let page = 0;
    let clicks = 0;
    let polls = 0;
    const header = row(['排名', '关键词', '月搜热度'].map((value) => cell(value)));
    const body = [
        row([cell('1'), cell('плед'), cell('1000')]),
        row([cell('2'), cell('подушка'), cell('900')]),
    ];
    const table = { querySelectorAll: (selector) => selector === 'tr' ? [header, body[page]] : [], closest: () => null };
    const next = {
        disabled: false,
        getClientRects: () => [{}],
        click: () => { clicks += 1; next.disabled = true; },
        getAttribute: () => null,
    };
    const pager = { getClientRects: () => [{}], querySelectorAll: (selector) => selector.includes('btn-next') ? [next]
        : selector.includes('is-active') ? [{ textContent: String(clicks + 1) }] : [] };
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector === 'table' ? [table]
            : selector.includes('.el-pagination') ? [pager] : [],
        querySelector: (selector) => selector.includes('.el-pagination') ? pager : null },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: { setTimeout: (callback) => setImmediate(() => {
            polls += 1;
            if (polls >= 2)
                page = 1;
            callback();
        }) },
        Date,
    });
    vm.runInContext(read('seerfar-content.js'), context);
    const first = await vm.runInContext('advanceMarketPage()', context);
    assert.equal(first.advanced, true);
    assert.equal(first.snapshot.records[0]['关键词'], 'подушка');
    assert.equal(clicks, 1);
    assert.ok(polls >= 3, 'the changed page was read twice after the data settled');
    const end = await vm.runInContext('advanceMarketPage()', context);
    assert.equal(end.done, true);
    assert.equal(clicks, 1);
});

test('a stop message cannot be cleared by a racing next-page request', () => {
    let listener;
    const context = vm.createContext({
        document: { querySelectorAll: () => [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: (callback) => { listener = callback; } } } },
        window: {},
    });
    vm.runInContext(read('seerfar-content.js'), context);
    listener({ type: 'SEERFAR_MARKET_STOP' }, {}, () => {});
    let response;
    listener({ type: 'SEERFAR_MARKET_NEXT_PAGE' }, {}, (value) => { response = value; });
    assert.equal(response.stopped, true);
});

test('with two pagers, only the one next to the captured market table is clicked', async () => {
    let current = 0;
    let wrongClicks = 0;
    const header = row(['排名', '关键词', '月搜热度'].map((value) => cell(value)));
    const bodies = [row([cell('1'), cell('плед'), cell('1000')]),
        row([cell('2'), cell('ковер'), cell('900')])];
    const widget = { parentElement: null, getBoundingClientRect: () => ({ top: 100, bottom: 200, left: 0, right: 600 }) };
    const table = { querySelectorAll: (selector) => selector === 'tr' ? [header, bodies[current]] : [],
        closest: () => widget };
    const makePager = (top, onClick) => ({
        getClientRects: () => [{}],
        getBoundingClientRect: () => ({ top, bottom: top + 30, left: 0, right: 600 }),
        querySelectorAll: (selector) => selector.includes('btn-next')
            ? [{ getClientRects: () => [{}], disabled: false, click: onClick }] : [],
    });
    const near = makePager(230, () => { current = 1; });
    const far = makePager(800, () => { wrongClicks += 1; });
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector === 'table' ? [table]
            : selector.includes('.el-pagination') ? [near, far] : [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: { setTimeout: (callback) => setImmediate(callback) },
        Date,
    });
    vm.runInContext(read('seerfar-content.js'), context);
    const result = await vm.runInContext('advanceMarketPage()', context);
    assert.equal(result.advanced, true);
    assert.equal(result.snapshot.records[0]['关键词'], 'ковер');
    assert.equal(wrongClicks, 0);
});

test('market ranking uses its dedicated pagination wrapper before other pagers', () => {
    const rankingPager = { getClientRects: () => [{}], querySelectorAll: () => [] };
    const otherPager = { getClientRects: () => [{}], querySelectorAll: () => [] };
    const wrapper = { getClientRects: () => [{}], querySelector: () => rankingPager };
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector === '[data-ranking-pagination]' ? [wrapper]
            : selector.includes('.el-pagination') ? [otherPager, rankingPager] : [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: {},
    });
    vm.runInContext(read('seerfar-content.js'), context);
    assert.equal(vm.runInContext('marketPager()', context), rankingPager);
});

test('custom pagination wrapper is selected instead of its total and size children', () => {
    const total = { getClientRects: () => [{}] };
    const sizes = { getClientRects: () => [{}] };
    const wrapper = { getClientRects: () => [{}], contains: (node) => node === total || node === sizes };
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector.includes('.el-pagination') ? []
            : selector.includes("[class*='pagination']") ? [wrapper, total, sizes] : [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/category' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: {},
    });
    vm.runInContext(read('seerfar-content.js'), context);
    assert.equal(vm.runInContext('marketPager()', context), wrapper);
});

test('multiple market ranking pagination wrappers bind to the captured report table', async () => {
    let current = 0;
    let wrongClicks = 0;
    let rightClicks = 0;
    const header = row(['排名', '关键词', '月搜热度'].map((value) => cell(value)));
    const body = [row([cell('1'), cell('плед'), cell('1000')]),
        row([cell('21'), cell('ковер'), cell('900')])];
    const makePager = (click) => ({
        getClientRects: () => [{}],
        querySelectorAll: (selector) => selector.includes('btn-next')
            ? [{ getClientRects: () => [{}], disabled: false, click }] : [],
    });
    const ownPager = makePager(() => { rightClicks += 1; current = 1; });
    const otherPager = makePager(() => { wrongClicks += 1; });
    const reportSection = { parentElement: null, contains: (node) => node === ownPager };
    const widget = { parentElement: reportSection, contains: () => false };
    const table = { querySelectorAll: (selector) => selector === 'tr' ? [header, body[current]] : [],
        closest: () => widget };
    const wrapper = (pager) => ({ getClientRects: () => [{}], querySelector: () => pager });
    const context = vm.createContext({
        document: { querySelectorAll: (selector) => selector === 'table' ? [table]
            : selector === '[data-ranking-pagination]' ? [wrapper(otherPager), wrapper(ownPager)] : [] },
        location: { origin: 'https://www.seerfar.cn', pathname: '/admin/market' },
        chrome: { runtime: { onMessage: { addListener: () => {} } } },
        window: { setTimeout: (callback) => setImmediate(callback) },
        Date,
    });
    vm.runInContext(read('seerfar-content.js'), context);
    const result = await vm.runInContext('advanceMarketPage()', context);
    assert.equal(result.advanced, true);
    assert.equal(result.snapshot.records[0]['关键词'], 'ковер');
    assert.equal(rightClicks, 1);
    assert.equal(wrongClicks, 0);
});

test('popup deduplicates repeated rows and splits uploads below the size cap', () => {
    const context = settingsContext('popup.js', 'const els =');
    const result = vm.runInContext(`(() => {
        const seen = new Set();
        const first = uniqueMarketRecords([{关键词:'плед'}, {关键词:'плед'}, {关键词:'подушка'}], seen);
        const second = uniqueMarketRecords([{关键词:'плед'}, {关键词:'ковер'}], seen);
        const chunks = marketRecordChunks(first, 45);
        return {first:first.length, second:second.length, chunks:chunks.map((part) => part.length)};
    })()`, context);
    assert.deepEqual({ ...result, chunks: Array.from(result.chunks) }, { first: 2, second: 1, chunks: [1, 1] });
});

test('user-started popup capture uploads two pages and obeys the configured cap', async () => {
    const elements = new Map();
    const element = (id) => {
        if (!elements.has(id))
            elements.set(id, {
                value: '', textContent: '', hidden: false, disabled: false, handlers: {},
                addEventListener(name, callback) { this.handlers[name] = callback; },
            });
        return elements.get(id);
    };
    element('market-max-pages').value = '2';
    const stored = { factoryBaseUrl: 'http://127.0.0.1:8766', factoryDeviceId: 'test-device',
        marketIngestToken: 'test-token' };
    const snapshots = [
        { dataset: 'keywords', records: [{ 关键词: 'плед', 月搜热度: '1000' }],
            page_url: 'https://www.seerfar.cn/admin/market', captured_at: '2026-10-07T00:00:00Z' },
        { dataset: 'keywords', records: [{ 关键词: 'подушка', 月搜热度: '900' }],
            page_url: 'https://www.seerfar.cn/admin/market', captured_at: '2026-10-07T00:00:01Z' },
    ];
    const messages = [];
    const uploaded = [];
    const chrome = {
        storage: { local: {
            get: async (keys) => Object.fromEntries(keys.map((key) => [key, stored[key]])),
            set: async (values) => Object.assign(stored, values),
        } },
        tabs: {
            query: async () => [{ id: 1, url: 'https://www.seerfar.cn/admin/market' }],
            sendMessage: (_tabId, message, callback) => {
                messages.push(message.type);
                callback(message.type === 'SEERFAR_MARKET_CAPTURE' ? snapshots[0]
                    : message.type === 'SEERFAR_MARKET_NEXT_PAGE'
                        ? { advanced: true, snapshot: snapshots[1] } : { stopped: true });
            },
        },
        runtime: { lastError: null },
    };
    const context = vm.createContext({
        URL, TextEncoder, btoa, chrome,
        document: { getElementById: element },
        fetch: async (_url, options) => {
            if (!options.body)
                return { ok: true, json: async () => ({ ok: true }) };
            const body = JSON.parse(options.body);
            uploaded.push(body);
            return { ok: true, json: async () => ({ inserted: body.records.length }) };
        },
        window: { close: () => {} },
    });
    vm.runInContext(read('popup.js'), context);
    await new Promise((resolve) => setImmediate(resolve)); // initialize() fills the saved token
    await element('capture-market').handlers.click();
    assert.equal(uploaded.length, 2);
    assert.deepEqual(messages, ['SEERFAR_MARKET_CAPTURE', 'SEERFAR_MARKET_NEXT_PAGE']);
    assert.equal(uploaded[0].records[0]['关键词'], 'плед');
    assert.equal(uploaded[1].records[0]['关键词'], 'подушка');
    assert.equal(uploaded[0].period_kind, 'rolling_30d');
    assert.match(uploaded[0].period, /^\d{4}-\d{2}$/);
    assert.equal(element('market-natural-period').hidden, true);
    assert.equal(element('market-rolling-period').hidden, false);
    assert.match(element('progress').textContent, /采集结束：2 页，接收 2 行，新入库 2 行/);
    assert.equal(element('stop-market').disabled, true);
});

function popupTokenHarness({ baseUrl = 'http://127.0.0.1:8766', savedToken, verifyResponse,
    verifyError } = {}) {
    const elements = new Map();
    const element = (id) => {
        if (!elements.has(id))
            elements.set(id, {
                value: '', textContent: '', hidden: false, disabled: false, handlers: {},
                addEventListener(name, callback) { this.handlers[name] = callback; },
            });
        return elements.get(id);
    };
    const stored = { factoryBaseUrl: baseUrl, factoryDeviceId: 'test-device' };
    if (savedToken)
        stored.marketIngestToken = savedToken;
    const requests = [];
    const chrome = {
        storage: { local: {
            get: async (keys) => Object.fromEntries(keys.map((key) => [key, stored[key]])),
            set: async (values) => Object.assign(stored, values),
            remove: async (key) => { delete stored[key]; },
        } },
        tabs: {
            query: async () => [{ id: 1, url: 'https://www.seerfar.cn/admin/market' }],
            sendMessage: (_tabId, _message, callback) => callback({ records: [], reason: '未识别表格' }),
        },
        runtime: { lastError: null },
    };
    const context = vm.createContext({
        URL, TextEncoder, btoa, chrome, document: { getElementById: element },
        fetch: async (url, options) => {
            requests.push({ url, options });
            if (verifyError)
                throw verifyError;
            return verifyResponse || { ok: true, status: 200, json: async () => ({ ok: true }) };
        },
        window: { close: () => {} },
    });
    vm.runInContext(read('popup.js'), context);
    return { element, stored, requests };
}

test('market token can be verified, saved, restored and cleared independently of capture', async () => {
    const popup = popupTokenHarness();
    await new Promise((resolve) => setImmediate(resolve));
    popup.element('market-token').value = 'test-secret';
    await popup.element('save-market-token').handlers.click();
    assert.equal(popup.stored.marketIngestToken, 'test-secret');
    assert.equal(popup.requests.length, 1);
    assert.equal(popup.requests[0].url, 'http://127.0.0.1:8766/api/market-data/stats');
    assert.equal(popup.requests[0].options.headers['X-Market-Ingest-Token'], 'test-secret');
    assert.match(popup.element('market-token-status').textContent, /已验证并保存/);
    assert.doesNotMatch(popup.element('market-token-status').textContent, /test-secret/);
    await popup.element('clear-market-token').handlers.click();
    assert.equal(popup.stored.marketIngestToken, undefined);
    assert.equal(popup.stored.factoryBaseUrl, 'http://127.0.0.1:8766');
    assert.equal(popup.stored.factoryDeviceId, 'test-device');
    assert.equal(popup.element('market-token').value, '');
    const reopened = popupTokenHarness({ savedToken: 'previously-saved' });
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(reopened.element('market-token').value, 'previously-saved');
    assert.equal(reopened.requests.length, 0);
});

test('capture saves a verified token even when the Seerfar table cannot be parsed', async () => {
    const popup = popupTokenHarness();
    await new Promise((resolve) => setImmediate(resolve));
    popup.element('market-token').value = 'test-secret';
    popup.element('market-max-pages').value = '1';
    await popup.element('capture-market').handlers.click();
    assert.equal(popup.stored.marketIngestToken, 'test-secret');
    assert.equal(popup.requests.length, 1);
    assert.match(popup.element('result').textContent, /未识别表格/);
    assert.doesNotMatch(popup.element('result').textContent, /test-secret/);
});

test('invalid token, malformed response, and failed connection never save or echo token', async () => {
    for (const config of [
        { verifyResponse: { ok: false, status: 401 } },
        { verifyResponse: { ok: false, status: 503 } },
        { verifyResponse: { ok: true, status: 200, json: async () => ({ ok: false }) } },
        { verifyError: new Error('network test-secret') },
    ]) {
        const popup = popupTokenHarness(config);
        await new Promise((resolve) => setImmediate(resolve));
        popup.element('market-token').value = 'test-secret';
        await popup.element('save-market-token').handlers.click();
        assert.equal(popup.stored.marketIngestToken, undefined);
        assert.equal(popup.requests.length, 1);
        assert.doesNotMatch(popup.element('market-token-status').textContent, /test-secret/);
        assert.match(popup.element('market-token-status').textContent, /令牌|连接/);
    }
});

test('market token verification refuses public HTTP before any request', async () => {
    const popup = popupTokenHarness({ baseUrl: 'http://43.132.190.110:8088' });
    await new Promise((resolve) => setImmediate(resolve));
    popup.element('market-token').value = 'test-secret';
    await popup.element('save-market-token').handlers.click();
    assert.equal(popup.stored.marketIngestToken, undefined);
    assert.equal(popup.requests.length, 0);
    assert.match(popup.element('market-token-status').textContent, /公网 HTTP/);
});
