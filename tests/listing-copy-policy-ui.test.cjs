'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');
const vm=require('node:vm');
const root=path.resolve(__dirname,'..');
const flow=fs.readFileSync(path.join(root,'web/listing-flow.js'),'utf8');
const card=fs.readFileSync(path.join(root,'web/listing-card.js'),'utf8');
const esc=value=>String(value).replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function extract(source,first,next){const start=source.indexOf(first),end=source.indexOf(next,start);assert.ok(start>=0&&end>start);return source.slice(start,end)}
test('copy policy separates the main phrase from fact-supported description queries',()=>{
    const ctx=vm.createContext({esc});vm.runInContext(extract(flow,'function flowCopyPolicyHtml(g){','\nfunction flowStudioSlots'),ctx);
    const html=ctx.flowCopyPolicyHtml({selected_keywords:{keywords:[{keyword:'Антистресс   игрушка',role:'core'},{keyword:'розовый призрак',role:'secondary'},{keyword:'广告词',role:'ad'}]}});
    assert.match(html,/Антистресс игрушка/);assert.match(html,/розовый призрак/);assert.match(html,/只规范大小写和空格/);assert.match(html,/emoji/);assert.match(html,/不保证排名/);assert.doesNotMatch(html,/广告词/);
});
test('category-only copy policy does not fabricate a main query and escapes phrases',()=>{
    const ctx=vm.createContext({esc});vm.runInContext(extract(flow,'function flowCopyPolicyHtml(g){','\nfunction flowStudioSlots'),ctx);
    assert.match(ctx.flowCopyPolicyHtml({}),/未填写主关键词/);
    assert.doesNotMatch(ctx.flowCopyPolicyHtml({selected_keywords:{keywords:[{keyword:'<img src=x>',role:'core'}]}}),/<img src=x>/);
});
function matchHarness(){
    const draft={form:{scope:'qa-scope',fields:[{attribute_id:10096,attribute_name:'颜色',dictionary_id:1}]},dictionaries:{},selectedSkus:[{sku_id:'S1',name:'绿色幽灵'}]};
    const basic={unresolvedScope:'qa-scope',unresolved:[{attribute_id:10096,field_name:'颜色',sku_id:'S1',source_value:'绿色',reason:'没有唯一官方值',evidence:'供应商规格属性',search_terms:['зелёный']}]};
    const callbacks=[],ctx=vm.createContext({esc,productDraft:()=>basic,listingDraft:()=>draft,listingControl:()=> 'dictionary',benchButton:(label,action,extra)=>`<button data-bench-action="${action}" ${extra}>${label}</button>`,
        state:{product:'P1'},notice:()=>{},CSS:{escape:String},$:()=>null,document:{addEventListener:(type,callback)=>callbacks.push(callback),querySelector:()=>null},applyListingAutofill:()=>0,loadListingAutofill:async()=>{},loadDictionary:async()=>{}});
    vm.runInContext(extract(card,'function benchAttributeMatchesHtml(){','\nconst benchSupportRenderer'),ctx);
    return {ctx,basic,draft,callbacks};
}
test('unmatched dictionary panel is absent when nothing needs review and escapes captured facts',()=>{
    const h=matchHarness();assert.match(h.ctx.benchAttributeMatchesHtml(),/绿色幽灵/);assert.match(h.ctx.benchAttributeMatchesHtml(),/查找官方选项/);
    h.basic.unresolved[0].source_value='<img src=x onerror=alert(1)>';assert.doesNotMatch(h.ctx.benchAttributeMatchesHtml(),/<img src=x/);
    h.basic.unresolved=[];assert.equal(h.ctx.benchAttributeMatchesHtml(),'');
});
test('a scoped match search opens the official dictionary without writing an attribute',async()=>{
    const h=matchHarness();let request;
    h.ctx.loadDictionary=async id=>{request=id};const button={dataset:{matchIndex:'0'},isConnected:true};
    await h.callbacks[0]({target:{closest:()=>button}});
    assert.equal(request,'10096');assert.equal(h.draft.scope,'S1');assert.equal(h.draft.dictionaries['10096'].query,'зелёный');assert.equal(h.draft.attributes,undefined);assert.equal(button.disabled,false);
});
test('old unmatched results cannot be shown or searched in a different shop/category scope',async()=>{
    const h=matchHarness();h.draft.form.scope='different-shop-category';let request=false;
    h.ctx.loadDictionary=async()=>{request=true};assert.equal(h.ctx.benchAttributeMatchesHtml(),'');
    await h.callbacks[0]({target:{closest:()=>({dataset:{matchIndex:'0'},isConnected:true})}});
    assert.equal(request,false);
});
