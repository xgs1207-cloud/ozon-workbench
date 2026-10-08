const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const read = name => fs.readFileSync(path.join(__dirname, '..', name), 'utf8');

function contentHarness(extra = '') {
    const listeners = new Set(), pageListeners = new Set();
    const context = vm.createContext({ URL,
        document: {createElement:()=>({get value(){return this.innerHTML || '';}})},
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
            buildDomPropertyImageData, applyDomPropertyImage, extractVideos, extractDomComboSkus,
            normalizeSkuListItem, findSkuInfoForCombo, extractStructuredSkus, enrichSkuDimensions,
            buildReadyCapture, cartesian, skuFromCombo}); ${extra} })();`), context);
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
test('product video DOM keeps real signed source, dimensions and static poster without fetching', () => {
    const h = contentHarness();
    const video = new FakeElement({currentSrc:'https://cloud.video.taobao.com/play/u/1/p/1/e/6/t/1/VID.mp4?auth_key=private-test',
        poster:'https://cbu01.alicdn.com/img/ibank/POSTER.jpg',duration:26,videoWidth:720,videoHeight:1280});
    const root = new FakeElement({all:{video:[video]}});
    h.context.document = {querySelectorAll:selector=>selector.startsWith('.od-picture-gallery,')?[root]:[]};
    const result = vm.runInContext('extractVideos([])',h.context);
    assert.equal(result.values.length,1);
    assert.match(result.values[0].source_url,/auth_key=private-test$/);
    assert.equal(result.values[0].duration_seconds,26);
    assert.equal(result.values[0].role,'main');
    assert.equal(result.values[0].poster_is_ozon_video_cover,false);
    assert.equal(result.values[0].offer_id,'1072823232979');
});
test('video source elements replace blob player URL, and detail shadow DOM is supported',()=>{
    const h=contentHarness();
    const source=new FakeElement({src:'https://tbm-auth.alicdn.com/real.mp4?sign=test',type:'video/mp4'});
    const video=new FakeElement({currentSrc:'blob:https://detail.1688.com/temp',all:{source:[source]},one:{source}});
    const host=new FakeElement({shadowRoot:new FakeElement({all:{video:[video]}})});
    h.context.document={querySelectorAll:selector=>selector.startsWith('#desc-lazyload-container,')?[host]:[]};
    const result=vm.runInContext('extractVideos([])',h.context);
    assert.equal(result.values.length,1);
    assert.equal(result.values[0].status,'metadata_only');
    assert.equal(result.values[0].role,'detail');
});
test('loaded video configs and VideoObject deduplicate and exclude advertisements, foreign offers and poster objects',()=>{
    const h=contentHarness();
    h.context.document={querySelectorAll:()=>[]};
    const result=vm.runInContext(`extractVideos([{data:{offerId:'1072823232979',
        videoInfo:{videoId:'vid-1',playUrl:'https://cloud.video.taobao.com/MAIN.mp4?sign=current',
          duration:30,cover:{url:'https://cbu01.alicdn.com/img/ibank/POSTER.jpg'},sku_ids:['SKU1','FOREIGN']},
        recommendation:{videoUrl:'https://cloud.video.taobao.com/AD.mp4'},
        ad:{videoUrl:'https://cloud.video.taobao.com/AD2.mp4'},
        liveVideo:{videoUrl:'https://cloud.video.taobao.com/LIVE.m3u8'},
        other:{offerId:'99999999',videoUrl:'https://cloud.video.taobao.com/OTHER.mp4'}
      }},{source:'ld_json',data:{'@type':'VideoObject',contentUrl:'https://tbm-auth.alicdn.com/DETAIL.mp4',
        thumbnailUrl:'https://cbu01.alicdn.com/img/ibank/STATIC.jpg',duration:'PT1M3S'}}],['SKU1'])`,h.context);
    assert.equal(result.values.length,2);
    assert.deepEqual(Array.from(result.values[0].sku_ids),['SKU1']);
    assert.equal(result.values[0].duration_seconds,null); // Generic config duration may be milliseconds.
    assert.equal(result.values[1].duration_seconds,63);
    assert.ok(result.values.every(item=>!/(AD|LIVE|OTHER|POSTER)/.test(item.source_url)));
});
test('video duration from loaded configs requires explicit seconds or an ISO duration',()=>{
    const h=contentHarness();
    h.context.document={querySelectorAll:()=>[]};
    const result=vm.runInContext(`extractVideos([{data:{videoInfo:[
      {videoId:'a',playUrl:'https://tbm-auth.alicdn.com/a.mp4',duration:30000},
      {videoId:'b',playUrl:'https://tbm-auth.alicdn.com/b.mp4',duration_seconds:30},
      {videoId:'c',playUrl:'https://tbm-auth.alicdn.com/c.mp4',duration:31,durationUnit:'seconds'}]}}])`,h.context);
    assert.deepEqual(Array.from(result.values,item=>item.duration_seconds),[null,30,31]);
});
test('passive loaded resource can enrich only a matching current product player ID',()=>{
    const h=contentHarness();
    const lazy=new FakeElement({attrs:{'data-video-id':'V123'}});
    const root=new FakeElement({all:{'[data-video-id], [data-video-url], [data-play-url]':[lazy]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('.od-picture-gallery,')?[root]:[]};
    h.context.performance={getEntriesByType:()=>[
        {name:'https://cloud.video.taobao.com/play/V123.mp4?sign=private-current'},
        {name:'https://cloud.video.taobao.com/play/RECOMMEND.mp4'},
        {name:'https://foreign.example/V123.mp4'}]};
    h.context.fetch=()=>{throw new Error('capture must never fetch video');};
    const result=vm.runInContext('extractVideos([])',h.context);
    assert.equal(result.values.length,1);
    assert.equal(result.values[0].source_url,'https://cloud.video.taobao.com/play/V123.mp4?sign=private-current');
    assert.equal(result.values[0].network_source,'loaded_resource_timing_bound_to_product_player');
    assert.equal(result.values[0].status,'metadata_only');
    assert.ok(result.warnings.some(value=>value.includes('未能确认商品归属')));
});
test('network resource is never promoted without an already product-bound player or JSON record',()=>{
    const h=contentHarness();
    h.context.document={querySelectorAll:()=>[]};
    h.context.performance={getEntriesByType:()=>[{name:'https://cloud.video.taobao.com/play/AD.mp4?sign=private'}]};
    const result=vm.runInContext('extractVideos([])',h.context);
    assert.equal(result.values.length,0);
    assert.ok(result.warnings.some(value=>value.includes('未能确认商品归属')));
    assert.ok(!JSON.stringify(result).includes('sign=private'));
});
test('loaded current-source query is refreshed while DOM and JSON aliases deduplicate',()=>{
    const h=contentHarness();
    const video=new FakeElement({currentSrc:'https://tbm-auth.alicdn.com/V1.mp4?sign=old'});
    const root=new FakeElement({all:{video:[video]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('.od-picture-gallery,')?[root]:[]};
    h.context.performance={getEntriesByType:()=>[{name:'https://tbm-auth.alicdn.com/V1.mp4?sign=current'}]};
    const result=vm.runInContext(`extractVideos([{data:{offerId:'1072823232979',videoInfo:{
        videoId:'V1',playUrl:'https://tbm-auth.alicdn.com/V1.mp4?sign=config'}}}])`,h.context);
    assert.equal(result.values.length,1);
    assert.equal(result.values[0].provider_video_id,'V1');
    assert.match(result.values[0].source_url,/sign=current$/);
});
test('protected current video remains protected even when DOM and network expose direct URL',()=>{
    const h=contentHarness();
    const video=new FakeElement({currentSrc:'https://tbm-auth.alicdn.com/V1.mp4'});
    const root=new FakeElement({all:{video:[video]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('.od-picture-gallery,')?[root]:[]};
    h.context.performance={getEntriesByType:()=>[{name:'https://tbm-auth.alicdn.com/V1.mp4?sign=current'}]};
    const result=vm.runInContext(`extractVideos([{data:{videoInfo:{videoId:'V1',
        playUrl:'https://tbm-auth.alicdn.com/V1.mp4',drm:true}}}])`,h.context);
    assert.equal(result.values.length,1);
    assert.equal(result.values[0].drm,true);
    assert.equal(result.values[0].status,'protected_media');
    assert.ok(!result.values[0].network_source);
});
test('nested open shadow video player is captured only within product scope',()=>{
    const h=contentHarness();
    const video=new FakeElement({currentSrc:'https://tbm-auth.alicdn.com/nested.mp4'});
    const shadow=new FakeElement({all:{video:[video]}});
    const host=new FakeElement({shadowRoot:shadow});
    const root=new FakeElement({all:{'*':[host]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('#desc-lazyload-container,')?[root]:[]};
    const result=vm.runInContext('extractVideos([])',h.context);
    assert.equal(result.values.length,1);
    assert.equal(result.values[0].role,'detail');
});
test('lazy player in recommendation widget is not product evidence',()=>{
    const h=contentHarness();
    const player=new FakeElement({attrs:{'data-video-id':'AD1','data-video-url':'https://tbm-auth.alicdn.com/AD1.mp4'}});
    player.closest=()=>({});
    const root=new FakeElement({all:{'[data-video-id], [data-video-url], [data-play-url]':[player]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('.od-picture-gallery,')?[root]:[]};
    h.context.performance={getEntriesByType:()=>[{name:'https://tbm-auth.alicdn.com/AD1.mp4'}]};
    assert.equal(vm.runInContext('extractVideos([])',h.context).values.length,0);
});
test('nested advertisement shadow host is excluded even when inner video closest cannot cross shadow',()=>{
    const h=contentHarness();
    const video=new FakeElement({currentSrc:'https://tbm-auth.alicdn.com/AD-shadow.mp4'});
    const host=new FakeElement({shadowRoot:new FakeElement({all:{video:[video]}})});
    host.closest=()=>({});
    const root=new FakeElement({all:{'*':[host]}});
    h.context.document={querySelectorAll:selector=>selector.startsWith('#desc-lazyload-container,')?[root]:[]};
    assert.equal(vm.runInContext('extractVideos([])',h.context).values.length,0);
});
test('blob, HLS and unloaded players retain explicit non-downloadable diagnostics',()=>{
    const h=contentHarness();
    h.context.document={querySelectorAll:()=>[]};
    const result=vm.runInContext(`extractVideos([{data:{videoInfo:[
      {videoId:'a',playUrl:'blob:https://detail.1688.com/a'},
      {videoId:'b',playUrl:'https://tbm-auth.alicdn.com/b.m3u8'}, {videoId:'c'},
      {videoId:'d',playUrl:'https://tbm-auth.alicdn.com/d.mp4',drm:true},
      {videoId:'e',playUrl:'javascript:alert(1)'}]}}])`,h.context);
    assert.deepEqual(Array.from(result.values,item=>item.status),['unsupported_blob','unsupported_stream','not_loaded','protected_media']);
    assert.equal(result.warnings.length,1);
});
test('page-world variable context excludes live and ad players, including explicit flags',()=>{
    const h=contentHarness();
    h.context.document={querySelectorAll:()=>[]};
    const result=vm.runInContext(`extractVideos([
      {data:{name:'videoPlayerConfig',data:{url:'https://tbm-auth.alicdn.com/good.mp4'}}},
      {data:{name:'livePlayerConfig',data:{url:'https://tbm-auth.alicdn.com/live.mp4'}}},
      {data:{videoInfo:{isLive:true,playUrl:'https://tbm-auth.alicdn.com/live2.mp4'}}},
      {data:{videoInfo:{role:'advertisement',playUrl:'https://tbm-auth.alicdn.com/ad.mp4'}}}
    ])`,h.context);
    assert.equal(result.values.length,1);
    assert.match(result.values[0].source_url,/good\.mp4$/);
});
test('page probe preserves loaded signed URLs and excludes credential fields',()=>{
    const state={offerId:'1072823232979',product:{videoInfo:{playUrl:'https://tbm-auth.alicdn.com/real.mp4?sign='+ 'x'.repeat(4000),
        videoId:'V1',poster:'https://cbu01.alicdn.com/img/ibank/POSTER.jpg',duration:24,
        token:'PRIVATE',authorization:'SECRET',cookie:'COOKIE'}}};
    const attributes={};
    const context=vm.createContext({window:{offerDetailData:state,dispatchEvent:()=>{}},CustomEvent:function(){},
        document:{documentElement:{setAttribute:(key,value)=>attributes[key]=value}}});
    vm.runInContext(read('page-probe.js'),context);
    const serialized=attributes['data-caf-window-product-data'];
    const parsed=JSON.parse(serialized);
    assert.equal(parsed[0].data.product.videoInfo.playUrl,state.product.videoInfo.playUrl);
    assert.ok(!serialized.includes('PRIVATE')&&!serialized.includes('SECRET')&&!serialized.includes('COOKIE'));
});
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
    assert.equal(h.context.__workbenchProductBridge.version, '0.4.35');
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
    const videos = [{source_url:'https://tbm-auth.alicdn.com/product.mp4?sign=private',status:'metadata_only'}];
    const posted = [], opened = [];
    const context = vm.createContext({ activePageKind:'1688',
        els:{capture:{},duplicate:{},progress:{}},
        getActiveTab:async()=>({id:1}), sendToTab:async()=>({is_collectable:true,skus,videos,source_url:'https://detail.1688.com/offer/1072823232979.html'}),
        showSkuImageWarning:()=>{},checkDuplicate:async()=>({exists:false}),
        postCapture:async(capture,newVersion)=>{posted.push({capture,newVersion});return {request_id:'test-immutable-request',state:'queued'};},
        waitForSkuSelection:()=>{throw new Error('must not open drawer');},
        setResult:()=>{},loadFactoryConfig:async()=>{},workbenchEntryUrl:(_kind,extra)=>`http://127.0.0.1:8766/?product_id=${extra.product_id}`,
        chrome:{tabs:{create:o=>opened.push(o)}},
    });
    vm.runInContext(body,context);
    await vm.runInContext('captureCurrentProduct(false)',context);
    assert.equal(posted.length,1);
    assert.equal(posted[0].capture.collection_mode,'all_skus');
    assert.equal(posted[0].capture.skus.length,56);
    assert.equal(posted[0].capture.videos,videos);
    assert.equal(posted[0].capture.ozon_category_selection,undefined);
    assert.equal(opened.length,0);
    assert.match(context.els.progress.textContent,/56.*后台/);
    assert.match(context.els.progress.textContent,/1 段视频资料/);
    assert.ok(!context.els.progress.textContent.includes('sign=private'));
    await vm.runInContext('captureCurrentProduct(true)',context);
    assert.equal(posted[1].newVersion,true);
    assert.match(popup,/collectorBackgroundRequest\('\/api\/collector\/jobs'/);
});

