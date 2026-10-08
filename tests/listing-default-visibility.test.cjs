'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../web/research-workbench.html'), 'utf8');
const source = html.slice(html.indexOf('function listingFieldDisplay('), html.indexOf('function listingFieldGroupsHtml('));

function hidden(field, options={}) {
  const draft = {scope:'common', selectedSkus:[{sku_id:'S1'}], perSku:{},
    attributes:{[field.attribute_id]:[{value:options.value??false}]},
    provenance:{attributes:{[field.attribute_id]:{source:'user_requested_default'}},per_sku_attributes:{}},
    fieldDisplay:{[field.attribute_id]:{display:'hidden',hide_when_default:true,default_match_values:['false']}},
    validationErrors:[],touched:new Set(),...options.draft};
  const context = vm.createContext({Object,Number,String,Set,
    listingEffective:(draft,sku)=>({...draft.attributes,...(draft.perSku[sku]||{})})});
  vm.runInContext(source,context);
  return context.listingFieldHidden(field,draft);
}

test('a valid false default is hidden even when required; zero is not treated as an empty value',()=>{
  assert.equal(hidden({attribute_id:201,required:true}),true);
  assert.equal(hidden({attribute_id:202,required:true},{value:1,draft:{fieldDisplay:{202:{display:'hidden',hide_when_default:true,default_match_values:['1']}}}}),true);
});

test('matching manual values are visible and are not mistaken for generated defaults',()=>{
  assert.equal(hidden({attribute_id:201},{draft:{provenance:{attributes:{201:{source:'manual'}}}}}),false);
});

test('newly touched common and per-SKU fields remain visible while editing',()=>{
  assert.equal(hidden({attribute_id:201},{draft:{touched:new Set(['common:201'])}}),false);
  assert.equal(hidden({attribute_id:201},{draft:{scope:'S1',perSku:{S1:{201:[{value:false}]}},
    touched:new Set(['S1:201']),provenance:{per_sku_attributes:{S1:{201:{source:'user_requested_default'}}}}}}),false);
});

test('a changed default value is exposed even before saving',()=>{
  assert.equal(hidden({attribute_id:201},{value:true}),false);
});

test('missing required defaults stay visible, including an explicit SKU clear',()=>{
  assert.equal(hidden({attribute_id:201,required:true},{draft:{attributes:{201:[]}}}),false);
  assert.equal(hidden({attribute_id:201,required:true},{draft:{perSku:{S1:{201:[]}}}}),false);
});

test('all selected SKU effective values must match before a common field can disappear',()=>{
  assert.equal(hidden({attribute_id:201},{draft:{selectedSkus:[{sku_id:'S1'},{sku_id:'S2'}],
    perSku:{S2:{201:[{value:true}]}},provenance:{attributes:{201:{source:'user_requested_default'}},
      per_sku_attributes:{S2:{201:{source:'user_requested_default'}}}}}}),false);
});

test('blank optional policies do not require invented values or default provenance',()=>{
  assert.equal(hidden({attribute_id:205},{draft:{attributes:{},provenance:{attributes:{}},
    fieldDisplay:{205:{display:'hidden',hide_when_blank:true}}}}),true);
  assert.equal(hidden({attribute_id:205,required:true},{draft:{attributes:{},provenance:{attributes:{}},
    fieldDisplay:{205:{display:'hidden',hide_when_blank:true}}}}),false);
});

test('manual blank clears and populated optional values remain actionable',()=>{
  assert.equal(hidden({attribute_id:205},{draft:{attributes:{205:[]},provenance:{attributes:{205:{source:'manual'}}},
    fieldDisplay:{205:{display:'hidden',hide_when_blank:true}}}}),false);
  assert.equal(hidden({attribute_id:205},{value:'一年',draft:{fieldDisplay:{205:{display:'hidden',hide_when_blank:true}}}}),false);
});

test('dictionary defaults need the actual dictionary ID as well as matching text',()=>{
  const policy={85:{display:'hidden',hide_when_default:true,default_match_values:['Нет бренда']}};
  assert.equal(hidden({attribute_id:85,dictionary_id:1},{value:'Нет бренда',draft:{fieldDisplay:policy}}),false);
  assert.equal(hidden({attribute_id:85,dictionary_id:1},{draft:{fieldDisplay:policy,attributes:{85:[{value:'Нет бренда',dictionary_value_id:501}]}}}),true);
});

test('unresolved conflicts, official validation failures and absent shared models stay visible',()=>{
  assert.equal(hidden({attribute_id:201},{draft:{fieldDisplay:{201:{display:'attention',hide_when_default:true,default_match_values:['false']}}}}),false);
  assert.equal(hidden({attribute_id:201},{draft:{validationErrors:['官方选项已失效']}}),false);
  assert.equal(hidden({attribute_id:9048},{value:'WB-123456ABCDEF',draft:{fieldDisplay:{9048:{display:'hidden',hide_when_default:true,default_match_values:[]}}}}),false);
});

test('keyword and hashtag policies remain visible',()=>{
  assert.equal(hidden({attribute_id:23171},{value:'#игрушка',draft:{fieldDisplay:{23171:{display:'standard',hide_when_default:false}}}}),false);
});

test('the only active header has no advanced console entry',()=>{
  assert.doesNotMatch(html, /href="\/advanced"/);
  assert.match(html, /new URLSearchParams\(location.search\).*product_id/);
});

function skuChoice(count) {
  const notices=[],counter={textContent:''},target={checked:true,classList:{contains:value=>value==='skuChoice'}};
  const skuSource=html.slice(html.indexOf('function listingSkuChoiceChanged('),html.indexOf("document.addEventListener('change',async e=>{listingSkuChoiceChanged"));
  const context=vm.createContext({document:{querySelectorAll:()=>Array.from({length:count})},
    state:{skus:{skus:Array.from({length:200})}},$:()=>counter,notice:text=>notices.push(text)});
  vm.runInContext(skuSource,context);context.listingSkuChoiceChanged(target);
  return {target,counter,notices};
}

test('current desktop allows 11 and 100 selections without the old ten-SKU gate',()=>{
  for(const count of [11,100]){const result=skuChoice(count);assert.equal(result.target.checked,true);assert.equal(result.notices.length,0);assert.match(result.counter.textContent,new RegExp(`已勾选 ${count} / 200`));}
  assert.doesNotMatch(html,/每次最多 10 个|一次最多选择 10 个上架规格/);
});

test('desktop 101st selection remains unselected and clearly requests a separate batch',()=>{
  const result=skuChoice(101);assert.equal(result.target.checked,false);assert.match(result.notices[0],/100.*分批/);assert.match(result.counter.textContent,/已勾选 100 \/ 200/);
});

test('capture price warnings do not disable selection, but unknown and identity errors do',()=>{
  const source=html.slice(html.indexOf('function listingSkuBlockingIssues('),html.indexOf('function listingSkuChoiceChanged('));
  const context=vm.createContext({});vm.runInContext(source,context);
  const row={collection_issues:['缺少有效采购价','原始规格标识缺失或重复，须人工核对','未知采集错误']};
  assert.deepEqual(Array.from(context.listingSkuBlockingIssues(row)),row.collection_issues.slice(1));
  assert.equal(row.collection_issues.length,3);assert.deepEqual(Array.from(context.listingSkuBlockingIssues({collection_issues:['缺少有效采购价']})),[]);
  assert.match(html,/listingSkuBlockingIssues\(x\).length\?'disabled'/);
});
