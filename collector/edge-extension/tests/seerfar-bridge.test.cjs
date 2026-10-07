const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');

function harness({ missing = false, old = false, injectionFailure = false, commandFailure = false,
    url = 'https://www.seerfar.cn/admin/market', pingError, tabError = false } = {}) {
    let ready = !missing, injections = 0;
    const messages = [];
    const chrome = {
        tabs: {
            get: async () => { if (tabError) throw new Error('No tab with id'); return { url }; },
            sendMessage: async (_id, msg) => {
                messages.push(msg.type);
                if (msg.type === 'SEERFAR_MARKET_PING') {
                    if (pingError) throw new Error(pingError);
                    if (!ready) throw new Error('Could not establish connection. Receiving end does not exist.');
                    return old ? undefined : { ready: true, version: '0.4.31' };
                }
                if (commandFailure) throw new Error('The message port closed before a response was received.');
                return { advanced: true };
            },
        },
        scripting: { executeScript: async options => {
            injections++;
            assert.equal(JSON.stringify(options.target), JSON.stringify({ tabId: 1, frameIds: [0] }));
            assert.equal(JSON.stringify(options.files), JSON.stringify(['seerfar-content.js']));
            assert.equal(options.world, undefined, 'keep collection in the default isolated world');
            if (injectionFailure) throw new Error('Cannot access contents');
            ready = true;
        } },
    };
    const context = vm.createContext({ chrome });
    vm.runInContext(read('seerfar-bridge.js'), context);
    return { context, messages, injections: () => injections,
        request: type => vm.runInContext(`sendSeerfarTabMessage(1, {type:${JSON.stringify(type)}})`, context) };
}

test('connected pages are pinged and not injected again', async () => {
    const h = harness();
    assert.equal((await h.request('SEERFAR_MARKET_CAPTURE')).advanced, true);
    assert.equal(h.injections(), 0);
    assert.deepEqual(h.messages, ['SEERFAR_MARKET_PING', 'SEERFAR_MARKET_CAPTURE']);
});

test('missing receiver is recovered before dispatching capture or next exactly once', async () => {
    const h = harness({ missing: true });
    await h.request('SEERFAR_MARKET_CAPTURE');
    await h.request('SEERFAR_MARKET_NEXT_PAGE');
    assert.equal(h.injections(), 1);
    assert.equal(h.messages.filter(x => x === 'SEERFAR_MARKET_CAPTURE').length, 1);
    assert.equal(h.messages.filter(x => x === 'SEERFAR_MARKET_NEXT_PAGE').length, 1);
});

test('concurrent connection recovery shares one injection', async () => {
    const h = harness({ missing: true });
    await Promise.all([h.request('SEERFAR_MARKET_CAPTURE'), h.request('SEERFAR_MARKET_STOP')]);
    assert.equal(h.injections(), 1);
});

test('an ambiguous lost command response never causes a second page click', async () => {
    const h = harness({ commandFailure: true });
    await assert.rejects(h.request('SEERFAR_MARKET_NEXT_PAGE'), /免重复翻页/);
    assert.equal(h.injections(), 0);
    assert.equal(h.messages.filter(x => x === 'SEERFAR_MARKET_NEXT_PAGE').length, 1);
});

test('old listener, closed port and denied injection produce actionable Chinese errors', async () => {
    for (const [options, expected] of [
        [{ old: true }, /旧版.*刷新/],
        [{ missing: true, injectionFailure: true }, /允许访问此网站/],
        [{ pingError: 'The message port closed before a response was received.' }, /连接已中断/],
    ]) {
        const h = harness(options);
        await assert.rejects(h.request('SEERFAR_MARKET_CAPTURE'), expected);
        assert.equal(h.messages.includes('SEERFAR_MARKET_CAPTURE'), false);
        if (!options.missing) assert.equal(h.injections(), 0);
    }
});

test('foreign pages, login, HTTP and closed tabs cannot trigger injection or commands', async () => {
    for (const options of [
        { url: 'https://seerfar.cn.attacker.example/admin/market' },
        { url: 'http://seerfar.cn/admin/market' },
        { url: 'https://www.seerfar.cn/admin/login' },
        { url: 'https://www.seerfar.cn/' },
        { tabError: true },
    ]) {
        const h = harness({ ...options, missing: true });
        await assert.rejects(h.request('SEERFAR_MARKET_CAPTURE'), /Seerfar/);
        assert.equal(h.injections(), 0);
        assert.equal(h.messages.length, 0);
    }
});

test('a rejected connection is evicted so the next manual attempt can recover', async () => {
    const h = harness({ old: true });
    await assert.rejects(h.request('SEERFAR_MARKET_CAPTURE'));
    assert.equal(vm.runInContext('seerfarBridgeConnections.size', h.context), 0);
});

test('content script reinjection adds no duplicate listener and preserves stop state', () => {
    const listeners = new Set();
    const runtime = { id: 'test-extension', onMessage: {
        addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    } };
    const context = vm.createContext({ chrome: { runtime } });
    const source = read('seerfar-content.js');
    vm.runInContext(source, context);
    const listener = [...listeners][0];
    let ready;
    listener({ type: 'SEERFAR_MARKET_PING' }, {}, value => { ready = value; });
    assert.equal(ready.version, '0.4.31');
    listener({ type: 'SEERFAR_MARKET_STOP' }, {}, () => {});
    vm.runInContext(source, context);
    vm.runInContext(source, context);
    assert.equal(listeners.size, 1);
    assert.equal([...listeners][0], listener);
    let result;
    listener({ type: 'SEERFAR_MARKET_NEXT_PAGE' }, {}, value => { result = value; });
    assert.equal(result.stopped, true);
});

test('stale extension context listener is replaced once, without lexical redeclaration errors', () => {
    const listeners = new Set();
    const chrome = { runtime: { id: 'test-extension', onMessage: {
        addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
    } } };
    const context = vm.createContext({ chrome });
    const source = read('seerfar-content.js');
    vm.runInContext(source, context);
    const prior = [...listeners][0];
    context.__seerfarMarketBridge.isCurrent = () => { throw new Error('Extension context invalidated'); };
    vm.runInContext(source, context);
    assert.equal(listeners.size, 1);
    assert.notEqual([...listeners][0], prior);
});

test('popup and background both load the common recovery transport', () => {
    assert.match(read('background.js'), /importScripts\('seerfar-bridge.js', 'market-jobs.js'\)/);
    const html = read('popup.html');
    assert.ok(html.indexOf('src="seerfar-bridge.js"') < html.indexOf('src="popup.js"'));
    const version = JSON.parse(read('manifest.json')).version;
    assert.equal(version, '0.4.33');
    assert.equal(JSON.parse(read('package.json')).version, version);
});
