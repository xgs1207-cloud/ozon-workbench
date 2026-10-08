'use strict';

// Executes production UI logic with memory-only APIs. No network or Ozon writes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../web/listing-publication.js'), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));
const deferred = () => {let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {resolve,reject,promise}};

function harness(){
    const events=[],calls=[];
    const context=vm.createContext({Map,Set,URL,URLSearchParams,Intl,JSON,Number,String,Error,
        state:{product:'P000001',view:'product',shop:'shop-a',guided:{source:{source_url:'https://detail.1688.com/offer/123456.html?share_token=private'}},skus:{skus:[{sku_id:'S1',sku_name:'粉色',listed:true},{sku_id:'S2',sku_name:'绿色',listed:true}]}},
        document:{addEventListener:(type,callback)=>events.push({type,callback}),querySelector:()=>null},
        benchShop:()=>context.state.shop,benchDocument:()=>({offer_ids:{offers:{S1:'xzj.jp.10.8.1',S2:'xzj.jp.10.8.2'}}}),
        selectedReadStore:()=>({id:context.state.shop,display_name:'测试店铺'}),flowStep:()=>context.step||'none',flowSetStep:step=>{context.step=step},
        renderProduct:()=>{},captureProductFields:()=>{},
        esc:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
        json:(method,body)=>({method,body:JSON.stringify(body)}),notice:()=>{},confirm:()=>true,
        api:async(url,options)=>{calls.push({url,options});throw Error('Unconfigured offline API fixture')}});
    vm.runInContext(source,context);
    const fixture={context,events,calls,
        config:()=>context.publicationEntry(),
        warehouses:(shop='shop-a',items=[{warehouse_id:'W1',name:'中国直发仓',eligible:true}])=>vm.runInContext(`listingPublication.warehouses.set(${JSON.stringify(shop)},{loaded:true,loading:false,data:{shop:${JSON.stringify(shop)},complete:true,items:${JSON.stringify(items)}}})`,context),
        ready:()=>{const entry=context.publicationEntry();entry.loaded=true;entry.data={...entry.data,saved:true,warehouse_id:'W1',warehouse_name:'中国直发仓'};entry.values.warehouse_id='W1';entry.selectionSignature=context.publicationSelectionSignature();fixture.warehouses();return entry},
        click:async(action,extras={})=>{const host={dataset:{publicationProduct:'P000001',publicationShop:'shop-a'}},button={dataset:{publicationAction:action,...extras},isConnected:false,closest:selector=>selector==='tr'?{querySelector:()=>({checked:extras.checked===true})}:host};await events.find(row=>row.type==='click').callback({target:{closest:()=>button}})}
    };return fixture;
}

test('configuration defaults to 100 but does not select a first warehouse or send requests',()=>{
    const h=harness();h.warehouses();
    assert.equal(h.config().values.stock,'100');assert.equal(h.config().values.warehouse_id,'');
    const html=h.context.publicationConfigHtml();assert.match(html,/请选择仓库/);assert.doesNotMatch(html,/value="W1" selected/);
    assert.match(html,/货源链接/);assert.match(html,/readonly value="https:\/\/detail\.1688\.com\/offer\/123456\.html"/);
    assert.equal(html.includes('share_token'),false);assert.equal(h.calls.length,0);
});

test('supplier link allows only canonical collected 1688 offer URLs',()=>{
    const h=harness();
    assert.equal(h.context.publicationSourceUrl('https://detail.1688.com/offer/123.html?q=secret'),'https://detail.1688.com/offer/123.html');
    for(const value of ['javascript:alert(1)','https://detail.1688.com.evil.test/offer/123.html','https://user:pass@detail.1688.com/offer/123.html','https://1688.com/offer/123.html'])assert.equal(h.context.publicationSourceUrl(value),'');
});

test('sourcing note displays the complete selected specification once and read-only',()=>{
    const h=harness(),entry=h.ready();entry.data.source_note_auto=true;
    entry.values.source_note='红色/内白 24cm/3.5L';
    const html=h.context.publicationConfigHtml();
    assert.match(html,/data-publication-input="source_note" readonly/);
    assert.match(html,/红色\/内白 24cm\/3.5L/);
    assert.equal((html.match(/data-publication-input="source_note"/g)||[]).length,1);
    assert.match(html,/每个货号保存自己的完整规格/);
});

