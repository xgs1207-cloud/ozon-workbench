/* One local document, one operational form. No reads here allocate offer IDs. */
const listingBench = {documents:new Map(), profiles:new Map(), prefixDrafts:new Map(), loading:new Set(), errors:new Map(), workspaces:new Map(), sets:new Map(), counts:new Map()};
function benchProfileId(){
    const key='ozon-workbench.employee-profile';
    try{let value=localStorage.getItem(key);if(!value){value=crypto.randomUUID();localStorage.setItem(key,value)}return value}
    catch{return listingBench.temporaryProfile||(listingBench.temporaryProfile=crypto.randomUUID())}
}
function benchShop(){return selectedReadStore()?.id||state.guided?.category_selection?.shop_id||''}
function benchScopeKey(product=state.product,shop=benchShop()){
    const category=product===state.product?state.guided?.category_selection||{}:{};
    return `${product}:${shop}:${category.category_id||''}:${category.type_id||''}`;
}
function benchDocument(){return listingBench.documents.get(benchScopeKey())}
const benchScalar=flowScalar;
flowScalar=function(value){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.hasOwn(value,'value')?benchScalar(value.value):benchScalar(value)};
function benchButton(label,action,extra='',disabled=false){return `<button class="btn secondary" data-bench-action="${action}" ${extra} ${disabled?'disabled':''}>${label}</button>`}
function benchSummaryHtml(){
    const doc=benchDocument(),summary=doc?.summary||state.guided?.workflow?.analysis||{},payload=summary.payload||{},facts=payload.facts||{},points=payload.selling_points||payload.key_selling_points||[];
    const rows=doc?.selected_skus||[],category=doc?.card?.category||state.guided?.category_selection||{};
    return `<section class="bench-summary"><h3>产品信息与卖点</h3><span class="tag ${summary.confirmed?'ok':'warn'}">${summary.confirmed?'已确认摘要':'摘要待确认'}</span><p class="bench-source-title">${esc(doc?.source_title||state.guided?.source?.title_zh||'')}</p>${category.category_path?`<p class="field-help">${esc(Array.isArray(category.category_path)?category.category_path.join(' / '):category.category_path)}</p>`:''}${rows.length?`<div class="bench-summary-specs">${rows.map(row=>`<p><b>${esc(row.name)}</b>${row.option_values?.length?`<br>${esc(row.option_values.map(x=>`${x.name||''} ${x.value||''}`).join(' / '))}`:''}</p>`).join('')}</div>`:''}${points.length?`<ul>${points.map(point=>`<li>${esc(typeof point==='string'?point:point.point_cn||point.text||point.claim||flowScalar(point))}</li>`).join('')}</ul>`:'<p class="field-help">摘要暂无已确认卖点。未知信息请留空，不根据图片比例猜测参数。</p>'}<dl class="bench-evidence">${Object.entries(facts).filter(([key,value])=>!['skus','source_refs','title_cn','selected_category','image_refs'].includes(key)&&value!=null&&value!==''&&flowScalar(value)!=='未确认').slice(0,12).map(([key,value])=>`<dt>${esc(flowFactLabels[key]||key)}</dt><dd>${esc(flowScalar(value))}</dd>`).join('')}</dl>${doc?.copy?.confirmed?`<details><summary>已确认俄文标题与标签</summary><p>${esc(doc.copy.title_ru)}</p>${flowTags(doc.copy.hashtags)}</details>`:''}</section>`;
}
const benchSummaryBase=benchSummaryHtml;
benchSummaryHtml=function(){
    const template=document.createElement('template');template.innerHTML=benchSummaryBase();
    const section=template.content.querySelector('.bench-summary');
    const list=section.querySelector('ul'),fallback=[...section.querySelectorAll('p.field-help')].find(p=>p.textContent.startsWith('摘要暂无'));
    const placeholder=list||fallback;
    if(placeholder){const content=document.createElement('div');content.className='bench-selling-points';content.innerHTML=flowOperatorPointsHtml(state.guided,'flow-summary-list','摘要暂无已确认卖点。未知信息请留空，不根据图片比例猜测参数。');placeholder.replaceWith(content)}
    return template.innerHTML;
};
listingBench.summaryTranslations=new Map();
document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-flow-action="summary-zh"]');if(!button)return;
    const product=state.product,shop=benchShop(),pending=listingBench.summaryTranslations.get(product);
    if(pending?.busy)return;
    const display=benchDocument()?.summary?.display_zh;
    const retry=display?.retry_requires_confirmation===true;
    if(retry&&!confirm('上次中文转换未完成，请先核对模型调用记录。确认再次调用文本模型？'))return;
    listingBench.summaryTranslations.set(product,{busy:true});button.disabled=true;button.textContent='正在转换中文…';
    try{
        await api(`/api/workbench/products/${encodeURIComponent(product)}/summary-display-zh`,json('POST',{input_fingerprint:button.dataset.summaryFingerprint||null,confirm_retry:retry}));
        await benchLoadDocument(product,shop);listingBench.summaryTranslations.delete(product);
        if(state.product===product&&state.view==='product'){captureProductFields();renderProduct()}
        notice('卖点已转换为中文；原摘要和俄文上架文案未改变');
    }catch(error){await benchLoadDocument(product,shop);listingBench.summaryTranslations.set(product,{error:error.message});notice(error.message,true);if(state.product===product&&state.view==='product')renderProduct()}
    finally{if(button.isConnected)button.disabled=false}
});
function benchMissingHtml(){
    const draft=listingDraft(),basic=productDraft(),doc=benchDocument(),names=new Map((draft?.form?.fields||[]).map(x=>[String(x.attribute_id),x.attribute_name||x.name]));
    const fields=draft?listingMissing(draft):[],messages=[];
    if(!doc?.offer_ids?.complete)messages.push('请先分配上架货号');
    for(const key of ['weight_g','length_mm','width_mm','height_mm'])if(!(Number(basic?.details?.[`package_${key}`])>0))messages.push(({weight_g:'含包装重量',length_mm:'包装长度',width_mm:'包装宽度',height_mm:'包装高度'})[key]);
    for(const row of state.skus?.skus?.filter(x=>x.listed)||[])if(!(Number(basic?.prices?.[row.sku_id]?.price??state.guided?.manual_prices?.prices?.[row.sku_id]?.price)>0))messages.push(`${row.sku_name||row.sku_id}：售价`);
    for(const item of fields)if(item.ids?.length)messages.push(...item.ids.map(id=>`${item.name||item.id||'商品属性'}：${names.get(String(id))||id}`));
    if(!draft)messages.push('请先确认官方类目并读取类目字段');
    return `<section class="bench-missing"><h3>仍需填写</h3>${messages.length?`<ul>${[...new Set(messages)].map(x=>`<li>${esc(x)}</li>`).join('')}</ul>`:'<p class="good">当前所需字段已填写，请保存并检查卡片。</p>'}${doc?.missing?.validation_errors?.length?`<p class="bad">${esc(doc.missing.validation_errors.join('；'))}</p>`:''}<p class="field-help">摘要用于填写参考。正式发布仍需校验真实属性、媒体与合规资料。</p></section>`;
}
const benchSupportRenderer=renderProductSupport;
renderProductSupport=function(){if(flowStep()!=='card'){benchSupportRenderer();return}const host=$('#productSupport');if(host)host.innerHTML=benchSummaryHtml()+benchMissingHtml()};
function benchOperationalHtml(){
    const doc=benchDocument(),offers=doc?.offer_ids?.offers||{},basic=productDraft(),shop=benchShop(),profile=listingBench.profiles.get(shop),prefix=listingBench.prefixDrafts.get(shop)??profile?.prefix??'';
    const selected=state.skus?.skus?.filter(row=>row.listed)||[],prices=state.guided?.manual_prices?.prices||{};
    return `<section class="panel bench-operational"><h2>上架信息</h2><div class="bench-prefix"><label class="field">员工固定货号前缀<input id="benchOfferPrefix" maxlength="28" value="${esc(prefix)}" placeholder="例如 xzj.jp" autocomplete="off"></label>${benchButton(profile?.prefix?'更新前缀并分配货号':'保存前缀并分配货号','reserve-offers','',!shop)}<p class="field-help">前缀只需保存一次。货号为「前缀.月.日.流水号」，北京时间每天从 1 递增；每个上架规格独立编号，已经分配的货号不会改变。</p></div><div class="bench-sku-operational">${selected.map(row=>{const price=basic?.prices?.[row.sku_id]||prices[row.sku_id]||{},offer=offers[row.sku_id]||row.offer_id||'';return `<div class="bench-sku-line"><label class="field">${selected.length>1?`${esc(row.sku_name||row.sku_id)} · `:''}货号<input readonly class="bench-offer" data-sku="${esc(row.sku_id)}" value="${esc(offer)}" placeholder="保存前缀后由系统分配"></label><label class="field">上架价格<div class="row"><input type="number" class="skuPrice" data-sku="${esc(row.sku_id)}" min="0.01" step="0.01" aria-label="${esc(row.sku_name||row.sku_id)} 上架价格" value="${esc(price.price??'')}"><select class="priceCurrency" aria-label="售价币种">${['CNY','RUB'].map(currency=>`<option value="${currency}" ${(price.currency||selectedReadStore()?.default_currency_code||'CNY')===currency?'selected':''}>${currency}</option>`).join('')}</select></div></label></div>`}).join('')||'<p class="muted">先选择要上架的规格。</p>'}</div><h3>含包装重量与尺寸</h3><div class="bench-package-grid">${[['weight_g','含包装重量','克'],['length_mm','包装长度','毫米'],['width_mm','包装宽度','毫米'],['height_mm','包装高度','毫米']].map(([key,label,unit])=>`<label class="field">${label}<div class="bench-unit-input"><input class="dim" data-kind="package" data-key="${key}" type="number" min="1" step="1" aria-label="${label}（${unit}）" value="${esc(basic?.details?.[`package_${key}`]??'')}"><span>${unit}</span></div><span data-detail-source="package_${key}">${detailSource(`package_${key}`)}</span></label>`).join('')}</div><p class="field-help">这是 Ozon 上架所需的含包装信息。采购价不作为上架售价；未知实测参数保持空白。</p><div class="section-footer"><span id="listingDetailsStatus" class="muted"></span>${benchButton('保存价格和包装信息','save-operational')}</div><div id="listingDetailsError" class="inline-error" hidden></div></section>`;
}
const benchOfficialRenderer=renderListingForm;
renderListingForm=function(){
    const template=document.createElement('template');template.innerHTML=benchOfficialRenderer();
    const draft=listingDraft(),doc=benchDocument();
    for(const label of template.content.querySelectorAll('.official-field .field-name')){
        const text=[...label.childNodes].find(node=>node.nodeType===Node.TEXT_NODE&&node.textContent.trim());
        if(text){const name=document.createElement('strong');name.className='official-field-title';name.textContent=text.textContent;text.replaceWith(name)}
    }
    for(const field of template.content.querySelectorAll('.official-field')){
        const name=field.querySelector('.official-field-title')?.textContent.trim();
        const input=field.querySelector('[data-listing-input]:not([data-collection])');
        if(name&&input?.tagName==='INPUT')input.placeholder=name;
    }
    const excluded=new Set((doc?.official_excluded_attribute_ids||doc?.card?.official_excluded_attribute_ids||[4191,23171]).map(String));
    for(const field of template.content.querySelectorAll('[data-official-field]')){
        const id=field.dataset.officialField;
        // Rich content has one visual editor; never render a second JSON input.
        if(typeof benchRichContentEnhance==='function' && (id==='11254' || /rich.content|富内容|丰富内容|рич.контент/i.test(field.querySelector('.official-field-title')?.textContent||''))){field.remove();continue}
        // Only hide an operational duplicate once its authoritative value exists.
        if(excluded.has(id)&&draft&&!draft.validationErrors?.length&&doc?.card?.field_display?.[id]?.display!=='attention'){const rows=draft.selectedSkus||[],filled=rows.length?rows.every(row=>(listingEffective(draft,row.sku_id||row.id)[id]||[]).length):Boolean(draft.attributes[id]?.length);if(field.dataset.required!=='true'||filled)field.remove()}
    }
    for(const group of template.content.querySelectorAll('.official-field-group'))if(!group.querySelector('.official-field'))group.remove();
    const info=template.content.querySelector('.official-form>p.muted');if(info&&draft)info.textContent=Array.isArray(draft.form.category_path)?draft.form.category_path.join(' / '):draft.form.category_path||'已确认官方类目';
    return template.innerHTML;
};
async function benchLoadDocument(product=state.product,shop=benchShop()){
    const key=benchScopeKey(product,shop);if(listingBench.loading.has(key))return;
    listingBench.loading.add(key);
    try{
        const [result,prefix]=await Promise.all([api(`/api/workbench/products/${encodeURIComponent(product)}/listing-document${shop?`?shop=${encodeURIComponent(shop)}`:''}`),shop?api(`/api/workbench/offer-prefix?profile_id=${encodeURIComponent(benchProfileId())}&shop=${encodeURIComponent(shop)}`):Promise.resolve(null)]);
        const category=result.document?.card?.category||{};
        const documentKey=`${product}:${shop}:${category.category_id||''}:${category.type_id||''}`;
        if(documentKey===key)listingBench.documents.set(key,result.document);
        const previousProfile=listingBench.profiles.get(shop);
        if(prefix?.profile&&(!previousProfile?.saved||prefix.profile.saved&&String(prefix.profile.updated_at||'')>=String(previousProfile.updated_at||'')))listingBench.profiles.set(shop,prefix.profile);
        if(product===state.product&&key===benchScopeKey())listingBench.errors.delete(product);
    }catch(error){listingBench.documents.delete(key);if(product===state.product&&key===benchScopeKey())listingBench.errors.set(product,error.message)}finally{listingBench.loading.delete(key)}
}
const benchFetchProduct=fetchProduct;
fetchProduct=async function(...args){const result=await benchFetchProduct(...args),product=state.product;if(product){await benchLoadDocument(product);if(state.product===product&&state.view==='product')renderProduct()}return result};
const benchStepRenderer=renderProduct;
function benchCardConfirmationState(g=state.guided){
    const sections=g?.review?.sections||{},basic=productDraft(),official=listingDraft(),reasons=[];
    if(g?.card_ready!==true)reasons.push('请先点击「填充并检查上架卡片」，成功编译当前资料后才能确认。');
    if(g?.category_selection?.shop_id&&g.category_selection.shop_id!==benchShop())reasons.push('当前店铺与已确认类目不一致，请重新确认类目并编译卡片。');
    if(basic?.dirty||basic?.pending?.size||official?.dirty)reasons.push('有未保存的商品资料、价格或官方属性，请先保存再重新编译。');
    if(listingFlow.skuDrafts?.has(state.product)||listingFlow.factDrafts?.has(state.product)||listingFlow.videoDrafts?.has(state.product))reasons.push('规格、补充事实或视频选择有未保存的修改。');
    for(const section of ['grouping','fields']){
        if(!sections[section])reasons.push(section==='grouping'?'尚未生成规格分组结果。':'尚未生成上架字段结果。');
        else reasons.push(...(sections[section].problems||[]));
    }
    const approved=sections.grouping?.approved===true&&sections.fields?.approved===true;
    return {ready:reasons.length===0,approved,reasons:[...new Set(reasons)]};
}
function benchCardConfirmationHtml(){
    const check=benchCardConfirmationState();
    return `<div class="bench-card-confirmation"><div class="flow-actionrow">${benchButton(check.approved?'规格分组与上架字段已确认':'确认规格分组与上架字段','confirm-card','',!check.ready||check.approved||listingFlow.busy)}</div>${check.reasons.length?`<ul class="field-help">${check.reasons.map(reason=>`<li>${esc(reason)}</li>`).join('')}</ul>`:'<p class="field-help">请核对当前规格、分组和上架字段后确认。这一步只保存人工审核，不向 Ozon 发布。</p>'}</div>`;
}
async function benchEnsureDocument(){
    const product=state.product,shop=benchShop(),key=benchScopeKey(product,shop);
    if(!product||!state.guided||benchDocument()||listingBench.loading.has(key)||listingBench.errors.has(product))return;
    await benchLoadDocument(product,shop);
    if(state.product===product&&state.view==='product'&&benchScopeKey()===key&&benchDocument())renderProduct();
}
renderProduct=function(){
    benchStepRenderer();if(!state.guided||!state.product)return;
    $('#main').classList.toggle('listing-card',flowStep()==='card');
    const card=document.querySelector('[data-pane="card"]'),official=$('#officialFieldsSection'),footer=card?.querySelector('.flow-footer'),group=[...card?.querySelectorAll('.panel')||[]].find(x=>x.querySelector('h2')?.textContent.includes('SKU 分组'));
    if(card){
        card.replaceChildren();card.insertAdjacentHTML('beforeend',benchOperationalHtml());
        const prefix=card.querySelector('.bench-prefix'),profile=listingBench.profiles.get(benchShop());
        if(profile?.saved&&prefix){
            const settings=document.createElement('details');settings.className='bench-prefix-settings';
            settings.innerHTML=`<summary>货号规则：${esc(profile.prefix)}.日期.流水号 · 修改前缀</summary>`;
            prefix.replaceWith(settings);settings.append(prefix);
            if(!benchDocument()?.offer_ids?.complete)card.querySelector('.bench-sku-operational').insertAdjacentHTML('afterend',`<div class="flow-actionrow">${benchButton('分配当前规格货号','reserve-offers')}</div>`);
        }else if(prefix?.querySelector('button'))prefix.querySelector('button').textContent='保存前缀并分配货号';
        if(official)card.append(official);
        if(group&&(state.skus?.active_count||0)>1)card.append(group);
        card.insertAdjacentHTML('beforeend',`<section class="panel bench-card-check"><h3>检查卡片</h3><p class="field-help">摘要、已确认文案与官方属性使用同一份商品资料。保存修改后，自动填充已知内容并核对缺项。</p><div class="row"><button class="btn secondary" data-action="listing-autofill">补齐有依据的官方选项</button>${flowButton('填充并检查上架卡片','prepare-card',!state.guided.workflow?.copy?.confirmed||listingFlow.busy)}</div>${benchCardConfirmationHtml()}</section>`);
        if(footer)card.append(footer);
        const error=listingBench.errors.get(state.product);if(error)card.insertAdjacentHTML('afterbegin',`<p class="flow-error" role="alert">读取商品资料失败：${esc(error)} ${benchButton('重新读取','reload-document')}</p>`);
        const operationError=flowOperationErrorHtml('card');if(operationError)card.insertAdjacentHTML('afterbegin',operationError);
    }
    hydrateProductFields();renderProductSupport();void benchEnsureDocument();
};
document.addEventListener('input',event=>{if(event.target.id==='benchOfferPrefix')listingBench.prefixDrafts.set(benchShop(),event.target.value)});
document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-bench-action]');if(!button||!['reserve-offers','save-operational','reload-document','confirm-card'].includes(button.dataset.benchAction))return;
    const product=state.product,shop=benchShop(),action=button.dataset.benchAction;let ownsBusy=false;button.disabled=true;
    try{
        if(action==='confirm-card'){
            if(listingFlow.busy)throw Error('当前操作仍在处理中，请稍后确认');
            captureProductFields();const check=benchCardConfirmationState();if(!check.ready)throw Error(check.reasons.join('；'));
            if(!confirm('确认已人工核对当前上架规格、分组及卡片字段？只保存本地审核结果，不会提交 Ozon 或修改线上库存。'))return;
            listingFlow.busy=true;ownsBusy=true;
            const sections=state.guided.review.sections;
            for(const section of ['grouping','fields']){
                if(sections[section]?.approved)continue;
                if(product===state.product&&shop===benchShop()){
                    captureProductFields();const latest=benchCardConfirmationState();if(!latest.ready)throw Error(latest.reasons.join('；'));
                }
                const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/approve`,json('POST',{section}));
                if(result.review){Object.assign(sections,result.review.sections||{});if(product===state.product&&shop===benchShop())state.guided.review=result.review}
            }
            notice('规格分组与上架字段已人工确认；尚未提交 Ozon');
            if(product===state.product&&shop===benchShop())await refreshProduct();
            return;
        }
        if(action==='reserve-offers'){
            const prefix=$('#benchOfferPrefix').value.trim(),profile_id=benchProfileId();if(!shop)throw Error('请选择已授权店铺');if(!prefix)throw Error('请填写固定货号前缀，例如 xzj.jp');
            const result=await api('/api/workbench/offer-prefix',json('PUT',{shop,prefix,profile_id}));listingBench.profiles.set(shop,result.profile);
            await api(`/api/workbench/products/${encodeURIComponent(product)}/offer-ids/reserve`,json('POST',{shop,profile_id}));await benchLoadDocument(product,shop);notice('货号已分配，保存和预览不会重复占用流水号');
        }
        if(action==='save-operational'){
            const draft=productDraft();captureProductFields();
            const beforeDetails={...draft.details},beforePrices=structuredClone(draft.prices);
            const prices=[...document.querySelectorAll('.skuPrice')].map(input=>({sku_id:input.dataset.sku,price:Number(input.value),currency:input.parentElement.querySelector('select').value}));
            if(prices.some(x=>!Number.isFinite(x.price)||x.price<=0))throw Error('请为每个所选规格填写大于 0 的上架价格');
            const details=Object.fromEntries(listingDetailKeys.map(key=>[key,String(draft.details[key]??'').trim()?key==='material'?String(draft.details[key]).trim():Number(draft.details[key]):null]));
            const saved=await api(`/api/workbench/products/${encodeURIComponent(product)}/listing-details`,json('PUT',{details}));
            for(const key of listingDetailKeys)if(draft.details[key]===beforeDetails[key]){draft.details[key]=saved.details[key]??null;draft.touched.delete(key);if(saved.provenance?.[key])draft.provenance[key]=saved.provenance[key]}
            draft.dirty=draft.touched.size>0;
            await api(`/api/workbench/products/${encodeURIComponent(product)}/prices`,json('PUT',{prices}));
            if(JSON.stringify(draft.prices)===JSON.stringify(beforePrices))draft.pending?.delete('prices');
            notice(draft.dirty||draft.pending?.has('prices')?'已保存发送时的资料；保存期间的新输入仍保留，请再次保存':'价格和含包装信息已保存，请保存官方属性并检查卡片');
            if(product===state.product)await refreshProduct();else await benchLoadDocument(product,shop);
        }else await benchLoadDocument(product,shop);
        if(state.product===product&&state.view==='product')renderProduct();
    }catch(error){notice(error.message,true);const host=product===state.product?$('#listingDetailsError'):null;if(host){host.hidden=false;host.textContent=error.message}}finally{if(action==='confirm-card'){if(ownsBusy)listingFlow.busy=false;if(!listingFlow.busy&&product===state.product&&shop===benchShop()&&state.view==='product')renderProduct();else if(button.isConnected)button.disabled=false}else if(button.isConnected)button.disabled=false}
});
function benchEqualNumeric(left,right){
    const a=String(left??'').trim(),b=String(right??'').trim();
    if(!a||!b)return a===b;
    const x=Number(a),y=Number(b);return Number.isFinite(x)&&Number.isFinite(y)?x===y:a===b;
}
function captureProductFields(){
    const draft=productDraft();if(!draft)return;
    document.querySelectorAll('[data-pane="card"] .dim,[data-pane="card"] .skuPrice').forEach(input=>{
        if(input.classList.contains('dim')){
            const key=`${input.dataset.kind}_${input.dataset.key}`;
            if(!benchEqualNumeric(input.value,draft.details[key]))captureProductInput(input);
        }else if(input.classList.contains('skuPrice')){
            const saved=draft.prices[input.dataset.sku]||state.guided?.manual_prices?.prices?.[input.dataset.sku]||{};
            const currency=input.parentElement.querySelector('.priceCurrency')?.value||input.parentElement.querySelector('select')?.value||'CNY';
            const savedCurrency=saved.currency||selectedReadStore()?.default_currency_code||'CNY';
            if(!benchEqualNumeric(input.value,saved.price)||currency!==savedCurrency)captureProductInput(input);
        }
    });
}
