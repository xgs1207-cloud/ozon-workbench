const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const extensionRoot = path.resolve(__dirname, '..');
const read = (name) => fs.readFileSync(path.join(extensionRoot, name), 'utf8');

function settingsContext(name, cutoff, chrome = {}) {
    const source = read(name);
    const context = vm.createContext({ URL, btoa, chrome });
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

function cell(text, rendered = text) {
    return { textContent: text, innerText: rendered };
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

test('unrecognized tables are rejected instead of guessed into the database', () => {
    const result = captureContext(['名称', '流量', '数量'], [
        row([cell('unknown'), cell('1000'), cell('30')]),
    ]);
    assert.equal(result.records.length, 0);
    assert.match(result.reason, /未找到匹配表格/);
});