test('publication blocks missing, dirty, stale, ineligible and cross-shop configurations',()=>{
    const h=harness();assert.throws(()=>h.context.publicationRequireReady(),/读取仓库配置/);
    const entry=h.ready();assert.deepEqual(plain(h.context.publicationRequireReady().items).map(row=>row.stock),[100,100]);
    entry.dirty=true;assert.throws(()=>h.context.publicationRequireReady(),/保存货源与仓库/);entry.dirty=false;
    entry.selectionSignature='old';assert.throws(()=>h.context.publicationRequireReady(),/规格或货号已改变/);entry.selectionSignature=h.context.publicationSelectionSignature();
    h.warehouses('shop-a',[{warehouse_id:'W1',name:'禁用仓',eligible:false}]);assert.throws(()=>h.context.publicationRequireReady(),/仓库不可用/);
    h.context.state.shop='shop-b';assert.throws(()=>h.context.publicationRequireReady(),/读取仓库配置/);
    assert.equal(h.calls.length,0);
});

test('publication requires a selected SKU with a reserved offer ID',()=>{
    const h=harness();h.ready();
    h.context.benchDocument=()=>({offer_ids:{offers:{S1:'one'}}});h.config().selectionSignature=h.context.publicationSelectionSignature();
    assert.throws(()=>h.context.publicationRequireReady(),/分配每个规格的货号/);
    h.context.state.skus.skus=[];h.config().selectionSignature=h.context.publicationSelectionSignature();
    assert.throws(()=>h.context.publicationRequireReady(),/选择上架规格/);
});

test('integer stock validation permits zero and rejects unsafe/non-integer quantities',()=>{
    const h=harness();assert.equal(h.context.publicationStock('0'),0);assert.equal(h.context.publicationStock('100'),100);
    for(const stock of ['',-1,'1.5','1e3','Infinity','1000001','9007199254740992'])assert.throws(()=>h.context.publicationStock(stock),/库存必须/);
});

test('cached warehouse GET never refreshes externally or auto-selects a warehouse',async()=>{
    const h=harness();h.context.api=async(url,options)=>{h.calls.push({url,options});return {ok:true,shop:'shop-a',items:[{warehouse_id:'W1',eligible:true}],complete:true}};
    await h.context.publicationLoadWarehouses('shop-a');await h.context.publicationLoadWarehouses('shop-a');
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].options,undefined);assert.match(h.calls[0].url,/warehouses\?shop=shop-a$/);
    assert.equal(h.config().values.warehouse_id,'');
});

test('late config reads preserve local edits and stay isolated during shop navigation',async()=>{
    const h=harness(),waiting=deferred();h.context.api=()=>waiting.promise;
    const pending=h.context.publicationLoadConfig('P000001','shop-a');
    h.config().dirty=true;h.config().values.stock='125';h.context.state.shop='shop-b';
    waiting.resolve({config:{shop:'shop-a',stock:100,warehouse_id:'W1',saved:true}});await pending;
    const old=h.context.publicationEntry('P000001','shop-a');assert.equal(old.values.stock,'125');assert.equal(old.data.stock,100);
    assert.equal(h.config().values.stock,'100');assert.equal(h.config().values.warehouse_id,'');
});

test('config failures do not trigger an automatic retry loop or permit submission',async()=>{
    const h=harness();h.context.api=async()=>{h.calls.push('GET');throw Error('读取失败')};
    await h.context.publicationLoadConfig();await h.context.publicationLoadConfig();
    assert.equal(h.calls.length,1);assert.throws(()=>h.context.publicationRequireReady(),/仓库配置读取失败/);
});

test('save sends supplier note and planned stock locally without source URL or inventory call',async()=>{
    const h=harness();const entry=h.ready();entry.dirty=true;entry.values.stock='0';entry.values.source_note='仅内部';
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {config:{shop:'shop-a',warehouse_id:'W1',stock:0,stock_by_sku:{},source_note:'仅内部',saved:true}}};
    await h.context.publicationSaveConfig();const payload=JSON.parse(h.calls[0].options.body);
    assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/publication-config$/);assert.equal(h.calls[0].options.method,'PUT');
    assert.equal(payload.stock,0);assert.equal(payload.source_note,'仅内部');assert.equal(Object.hasOwn(payload,'source_url'),false);assert.equal(entry.dirty,false);
});

test('new edits made during draft save remain unsaved after the response',async()=>{
    const h=harness(),waiting=deferred(),entry=h.ready();entry.dirty=true;
    h.context.api=()=>waiting.promise;const pending=h.context.publicationSaveConfig();
    entry.values.stock='250';entry.revision++;
    waiting.resolve({config:{shop:'shop-a',warehouse_id:'W1',stock:100,saved:true}});const result=await pending;
    assert.equal(result.saved,false);assert.equal(entry.values.stock,'250');assert.equal(entry.data.stock,100);assert.equal(entry.dirty,true);
});

test('invalid planned stock and excessive notes never send a save request',async()=>{
    const h=harness(),entry=h.ready();entry.values.stock='-1';await assert.rejects(h.context.publicationSaveConfig(),/库存必须/);
    entry.values.stock='100';entry.values.source_note='长'.repeat(501);await assert.rejects(h.context.publicationSaveConfig(),/最多 500/);
    assert.equal(h.calls.length,0);
});