test('real spec values containing slashes retain full colour and volume labels', () => {
    const h = contentHarness();
    const result = vm.runInContext(`normalizeSkuListItem({skuId:'6281570506082',
        specAttrs:'红色/内白>24cm/3.5L'},0,'script_init_data')`, h.context);
    assert.deepEqual(Array.from(result.option_values, option => option.value_cn), ['红色/内白','24cm/3.5L']);
});

test('SKU map matching rejects partial IDs and non-existent cartesian combinations', () => {
    const h = contentHarness();
    const combo = JSON.stringify([{id:'12',name:'红色/内白'},{id:'30',name:'24cm/3.5L'}]);
    assert.equal(vm.runInContext(`findSkuInfoForCombo(${combo},{'112>300':{skuId:'6281570506082'}})`, h.context), null);
    assert.equal(vm.runInContext(`findSkuInfoForCombo(${combo},{'12>31':{skuId:'6281570506082'}})`, h.context), null);
    const found = vm.runInContext(`findSkuInfoForCombo(${combo},{'红色/内白>24cm/3.5L':{skuId:'6281570506082'}})`, h.context);
    assert.equal(found.value.skuId, '6281570506082');
});

function potDom(h) {
    h.context.Element = FakeElement;
    const choice = (name,image) => new FakeElement({className:'image-button',textContent:name,
        attrs:{title:name},all:{img:[fakeImage(image)]}});
    const colour = new FakeElement({className:'feature-item',one:{
        '.feature-item-label h3, .feature-item-label':new FakeElement({textContent:'颜色'})},
        all:{'.image-button, .text-button, .sku-item, .value-item, .feature-value, li, button, [role="button"], [data-value], [data-name]':[
            choice('红色/内白','https://cbu01.alicdn.com/img/ibank/RED.jpg_sum.jpg'),
            choice('南瓜橘/内白','https://cbu01.alicdn.com/img/ibank/ORANGE.jpg_sum.jpg')]}});
    const size = new FakeElement({className:'feature-item',one:{
        '.feature-item-label h3, .feature-item-label':new FakeElement({textContent:'规格'})},
        all:{'.expand-view-item':[new FakeElement({className:'expand-view-item',one:{
            '.item-label':new FakeElement({textContent:'24cm/3.5L'})}})]}});
    h.context.document = {createElement:()=>({get value(){return this.innerHTML || '';}}),
        querySelectorAll:selector => selector === '#skuSelection .feature-item, .module-od-sku-selection .feature-item'
        ? [colour,size] : []};
}

