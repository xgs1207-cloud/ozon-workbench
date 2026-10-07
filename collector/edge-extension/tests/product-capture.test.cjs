const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');

function contentHarness(extra = '') {
    const listeners = new Set(), pageListeners = new Set();
    const context = vm.createContext({ URL,
        location: { hostname: 'detail.1688.com', pathname: '/offer/1072823232979.html',
            origin: 'https://detail.1688.com', href: 'https://detail.1688.com/offer/1072823232979.html?share_token=test-only' },
        window: { addEventListener: (_name, fn) => pageListeners.add(fn),
            removeEventListener: (_name, fn) => pageListeners.delete(fn) },
        chrome: { runtime: { id: 'qa-extension', onMessage: {
            addListener: fn => listeners.add(fn), removeListener: fn => listeners.delete(fn),
        } } },
    });
    const load = () => vm.runInContext(read('content.js').replace(/\}\)\(\);\s*$/,
        `Object.assign(globalThis, {offerImgListDetailUrls, productReadFailure, is1688OfferPage,
            normalizeImageUrl, extractMainImages, extractDetailImages, extractDomSkuGroups,
            buildDomPropertyImageData, applyDomPropertyImage}); ${extra} })();`), context);
    load();
    return { context, listeners, pageListeners, load };
}

test('offer image traversal accepts real window objects, JSON-LD and snippet arrays together', () => {
    const h = contentHarness();
    const result = vm.runInContext(`offerImgListDetailUrls([
        {source:'window_variable',data:{name:'offerDetailData',data:{offerImgList:['https://cbu01.alicdn.com/img/ibank/DETAIL1.jpg']}}},
        {source:'ld_json',data:{offerImgList:['https://cbu01.alicdn.com/img/ibank/DETAIL2.jpg']}},
        {source:'script_init_data',data:[{name:'offer',data:{offerImgList:['//cbu01.alicdn.com/img/ibank/DETAIL3.jpg']}}]},
        {data:null}, {data:42}, {data:'not-product-data'}
    ],[],[])`, h.context);
    assert.deepEqual(Array.from(result, x => x.url), [
        'https://cbu01.alicdn.com/img/ibank/DETAIL1.jpg',
        'https://cbu01.alicdn.com/img/ibank/DETAIL2.jpg',
        'https://cbu01.alicdn.com/img/ibank/DETAIL3.jpg',
    ]);
});

test('detail supplement excludes main/SKU image IDs and duplicate or invalid URLs', () => {
    const h = contentHarness();
    const result = vm.runInContext(`offerImgListDetailUrls([{data:{offerImgList:[
        'https://cbu01.alicdn.com/img/ibank/MAIN1.jpg_100x100.jpg',
        'https://cbu01.alicdn.com/img/ibank/SKU1.jpg',
        'https://cbu01.alicdn.com/img/ibank/DETAIL1.jpg',
        'https://cbu01.alicdn.com/img/ibank/DETAIL1.jpg', 'javascript:alert(1)', null, {}
    ]}}],['https://cbu01.alicdn.com/img/ibank/MAIN1.jpg'],['https://cbu01.alicdn.com/img/ibank/SKU1.jpg'])`, h.context);
    assert.equal(result.length, 1);
    assert.equal(result[0].source, 'offer_img_list');
    assert.match(result[0].url, /DETAIL1/);
});

test('Alibaba thumbnail transforms resolve to originals without rewriting other hosts', () => {
    const h = contentHarness();
    for (const suffix of ['_sum.jpg', '_b.jpg', '_.webp', '_400x400.jpg'])
        assert.equal(vm.runInContext(`normalizeImageUrl('https://cbu01.alicdn.com/img/ibank/ONE.jpg${suffix}')`, h.context),
            'https://cbu01.alicdn.com/img/ibank/ONE.jpg');
    assert.equal(vm.runInContext("normalizeImageUrl('https://cdn.example/ONE.jpg_sum.jpg')", h.context),
        'https://cdn.example/ONE.jpg_sum.jpg');
    for (const value of ['undefined', 'null', 'javascript:alert(1)', 'file:///C:/image.jpg'])
        assert.equal(vm.runInContext(`normalizeImageUrl(${JSON.stringify(value)})`, h.context), null);
});

