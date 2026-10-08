/* Supplier details stay private. Saving this draft never writes Ozon stock. */
const listingPublication = {
    configs:new Map(), warehouses:new Map(), histories:new Map(), operations:new Set()
};
function publicationKey(product=state.product,shop=benchShop()){return JSON.stringify([product||'',shop||''])}
function publicationCurrent(){return listingPublication.configs.get(publicationKey())}
function publicationDefaults(shop){return {shop,warehouse_id:null,warehouse_name:null,stock:100,stock_by_sku:{},source_note:'',source_url:'',saved:false}}
function publicationEntry(product=state.product,shop=benchShop()){
    const key=publicationKey(product,shop);
    if(!listingPublication.configs.has(key))listingPublication.configs.set(key,{product,shop,data:publicationDefaults(shop),values:{warehouse_id:'',stock:'100',stock_by_sku:{},source_note:''},dirty:false,revision:0,loaded:false,loading:false,error:''});
    return listingPublication.configs.get(key);
}
function publicationSourceUrl(value){
    try{const url=new URL(String(value||''));return ['http:','https:'].includes(url.protocol)&&url.hostname==='detail.1688.com'&&!url.username&&!url.password&&/^\/offer\/\d+\.html$/.test(url.pathname)?`https://detail.1688.com${url.pathname}`:''}catch{return ''}
}
function publicationSelectedSkus(){return (state.skus?.skus||[]).filter(row=>row.listed).map(row=>({id:String(row.sku_id),name:row.sku_name||row.sku_id}))}
function publicationSelectionSignature(product=state.product,shop=benchShop()){
    if(product!==state.product||shop!==benchShop())return '';
    const offers=benchDocument()?.offer_ids?.offers||{};
    return JSON.stringify([publicationSelectedSkus().map(row=>[row.id,offers[row.id]||'']).sort((a,b)=>a[0].localeCompare(b[0])),publicationSourceUrl(state.guided?.source?.source_url)]);
}
function publicationStock(value){
    const text=String(value??'').trim();
    if(!/^\d+$/.test(text)||!Number.isSafeInteger(Number(text))||Number(text)>1000000)throw Error('库存必须填写 0–1000000 的整数');
    return Number(text);
}
function publicationVerifiedStock(row){
    const snapshot=row.stock_readback||{},reserved=snapshot.reserved,free=snapshot.free_stock??(snapshot.present!=null&&reserved!=null?Number(snapshot.present)-Number(reserved):null);
    if(snapshot.absence===true)return '该仓暂无库存记录（已回读）';
    if(free!=null&&Number.isSafeInteger(Number(free)))return `可售 ${Number(free)} 件${reserved!=null&&Number.isSafeInteger(Number(reserved))?`，预留 ${Number(reserved)} 件`:''}（已回读）`;
    if(snapshot.present!=null&&Number.isSafeInteger(Number(snapshot.present)))return `在库 ${Number(snapshot.present)} 件（可售数量待回读）`;
    return ['complete','completed','updated','stock_complete','stock_acknowledged'].includes(row.stock_status)?'API 已确认同步，数量待回读':'未确认';
}
function publicationStockComplete(row){return ['complete','completed','updated','stock_complete','readback_confirmed'].includes(row.stock_status)&&row.stock_readback_matches===true}
function publicationRecordDetails(row){
    const attempts=Array.isArray(row.attempts)?row.attempts:[],ids=[row.task_id?['导入任务',row.task_id]:null,row.ozon_product_id?['Ozon 商品 ID',row.ozon_product_id]:null,row.source_sku_id?['货源规格',row.source_sku_id]:null].filter(Boolean);
    if(!ids.length&&!attempts.length)return '';
    return `<details class="publication-record-details"><summary>查看货号记录</summary><dl>${ids.map(([label,value])=>`<dt>${label}</dt><dd>${esc(value)}</dd>`).join('')}</dl>${attempts.length?`<ol>${attempts.map(attempt=>`<li>${esc(publicationTime(attempt.created_at||attempt.updated_at||attempt.at||attempt.started_at))} · ${esc(attempt.kind||attempt.action||attempt.operation||'接口尝试')} · ${esc(attempt.status||attempt.state||'已记录')}${attempt.error?`<p class="inline-error">${esc(typeof attempt.error==='string'?attempt.error:JSON.stringify(attempt.error))}</p>`:''}${attempt.details?`<details><summary>接口记录</summary><pre>${esc(JSON.stringify(attempt.details,null,2).slice(0,8000))}</pre></details>`:''}</li>`).join('')}</ol>`:''}</details>`;
}
function publicationButton(text,action,extra='',disabled=false){return `<button type="button" class="btn secondary" data-publication-action="${action}" ${extra} ${disabled?'disabled':''}>${text}</button>`}
function publicationScopeAttrs(entry){return `data-publication-product="${esc(entry.product)}" data-publication-shop="${esc(entry.shop)}"`}
function publicationConfigHtml(){
    const entry=publicationEntry(),cache=listingPublication.warehouses.get(entry.shop),busy=entry.loading||listingPublication.operations.has(publicationKey()),values=entry.values,frozen=entry.data.frozen===true;
    const source=publicationSourceUrl(entry.data.source_url||state.guided?.source?.source_url),eligible=(cache?.data?.items||[]).filter(row=>row.eligible===true);
    const selected=eligible.find(row=>String(row.warehouse_id)===String(values.warehouse_id));
    const selectedMissing=cache?.data?.complete===true&&values.warehouse_id&&!selected;
    const overrides=Object.entries(values.stock_by_sku||{}).filter(([sku])=>publicationSelectedSkus().some(row=>row.id===sku));
    return `<section class="panel publication-config" ${publicationScopeAttrs(entry)} aria-labelledby="publicationConfigTitle"><div class="publication-heading"><h3 id="publicationConfigTitle">货源与仓库</h3><span class="tag ${entry.data.saved&&!entry.dirty?'ok':'warn'}">${frozen?'提交配置已锁定':entry.loading?'读取中':entry.dirty?'修改未保存':entry.data.saved?'草稿已保存':'待配置'}</span></div><div class="publication-grid"><label class="field publication-source"><strong>货源链接</strong><div class="publication-source-control"><input readonly value="${esc(source)}" aria-label="采集的 1688 货源链接" placeholder="未采集到 1688 货源链接">${source?`<a href="${esc(source)}" target="_blank" rel="noopener noreferrer" class="publication-source-open">打开货源</a>`:''}</div></label><label class="field"><strong>货源备注</strong><input data-publication-input="source_note" ${frozen?'readonly':''} value="${esc(values.source_note)}" maxlength="500" placeholder="选填，仅保存到内部上架记录"></label><label class="field"><strong>选择仓库</strong><select data-publication-input="warehouse_id" aria-label="上架仓库" ${frozen||!entry.shop||!cache?.data?.complete?'disabled':''}><option value="">${!entry.shop?'先选择已授权店铺':cache?.loading?'正在读取仓库':!cache?.data?.complete?'先读取店铺仓库':'请选择仓库'}</option>${selectedMissing?`<option value="${esc(values.warehouse_id)}" selected disabled>原仓库不可用，请重新选择</option>`:''}${eligible.map(row=>`<option value="${esc(row.warehouse_id)}" ${String(row.warehouse_id)===String(values.warehouse_id)?'selected':''}>${esc(row.name||row.warehouse_id)}</option>`).join('')}</select></label><label class="field"><strong>每个 SKU 的库存</strong><input data-publication-input="stock" ${frozen?'readonly':''} type="number" min="0" max="1000000" step="1" inputmode="numeric" value="${esc(values.stock)}" aria-label="每个 SKU 的库存数量"><span class="field-help">${frozen?'已保存的计划库存。线上数量以回读记录为准。':'默认 100，可修改。导入成功后才向所选仓库同步。'}</span></label></div>${overrides.length?`<details class="publication-overrides"><summary>部分规格有单独库存（${overrides.length} 项）</summary>${overrides.map(([sku,value])=>`<label class="field">${esc(publicationSelectedSkus().find(row=>row.id===sku)?.name||sku)}<input type="number" min="0" max="1000000" step="1" data-publication-stock-sku="${esc(sku)}" ${frozen?'readonly':''} value="${esc(value)}"></label>`).join('')}<p class="field-help">修改上方公共库存会清除单独库存，重新应用于所有所选规格。</p></details>`:''}<div class="publication-actions">${publicationButton(cache?.data?.complete?'刷新真实仓库':'读取真实仓库','refresh-warehouses','',!entry.shop||cache?.loading)}${frozen?'<span class="field-help">已提交配置只读；补库存使用下方按货号保存的记录。</span>':publicationButton(busy?'正在保存…':'保存货源与仓库','save-config','',!entry.shop||busy)}</div>${cache?.error?`<p class="inline-error" role="alert">${esc(cache.error)}</p>`:''}${cache?.data?.complete&&!eligible.length?'<p class="inline-error" role="alert">该店铺暂无可同步库存的仓库。请在 Ozon 后台确认仓库状态后刷新。</p>':''}${selectedMissing?'<p class="inline-error" role="alert">已保存仓库不在当前可用列表中，发布前请重新选择并保存。</p>':''}${entry.error?`<p class="inline-error" role="alert">${esc(entry.error)} ${publicationButton('重新读取草稿','reload-config','',entry.loading)}</p>`:''}<p class="field-help">仓库来自当前店铺；货源与备注仅供内部使用。保存草稿不会创建商品或修改线上库存。</p></section>`;
}
function publicationSummaryHtml(){
    const entry=publicationEntry(),values=entry.values,cache=listingPublication.warehouses.get(entry.shop),warehouse=(cache?.data?.items||[]).find(row=>String(row.warehouse_id)===String(values.warehouse_id));
    const name=warehouse?.name||entry.data.warehouse_name||'未选择仓库',source=publicationSourceUrl(entry.data.source_url||state.guided?.source?.source_url),offers=benchDocument()?.offer_ids?.offers||{};
    const rows=publicationSelectedSkus();
    return `<section class="panel publication-summary publication-preview" ${publicationScopeAttrs(entry)}><div class="publication-heading"><h2>发布目标与库存</h2><span class="tag ${entry.data.saved&&!entry.dirty?'ok':'warn'}">${entry.dirty?'配置未保存':entry.data.saved?'配置已保存':'尚未配置'}</span></div><dl class="publication-summary-meta"><dt>店铺</dt><dd>${esc(selectedReadStore()?.display_name||entry.shop||'未选择店铺')}</dd><dt>仓库</dt><dd>${esc(name)}</dd>${source?`<dt>货源</dt><dd><a href="${esc(source)}" target="_blank" rel="noopener noreferrer">${esc(source)}</a></dd>`:''}</dl><div class="tablewrap"><table><thead><tr><th>货号</th><th>规格</th><th>计划库存</th></tr></thead><tbody>${rows.map(row=>`<tr><td>${esc(offers[row.id]||'未分配货号')}</td><td>${esc(row.name)}</td><td>${esc(values.stock_by_sku?.[row.id]??values.stock)}</td></tr>`).join('')||'<tr><td colspan="3">尚未选择上架规格。</td></tr>'}</tbody></table></div><p class="field-help">商品导入成功后同步库存；API 受理、商品审核与库存同步分别记录。</p>${publicationButton('返回填写卡片','edit-config')}</section>`;
}
function publicationRequireReady(){
    const shop=benchShop(),entry=publicationCurrent(),cache=listingPublication.warehouses.get(shop);
    if(!shop)throw Error('请选择已授权店铺');
    if(!entry?.loaded||entry.loading)throw Error('正在读取仓库配置，请稍后重试');
    if(entry.error)throw Error(`仓库配置读取失败：${entry.error}`);
    if(entry.data.frozen===true)throw Error('该商品已有提交记录，请在上架记录继续库存流程或刷新现有结果，不要重复提交');
    if(entry.selectionSignature!==publicationSelectionSignature())throw Error('规格或货号已改变，请重新读取并保存仓库配置');
    if(entry.dirty||!entry.data.saved)throw Error('请先在填写卡片步骤保存货源与仓库配置');
    if(entry.data.shop!==shop)throw Error('店铺已改变，请为当前店铺重新配置仓库');
    if(!cache?.data?.complete)throw Error('请先读取并确认当前店铺的真实仓库');
    if(cache.error)throw Error(`最近一次仓库读取失败：${cache.error}`);
    const warehouse=(cache.data.items||[]).find(row=>row.eligible===true&&String(row.warehouse_id)===String(entry.data.warehouse_id));
    if(!warehouse)throw Error('已选仓库不可用，请重新选择并保存');
    const stock=publicationStock(entry.data.stock),stock_by_sku={};
    const offers=benchDocument()?.offer_ids?.offers||{},items=publicationSelectedSkus().map(row=>{const quantity=publicationStock(entry.data.stock_by_sku?.[row.id]??stock);stock_by_sku[row.id]=quantity;return {source_sku_id:row.id,name:row.name,sku_name:row.name,offer_id:offers[row.id]||'',stock:quantity}});
    if(!items.length||items.some(row=>!row.offer_id))throw Error('请先选择上架规格并分配每个规格的货号');
    return {config:entry.data,shop,warehouse,stock_by_sku,items};
}
function publicationCaptureInput(input){
    const host=input.closest('[data-publication-product]');if(!host)return;
    const entry=publicationEntry(host.dataset.publicationProduct,host.dataset.publicationShop),key=input.dataset.publicationInput,sku=input.dataset.publicationStockSku;
    if(!['warehouse_id','stock','source_note'].includes(key)&&!sku)return;
    if(entry.data.frozen===true)return;
    if(sku)entry.values.stock_by_sku[sku]=input.value;
    else{entry.values[key]=input.value;if(key==='stock'){entry.values.stock_by_sku={};host.querySelector('.publication-overrides')?.remove()}}
    entry.dirty=true;entry.revision++;entry.error='';
    const badge=host.querySelector('.publication-heading .tag');if(badge){badge.textContent='修改未保存';badge.className='tag warn'}
}
function publicationRedraw(product,shop){
    if(product!==state.product||shop!==benchShop()||state.view!=='product'||!['card','preview'].includes(flowStep()))return;
    const focused=document.activeElement,owner=focused?.closest?.('[data-publication-product]');
    const focus=owner?.dataset.publicationProduct===product&&owner?.dataset.publicationShop===shop?{
        input:focused.dataset.publicationInput,sku:focused.dataset.publicationStockSku,history:focused.dataset.publicationHistory,
        start:focused.selectionStart,end:focused.selectionEnd}:null;
    captureProductFields();renderProduct();
    if(focus&&(focus.input||focus.sku||focus.history)){
        const next=[...document.querySelectorAll('[data-publication-input],[data-publication-stock-sku],[data-publication-history]')].find(input=>{
            const host=input.closest('[data-publication-product]');
            return host?.dataset.publicationProduct===product&&host?.dataset.publicationShop===shop&&input.dataset.publicationInput===focus.input&&input.dataset.publicationStockSku===focus.sku&&input.dataset.publicationHistory===focus.history;
        });
        if(next){next.focus({preventScroll:true});if(typeof focus.start==='number'&&typeof next.setSelectionRange==='function')try{next.setSelectionRange(focus.start,focus.end)}catch{/* Number controls do not support text ranges. */}}
    }
}
async function publicationLoadConfig(product=state.product,shop=benchShop(),force=false){
    if(!product||!shop)return;
    const entry=publicationEntry(product,shop);if(entry.loading||entry.loaded&&!force)return;
    entry.loading=true;const selectionSignature=publicationSelectionSignature(product,shop);
    try{
        const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/publication-config?shop=${encodeURIComponent(shop)}`),config=result.config;
        if(!config||config.shop!==shop)throw Error('仓库配置与所选店铺不匹配');
        entry.data={...publicationDefaults(shop),...config};entry.loaded=true;entry.error='';entry.selectionSignature=selectionSignature;
        if(!entry.dirty||config.frozen===true){entry.values={warehouse_id:String(config.warehouse_id||''),stock:String(config.stock??100),stock_by_sku:{...(config.stock_by_sku||{})},source_note:config.source_note||''};if(config.frozen===true)entry.dirty=false}
    }catch(error){entry.error=error.message;entry.loaded=true;entry.selectionSignature=selectionSignature}
    finally{entry.loading=false;publicationRedraw(product,shop)}
}
async function publicationLoadWarehouses(shop=benchShop(),refresh=false,product=state.product){
    if(!shop)return;
    let cache=listingPublication.warehouses.get(shop);if(!cache){cache={loaded:false,loading:false,error:'',data:null};listingPublication.warehouses.set(shop,cache)}
    if(cache.loading||cache.loaded&&!refresh)return;cache.loading=true;
    try{
        const result=await api(refresh?'/api/workbench/warehouses/refresh':`/api/workbench/warehouses?shop=${encodeURIComponent(shop)}`,refresh?json('POST',{shop}):undefined);
        if(result.shop!==shop||!Array.isArray(result.items))throw Error('仓库列表与所选店铺不匹配');
        cache.data=result;cache.loaded=true;cache.error='';
    }catch(error){cache.error=error.message;cache.loaded=true}
    finally{cache.loading=false;publicationRedraw(product,shop)}
}
async function publicationSaveConfig(product=state.product,shop=benchShop()){
    const entry=publicationEntry(product,shop),cache=listingPublication.warehouses.get(shop),revision=entry.revision,selectionSignature=publicationSelectionSignature(product,shop);
    if(!shop)throw Error('请选择已授权店铺');
    if(entry.data.frozen===true)throw Error('已提交的仓库配置已经锁定，库存恢复使用原货号记录，不会改为新的仓库或数量');
    if(product!==state.product||shop!==benchShop())throw Error('商品或店铺已改变，请返回对应商品保存配置');
    const warehouse=(cache?.data?.items||[]).find(row=>row.eligible===true&&String(row.warehouse_id)===String(entry.values.warehouse_id));
    if(!cache?.data?.complete||!warehouse)throw Error('请读取真实仓库并选择可用仓库');
    const selected=new Set(publicationSelectedSkus().map(row=>row.id));
    const stock=publicationStock(entry.values.stock),stock_by_sku=Object.fromEntries(Object.entries(entry.values.stock_by_sku||{}).filter(([sku])=>selected.has(sku)).map(([sku,value])=>[sku,publicationStock(value)]));
    const source_note=String(entry.values.source_note||'');if(source_note.length>500)throw Error('货源备注最多 500 个字符');
    const payload={shop,warehouse_id:String(warehouse.warehouse_id),stock,stock_by_sku,source_note};
    const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/publication-config`,json('PUT',payload));
    if(!result.config||result.config.shop!==shop)throw Error('保存结果与店铺不匹配，请重新读取草稿');
    entry.data={...publicationDefaults(shop),...result.config};entry.loaded=true;entry.error='';entry.selectionSignature=selectionSignature;
    if(entry.revision===revision){entry.values={warehouse_id:String(entry.data.warehouse_id||''),stock:String(entry.data.stock??100),stock_by_sku:{...(entry.data.stock_by_sku||{})},source_note:entry.data.source_note||''};entry.dirty=false}
    return {saved:!entry.dirty,config:entry.data};
}
function publicationHistoryEntry(product=state.product,shop=benchShop()){
    const key=publicationKey(product,shop);if(!listingPublication.histories.has(key))listingPublication.histories.set(key,{product,shop,scope:'product',query:'',offset:0,limit:30,loading:false,loaded:false,error:'',data:{items:[],total:0,next_offset:null},request:0});return listingPublication.histories.get(key);
}
function publicationStatus(kind,value){
    const labels={import:{imported:'已导入',complete:'已导入',completed:'已导入',processing:'处理中',pending:'等待导入',submitted:'已受理',rejected:'导入拒绝',failed:'导入失败',unknown:'导入待核对'},stock:{complete:'已同步',completed:'已同步',updated:'接口已确认，回读待核对',stock_acknowledged:'库存接口已确认，待实际回读',readback_confirmed:'实际库存回读已核对',stock_complete:'已同步',pending:'待同步',stock_pending:'待同步',failed:'同步失败',partial:'部分同步',stock_partial:'部分同步',unknown:'结果待核对',stock_unknown:'结果待核对',not_started:'未同步',not_configured:'未配置',pending_price:'等待价格生效',ready:'等待同步',running:'同步中',rate_limited:'调用受限，稍后继续',readback_mismatch:'回读库存不一致'},product:{imported:'已创建，待审核',processing:'处理中',moderation:'审核中',approved:'审核通过',rejected:'审核未通过',unknown:'待回读',not_checked:'待回读',price_sent:'价格已发送，待处理',stock_sent:'库存已发送，待处理',sale:'在售（平台状态）',active:'启用（平台状态）',sent:'已发送，待处理',sent_for_moderation:'已送审',awaiting_approval:'等待审核',failed:'平台处理失败',not_sent:'尚未发送'}};
    return labels[kind]?.[value]||value||(kind==='stock'?'未同步':'待回读');
}
function publicationTime(value){try{const date=new Date(value);return Number.isNaN(date.getTime())?'—':new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date)}catch{return '—'}}
function publicationHistoryHtml(){
    const entry=publicationHistoryEntry(),data=entry.data,items=data.items||[];
    return `<section class="panel publication-history" ${publicationScopeAttrs(entry)} aria-labelledby="publicationHistoryTitle"><div class="publication-heading"><h2 id="publicationHistoryTitle">上架记录</h2><span class="tag">${Number(data.total)||0} 条</span></div><div class="publication-history-search publication-history-tools"><label class="field"><strong>记录范围</strong><select data-publication-history="scope" aria-label="上架记录范围"><option value="product" ${entry.scope==='product'?'selected':''}>当前商品</option><option value="shop" ${entry.scope==='shop'?'selected':''}>当前店铺全部货号</option></select></label><label class="field publication-history-query"><strong>按货号查找</strong><input data-publication-history="query" value="${esc(entry.query)}" maxlength="100" placeholder="输入完整货号或前缀" aria-label="按货号搜索上架记录"></label>${publicationButton(entry.loading?'读取中…':'查询记录','search-history','',entry.loading)}</div>${entry.error?`<p class="inline-error" role="alert">${esc(entry.error)}</p>`:''}${entry.loading?'<p class="field-help" role="status">正在读取内部上架记录…</p>':''}<div class="tablewrap publication-history-scroll"><table class="publication-history-table"><thead><tr><th>货号 / 货源</th><th>仓库 / 库存</th><th>商品导入</th><th>商品状态</th><th>库存状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody>${items.map(row=>{
        const source=publicationSourceUrl(row.source_url),unknown=['unknown','stock_unknown'].includes(row.stock_status),pending=!publicationStockComplete(row),readbackOnly=['updated','stock_acknowledged'].includes(row.stock_status),recordError=row.error||row.stock_error||row.import_error||'';
        const officialId=String(row.ozon_sku||row.ozon_sku_id||'');const ozonUrl=/^\d+$/.test(officialId)?`https://www.ozon.ru/product/${officialId}/`:'';
        return `<tr><td><strong>${esc(row.offer_id||'未取得货号')}</strong><div>${source?`<a href="${esc(source)}" target="_blank" rel="noopener noreferrer">1688 货源</a>`:''}${ozonUrl?` <a href="${ozonUrl}" target="_blank" rel="noopener noreferrer">Ozon 商品卡</a>`:''}</div>${row.source_note?`<small>${esc(row.source_note)}</small>`:''}${publicationRecordDetails(row)}</td><td>${esc(row.warehouse_name||row.warehouse_id||'未记录仓库')}<br><span class="field-help">计划 ${esc(row.stock??'—')} 件</span><br><small>${esc(publicationVerifiedStock(row))}</small></td><td>${esc(publicationStatus('import',row.import_status))}</td><td>${esc(publicationStatus('product',row.product_status))}</td><td>${esc(publicationStockComplete(row)?'库存回读已匹配':publicationStatus('stock',row.stock_status))}${recordError?`<details><summary>查看问题</summary><p class="inline-error">${esc(typeof recordError==='string'?recordError:JSON.stringify(recordError))}</p></details>`:''}</td><td>${esc(publicationTime(row.updated_at))}</td><td>${row.stock_status==='not_configured'?'旧记录未配置库存':pending?`${unknown?`<label class="publication-retry-choice"><input type="checkbox" class="publicationRetryUnknown">我已核对，仍需重试</label>`:''}${publicationButton(unknown?'确认重试库存':readbackOnly?'刷新库存回读':'继续库存流程','continue-stock',`data-target-product="${esc(row.product_id)}" data-target-shop="${esc(row.shop||entry.shop)}" data-stock-unknown="${unknown}" data-stock-readback="${readbackOnly}"`,listingPublication.operations.has(publicationKey(row.product_id,row.shop||entry.shop))||!row.product_id)}`:'已完成库存同步'}</td></tr>`;
    }).join('')||`<tr><td colspan="7" class="publication-history-empty">${entry.loading?'正在读取…':entry.query?'没有找到该货号。可缩短搜索词或切换记录范围。':'还没有上架记录。提交后会按货号保存，未完成导入或库存同步也会保留状态。'}</td></tr>`}</tbody></table></div><div class="publication-history-pagination publication-pagination"><span class="field-help">${items.length?`${entry.offset+1}–${entry.offset+items.length} / ${Number(data.total)||items.length}`:'0 条'} · 当前店铺隔离保存</span><div>${publicationButton('上一页','history-previous','',entry.loading||entry.offset===0)}${publicationButton('下一页','history-next','',entry.loading||data.next_offset==null)}</div></div><p class="field-help">“已导入”不等于审核通过。“继续库存流程”仅回读已有提交并补库存，不会重新导入商品。</p></section>`;
}
async function publicationLoadHistory(product=state.product,shop=benchShop(),force=false){
    if(!product||!shop)return;
    const entry=publicationHistoryEntry(product,shop);if(entry.loading||entry.loaded&&!force)return;
    const request=++entry.request,scope=entry.scope,query=entry.query,offset=entry.offset;entry.loading=true;
    const url=scope==='shop'?'/api/workbench/publications':`/api/workbench/products/${encodeURIComponent(product)}/listing-publications`;
    try{
        const result=await api(url+'?'+new URLSearchParams({shop,q:query,limit:String(entry.limit),offset:String(offset)}));
        if(request===entry.request){entry.data=result;entry.error='';entry.loaded=true}
    }catch(error){if(request===entry.request){entry.error=error.message;entry.loaded=true}}
    finally{if(request===entry.request){entry.loading=false;publicationRedraw(product,shop)}}
}
async function publicationRefreshAfterSubmit(product=state.product,shop=benchShop()){
    const entry=publicationHistoryEntry(product,shop);entry.loaded=false;
    await Promise.all([publicationLoadHistory(product,shop,true),publicationLoadConfig(product,shop,true)]);
}
async function publicationContinueStock(product,shop,retryUnknown=false){
    const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/publications/continue`,json('POST',{shop,confirm:'UPDATE_STOCK',retry_unknown:retryUnknown}));
    // Cached record reads are safe; never re-import from this action.
    for(const entry of listingPublication.histories.values())if(entry.shop===shop)entry.loaded=false;
    if(state.product&&benchShop()===shop)await publicationLoadHistory(state.product,shop,true);
    return result;
}
async function publicationEnsureView(){
    if(!state.product||!state.guided||!['card','preview'].includes(flowStep()))return;
    const product=state.product,shop=benchShop();if(!shop)return;
    const entry=publicationEntry(product,shop),changed=entry.loaded&&entry.selectionSignature!==publicationSelectionSignature(product,shop);
    void publicationLoadConfig(product,shop,changed);void publicationLoadWarehouses(shop,false,product);
    if(flowStep()==='preview')void publicationLoadHistory(product,shop);
}
const publicationProductRenderer=renderProduct;
renderProduct=function(){
    publicationProductRenderer();if(!state.product||!state.guided)return;
    const card=document.querySelector('[data-pane="card"]'),operational=card?.querySelector('.bench-operational');
    if(operational)operational.insertAdjacentHTML('afterend',publicationConfigHtml());
    const preview=document.querySelector('[data-pane="preview"]');
    if(preview){preview.insertAdjacentHTML('afterbegin',publicationSummaryHtml());const footer=preview.querySelector('.flow-footer');if(footer)footer.insertAdjacentHTML('beforebegin',publicationHistoryHtml());else preview.insertAdjacentHTML('beforeend',publicationHistoryHtml())}
    void publicationEnsureView();
};
document.addEventListener('input',event=>{
    if(event.target.dataset.publicationInput||event.target.dataset.publicationStockSku)publicationCaptureInput(event.target);
    if(event.target.dataset.publicationHistory==='query'){const host=event.target.closest('[data-publication-product]'),entry=publicationHistoryEntry(host.dataset.publicationProduct,host.dataset.publicationShop);entry.query=event.target.value}
});
document.addEventListener('change',event=>{
    if(event.target.dataset.publicationInput==='warehouse_id')publicationCaptureInput(event.target);
    if(event.target.dataset.publicationHistory==='scope'){const host=event.target.closest('[data-publication-product]'),entry=publicationHistoryEntry(host.dataset.publicationProduct,host.dataset.publicationShop);entry.scope=event.target.value==='shop'?'shop':'product';entry.offset=0;entry.loaded=false;entry.request++;entry.loading=false;void publicationLoadHistory(entry.product,entry.shop,true)}
    if(event.target.id==='ozonStore'){renderProduct();void publicationEnsureView()}
});
document.addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.dataset.publicationHistory==='query'){event.preventDefault();event.target.closest('.publication-history')?.querySelector('[data-publication-action="search-history"]')?.click()}});
document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-publication-action]');if(!button)return;
    const host=button.closest('[data-publication-product]'),product=host?.dataset.publicationProduct||state.product,shop=host?.dataset.publicationShop||benchShop(),action=button.dataset.publicationAction;
    if(action==='edit-config'){captureProductFields();flowSetStep('card');return}
    const key=publicationKey(button.dataset.targetProduct||product,button.dataset.targetShop||shop);if(listingPublication.operations.has(key))return;
    listingPublication.operations.add(key);button.disabled=true;
    try{
        if(action==='refresh-warehouses')await publicationLoadWarehouses(shop,true,product);
        if(action==='reload-config')await publicationLoadConfig(product,shop,true);
        if(action==='save-config'){const saved=await publicationSaveConfig(product,shop);notice(saved.saved?'货源与仓库已保存；没有修改线上库存':'已保存发送时的配置；保存期间的新输入仍保留，请再次保存')}
        if(['search-history','history-previous','history-next'].includes(action)){
            const entry=publicationHistoryEntry(product,shop);entry.offset=action==='search-history'?0:action==='history-previous'?Math.max(0,entry.offset-entry.limit):entry.data.next_offset??entry.offset;await publicationLoadHistory(product,shop,true);
        }
        if(action==='continue-stock'){
            const target=button.dataset.targetProduct,targetShop=button.dataset.targetShop,unknown=button.dataset.stockUnknown==='true',readbackOnly=button.dataset.stockReadback==='true';
            if(!target||!targetShop||targetShop!==shop)throw Error('记录与当前店铺不匹配，请重新查询');
            if(unknown&&!button.closest('tr')?.querySelector('.publicationRetryUnknown')?.checked)throw Error('上次库存结果不确定。请先在 Ozon 后台核对，再勾选确认重试');
            const rows=(publicationHistoryEntry(product,shop).data.items||[]).filter(row=>row.product_id===target&&(row.shop||shop)===targetShop),offers=rows.map(row=>`${row.offer_id}（${row.warehouse_name||row.warehouse_id||'原仓库'}：${row.stock??'已保存'}件）`).join('\n');
            if(!confirm(`确认继续店铺「${targetShop}」商品 ${target} 的所有已保存货号的库存流程？\n以下仅为当前查询页中的记录：\n${offers||'以已保存台账为准'}\n${readbackOnly?'已收到接口确认的库存请求只刷新实际库存回读，不重复写入；同商品其他待补库存的货号仍按各自已保存目标执行。':'会回读已有任务，并为本商品已经导入的货号向各自记录中的仓库补库存。'}不会重新导入商品。${unknown?'\n你已核对本商品结果不明的库存调用；确认后可重试这些调用，可能覆盖当前库存。':'\n结果不明的库存调用不会自动重试，需先核对。'}`))return;
            const result=await publicationContinueStock(target,targetShop,unknown);notice(result.status==='stock_complete'?'库存回读已匹配计划数量，请核对商品审核状态':result.status==='stock_unknown'?'库存结果待核对，未自动重试':result.status==='stock_acknowledged'?'库存接口已确认，实际库存回读尚未匹配；请稍后刷新回读':result.pending?'导入或库存回读仍在处理中；请查看各货号状态':'库存流程已更新，请查看各货号状态',result.ok===false);
        }
    }catch(error){const entry=publicationEntry(product,shop);if(action==='save-config')entry.error=error.message;else if(action.includes('history')||action==='continue-stock')publicationHistoryEntry(product,shop).error=error.message;notice(error.message,true)}
    finally{listingPublication.operations.delete(key);if(button.isConnected)button.disabled=false;publicationRedraw(product,shop)}
});
// The sourcing note is an operational specification, not arbitrary copy.
const publicationSpecificationConfigHtml=publicationConfigHtml;
publicationConfigHtml=function(){
    const html=publicationSpecificationConfigHtml();
    if(!publicationEntry()?.data.source_note_auto)return html;
    return html.replace('<strong>货源备注</strong>','<strong>货源备注 · 商品规格</strong>')
        .replace(/data-publication-input="source_note"(?: readonly)?/,'data-publication-input="source_note" readonly')
        .replace('placeholder="选填，仅保存到内部上架记录">','placeholder="由已选商品规格自动填写"><span class="field-help">按真实规格自动记录，多规格发布后每个货号保存自己的完整规格。</span>');
};
