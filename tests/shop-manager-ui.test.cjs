'use strict';
const assert=require('node:assert/strict'),test=require('node:test'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../web/shop-manager.js'),'utf8'),pure=require('../web/shop-manager.js');
const wait=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve}};
const fixtureShop=(id='a',overrides={})=>({id,display_name:'店铺 '+id,enabled:true,credentials_ready:true,is_default:id==='a',default_currency_code:'CNY',connection_status:'connected',advertising:{configured:false,connection_status:'not_configured'},...overrides});
const classList=()=>{const values=new Set();return {add:v=>values.add(v),remove:v=>values.delete(v),toggle:(v,on)=>on?values.add(v):values.delete(v),contains:v=>values.has(v)}};
function harness(respond){
    const calls=[],timers=new Map(),regions=new Map(['list','editor','editor-status','security'].map(name=>[name,{innerHTML:''}])),fields=new Map(),hostEvents=new Map(),globalEvents=new Map(),browserEvents=new Map();let timeId=0;
    const notice={textContent:'',hidden:true,classList:classList()},fieldset={disabled:false},count={textContent:''},submit={textContent:''};
    for(const key of ['seller_client_id','seller_api_key','advertising_client_id','advertising_client_secret','display_name','shop_id'])fields.set(key,{value:'',focus(){this.focused=true}});
    const host={innerHTML:'',isConnected:true,classList:classList(),addEventListener:(type,fn)=>hostEvents.set(type,fn),removeEventListener:type=>hostEvents.delete(type),querySelector(selector){if(selector==='[data-shop-notice]')return notice;if(selector==='[data-shop-fields]')return fieldset;if(selector==='[data-shop-count]')return count;if(selector==='button[type="submit"]')return submit;const region=/data-shop-region="([^"]+)"/.exec(selector)?.[1];if(region)return regions.get(region);const name=/name="([^"]+)"/.exec(selector)?.[1];if(name)return fields.get(name);return null}};
    const document={body:{classList:classList()},querySelector:()=>host,addEventListener:(type,fn)=>globalEvents.set(type,fn),removeEventListener:type=>globalEvents.delete(type)};
    const win={document,addEventListener:(type,fn)=>browserEvents.set(type,fn),removeEventListener:type=>browserEvents.delete(type),fetch:async(url,options)=>{calls.push({url,options});const data=respond?await respond(url,options):{ok:true,shops:[fixtureShop(),fixtureShop('b')],authorization_context:{can_submit_credentials:true}};return data?.http?{ok:false,status:data.http,json:async()=>data.body}:{ok:true,status:200,json:async()=>data}}};
    vm.runInContext(source,vm.createContext({window:win,module:{exports:{}},Map,Set,JSON,Object,Array,Number,String,Error,AbortController,setTimeout:(fn,delay)=>{const id=++timeId;timers.set(id,{fn,delay});return id},clearTimeout:id=>timers.delete(id)}));
    return {win,host,document,regions,fields,notice,fieldset,hostEvents,globalEvents,browserEvents,calls,timers,
        click:(action,extra={})=>hostEvents.get('click')({target:{closest:()=>({dataset:{shopAction:action,...extra},disabled:false})}}),
        input:(name,value,type='text')=>{if(fields.has(name))fields.get(name).value=value;hostEvents.get('input')({target:{name,value,type,checked:value,dataset:{}}})},
        submit:()=>hostEvents.get('submit')({target:{matches:()=>true,querySelector:selector=>fields.get(/name="([^"]+)"/.exec(selector)?.[1])},preventDefault(){}})};
}
const draft={shop_id:'a',display_name:'原店',default_currency_code:'CNY',make_default:false};
test('create requires Seller pair and emits independently optional Performance pair',()=>{
    assert.throws(()=>pure.buildPayload({mode:'create',draft,credentials:{}}),/新增店铺需要/);
    const body=pure.buildPayload({mode:'create',draft,credentials:{seller_client_id:'123',seller_api_key:'seller-secret',advertising_client_id:'service',advertising_client_secret:'ad-secret'}});
    assert.equal(body.mode,'create');assert.equal(body.seller_client_id,'123');assert.equal(body.advertising_client_secret,'ad-secret');assert.equal('make_default' in body,false);
    assert.throws(()=>pure.buildPayload({mode:'create',draft,credentials:{seller_client_id:'123',seller_api_key:'key'},existingIds:['a']}),/标识已存在/);
});
test('update blank credentials are omitted and immutable ID comes from selected record',()=>{
    const body=pure.buildPayload({mode:'update',shop:fixtureShop('a'),draft:{...draft,shop_id:'changed'},credentials:{seller_client_id:'',seller_api_key:' ',advertising_client_id:'',advertising_client_secret:''}});
    assert.deepEqual(body,{mode:'update',shop_id:'a',display_name:'原店',default_currency_code:'CNY'});
    for(const pair of [{seller_client_id:'123'},{seller_api_key:'secret'},{advertising_client_id:'service'},{advertising_client_secret:'secret'}])assert.throws(()=>pure.buildPayload({mode:'update',shop:fixtureShop(),draft,credentials:pair}),/必须一起填写/);
});
test('partial result explicitly preserves existing ads and fine 422 errors never echo request input',()=>{
    const message=pure.resultMessage({partial:true,advertising_saved:false,warning_codes:['advertising_save_failed'],message:'secret-should-not-show'});assert.equal(message.warning,true);assert.match(message.text,/店铺资料已保存/);assert.match(message.text,/原有广告授权保持不变/);assert.doesNotMatch(message.text,/secret-should/);
    const error=pure.errorMessage(422,{detail:[{loc:['body','advertising_client_secret'],type:'too_short',msg:'private-secret',input:'private-secret'}]});assert.match(error,/广告 client_secret/);assert.doesNotMatch(error,/private-secret/);
});
test('partial default or cache failure does not falsely claim successful ads were unchanged',()=>{
    const message=pure.resultMessage({partial:true,advertising_saved:true,default_saved:false,warning_codes:['default_save_failed','cache_refresh_failed'],warning:'untrusted-private-input'});
    assert.equal(message.warning,true);assert.match(message.text,/广告授权已保存/);assert.match(message.text,/未能设置默认店铺/);assert.match(message.text,/缓存尚未刷新/);assert.doesNotMatch(message.text,/广告授权未更新|untrusted-private-input/);
});
test('mount only reads cached management data and never reveals saved secrets',async()=>{
    const h=harness();await h.win.ShopManager.mount(h.host);assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/api/workbench/shop-management');assert.equal(h.calls[0].options.method,undefined);
    assert.match(h.regions.get('editor').innerHTML,/Seller API/);assert.match(h.regions.get('editor').innerHTML,/广告 Performance API/);assert.match(h.regions.get('editor').innerHTML,/name="shop_id" value="a"[^>]*readonly/);assert.match(h.host.innerHTML,/本工作台成员共用/);
    assert.equal(source.includes('localStorage'),false);assert.equal(source.includes('sessionStorage'),false);assert.equal(source.includes('console.'),false);h.win.ShopManager.unmount();
});
test('metadata-only update sends no secrets and invokes saved callback without remount',async()=>{
    let saved=0;const h=harness((url,options)=>options.method==='POST'?{ok:true,shop:fixtureShop('a',{display_name:'更名'}),advertising_saved:false}:{ok:true,shops:[fixtureShop('a',{display_name:'更名'})],authorization_context:{can_submit_credentials:true}});
    await h.win.ShopManager.mount(h.host,{onSaved:()=>saved++});h.input('display_name','更名');h.input('default_currency_code','RUB');h.input('shop_id','spoofed');await h.submit();
    const body=JSON.parse(h.calls.find(c=>c.options.method==='POST').options.body);assert.equal(body.shop_id,'a');assert.equal(body.display_name,'更名');assert.equal(body.default_currency_code,'RUB');assert.equal('seller_api_key' in body,false);assert.equal('advertising_client_secret' in body,false);assert.equal(saved,1);assert.equal(h.win.ShopManager.isBusy(),false);h.win.ShopManager.unmount();
});
test('save snapshots shop, clears credentials immediately, and locks selection and external navigation',async()=>{
    const pending=wait();const h=harness((url,options)=>options.method==='POST'?pending.promise:{ok:true,shops:[fixtureShop(),fixtureShop('b')],authorization_context:{can_submit_credentials:true}});
    await h.win.ShopManager.mount(h.host);h.fields.get('seller_client_id').value='123';h.fields.get('seller_api_key').value='new-private-key';const save=h.submit();assert.equal(h.fields.get('seller_api_key').value,'');assert.equal(h.fields.get('seller_client_id').value,'');assert.equal(h.win.ShopManager.isBusy(),true);assert.equal(h.fieldset.disabled,true);
    await h.click('select',{shopId:'b'});await h.click('new');assert.match(h.regions.get('editor').innerHTML,/name="shop_id" value="a"/);
    let prevented=false,stopped=false;h.globalEvents.get('click')({target:{closest:()=>({dataset:{view:'listing'}})},preventDefault(){prevented=true},stopImmediatePropagation(){stopped=true}});assert.equal(prevented,true);assert.equal(stopped,true);assert.equal(h.win.ShopManager.canLeave(),false);
    pending.resolve({ok:true,shop:fixtureShop('a'),advertising_saved:false});await save;assert.equal(JSON.parse(h.calls.find(c=>c.options.method==='POST').options.body).shop_id,'a');assert.equal(h.win.ShopManager.isBusy(),false);h.win.ShopManager.unmount();
});
test('insecure connection disables form and all writes',async()=>{
    const h=harness(()=>({ok:true,shops:[fixtureShop()],authorization_context:{can_submit_credentials:false,reason:'请使用 HTTPS'}}));await h.win.ShopManager.mount(h.host);assert.equal(h.fieldset.disabled,true);assert.match(h.regions.get('security').innerHTML,/请使用 HTTPS/);
    await h.submit();for(const action of ['test','enabled','default'])await h.click(action);assert.equal(h.calls.length,1);assert.match(h.notice.textContent,/不能安全提交/);h.win.ShopManager.unmount();
});
test('unmount clears every credential field and aborts pending save; late result cannot repaint',async()=>{
    const pending=wait();let saved=0;const h=harness((url,options)=>options.method==='POST'?pending.promise:{ok:true,shops:[fixtureShop()],authorization_context:{can_submit_credentials:true}});await h.win.ShopManager.mount(h.host,{onSaved:()=>saved++});const save=h.submit();const post=h.calls.find(c=>c.options.method==='POST');
    for(const name of ['seller_client_id','seller_api_key','advertising_client_id','advertising_client_secret'])h.fields.get(name).value='must-clear';h.win.ShopManager.unmount();for(const field of h.fields.values())if(field.value==='must-clear')assert.fail('credential retained');assert.equal(post.options.signal.aborted,true);assert.equal(h.host.classList.contains('shop-manager'),false);assert.equal(h.document.body.classList.contains('shop-manager-mode'),false);assert.equal(h.globalEvents.size,0);assert.equal(h.browserEvents.size,0);
    const html=h.regions.get('editor').innerHTML;pending.resolve({ok:true,shop:fixtureShop('other'),advertising_saved:true});await save;assert.equal(h.regions.get('editor').innerHTML,html);assert.equal(saved,0);assert.equal(h.timers.size,0);
});
test('late old mount GET cannot replace a fresh mounted scope',async()=>{
    const pending=wait();let reads=0;const h=harness(()=>++reads===1?pending.promise:{ok:true,shops:[fixtureShop('fresh')],authorization_context:{can_submit_credentials:true}});
    const stale=h.win.ShopManager.mount(h.host);await h.win.ShopManager.mount(h.host);pending.resolve({ok:true,shops:[fixtureShop('stale')],authorization_context:{can_submit_credentials:true}});await stale;assert.match(h.regions.get('editor').innerHTML,/value="fresh"/);assert.doesNotMatch(h.regions.get('editor').innerHTML,/value="stale"/);h.win.ShopManager.unmount();
});
test('partial save remains editable with precise warning and old advertising authorization',async()=>{
    const shop=fixtureShop('a',{advertising:{configured:true,connection_status:'connected'}});const h=harness((url,options)=>options.method==='POST'?{ok:true,shop,partial:true,advertising_saved:false}:{ok:true,shops:[shop],authorization_context:{can_submit_credentials:true}});
    await h.win.ShopManager.mount(h.host);h.fields.get('advertising_client_id').value='service';h.fields.get('advertising_client_secret').value='wrong-private';await h.submit();assert.match(h.notice.textContent,/广告授权未更新/);assert.match(h.regions.get('editor-status').innerHTML,/广告已授权/);assert.equal(h.fields.get('advertising_client_secret').value,'');assert.equal(h.fieldset.disabled,false);h.win.ShopManager.unmount();
});
test('Seller test and state changes use selected shop and explicit existing routes only',async()=>{
    const h=harness((url,options)=>options.method?{ok:true,shop:fixtureShop()}:{ok:true,shops:[fixtureShop()],authorization_context:{can_submit_credentials:true}});await h.win.ShopManager.mount(h.host);await h.click('test');await h.click('enabled');
    assert.ok(h.calls.some(c=>c.url==='/api/workbench/stores/a/test'&&c.options.method==='POST'&&c.options.body==='{}'));assert.ok(h.calls.some(c=>c.url==='/api/workbench/stores/a/settings'&&c.options.method==='PUT'&&JSON.parse(c.options.body).enabled===false));assert.ok(h.calls.every(c=>!c.url.includes('/campaign')&&!c.url.includes('/statistics')));h.win.ShopManager.unmount();
});
test('new shop submission uses explicit create mode and switches to saved immutable identity',async()=>{
    let shops=[fixtureShop()];const h=harness((url,options)=>{if(options.method==='POST'){const body=JSON.parse(options.body),shop=fixtureShop(body.shop_id,{display_name:body.display_name,is_default:false});shops=[...shops,shop];return {ok:true,shop,advertising_saved:false}}return {ok:true,shops,authorization_context:{can_submit_credentials:true}}});
    await h.win.ShopManager.mount(h.host);await h.click('new');h.input('shop_id','new_shop');h.input('display_name','新店铺');h.fields.get('seller_client_id').value='321';h.fields.get('seller_api_key').value='secret-new';await h.submit();
    const body=JSON.parse(h.calls.find(c=>c.options.method==='POST').options.body);assert.equal(body.mode,'create');assert.equal(body.shop_id,'new_shop');assert.match(h.regions.get('editor').innerHTML,/name="shop_id" value="new_shop"[^>]*readonly/);assert.equal(h.fields.get('seller_api_key').value,'');h.win.ShopManager.unmount();
});
test('failed external cache callback preserves partial-save notice',async()=>{
    const shop=fixtureShop();const h=harness((url,options)=>options.method==='POST'?{ok:true,shop,partial:true,advertising_saved:false}:{ok:true,shops:[shop],authorization_context:{can_submit_credentials:true}});
    await h.win.ShopManager.mount(h.host,{onSaved:()=>{throw Error('external cache failed')}});await h.submit();assert.match(h.notice.textContent,/广告授权未更新/);assert.match(h.notice.textContent,/外部店铺列表刷新未完成/);assert.equal(h.win.ShopManager.isBusy(),false);h.win.ShopManager.unmount();
});
test('business and validation response bodies cannot echo credentials in UI errors',async()=>{
    const h=harness((url,options)=>options.method==='POST'?{http:422,body:{detail:[{loc:['body','seller_api_key'],msg:'secret-private',input:'secret-private'}]}}:{ok:true,shops:[fixtureShop()],authorization_context:{can_submit_credentials:true}});
    await h.win.ShopManager.mount(h.host);await h.submit();assert.match(h.notice.textContent,/Seller API Key/);assert.doesNotMatch(h.notice.textContent,/secret-private/);assert.equal(h.win.ShopManager.isBusy(),false);h.win.ShopManager.unmount();
});