class FakeElement {
    constructor(props = {}) { Object.assign(this, props); }
    getAttribute(name) { return this.attrs?.[name] || null; }
    querySelectorAll(selector) { return this.all?.[selector] || []; }
    querySelector(selector) { return this.one?.[selector] || null; }
    closest() { return null; }
    matches() { return false; }
}
function fakeImage(url) { return new FakeElement({ tagName: 'IMG', currentSrc: url,
    naturalWidth: 600, naturalHeight: 1200 }); }

test('main gallery reads CSS cover images and deduplicates full size originals', () => {
    const h = contentHarness();
    h.context.Element = FakeElement;
    const cover = url => new FakeElement({ tagName: 'LI', ownerDocument: { defaultView: {
        getComputedStyle: () => ({ backgroundImage: `url("${url}")` }),
    } } });
    h.context.document = { querySelectorAll: selector => selector.startsWith('.od-picture-gallery-list')
        ? [cover('https://cbu01.alicdn.com/img/ibank/ONE.jpg_b.jpg'),
            cover('https://cbu01.alicdn.com/img/ibank/ONE.jpg_sum.jpg'), cover('undefined')] : [] };
    const result = vm.runInContext('extractMainImages()', h.context);
    assert.equal(result.values.length, 1);
    assert.equal(result.values[0].url, 'https://cbu01.alicdn.com/img/ibank/ONE.jpg');
    assert.match(result.selectors[0], /:not\(\.video-image-cover\)/);
});

test('detail images are read from the product description open shadow root only', () => {
    const h = contentHarness();
    const shadow = { querySelectorAll: selector => selector === 'img'
        ? [fakeImage('https://cbu01.alicdn.com/img/ibank/DETAIL.jpg'),
            fakeImage('https://cbu01.alicdn.com/img/ibank/DETAIL.jpg_1000x1000.jpg')] : [] };
    const host = new FakeElement({ shadowRoot: shadow });
    h.context.document = { querySelectorAll: selector => selector === '.module-od-product-description, v-detail-h.html-description'
        ? [host] : [] };
    const result = vm.runInContext('extractDetailImages()', h.context);
    assert.equal(result.values.length, 1);
    assert.equal(result.values[0].source, 'detail_shadow_dom');
});

test('current 1688 expanded sale rows preserve exact labels and map SKU-specific images', () => {
    const h = contentHarness();
    h.context.Element = FakeElement;
    const makeRow = (name, image) => new FakeElement({ className: 'expand-view-item', one: {
        '.item-label': new FakeElement({ textContent: name, attrs: { title: name } }),
        '.item-image-icon': new FakeElement({ all: { img: [fakeImage(image)] } }),
    } });
    const group = new FakeElement({ className: 'feature-item', one: {
        '.feature-item-label h3, .feature-item-label': new FakeElement({ textContent: '颜色' }),
    }, all: { '.expand-view-item': [
        makeRow('绿色圣诞树捏捏乐（盒装）', 'https://cbu01.alicdn.com/img/ibank/GREEN.jpg_sum.jpg'),
        makeRow('圣诞树+老人混装12件套（红纸盒款）', 'https://cbu01.alicdn.com/img/ibank/MIXED.jpg_sum.jpg'),
    ] } });
    h.context.document = { querySelectorAll: selector => selector === '#skuSelection .feature-item, .module-od-sku-selection .feature-item'
        ? [group] : [] };
    const groups = vm.runInContext('extractDomSkuGroups()', h.context);
    assert.equal(groups[0].name, '颜色');
    assert.equal(groups[0].values.length, 2);
    assert.equal(groups[0].values[1].name, '圣诞树+老人混装12件套（红纸盒款）');
    const mapped = vm.runInContext(`applyDomPropertyImage({sku_name:'圣诞树+老人混装12件套（红纸盒款）',
        image_url:'unknown',option_values:[]},buildDomPropertyImageData(extractDomSkuGroups()).lookup)`, h.context);
    assert.equal(mapped.image_url, 'https://cbu01.alicdn.com/img/ibank/MIXED.jpg');
    assert.equal(mapped.sku_image_missing, false);
});

