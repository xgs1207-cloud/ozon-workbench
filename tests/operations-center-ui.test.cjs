'use strict';
const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/operations-center.js'),'utf8');
const pure=require('../web/operations-center.js');
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return{resolve,promise}};
function harness(respond) {
    const calls=[],timers=new Map();let timerId=0;
    const regions=new Map(['toolbar','catalog','products','detail','settings','advertising'].map(name=>[name,{innerHTML:''}]));
    const notice={hidden:true,textContent:'',classList:{toggle(){}}},inputs=new Map(),handlers=new Map();
    const host={isConnected:true,innerHTML:'',classList:{add(value){this[value]=true},remove(value){delete this[value]},contains(value){return this[value]===true}},addEventListener:(type,fn)=>handlers.set(type,fn),removeEventListener:type=>handlers.delete(type),querySelector:selector=>selector==='[data-ops-notice]'?notice:regions.get(/data-ops-region="([^"]+)/.exec(selector)?.[1]) || inputs.get(selector) || null};
    const win={document:{querySelector:()=>host},fetch:async(url,options)=>{calls.push({url,options});const value=respond?await respond(url,options):fixtures(url);return {ok:true,status:200,json:async()=>value}}};
    const context=vm.createContext({window:win,module:{exports:{}},URL,URLSearchParams,Map,Set,JSON,Date,Intl,Number,String,Error,AbortController,
        setTimeout:(fn,delay)=>{const id=++timerId;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id)});
    vm.runInContext(source,context);
    return{win,host,regions,notice,inputs,handlers,calls,timers,context,
        click:(action,extra={})=>handlers.get('click')({target:{closest:()=>({dataset:{opsAction:action,...extra},disabled:false,isConnected:true})}}),
        change:(dataset,other={})=>handlers.get('change')({target:{dataset,...other}})};
}
function fixtures(url) {
    if(url==='/api/operations/config')return{shops:[{id:'a',name:'店铺 A'},{id:'b',name:'店铺 B'}],credential_security:{can_submit_credentials:true},advertising_write_enabled:false};
    if(url.startsWith('/api/operations/products'))return{items:[{offer_id:'xzj.jp.10.8.1',product_id:'P000007',source_note:'绿色 / 24cm',import_status:'imported'}],total:1};
    if(url.startsWith('/api/operations/advertising/status'))return{ready:false,status:'not_configured'};
    if(url.startsWith('/api/operations/schedule'))return{enabled:false,days:7};
    if(url.startsWith('/api/operations/product?'))return{product:{offer_id:'xzj.jp.10.8.1'},snapshots:[],queries:{items:[]},diagnostics:[],jobs:[]};
    return{items:[]};
}

const catalogPage=(offers=['old-offer','new-offer'],next=true)=>({items:offers.map((offer,i)=>({offer_id:offer,name:'离线商品 '+offer,ozon_product_id:String(100+i),ozon_sku:String(900+i),price:i===0?null:0,currency:'RUB',status:'processed'})),page_token:offers.includes('new-offer')?'11111111-1111-4111-8111-111111111111':'22222222-2222-4222-8222-222222222222',has_more:next,total:3,fetched_at:'2026-10-08T01:00:00Z',warning_codes:[]});