test('saving drops old unselected SKU overrides rather than assigning them to the new selection',async()=>{
    const h=harness(),entry=h.ready();entry.values.stock_by_sku={S1:'80',OLD:'999'};
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {config:{shop:'shop-a',warehouse_id:'W1',stock:100,stock_by_sku:{S1:80},saved:true}}};
    await h.context.publicationSaveConfig();assert.deepEqual(JSON.parse(h.calls[0].options.body).stock_by_sku,{S1:80});
});

test('a failed latest warehouse refresh blocks submission even if an older cached warehouse exists',()=>{
    const h=harness();h.ready();vm.runInContext("listingPublication.warehouses.get('shop-a').error='权限不足'",h.context);
    assert.throws(()=>h.context.publicationRequireReady(),/最近一次仓库读取失败/);
});

test('background refresh restores an editable publication input and its text selection',()=>{
    const h=harness();h.ready();h.context.step='card';const owner={dataset:{publicationProduct:'P000001',publicationShop:'shop-a'}};
    const focused={dataset:{publicationInput:'source_note'},selectionStart:2,selectionEnd:4,closest:()=>owner};
    const next={dataset:{publicationInput:'source_note'},closest:()=>owner,focus:()=>{next.focused=true},setSelectionRange:(start,end)=>{next.selection=[start,end]}};
    h.context.document.activeElement=focused;h.context.document.querySelectorAll=()=>[next];
    h.context.publicationRedraw('P000001','shop-a');assert.equal(next.focused,true);assert.deepEqual(next.selection,[2,4]);assert.equal(h.calls.length,0);
});

test('history searches and pagination are scoped to the selected shop and chosen range',async()=>{
    const h=harness(),entry=h.context.publicationHistoryEntry();entry.scope='shop';entry.query='xzj.jp.10.8';entry.offset=30;
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {items:[],total:45,next_offset:null}};
    await h.context.publicationLoadHistory('P000001','shop-a',true);
    const url=new URL('http://fixture.test'+h.calls[0].url);assert.equal(url.pathname,'/api/workbench/publications');
    assert.equal(url.searchParams.get('shop'),'shop-a');assert.equal(url.searchParams.get('q'),'xzj.jp.10.8');assert.equal(url.searchParams.get('offset'),'30');assert.equal(h.calls[0].options,undefined);
    entry.scope='product';await h.context.publicationLoadHistory('P000001','shop-a',true);assert.match(h.calls[1].url,/products\/P000001\/listing-publications\?/);
});

test('history distinguishes desired stock, confirmed API result and verified quantity',()=>{
    const h=harness();assert.equal(h.context.publicationVerifiedStock({stock:100,stock_status:'updated'}),'API 已确认同步，数量待回读');
    assert.equal(h.context.publicationVerifiedStock({stock:100,stock_readback:{present:80},stock_status:'readback_mismatch'}),'在库 80 件（可售数量待回读）');
    assert.equal(h.context.publicationVerifiedStock({stock:100,stock_readback:{present:105,reserved:5,free_stock:100},stock_status:'updated'}),'可售 100 件，预留 5 件（已回读）');
    const entry=h.context.publicationHistoryEntry();entry.data={total:1,items:[{shop:'shop-a',offer_id:'xzj.jp.10.8.1',product_id:'P000001',stock:100,stock_status:'readback_mismatch',stock_readback:{present:85,reserved:5,free_stock:80},task_id:'task123',ozon_product_id:'987',attempts:[{kind:'stock',state:'updated',started_at:'2026-10-08T08:00:00Z',details:{result:'updated'}}]}]};
    const html=h.context.publicationHistoryHtml();assert.match(html,/计划 100 件/);assert.match(html,/可售 80 件，预留 5 件（已回读）/);assert.match(html,/回读库存不一致/);assert.match(html,/task123/);assert.match(html,/987/);assert.match(html,/接口记录/);
});

test('unknown stock retries require an explicit checked acknowledgment before any request',async()=>{
    const h=harness();await h.click('continue-stock',{targetProduct:'P000001',targetShop:'shop-a',stockUnknown:'true',checked:false});
    assert.equal(h.calls.length,0);
});

test('continue-stock never re-imports and uses the record product snapshot after navigation',async()=>{
    const h=harness();h.context.state.product='P000002';h.context.state.shop='shop-b';
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {status:'stock_pending',pending:true,ok:true,items:[]}};
    await h.context.publicationContinueStock('P000001','shop-a',false);
    assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/products\/P000001\/publications\/continue$/);
    assert.deepEqual(JSON.parse(h.calls[0].options.body),{shop:'shop-a',confirm:'UPDATE_STOCK',retry_unknown:false});
    assert.equal(h.calls.some(call=>call.url.includes('submit')||call.url.includes('import')),false);
});

