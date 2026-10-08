'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const test=require('node:test');
const vm=require('node:vm');

const html=fs.readFileSync(path.join(__dirname,'../web/research-workbench.html'),'utf8');
const start=html.indexOf('function applyListingAutofill(result)');
const end=html.indexOf('async function loadListingAutofill',start);
assert.ok(start>=0&&end>start,'production autofill merger must be extractable');
const implementation=html.slice(start,end);
const green=[{value:'Зеленый',dictionary_value_id:901}];
const blue=[{value:'Синий',dictionary_value_id:902}];

function harness(){
    const basic={details:{},touched:new Set(),provenance:{}};
    const draft={form:{scope:'fixture-shop-category-type'},attributes:{},perSku:{},
        selectedSkus:[{sku_id:'S1'},{sku_id:'S2'}],touched:new Set(),
        provenance:{attributes:{},per_sku_attributes:{}},dirty:false};
    const context=vm.createContext({productDraft:()=>basic,listingDraft:()=>draft,listingDetailKeys:[]});
    vm.runInContext(implementation,context);
    const result={scope:draft.form.scope,per_sku_attributes:{S1:{10096:green},S2:{10096:green}},
        provenance:{per_sku_attributes:{S1:{10096:{source:'collected_product'}},S2:{10096:{source:'collected_product'}}}}};
    return {basic,draft,result,apply:()=>context.applyListingAutofill(result)};
}

test('an incoming SKU suggestion cannot override an unsaved common manual colour',()=>{
    const h=harness();h.draft.attributes[10096]=blue;h.draft.touched.add('common:10096');
    assert.equal(h.apply(),0);
    assert.equal(h.draft.attributes[10096],blue);
    assert.equal(Object.hasOwn(h.draft.perSku.S1,10096),false);
    assert.equal(Object.hasOwn(h.draft.perSku.S2,10096),false);
    assert.equal(h.draft.dirty,false);
});

test('an incoming SKU suggestion cannot bypass an explicit common clear',()=>{
    const h=harness();const cleared=[];h.draft.attributes[10096]=cleared;h.draft.touched.add('common:10096');
    assert.equal(h.apply(),0);
    assert.equal(h.draft.attributes[10096],cleared);
    assert.equal(Object.hasOwn(h.draft.perSku.S1,10096),false);
    assert.equal(Object.hasOwn(h.draft.perSku.S2,10096),false);
});

test('an untouched automatically populated common value allows real SKU distinctions',()=>{
    const h=harness();h.draft.attributes[10096]=blue;
    h.draft.provenance.attributes[10096]={source:'collected_product'};
    assert.equal(h.apply(),2);
    assert.equal(h.draft.attributes[10096],blue);
    assert.equal(h.draft.perSku.S1[10096],green);
    assert.equal(h.draft.perSku.S2[10096],green);
});

test('manual common edits block only their attribute, not other evidenced SKU facts',()=>{
    const h=harness();h.draft.touched.add('common:10096');h.draft.attributes[10096]=[];
    const material=[{value:'Силикон',dictionary_value_id:701}];
    h.result.per_sku_attributes.S1[10]=material;
    assert.equal(h.apply(),1);
    assert.equal(Object.hasOwn(h.draft.perSku.S1,10096),false);
    assert.equal(h.draft.perSku.S1[10],material);
});

test('per-SKU manual edits and clears remain protected independently',()=>{
    const h=harness();h.draft.perSku.S1={10096:[]};h.draft.touched.add('S1:10096');
    assert.equal(h.apply(),1);
    assert.equal(h.draft.perSku.S1[10096].length,0);
    assert.equal(h.draft.perSku.S2[10096],green);
});

test('foreign dictionary scope does not install common or SKU attributes',()=>{
    const h=harness();h.result.scope='different-shop-category-type';
    h.result.attributes={10096:green};
    assert.equal(h.apply(),0);
    assert.equal(Object.hasOwn(h.draft.attributes,10096),false);
    assert.deepEqual(h.draft.perSku,{});
});