test('catalog import is explicit; cached next/back clears selection and cannot submit raw remote identity',async()=>{
    const h=harness((url,options)=>url==='/api/operations/catalog/read'?(JSON.parse(options.body).previous_page_token?catalogPage(['last-offer'],false):catalogPage()):url==='/api/operations/catalog/add'?{imported:1,existing:0,queued:1,deduplicated:0,queue_errors:[]}:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('catalog-open');
    assert.ok(!h.calls.some(c=>c.url==='/api/operations/catalog/read'));
    await h.click('catalog-read');h.change({opsCatalogOffer:'new-offer'},{checked:true});await h.click('catalog-add');
    const body=JSON.parse(h.calls.find(c=>c.url==='/api/operations/catalog/add').options.body);
    assert.deepEqual(body,{shop:'a',page_token:'11111111-1111-4111-8111-111111111111',offer_ids:['new-offer'],analyze:true,days:7,include_traffic:true});
    assert.match(h.notice.textContent,/新增 1 个/);assert.match(h.regions.get('catalog').innerHTML,/本页已选 0/);
    await h.click('catalog-next');assert.match(h.regions.get('catalog').innerHTML,/last-offer/);
    assert.deepEqual(JSON.parse(h.calls.filter(c=>c.url==='/api/operations/catalog/read')[1].options.body),{shop:'a',limit:50,previous_page_token:body.page_token});
    h.change({opsCatalogOffer:'last-offer'},{checked:true});await h.click('catalog-previous');
    assert.match(h.regions.get('catalog').innerHTML,/new-offer/);assert.match(h.regions.get('catalog').innerHTML,/本页已选 0/);
    h.win.OperationsCenter.unmount();
});

test('catalog current-page filter is literal, selection caps at 100, and no-analysis choice is respected',async()=>{
    const page=catalogPage(Array.from({length:110},(_,i)=>'offer-'+i),false);const h=harness((url,opts)=>url==='/api/operations/catalog/read'?page:url==='/api/operations/catalog/add'?{imported:1,existing:0,queued:0,deduplicated:0}:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('catalog-open');await h.click('catalog-read');await h.click('catalog-select');
    assert.match(h.regions.get('catalog').innerHTML,/本页已选 100/);await h.click('catalog-clear');
    h.inputs.set('[data-ops-input="catalog-query"]',{value:'offer-109'});await h.click('catalog-filter');await h.click('catalog-select');
    h.change({opsInput:'catalog-analyze'},{checked:false});await h.click('catalog-add');
    const body=JSON.parse(h.calls.find(c=>c.url==='/api/operations/catalog/add').options.body);assert.deepEqual(body.offer_ids,['offer-109']);assert.equal(body.analyze,false);
    assert.deepEqual(pure.catalogItems(catalogPage(),'<script>'),[]);h.win.OperationsCenter.unmount();
});

test('catalog close and shop-switch suppress late page results, without stopping persisted jobs',async()=>{
    const waiting=deferred();const h=harness((url)=>url==='/api/operations/catalog/read'?waiting.promise:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('catalog-open');const running=h.click('catalog-read');
    const call=h.calls.find(c=>c.url==='/api/operations/catalog/read');await h.click('catalog-close');assert.equal(call.options.signal.aborted,true);
    await h.click('catalog-open');h.change({opsInput:'shop'},{value:'b'});waiting.resolve(catalogPage(['A-stale'],false));await running;
    assert.equal(h.regions.get('catalog').innerHTML,'');assert.doesNotMatch(h.regions.get('products').innerHTML,/A-stale/);h.win.OperationsCenter.unmount();
});

test('import locks scope and duplicate clicks; partial queue failure remains an honest saved import',async()=>{
    const waiting=deferred();const h=harness(url=>url==='/api/operations/catalog/read'?catalogPage():url==='/api/operations/catalog/add'?waiting.promise:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('catalog-open');await h.click('catalog-read');h.change({opsCatalogOffer:'new-offer'},{checked:true});
    const pending=h.click('catalog-add');await h.click('catalog-add');h.change({opsInput:'shop'},{value:'b'});await h.click('catalog-close');
    assert.match(h.regions.get('toolbar').innerHTML,/<option value="a" selected>/);assert.match(h.regions.get('catalog').innerHTML,/正在加入/);
    waiting.resolve({imported:1,existing:0,queued:0,queue_errors:[{offer_id:'new-offer',code:'queue_unavailable'}]});await pending;
    assert.equal(h.calls.filter(c=>c.url==='/api/operations/catalog/add').length,1);assert.match(h.notice.textContent,/部分分析未能排队/);h.win.OperationsCenter.unmount();
});

test('catalog warns incomplete pagination, escapes source strings, and does not fabricate missing price',async()=>{
    const page={...catalogPage(),warning_codes:['cursor_loop','page_limit'],has_more:false};const h=harness(url=>url==='/api/operations/catalog/read'?page:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('catalog-open');await h.click('catalog-read');
    assert.match(h.regions.get('catalog').innerHTML,/不能视为全部读取完毕/);assert.match(h.regions.get('catalog').innerHTML,/200 页读取上限/);assert.match(h.regions.get('catalog').innerHTML,/待回读/);assert.match(h.regions.get('catalog').innerHTML,/>0</);
    const html=pure.catalogRows([{offer_id:'<script>',name:'<img onerror=bad>',thumbnail:'javascript:bad'}],new Set());assert.doesNotMatch(html,/<script>|<img/);assert.match(html,/&lt;img/);
    for(const url of ['javascript:bad','http://x.test/a','https://user:secret@x.test/a'])assert.equal(pure.safeThumbnail(url),'');
    assert.equal(pure.ozonLink('9001'),'https://www.ozon.ru/product/9001/');assert.equal(pure.ozonLink('" onclick=bad'),'');h.win.OperationsCenter.unmount();
});
test('numeric absence remains unavailable and ratios reject zero denominators',()=>{
    for(const value of [null,undefined,'','  ',false,true,[],NaN,Infinity])assert.equal(pure.metric(value),'暂无数据');
    assert.equal(pure.metric(0),'0');assert.equal(pure.finiteRatio(10,0),null);assert.equal(pure.finiteRatio(0,20),0);
});
test('initial shop prefers authorized enabled default, then authorized, enabled, and first record',()=>{
    const sample={id:'sample',enabled:true,credentials_ready:false,is_default:false};
    const authorized={id:'authorized',enabled:true,credentials_ready:true,is_default:false};
    const preferred={id:'preferred',enabled:true,credentials_ready:true,is_default:true};
    assert.equal(pure.initialShopId([sample,authorized,preferred]),'preferred');
    assert.equal(pure.initialShopId([{...preferred,enabled:false},sample,authorized]),'authorized');
    assert.equal(pure.initialShopId([{...preferred,credentials_ready:false},authorized]),'authorized');
    assert.equal(pure.initialShopId([{id:'disabled',enabled:false},sample]),'sample');
    assert.equal(pure.initialShopId([{id:'first',enabled:false},{id:'second',enabled:false}]),'first');
    assert.equal(pure.initialShopId([{id:'first',enabled:'false',credentials_ready:'true',is_default:'true'},sample]),'sample');
    assert.equal(pure.initialShopId([]),'');assert.equal(pure.initialShopId(null),'');
});
test('mount skips stale sample default and manual selection still switches isolated cached reads',async()=>{
    const shops=[{id:'sample',name:'示例店',enabled:true,credentials_ready:false,is_default:false},{id:'actual',name:'当前默认店',enabled:true,credentials_ready:true,is_default:true}];
    const h=harness(url=>url==='/api/operations/config'?{shops,credential_security:{can_submit_credentials:true}}:fixtures(url));
    await h.win.OperationsCenter.mount(h.host);
    assert.match(h.regions.get('toolbar').innerHTML,/<option value="actual" selected>/);
    assert.ok(h.calls.some(call=>call.url.startsWith('/api/operations/products?shop=actual')));
    assert.ok(!h.calls.some(call=>call.url.startsWith('/api/operations/products?shop=sample')));
    h.change({opsInput:'shop'},{value:'sample'});await new Promise(resolve=>setImmediate(resolve));
    assert.match(h.regions.get('toolbar').innerHTML,/<option value="sample" selected>/);
    assert.ok(h.calls.some(call=>call.url.startsWith('/api/operations/products?shop=sample')));
    h.win.OperationsCenter.unmount();
});
test('source URLs are canonical 1688 links with no credential or tracking leak',()=>{
    assert.equal(pure.safeSource('https://detail.1688.com/offer/123.html?share_token=private'),'https://detail.1688.com/offer/123.html');
    for(const url of ['javascript:alert(1)','https://detail.1688.com.evil.test/offer/123.html','https://user:pass@detail.1688.com/offer/123.html','http://detail.1688.com/offer/123.html'])assert.equal(pure.safeSource(url),'');
});
test('all user/source strings are escaped in tables and diagnostics',()=>{
    const html=pure.productRows([{offer_id:'<img src=x onerror=alert(1)>',source_note:'<script>bad</script>',source_url:'javascript:bad'}])+pure.diagnostics([{message:'<script>bad</script>',evidence:{value:'<img>'}}]);
    assert.doesNotMatch(html,/<script>|<img /);assert.match(html,/&lt;script&gt;/);assert.match(html,/&lt;img/);
});
test('latest metrics observation prevents overlapping windows from being double-counted',()=>{
    const items=[{kind:'metrics',fetched_at:'2026-10-01',date_from:'2026-09-01',data:{items:[{day:'2026-09-01',metrics:{ordered_units:1}}]}},{kind:'metrics',fetched_at:'2026-10-02',date_from:'2026-09-01',endpoint:'/v1/analytics/data',data:{items:[{day:'2026-09-01',metrics:{ordered_units:2}}]}},{kind:'health',data:{items:[{day:'fake'}]}}];
    assert.equal(pure.metricObservations(items).length,1);assert.equal(pure.metricObservations(items)[0].metrics.ordered_units,2);
    const html=pure.snapshotRows(pure.metricObservations(items));assert.match(html,/\/v1\/analytics\/data/);assert.match(html,/暂无数据/);
});
test('query rows keep users distinct from ad clicks and preserve period',()=>{
    const rows=pure.queryObservations({status:'available',date_from:'2026-09-01',date_to:'2026-09-07',items:[{query:'кастрюля чугунная',clicks:99,unique_search_users:0,position:2.1}]});
    const html=pure.queryRows(rows);assert.match(html,/2026-09-01 至 2026-09-07/);assert.doesNotMatch(html,/>99</);assert.match(html,/>0</);assert.match(html,/暂无数据/);
});
test('mount only reads cached workbench endpoints, never activates or spends ads',async()=>{
    const h=harness();await h.win.OperationsCenter.mount(h.host);
    assert.equal(h.calls.length,6);assert.ok(h.calls.every(c=>!c.options.method));
    assert.ok(h.calls.every(c=>c.url.startsWith('/api/operations/')));assert.ok(h.calls.every(c=>!c.url.includes('/campaigns')));
    assert.match(h.host.innerHTML,/不会自动修改链接、开启广告/);assert.match(h.regions.get('products').innerHTML,/xzj\.jp\.10\.8\.1/);
    h.win.OperationsCenter.unmount();assert.equal(h.handlers.size,0);assert.equal(h.timers.size,0);
});
test('late results from old shop cannot replace new shop records',async()=>{
    const waiting=deferred();let hold=false;
    const h=harness(async url=>{if(hold && url.includes('/products?shop=a'))return waiting.promise;if(url.includes('/products?shop=b'))return{items:[{offer_id:'B-only'}],total:1};return fixtures(url)});
    await h.win.OperationsCenter.mount(h.host);hold=true;const old=h.win.OperationsCenter.refresh();
    h.change({opsInput:'shop'},{value:'b'});await new Promise(resolve=>setImmediate(resolve));
    waiting.resolve({items:[{offer_id:'A-stale'}],total:1});await old;await new Promise(resolve=>setImmediate(resolve));
    assert.match(h.regions.get('products').innerHTML,/B-only/);assert.doesNotMatch(h.regions.get('products').innerHTML,/A-stale/);h.win.OperationsCenter.unmount();
});
test('late search results cannot replace newer same-shop search',async()=>{
    const first=deferred();let hold=false;
    const h=harness(async url=>{if(hold && url.includes('/products?') && url.includes('q=old'))return first.promise;if(url.includes('q=new'))return{items:[{offer_id:'new-result'}],total:1};return fixtures(url)});
    await h.win.OperationsCenter.mount(h.host);hold=true;const input={value:'old'};h.inputs.set('[data-ops-input="query"]',input);const old=h.click('search');input.value='new';await h.click('search');first.resolve({items:[{offer_id:'stale-result'}],total:1});await old;
    assert.match(h.regions.get('products').innerHTML,/new-result/);assert.doesNotMatch(h.regions.get('products').innerHTML,/stale-result/);h.win.OperationsCenter.unmount();
});
test('advertising secrets are cleared immediately and are not persisted in UI state',async()=>{
    const waiting=deferred();const h=harness(async(url,options)=>url==='/api/operations/advertising/authorize'?waiting.promise:fixtures(url));await h.win.OperationsCenter.mount(h.host);
    const id={value:'service-account'},secret={value:'secret-value'},form={dataset:{opsForm:'authorization'},querySelector:selector=>selector.includes('client_secret')?secret:id};
    const submit=h.handlers.get('submit')({target:form,preventDefault(){}});
    assert.equal(id.value,'');assert.equal(secret.value,'');assert.equal(source.includes('localStorage'),false);assert.equal(source.includes('console.'),false);
    waiting.resolve({ready:true});await submit;assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);assert.doesNotMatch(h.host.innerHTML,/secret-value/);h.win.OperationsCenter.unmount();
});
test('insecure transport blocks credential submit entirely',async()=>{
    const h=harness(url=>url==='/api/operations/config'?{shops:[{id:'a'}],credential_security:{can_submit_credentials:false,reason:'请使用 HTTPS'}}:fixtures(url));await h.win.OperationsCenter.mount(h.host);await h.click('advertising');
    assert.match(h.regions.get('advertising').innerHTML,/请使用 HTTPS/);
    await h.handlers.get('submit')({target:{dataset:{opsForm:'authorization'}},preventDefault(){}});
    assert.equal(h.calls.filter(c=>c.options.method==='POST').length,0);h.win.OperationsCenter.unmount();
});
test('read-only sync is explicit and snapshots traffic selection',async()=>{
    const h=harness();await h.win.OperationsCenter.mount(h.host);h.change({opsInput:'include-traffic'},{checked:false});await h.click('sync',{offer:'one'});
    const call=h.calls.find(c=>c.url==='/api/operations/sync');assert.deepEqual(JSON.parse(call.options.body),{shop:'a',offer_id:'one',days:7,include_traffic:false});h.win.OperationsCenter.unmount();
});
test('official uppercase report states are normalized and uncertain submissions cannot duplicate',()=>{
    assert.equal(pure.status('NOT_STARTED'),'处理中');assert.equal(pure.terminal('IN_PROGRESS'),false);assert.equal(pure.terminal('OK'),true);assert.equal(pure.terminal('ERROR'),true);assert.equal(pure.terminal('SUBMISSION_UNCERTAIN'),true);
    assert.equal(pure.terminal('partial'),true);assert.equal(pure.canCreateReport({status:'SUBMISSION_UNCERTAIN'}),false);assert.equal(pure.canCreateReport({status:'OK'}),true);
});
test('official raw report headers are separate columns, with derived metrics clearly marked',()=>{
    const html=pure.reportRows([{campaign_id:'C1',file:'safe.csv',row_number:1,raw:{Показы:'10',Клики:'0'},derived:{ctr:0}}]);
    assert.match(html,/<th>Показы<\/th>/);assert.match(html,/<th>Клики<\/th>/);assert.match(html,/<th>计算 · ctr<\/th>/);assert.doesNotMatch(html,/<th>raw<\/th>/);
});
function advertisingFixture(url) {
    if(url.startsWith('/api/operations/advertising/status'))return{ready:true,status:'connected'};
    if(url.startsWith('/api/operations/advertising/campaigns'))return{items:[{id:'C1',title:'只读测试活动'}]};
    return fixtures(url);
}
async function beginReport(h) {
    await h.win.OperationsCenter.mount(h.host);await h.click('advertising');await h.click('campaigns');h.change({opsCampaign:'C1'},{checked:true});
    const form={dataset:{opsForm:'report'},querySelector:selector=>({value:selector.includes('date_from')?'2026-10-01':'2026-10-07'})};
    await h.handlers.get('submit')({target:form,preventDefault(){}});
}
test('report polling is bounded to 30 reads and unmount clears timers',async()=>{
    const h=harness(url=>url==='/api/operations/advertising/reports'?{uuid:'U1',status:'NOT_STARTED'}:url.startsWith('/api/operations/advertising/reports/U1')?{status:'NOT_STARTED'}:advertisingFixture(url));
    await beginReport(h);
    for(let i=0;i<35;i++){const entry=[...h.timers].find(([,t])=>t.delay===10000);if(!entry)break;h.timers.delete(entry[0]);entry[1].fn();await new Promise(resolve=>setImmediate(resolve))}
    assert.equal(h.calls.filter(c=>c.url.startsWith('/api/operations/advertising/reports/U1')).length,30);assert.match(h.regions.get('advertising').innerHTML,/已停止自动等待/);
    assert.equal([...h.timers.values()].filter(t=>t.delay===10000).length,0);assert.equal(h.calls.filter(c=>c.options.method==='POST').length,1);
    h.win.OperationsCenter.unmount();assert.equal(h.timers.size,0);
});
test('submission uncertainty stops polling, exposes recovery instructions and blocks another create',async()=>{
    const h=harness(url=>url==='/api/operations/advertising/reports'?{uuid:'U2',status:'SUBMISSION_UNCERTAIN'}:url.startsWith('/api/operations/advertising/reports/U2')?{status:'SUBMISSION_UNCERTAIN',recovery_instructions:'请在官方后台核对 UUID，不要重复提交。'}:advertisingFixture(url));
    await beginReport(h);assert.match(h.regions.get('advertising').innerHTML,/请在官方后台核对 UUID/);assert.doesNotMatch(h.regions.get('advertising').innerHTML,/Ozon 正在生成报表/);
    assert.equal([...h.timers.values()].filter(t=>t.delay===10000).length,0);h.win.OperationsCenter.unmount();
});
test('navigation unmount aborts in-flight fetch and cannot repaint old container',async()=>{
    const pending=deferred();const h=harness(url=>url==='/api/operations/config'?pending.promise:fixtures(url));
    const mount=h.win.OperationsCenter.mount(h.host);const request=h.calls[0];h.win.OperationsCenter.unmount();assert.equal(request.options.signal.aborted,true);
    pending.resolve(fixtures('/api/operations/config'));await mount;assert.equal(h.calls.length,1);assert.equal(h.timers.size,0);
});
test('campaign paging retains cross-page selections and explicit refresh resets selection',async()=>{
    const h=harness(url=>url.startsWith('/api/operations/advertising/campaigns')?url.includes('page=2')?{items:[{id:'C2',title:'第二页活动'}],page:2,page_size:50,has_more:false}:{items:[{id:'C1',title:'第一页活动'}],page:1,page_size:50,has_more:true}:advertisingFixture(url));
    await h.win.OperationsCenter.mount(h.host);await h.click('advertising');await h.click('campaigns');h.change({opsCampaign:'C1'},{checked:true});await h.click('campaigns-next');
    assert.match(h.regions.get('advertising').innerHTML,/第二页活动/);assert.match(h.regions.get('advertising').innerHTML,/跨页已选 1 \/ 10/);assert.match(h.regions.get('advertising').innerHTML,/data-ops-action="campaigns-next"  disabled/);
    h.change({opsCampaign:'C2'},{checked:true});await h.click('campaigns-previous');assert.match(h.regions.get('advertising').innerHTML,/data-ops-campaign="C1"[^>]*checked/);assert.match(h.regions.get('advertising').innerHTML,/跨页已选 2 \/ 10/);
    await h.click('campaigns');assert.match(h.regions.get('advertising').innerHTML,/跨页已选 0 \/ 10/);assert.equal(h.calls.filter(c=>c.url.includes('/campaigns?')).length,4);h.win.OperationsCenter.unmount();assert.equal(h.host.classList.contains('operations-center'),false);
});
test('human diagnosis headings and health do not imply selected warehouse inventory',()=>{
    const diagnosis=pure.diagnostics([{code:'query_sales_observed',message:'关键词有 1 笔订单'}]);assert.match(diagnosis,/搜索词已产生订单/);assert.doesNotMatch(diagnosis,/<h4>query_sales_observed/);
    const html=pure.healthHtml([{kind:'health',status:'available',fetched_at:'2026-10-08T00:00:00Z',data:{moderate_status:'approved',has_stock:true,price:123,currency:'RUB'}}]);
    assert.match(html,/审核状态/);assert.match(html,/已通过/);assert.match(html,/平台返回有库存/);assert.match(html,/123 RUB/);assert.match(html,/不代表所选仓库的库存数量/);
    const unknown=pure.healthHtml([]);assert.match(unknown,/待核实/);assert.doesNotMatch(unknown,/平台返回无库存/);
});
test('report metadata columns only exist when returned; saved unresolved reports block create',()=>{
    const html=pure.reportRows([{file:'10.csv',raw:{Показы:10,Клики:2},derived:{ctr:.2}}]);assert.match(html,/报表文件/);assert.doesNotMatch(html,/活动 ID|报表行号|campaign_id|row_number/);
    assert.equal(pure.historyBlocksReport([{state:'NOT_STARTED'}]),true);assert.equal(pure.historyBlocksReport([{state:'SUBMISSION_UNCERTAIN'}]),true);assert.equal(pure.historyBlocksReport([{state:'OK'}]),false);
});
test('terminal report refreshes saved history after polling instead of showing an old pending state',async()=>{
    let ready=false;
    const h=harness(url=>url==='/api/operations/advertising/reports'?{uuid:'U3',status:'NOT_STARTED'}:url.startsWith('/api/operations/advertising/reports/U3')?(ready=true,{status:'OK',rows:[{file:'1.csv',raw:{Показы:12}}]}):url.startsWith('/api/operations/advertising/reports?')?{items:ready?[{uuid:'U3',status:'OK',date_from:'2026-10-01',date_to:'2026-10-07'}]:[]}:advertisingFixture(url));
    await beginReport(h);assert.match(h.regions.get('advertising').innerHTML,/报表文件/);assert.doesNotMatch(h.regions.get('advertising').innerHTML,/当前店铺有未完成/);assert.match(h.regions.get('advertising').innerHTML,/2026-10-01 至 2026-10-07/);h.win.OperationsCenter.unmount();
});