test('submitted configuration is frozen in the editor and cannot be saved or resubmitted',async()=>{
    const h=harness(),entry=h.ready();entry.data.frozen=true;
    const html=h.context.publicationConfigHtml();assert.match(html,/提交配置已锁定/);assert.match(html,/data-publication-input="stock" readonly/);assert.match(html,/data-publication-input="warehouse_id"[^>]*disabled/);assert.equal(html.includes('data-publication-action="save-config"'),false);
    await assert.rejects(h.context.publicationSaveConfig(),/已经锁定/);assert.throws(()=>h.context.publicationRequireReady(),/已有提交记录/);assert.equal(h.calls.length,0);
});

test('post-submit reload replaces late edits with the frozen intent rather than presenting them as published',async()=>{
    const h=harness(),entry=h.ready();entry.dirty=true;entry.values.stock='250';
    h.context.api=async()=>({config:{shop:'shop-a',warehouse_id:'W1',stock:100,saved:true,frozen:true}});
    await h.context.publicationLoadConfig('P000001','shop-a',true);assert.equal(entry.values.stock,'100');assert.equal(entry.dirty,false);assert.equal(entry.data.frozen,true);
});

test('legacy imports without inventory configuration never offer a continue stock button',()=>{
    const h=harness(),entry=h.context.publicationHistoryEntry();entry.data={total:1,items:[{product_id:'P000001',shop:'shop-a',offer_id:'old',stock_status:'not_configured'}]};
    const html=h.context.publicationHistoryHtml();assert.match(html,/旧记录未配置库存/);assert.equal(html.includes('data-publication-action="continue-stock"'),false);
});

test('an empty complete inventory readback is absence, not an invented zero stock',()=>{
    const h=harness(),readback={absence:true,observed:false,status:'no_entry',query_complete:true,warehouse_id:'W1'};
    const text=h.context.publicationVerifiedStock({stock:100,stock_status:'updated',stock_readback:readback});
    assert.equal(text,'该仓暂无库存记录（已回读）');assert.equal(text.includes('0 件'),false);
});

test('updated stock without a matching readback retains a readback refresh action',()=>{
    const h=harness(),entry=h.context.publicationHistoryEntry();
    entry.data={total:1,items:[{product_id:'P000001',shop:'shop-a',offer_id:'one',stock:100,stock_status:'updated',stock_readback_matches:false}]};
    const html=h.context.publicationHistoryHtml();assert.match(html,/刷新库存回读/);assert.match(html,/data-stock-readback="true"/);assert.equal(html.includes('已完成库存同步'),false);
    assert.equal(h.context.publicationStockComplete({stock_status:'updated'}),false);
});

test('readback-confirmed status is complete only with an explicit matching inventory readback',()=>{
    const h=harness(),entry=h.context.publicationHistoryEntry();
    for(const state of ['updated','readback_confirmed']){
        assert.equal(h.context.publicationStockComplete({stock_status:state,stock_readback_matches:true}),true);
        assert.equal(h.context.publicationStockComplete({stock_status:state,stock_readback_matches:false}),false);
    }
    entry.data={total:1,items:[{product_id:'P000001',shop:'shop-a',offer_id:'one',stock_status:'readback_confirmed',stock_readback_matches:true,stock_readback:{present:105,reserved:5,free_stock:100}}]};
    const html=h.context.publicationHistoryHtml();assert.match(html,/库存回读已匹配/);assert.match(html,/已完成库存同步/);assert.equal(html.includes('data-publication-action="continue-stock"'),false);
    assert.match(h.context.publicationStatus('stock','readback_confirmed'),/实际库存回读已核对/);
});

test('acknowledgement and price submission statuses are Chinese and not falsely approved',()=>{
    const h=harness();assert.match(h.context.publicationStatus('stock','stock_acknowledged'),/库存接口已确认，待实际回读/);
    for(const state of ['price_sent','stock_sent','sale','active']){const text=h.context.publicationStatus('product',state);assert.match(text,/[\u3400-\u9fff]/);assert.equal(text.includes('审核通过'),false)}
});

test('readback refresh confirmation states product-wide scope and never uses an import endpoint',async()=>{
    const h=harness();let confirmation='';h.context.confirm=text=>{confirmation=text;return true};
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {status:'stock_acknowledged',pending:true,ok:false,items:[]}};
    await h.click('continue-stock',{targetProduct:'P000001',targetShop:'shop-a',stockUnknown:'false',stockReadback:'true'});
    assert.match(confirmation,/所有已保存货号/);assert.match(confirmation,/只刷新实际库存回读，不重复写入/);assert.match(confirmation,/其他待补库存/);assert.match(confirmation,/不会重新导入商品/);
    assert.match(h.calls[0].url,/publications\/continue$/);assert.equal(h.calls.some(call=>call.url.includes('import')||call.url.includes('submit')),false);
});