test('screenshot pot image buttons plus singleton size produce exact full SKU labels and images', () => {
    const h = contentHarness(); potDom(h);
    const groups = vm.runInContext('extractDomSkuGroups()', h.context);
    assert.deepEqual(Array.from(groups, group=>group.name), ['颜色','规格']);
    const result = vm.runInContext('extractDomComboSkus(110)', h.context);
    assert.deepEqual(Array.from(result.skus, sku=>sku.sku_name), ['红色/内白 24cm/3.5L','南瓜橘/内白 24cm/3.5L']);
    assert.deepEqual(Array.from(result.skus, sku=>sku.image_url),
        ['https://cbu01.alicdn.com/img/ibank/RED.jpg','https://cbu01.alicdn.com/img/ibank/ORANGE.jpg']);
    assert.ok(result.skus.every(sku=>sku.option_values.length===2));
    const enriched = vm.runInContext(`enrichSkuDimensions({sku_name:'红色/内白',
        option_values:[{name_cn:'规格1',value_cn:'红色/内白'}]},extractDomSkuGroups())`, h.context);
    assert.equal(enriched.sku_name,'红色/内白 24cm/3.5L');
});

test('structured map combinations are preferred over incomplete same-ID summaries', () => {
    const h = contentHarness(); potDom(h);
    const result = vm.runInContext(`extractStructuredSkus([{data:{
        skuProps:[{prop:'颜色',values:[{valueId:'red',name:'红色/内白'}, {valueId:'orange',name:'南瓜橘/内白'}]},
            {prop:'规格',values:[{valueId:'size24',name:'24cm/3.5L'}]}],
        skuMap:{'red>size24':{skuId:'6281570506082',skuName:'红色/内白',price:110},
            'orange>size24':{skuId:'6281570506083',skuName:'南瓜橘/内白',price:110}}
    }}])`, h.context);
    assert.equal(result.skus[0].sku_name,'红色/内白 24cm/3.5L');
    assert.equal(result.skus[0].option_values.length,2);
    assert.equal(result.skus[0].image_url,'https://cbu01.alicdn.com/img/ibank/RED.jpg');
});