test('product script reinjection keeps exactly one runtime and page-data listener', () => {
    const h = contentHarness();
    const listener = [...h.listeners][0];
    h.load(); h.load();
    assert.equal(h.listeners.size, 1);
    assert.equal(h.pageListeners.size, 1);
    assert.equal([...h.listeners][0], listener);
    assert.equal(h.context.__workbenchProductBridge.version, '0.4.32');
});

test('invalidated product context is replaced without duplicate callbacks or declarations', () => {
    const h = contentHarness();
    h.context.__workbenchProductBridge.isCurrent = () => { throw new Error('context invalidated'); };
    h.load();
    assert.equal(h.listeners.size, 1);
    assert.equal(h.pageListeners.size, 1);
});

test('unhandled content messages do not hold a Chrome response channel open', () => {
    const h = contentHarness();
    assert.equal([...h.listeners][0]({ type: 'UNKNOWN_TEST_MESSAGE' }, {}, () => {
        throw new Error('unexpected response');
    }), undefined);
});

test('1688 extraction failure returns uncollectable data with explicit diagnostics', async () => {
    const h = contentHarness("buildReadyCapture = async () => { throw new Error('unexpected data shape'); };");
    const result = await new Promise(resolve => [...h.listeners][0]({ type: 'COLLECTOR_PREVIEW' }, {}, resolve));
    assert.equal(result.is_collectable, false);
    assert.match(result.reason, /1688 商品读取失败：unexpected data shape/);
    assert.equal(result.skus.length, 0);
});

test('a synchronous Ozon extraction failure also responds instead of dropping the channel', async () => {
    const h = contentHarness("buildOzonReferenceCapture = () => { throw new Error('qa Ozon failure'); };");
    const result = await new Promise(resolve => [...h.listeners][0]({ type: 'COLLECTOR_OZON_PREVIEW' }, {}, resolve));
    assert.equal(result.is_collectable, false);
    assert.match(result.reason, /Ozon 商品读取失败/);
});

test('SKU drawer cannot open using a different offer or failed extraction', () => {
    const h = contentHarness();
    let result;
    [...h.listeners][0]({ type: 'OPEN_SKU_SELECTOR', capture: {
        is_collectable: true, source_url: 'https://detail.1688.com/offer/99999999999.html',
    } }, {}, value => { result = value; });
    assert.equal(result.opened, false);
    assert.match(result.error, /已切换/);
});

function bridgeHarness({ ready = true, legacy = false, permissionDenied = false, sendFailure = false,
    url = 'https://detail.1688.com/offer/1072823232979.html?share_token=qa', tabClosed = false } = {}) {
    const files = [], messages = [];
    const context = vm.createContext({ URL, chrome: {
        tabs: {
            get: async () => { if (tabClosed) throw new Error('closed'); return { url }; },
            sendMessage: async (_id, msg) => {
                messages.push(msg);
                if (sendFailure) throw new Error('message channel closed');
                return { is_collectable: true };
            },
        },
        scripting: { executeScript: async options => {
            if (permissionDenied) throw new Error('cannot access');
            if (options.files) { files.push(options.files[0]); ready = true; return [{}]; }
            return [{ result: { ready, legacy } }];
        } },
    } });
    vm.runInContext(read('product-bridge.js'), context);
    return { context, files, messages,
        request: msg => vm.runInContext(`sendProductTabMessage(1, ${JSON.stringify(msg || {type:'COLLECTOR_PREVIEW'})})`, context) };
}

test('product bridge reloads a missing receiver only once before reading', async () => {
    const h = bridgeHarness({ ready: false });
    await Promise.all([h.request(), h.request()]);
    assert.deepEqual(h.files, ['content.js']);
    assert.equal(h.messages.length, 2);
    assert.equal(vm.runInContext('productBridgeConnections.size', h.context), 0);
});

test('connected product bridge never reinjects and does not retry ambiguous operations', async () => {
    const h = bridgeHarness({ sendFailure: true });
    await assert.rejects(h.request(), /读取中断/);
    assert.equal(h.files.length, 0);
    assert.equal(h.messages.length, 1);
});

