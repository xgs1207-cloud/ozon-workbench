'use strict';
// Pure production logic, deferred memory APIs only. No browsers, models or Ozon writes.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');
const vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../web/listing-rich-content.js'),'utf8');
const plain=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {resolve,reject,promise}};
function harness(){
    const calls=[],events=[];
    const context=vm.createContext({Map,Set,Date,JSON,Number,String,Error,
        state:{product:'P1',shop:'shop-a',guided:{category_selection:{category_id:1,type_id:2}}},
        benchShop:()=>context.state.shop,
        benchScopeKey:(product=context.state.product,shop=context.state.shop)=>`${product}:${shop}:${context.state.guided.category_selection.category_id}:2`,
        renderProduct:()=>{},esc:value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char])),
        document:{addEventListener:(type,callback)=>events.push({type,callback}),querySelector:()=>null},
        api:async(url,options)=>{calls.push({url,options});throw Error('Unconfigured offline API fixture')},
        json:(method,body)=>({method,body:JSON.stringify(body)}),confirm:()=>true,notice:()=>{}
    });
    vm.runInContext(source,context);
    context.richRenderConversation=()=>{};context.richUpdateStatus=()=>{};context.richRenderBlocks=()=>{};
    context.richEditorError=(session,message='')=>{session.error=message};
    const entry=context.richEntry();
    const session={entry,revision:3,context:'context-a',blocks:[{id:'T1',type:'text',image_id:null,title:'Дизайн',text:'Описание'}],media:[{id:'IMG1',kind:'generated',preview_url:'/api/workbench/products/P1/rich-content/images/IMG1'}],history:[],prompt:'突出已确认的外观，加入关键词',dirty:false,localRevision:0,aiBusy:false,saving:false,uploading:false,open:true,nodes:null};
    return {context,calls,events,entry,session};
}
test('rich entries are isolated by product, authorized shop and category',()=>{
    const h=harness();assert.equal(h.context.richEntry(),h.entry);
    h.context.state.shop='shop-b';assert.notEqual(h.context.richEntry(),h.entry);
    h.context.state.shop='shop-a';h.context.state.guided.category_selection.category_id=5;
    assert.notEqual(h.context.richEntry(),h.entry);assert.equal(h.calls.length,0);
});
test('preview URLs reject remote and cross-product paths without fetching',()=>{
    const h=harness();
    assert.equal(h.context.richImageUrl(h.entry,h.session.media[0]),'/api/workbench/products/P1/rich-content/images/IMG1');
    for(const preview_url of ['https://evil.test/a.png','javascript:alert(1)','/api/workbench/products/P2/media/a.png','/api/workbench/products/P1/media/../secret','/api/workbench/products/P1/media/%2e%2e/secret','/api/workbench/products/P1/other/secret','/api/workbench/products/P1/media/a.png#x','/api/workbench/products/P1/media\\a.png'])assert.equal(h.context.richImageUrl(h.entry,{preview_url}),'');
    assert.equal(h.calls.length,0);
});
test('moving blocks changes only order and supports edge positions',()=>{
    const h=harness(),blocks=[{id:'A'},{id:'B'},{id:'C'}];
    assert.deepEqual(plain(h.context.richMove(blocks,'B',-1)).map(row=>row.id),['B','A','C']);
    assert.equal(h.context.richMove(blocks,'A',-1),blocks);assert.equal(h.context.richMove(blocks,'missing',1),blocks);
    assert.deepEqual(blocks.map(row=>row.id),['A','B','C']);
});
test('validation allows empty optional content and checks real media IDs',()=>{
    const h=harness();assert.deepEqual(plain(h.context.richValidBlocks([],h.session.media)),[]);
    assert.equal(h.context.richValidBlocks([{id:'A',type:'image',image_id:'IMG1',title:'',text:''}],h.session.media).length,1);
    assert.throws(()=>h.context.richValidBlocks([{id:'A',type:'image',image_id:'https://evil.test',title:'',text:''}],h.session.media),/选择一张/);
    assert.throws(()=>h.context.richValidBlocks([{id:'A',type:'text',image_id:null,title:'',text:' '}],h.session.media),/填写标题或正文/);
    assert.throws(()=>h.context.richValidBlocks([h.session.blocks[0],h.session.blocks[0]],h.session.media),/编号/);
    assert.throws(()=>h.context.richValidBlocks([{id:'A',type:'html',title:'',text:'<script>'}],h.session.media),/类型/);
});
test('cached reads do not retry failures automatically or call paid generation',async()=>{
    const h=harness();await h.context.richLoad(h.entry);await h.context.richLoad(h.entry);
    assert.equal(h.calls.length,1);assert.equal(h.calls[0].options,undefined);assert.match(h.entry.error,/offline/);
    h.context.api=async()=>({attribute_id:11254,revision:3,blocks:[]});
    await h.context.richLoad(h.entry,true);assert.equal(h.entry.error,'');assert.equal(h.entry.loaded,true);
});
test('concurrent rich reads share one request and both await saved content',async()=>{
    const h=harness(),waiting=deferred();h.context.api=()=>{h.calls.push('GET');return waiting.promise};
    const first=h.context.richLoad(h.entry),second=h.context.richLoad(h.entry,true);
    waiting.resolve({attribute_id:11254,revision:3,blocks:[]});await Promise.all([first,second]);
    assert.equal(h.calls.length,1);assert.equal(h.entry.data.revision,3);assert.equal(h.entry.loading,false);
});
test('Chinese AI prompt sends current blocks, history and optimistic revision',async()=>{
    const h=harness();h.session.history=[{role:'assistant',content:'上一份候选'}];
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {candidate:{id:'C1',context_fingerprint:'context-a',revision:3,blocks:[{...h.session.blocks[0],text:'Новый текст'}],message_zh:'突出设计和关键词'}}};
    const before=plain(h.session.blocks);await h.context.richGenerate(h.session);
    assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/rich-content\/generate\?shop=shop-a$/);
    const body=JSON.parse(h.calls[0].options.body);assert.equal(body.revision,3);assert.equal(body.context_fingerprint,'context-a');assert.match(body.prompt,/关键词/);
    assert.deepEqual(body.blocks,before);assert.equal(body.history.length,1);
    assert.deepEqual(plain(h.session.blocks),before);assert.equal(h.session.candidate.id,'C1');assert.equal(h.session.aiBusy,false);
});
test('late AI candidate never overwrites manual edits made while generating',async()=>{
    const h=harness(),waiting=deferred();h.context.api=()=>waiting.promise;
    const pending=h.context.richGenerate(h.session);
    h.session.blocks[0].text='人工修改';h.context.richDirty(h.session);
    waiting.resolve({candidate:{id:'C1',context_fingerprint:'context-a',blocks:[{...h.session.blocks[0],text:'AI修改'}]}});
    await pending;assert.equal(h.session.blocks[0].text,'人工修改');assert.equal(h.session.dirty,true);
    assert.equal(h.session.candidate.blocks[0].text,'AI修改');
});
test('candidate selection is explicit and decline preserves unsaved edits',()=>{
    const h=harness();h.session.candidate={blocks:[{...h.session.blocks[0],text:'AI修改'}]};h.session.dirty=true;
    h.context.confirm=()=>false;assert.equal(h.context.richSelectCandidate(h.session),false);assert.equal(h.session.blocks[0].text,'Описание');
    h.context.confirm=()=>true;assert.equal(h.context.richSelectCandidate(h.session),true);assert.equal(h.session.blocks[0].text,'AI修改');assert.equal(h.calls.length,0);
});
test('AI failure restores button state and manual blocks with no retry',async()=>{
    const h=harness(),before=plain(h.session.blocks);h.context.api=async()=>{h.calls.push('POST');throw Error('模型超时，请核对调用记录')};
    await assert.rejects(h.context.richGenerate(h.session),/模型超时/);assert.equal(h.calls.length,1);
    assert.equal(h.session.aiBusy,false);assert.deepEqual(plain(h.session.blocks),before);assert.match(h.session.error,/模型超时/);
});
test('empty prompt, oversized prompt and duplicate generation never call API',async()=>{
    const h=harness();h.session.prompt=' ';await assert.rejects(h.context.richGenerate(h.session),/中文/);
    h.session.prompt='长'.repeat(6001);await assert.rejects(h.context.richGenerate(h.session),/6000/);
    h.session.aiBusy=true;await assert.rejects(h.context.richGenerate(h.session),/处理中/);assert.equal(h.calls.length,0);
});
test('context navigation prevents saving, generating and candidate replacement',async()=>{
    const h=harness();h.context.state.product='P2';
    await assert.rejects(h.context.richSave(h.session),/商品或类目已改变/);
    await assert.rejects(h.context.richGenerate(h.session),/商品或类目已改变/);
    assert.throws(()=>h.context.richSelectCandidate(h.session),/商品或类目已改变/);assert.equal(h.calls.length,0);
});
test('manual apply writes only rich draft blocks, not uploads or Ozon',async()=>{
    const h=harness();h.session.open=false;
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {attribute_id:11254,revision:4,context_fingerprint:'context-a',blocks:h.session.blocks,media:h.session.media}};
    await h.context.richSave(h.session);assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.method,'PUT');
    assert.deepEqual(Object.keys(JSON.parse(h.calls[0].options.body)).sort(),['blocks','context_fingerprint','revision']);
    assert.equal(h.session.revision,4);assert.equal(h.session.entry.data.revision,4);assert.equal(h.session.dirty,false);
    assert.doesNotMatch(h.calls[0].url,/publish|submit|stocks/);
});
test('revision conflicts preserve unsaved blocks and return actionable error',async()=>{
    const h=harness();h.session.dirty=true;h.context.api=async()=>{throw Error('草稿已更新，请重新读取后再应用')};
    await assert.rejects(h.context.richSave(h.session),/重新读取/);assert.equal(h.session.dirty,true);assert.equal(h.session.revision,3);assert.equal(h.session.saving,false);
});
test('late save response preserves new manual edits and updates revision for retry',async()=>{
    const h=harness(),waiting=deferred();h.session.open=false;h.context.api=()=>waiting.promise;
    const pending=h.context.richSave(h.session);h.session.blocks[0].text='后续人工修改';h.context.richDirty(h.session);
    waiting.resolve({revision:4,context_fingerprint:'context-a',blocks:[{id:'T1',type:'text',image_id:null,title:'Дизайн',text:'Описание'}]});
    await pending;assert.equal(h.session.blocks[0].text,'后续人工修改');assert.equal(h.session.dirty,true);assert.equal(h.session.revision,4);
});
test('cancel closes the editor without saving or changing persisted content',()=>{
    const h=harness(),events=[];h.entry.data={blocks:plain(h.session.blocks),revision:3};
    h.session.nodes={dialog:{close:()=>events.push('close'),remove:()=>events.push('remove')}};
    h.session.blocks[0].text='未保存';h.session.dirty=true;
    h.context.richCloseEditor(h.session);assert.equal(h.entry.data.blocks[0].text,'Описание');
    assert.equal(h.entry.data.revision,3);assert.equal(h.session.open,false);assert.deepEqual(events,['close','remove']);assert.equal(h.calls.length,0);
});
test('upload MIME, size and batch limits fail before any request',async()=>{
    const h=harness();
    await assert.rejects(h.context.richImportImages(h.session,[{type:'image/svg+xml',size:20}]),/PNG/);
    await assert.rejects(h.context.richImportImages(h.session,[{type:'image/png',size:10*1024*1024+1}]),/10 MB/);
    await assert.rejects(h.context.richImportImages(h.session,Array.from({length:11},()=>({type:'image/png',size:20}))),/10 张/);
    assert.equal(h.calls.length,0);
});
test('uploaded images join media pool but do not auto-add or publish a block',async()=>{
    const h=harness();h.session.open=false;h.context.richReadFile=async()=> 'safe-base64';
    h.context.api=async(url,options)=>{h.calls.push({url,options});return {image:{id:'IMG2',kind:'imported',preview_url:'/api/workbench/products/P1/rich-content/images/IMG2'}}};
    await h.context.richImportImages(h.session,[{name:'商品.png',type:'image/png',size:1024}]);
    assert.equal(h.session.media.length,2);assert.equal(h.session.blocks.length,1);assert.equal(h.session.dirty,false);
    assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/rich-content\/images\?/);
    assert.deepEqual(JSON.parse(h.calls[0].options.body),{filename:'商品.png',data_base64:'safe-base64'});
});
test('static dialog shell has cancel, keyboard close, media filtering and AI controls',()=>{
    assert.match(source,/dialog\.addEventListener\('cancel'/);assert.match(source,/dialog\.showModal\(\)/);
    assert.match(source,/data-rich-action="cancel"/);assert.match(source,/data-rich-action="apply"/);
    assert.match(source,/value="generated"/);assert.match(source,/value="imported"/);
    assert.match(source,/rich-ai-prompt/);assert.doesNotMatch(source,/target="_blank"/);
    // Dynamic product text is never interpolated into the dialog shell.
    assert.match(source,/node\.textContent=text/);assert.doesNotMatch(source,/innerHTML=.*candidate/);
});