test('navigation during DOM warming fails before mixing another offer snapshot', async () => {
    const h = contentHarness(`warmAllSkuImages = async () => { location.pathname='/offer/9999999999999.html'; };
        warmProductAttributeTables = async () => {throw new Error('must not continue')};`);
    await assert.rejects(vm.runInContext('buildReadyCapture()',h.context), /页面已切换.*尚未提交/);
});

test('product transport rejects a capture returned for a foreign offer', async () => {
    const h=bridgeHarness();
    h.context.chrome.tabs.sendMessage=async()=>({is_collectable:true,
        source_url:'https://detail.1688.com/offer/9999999999999.html'});
    await assert.rejects(h.request({type:'COLLECTOR_CAPTURE'}), /读取中断/);
});

test('collection keeps more than 300 real combinations and rejects oversize instead of truncating',()=>{
    const h=contentHarness();
    const result=vm.runInContext(`cartesian([{values:Array.from({length:350},(_,id)=>({id,name:String(id)}))}])`,h.context);
    assert.equal(result.length,350);
    assert.throws(()=>vm.runInContext(`cartesian([{values:Array(100).fill({})},{values:Array(21).fill({})}])`,h.context),/不会静默截断/);
});

test('a SKU source never includes other SKU images or repeats the full offer map',()=>{
    const h=contentHarness();
    const sku=vm.runInContext(`skuFromCombo([{name:'红色/内白',id:'red',image_url:'unknown',
        option:{name_cn:'颜色',value_cn:'红色/内白'},raw:{value:'红色/内白'}}],
        {key:'red',value:{skuId:'6281570506082',price:110}},0,
        {skuMap:{orange:{skuId:'6281570506083',imageUrl:'https://cbu01.alicdn.com/OTHER.jpg'}}})`,h.context);
    assert.equal(sku.image_url,'unknown');
    assert.ok(!JSON.stringify(sku.source_data).includes('OTHER.jpg'));
    assert.equal(sku.source_data.source_data,undefined);
});