test('legacy script and denied permissions provide actionable errors before collection', async () => {
    for (const [options, expected] of [
        [{ ready: false, legacy: true }, /旧版.*刷新/],
        [{ permissionDenied: true }, /允许插件访问/],
    ]) {
        const h = bridgeHarness(options);
        await assert.rejects(h.request(), expected);
        assert.equal(h.files.length, 0);
        assert.equal(h.messages.length, 0);
    }
});

test('foreign domains, non-product pages, HTTP and closed tabs are rejected', async () => {
    for (const options of [
        { url: 'https://detail.1688.com.evil.test/offer/1072823232979.html' },
        { url: 'https://www.1688.com/' },
        { url: 'http://detail.1688.com/offer/1072823232979.html' },
        { url: 'https://www.ozon.ru/category/tools-12/' },
        { tabClosed: true },
    ]) {
        const h = bridgeHarness(options);
        await assert.rejects(h.request(), /商品/);
        assert.equal(h.files.length, 0);
        assert.equal(h.messages.length, 0);
    }
});

test('a product page switch cannot mix a previous capture with a new SKU drawer', async () => {
    const h = bridgeHarness();
    await assert.rejects(h.request({ type: 'OPEN_SKU_SELECTOR', capture: {
        source_url: 'https://detail.1688.com/offer/99999999999.html' } }), /上一件商品/);
    await assert.rejects(h.request({ type: 'COLLECTOR_OZON_PREVIEW' }), /类型已改变/);
    assert.equal(h.messages.length, 0);
});

test('share links are accepted by offer path and forged Ozon hosts are rejected', () => {
    const h = bridgeHarness();
    assert.equal(vm.runInContext("productPageKind('https://detail.1688.com/offer/1072823232979.html?share_token=qa&offerId=1072823232979')", h.context), '1688');
    assert.equal(vm.runInContext("productPageKind('https://www.ozon.ru/product/toy-123456/')", h.context), 'ozon');
    assert.equal(vm.runInContext("productPageKind('https://evilozon.ru/product/toy-123456/')", h.context), null);
});

test('popup loads both transports and safely checks capture before opening a drawer', () => {
    const html = read('popup.html');
    assert.ok(html.indexOf('src="product-bridge.js"') < html.indexOf('src="popup.js"'));
    assert.match(read('popup.js'), /if \(!capture\?\.is_collectable\) throw new Error/);
    assert.match(read('popup.js'), /result\?\.opened !== true/);
});

test('1688 capture saves all variants without opening the SKU drawer or selecting a category', async () => {
    const popup = read('popup.js');
    const body = popup.slice(popup.indexOf('async function captureCurrentProduct('),
        popup.indexOf('async function captureCurrentOzonReference('));
    const skus = Array.from({length:56},(_,i)=>({sku_id:String(i+100),purchase_price:4.5}));
    const posted = [], opened = [];
    const context = vm.createContext({ activePageKind:'1688',
        els:{capture:{},duplicate:{},progress:{}},
        getActiveTab:async()=>({id:1}), sendToTab:async()=>({is_collectable:true,skus,source_url:'https://detail.1688.com/offer/1072823232979.html'}),
        showSkuImageWarning:()=>{},checkDuplicate:async()=>({exists:false}),
        postCapture:async(capture,newVersion)=>{posted.push({capture,newVersion});return {product_id:'P000002',counts:{skus:56}};},
        waitForSkuSelection:()=>{throw new Error('must not open drawer');},
        setResult:()=>{},loadFactoryConfig:async()=>{},workbenchEntryUrl:(_kind,extra)=>`http://127.0.0.1:8766/?product_id=${extra.product_id}`,
        chrome:{tabs:{create:o=>opened.push(o)}},
    });
    vm.runInContext(body,context);
    await vm.runInContext('captureCurrentProduct(false)',context);
    assert.equal(posted.length,1);
    assert.equal(posted[0].capture.collection_mode,'all_skus');
    assert.equal(posted[0].capture.skus.length,56);
    assert.equal(posted[0].capture.ozon_category_selection,undefined);
    assert.equal(opened.length,1);
    assert.match(context.els.progress.textContent,/56.*工作台/);
    await vm.runInContext('captureCurrentProduct(true)',context);
    assert.equal(posted[1].newVersion,true);
    assert.match(popup,/\/api\/collector\/products\$\{allowNewVersion \? '\?allow_new_version=true'/);
});
