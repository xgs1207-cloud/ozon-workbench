/* Desktop listing flow. Reuses the official form editor, not the legacy all-at-once runner. */
const listingFlow = {steps: new Map(), busy: false, errors: new Map(), words: new Map(), skuDrafts:new Map(), factDrafts:new Map(), videoDrafts:new Map()};
const listingFlowSteps = [
    ['specs', '选择规格'], ['analysis', '商品摘要'], ['keywords', '选择关键词'],
    ['copy', '确认文案'], ['media', '图片与视频'], ['card', '填写卡片'], ['preview', '预览与发布']
];
const legacyProductRenderer = renderProduct;
const listingFlowStyle = document.createElement('style');
listingFlowStyle.textContent = `
 .listing-stepstrip{display:flex;gap:5px;margin:24px 0 34px;border-bottom:1px solid #e9edf3;padding-bottom:16px}
 .listing-stepstrip button{flex:1;border:0;background:white;display:flex;align-items:center;gap:7px;padding:8px 3px;font-size:12px;white-space:nowrap;color:#637389;cursor:pointer}
 .listing-stepstrip .step-number{width:27px;height:27px;border-radius:8px;background:#f1f4f8;display:grid;place-items:center;font-weight:650;flex-shrink:0}
 .listing-stepstrip button.on{color:#111820;font-weight:650}.listing-stepstrip button.on .step-number{background:#111820;color:white}
 .listing-stepstrip button.done .step-number{background:#edf6f2;color:#18704d}.listing-stepstrip button:focus-visible{outline:2px solid #1557e8;outline-offset:3px}
 .flow-pane[hidden]{display:none!important}.flow-pane .panel{margin-bottom:30px}.flow-lead{color:#637389;font-size:13px;line-height:1.8;margin-bottom:22px}
 .flow-footer{display:flex;justify-content:space-between;gap:12px;border-top:1px solid #edf0f4;padding-top:20px;margin-top:28px}
 .flow-summary-list{margin:0;padding-left:20px;font-size:14px;line-height:1.9}.flow-facts{display:grid;grid-template-columns:155px 1fr;margin:18px 0;font-size:13px}
 .flow-facts dt,.flow-facts dd{padding:12px 0;margin:0;border-bottom:1px solid #edf0f4;overflow-wrap:anywhere}.flow-facts dt{color:#637389}
 .flow-candidate{border:1px solid #dfe5ef;border-radius:12px;margin-bottom:18px;padding:21px}.flow-candidate.selected{border-color:#1557e8;background:#fbfcff}
 .flow-candidate h3{margin:0 0 12px!important}.flow-candidate p{line-height:1.8;font-size:13px;white-space:pre-wrap}.flow-candidate-title{font-size:15px!important;font-weight:650}
 .flow-tags{display:flex;flex-wrap:wrap;gap:6px;margin:12px 0}.flow-tags span{padding:5px 8px;background:#f2f5f9;border-radius:5px;font-size:12px;overflow-wrap:anywhere}
 .flow-keywords{max-height:310px;overflow:auto;margin:18px 0}.flow-keyword-row{display:grid;grid-template-columns:minmax(0,1fr) 95px 145px;align-items:center;gap:12px;border-bottom:1px solid #edf0f4;padding:12px 0;font-size:13px}
 .flow-video{border-top:1px solid #edf0f4;padding:18px 0}.flow-video video{width:100%;max-height:330px;border-radius:8px;background:#f6f8fb;margin:12px 0}.flow-video .text{width:100%}
 .flow-video-state{font-size:12px;color:#637389}.flow-error{padding:13px 16px;background:#fff1f2;color:#9b2331;border-radius:8px;font-size:13px;line-height:1.7;margin-bottom:20px;overflow-wrap:anywhere}
 .flow-status{font-size:13px;color:#637389;line-height:1.8;margin:14px 0}.flow-actionrow{display:flex;flex-wrap:wrap;gap:10px;margin:18px 0}
 .flow-publish-result{white-space:pre-wrap;font-size:12px;line-height:1.7;max-height:380px;overflow:auto}
 .flow-pane input[type=file]{display:block;margin:14px 0;font-size:13px}.flow-pane .product-source-images{margin:16px 0}
 .flow-slot{border-top:1px solid #edf0f4;padding:16px 0}.flow-slot summary{cursor:pointer;font-size:13px;font-weight:600;line-height:1.7}.flow-slot .slot{border:0;padding:18px 0 0}.flow-image-placeholder{width:100%;height:132px;border-radius:8px;display:grid;place-items:center;background:#f4f6fa;color:#8290a3;font-size:12px}
`;
document.head.append(listingFlowStyle);

function flowStep() {return listingFlow.steps.get(state.product) || 'specs'}
function flowSetStep(step) {
    listingFlow.steps.set(state.product, step);
    renderProduct();
    window.scrollTo({top:0, behavior:'instant'});
}
function flowButton(label, action, disabled=false, extra='') {
    return `<button class="btn ${action.startsWith('next')?'':'secondary'}" data-flow-action="${action}" ${disabled?'disabled':''} ${extra}>${label}</button>`;
}
function flowTags(tags) {return `<div class="flow-tags">${(tags||[]).map(x=>`<span>${esc(x)}</span>`).join('')}</div>`}
const flowFactLabels={title_cn:'商品名称',product_type:'商品类型',category_cn:'原始类目',material:'材质',materials:'材质',material_zh:'材质',material_ru:'材质（俄文）',brand:'品牌',pack_count:'包装数量',package_quantity:'包装内数量',skus:'所选规格',dimensions:'尺寸',weight:'重量',features:'已知特点',usage:'用途',load_capacity:'承重',certifications:'认证',functions:'功能',accessories:'配件',sku_id:'规格编号',name_cn:'规格名称',name_zh:'规格名称',properties:'规格参数',price_cny:'采购价（元）',image_refs:'图片来源'};
function flowScalar(value) {
    if (value == null || value === '' || value === 'unknown') return '未确认';
    if (Array.isArray(value)) return value.length?value.map(flowScalar).join('；'):'未确认';
    if (typeof value === 'object') return Object.entries(value).filter(([k])=>!['source_refs','metadata','schema_version','image_refs'].includes(k)).map(([k,v])=>`${flowFactLabels[k]||k}：${flowScalar(v)}`).join('；')||'未确认';
    return String(value);
}
function flowAnalysisHtml(g) {
    const a = g.workflow?.analysis || {}, data = a.payload || {}, facts = data.facts || {};
    const labels = flowFactLabels;
    const points = data.selling_points || data.key_selling_points || [];
    const unknowns = data.unknowns || data.missing_information || [];
    const stale = a.status === 'stale', extra=listingFlow.factDrafts.get(state.product)||{};
    return `<section class="panel"><h2>商品信息与卖点 <span class="tag ${a.confirmed?'ok':'warn'}">${a.confirmed?'已确认':stale?'内容已过期':a.status==='ready'?'待确认':'待分析'}</span></h2>
    <p class="flow-lead">豆包只总结本次所选规格。未知信息保持空白，卖点不能超出采集资料。修改规格或真实事实后，需要重新核对。</p>
    ${stale?'<div class="hint warn">下面是旧版本，仅供查看。请按最新规格重新分析，不能用于生成文案。</div>':''}
    ${Object.keys(facts).length?`<dl class="flow-facts">${Object.entries(facts).filter(([k])=>!['source_refs','selected_category'].includes(k)).map(([k,v])=>`<dt>${esc(labels[k]||k)}</dt><dd>${esc(flowScalar(v))}</dd>`).join('')}</dl>`:'<div class="empty">先确认要卖的规格，系统会自动开始商品分析；失败时可在这里重试。</div>'}
    <h3>可使用的卖点</h3>${points.length?`<ul class="flow-summary-list">${points.map(x=>`<li>${esc(typeof x==='string'?x:x.point_cn||x.text||x.claim||x.title||flowScalar(x))}</li>`).join('')}</ul>`:'<p class="muted">还没有可确认的卖点。</p>'}
    ${unknowns.length?`<h3>待补充信息</h3><ul class="flow-summary-list">${unknowns.map(x=>`<li>${esc(typeof x==='string'?x:`${labels[x.field]||x.field||'未知字段'}：${x.reason||'采集资料未能确认'}`)}</li>`).join('')}</ul>`:''}
    ${(data.risks||[]).length?`<h3>风险提示</h3><ul class="flow-summary-list">${data.risks.map(x=>`<li>${esc(x.message||flowScalar(x))}</li>`).join('')}</ul>`:''}
    <details style="margin:20px 0"><summary>补充有依据的材质或包装内数量</summary><div class="grid2" style="margin-top:14px"><label class="field">材质<input id="flowMaterial" value="${esc(extra.material??g.human_confirmations?.material??'')}" placeholder="无法确认时留空"></label><label class="field">包装内数量<input id="flowQuantity" type="number" min="1" value="${esc(extra.quantity??g.human_confirmations?.package_quantity??'')}"></label></div>${flowButton('保存事实并重新分析','facts')}</details>
    <div class="flow-actionrow">${flowButton(a.status==='missing'?'分析所选规格':'按最新资料分析','analyze',!state.skus?.active_count||listingFlow.busy)}${flowButton('确认摘要和卖点','confirm-analysis',a.status!=='ready'||listingFlow.busy)}</div>
    <p class="field-help">相同输入优先使用已保存结果，不重复调用；此处不会生成图片或提交商品。</p></section>`;
}
function flowCandidatesHtml(g) {
    const c = g.workflow?.copy || {}, modes={search_first:'搜索匹配优先',conversion_first:'买家理解优先',differentiation_first:'真实差异优先'};
    return `<section class="panel"><h2>选择一组俄文文案</h2><p class="flow-lead">三组候选使用相同的已确认事实，只改变表达重点。选定之前不会填入正式卡片。</p>
    ${c.status==='stale'?'<div class="hint warn">规格、事实、类目或关键词已经改变。这些候选已过期，请重新生成。</div>':''}
    <div class="flow-actionrow">${flowButton('生成三组标题、简介和标签','candidates',!g.workflow?.analysis?.confirmed||listingFlow.busy)}</div>
    ${(c.candidates||[]).map(x=>`<article class="flow-candidate ${c.selected_id===x.id?'selected':''}"><h3>${esc(modes[x.mode]||x.label||x.mode)}</h3><p class="flow-candidate-title">${esc(x.title_ru||x.title)}</p><p>${esc(x.description_ru||x.description)}</p>${flowTags(x.hashtags)}${(x.audit?.risk_flags||[]).length?`<p class="bad">${esc(x.audit.risk_flags.join('；'))}</p>`:''}${flowButton(c.selected_id===x.id?'已选择本组':'采用本组','choose-copy',c.status==='stale'||listingFlow.busy,`data-candidate="${esc(x.id)}"`)}</article>`).join('')||'<div class="empty">在上一步保存关键词并确认真实 Ozon 类目后，生成候选文案。</div>'}</section>`;
}
function flowVideosHtml(g) {
    const rows=g.video_library?.videos||[], selection=listingFlow.videoDrafts.get(state.product)||g.media_selection||{}, selected=new Map((selection.videos||[]).map(x=>[x.video_id,x]));
    const statuses={discovered:'已发现视频',ready:'文件已就绪',downloaded:'文件已就绪',unsupported:'暂不支持',expired:'地址已失效',failed:'获取失败',login_required:'需要登录',pending:'待获取',metadata_only:'已记录地址'};
    return `<section class="panel"><h2>商品视频 <span class="tag">${rows.length} 段</span></h2><p class="flow-lead">原视频是采集资料，不自动公开、不自动上架。确认使用权、内容与规格一致后，才选择用于 Ozon 的稳定视频链接。</p>
    ${rows.map(v=>flowVideoRow(g,v,selected.get(v.video_id))).join('')||'<div class="empty">未发现商品视频。可以先在 1688 商品页正常播放后重新采集，或上传供应商原视频文件。</div>'}
    <label class="field">上传供应商原文件（MP4/MOV，首版最多 100 MB）<input id="flowVideoFile" type="file" accept=".mp4,.mov,video/mp4,video/quicktime"></label>${flowButton('上传到私有商品资料库','upload-video',listingFlow.busy)}
    <p class="field-help">Ozon 普通视频为 8 秒至 5 分钟，最多 5 段；短视频封面不是 JPG 缩略图。需人工核对时长、画面和上传渠道支持情况。</p>
    <label class="checkrow" style="margin-top:20px"><input id="flowVideoRights" type="checkbox" ${selection.rights_confirmed?'checked':''}>确认有使用授权、无联系方式和误导内容，且与本次所选规格一致</label><div class="flow-actionrow">${flowButton('保存上架视频选择','save-videos',listingFlow.busy)}</div></section>`;
}
function flowVideoRow(g,v,chosen){
    const ready=v.has_file===true, options=(g.source?.skus||[]).filter(x=>(state.skus.selected||[]).includes(x.sku_id));
    return `<div class="flow-video" data-video="${esc(v.video_id)}"><b>${esc(v.title||v.video_id)}</b>
    <p class="flow-video-state">${esc(v.message||v.status||'已记录')}${ready?` · ${v.media_verified?'已验证技术参数':'技术参数尚未验证，暂不能上架'}`:''}</p>
    ${v.media_verified?`<p class="field-help">${n(v.duration_seconds)} 秒 · ${n(v.width)} × ${n(v.height)} · ${n(v.size_bytes/1024/1024)} MB</p>`:''}
    ${ready?`<video controls preload="metadata" src="/api/workbench/products/${state.product}/videos/${encodeURIComponent(v.video_id)}/file"></video>`:''}
    <div class="flow-actionrow">${flowButton('获取原视频文件','download-video',!v.can_download||listingFlow.busy,`data-video="${esc(v.video_id)}"`)}</div>
    <label class="checkrow"><input class="flowVideoUse" type="checkbox" ${chosen&&(chosen.use??true)?'checked':''} ${!ready||!v.media_verified?'disabled':''}>将本段视频加入上架资料</label>
    <label class="field">关联规格<select class="flowVideoSku"><option value="">适用于全部所选规格（需核对）</option>${options.map(x=>`<option value="${esc(x.sku_id)}" ${chosen?.source_sku_id===x.sku_id?'selected':''}>${esc(x.sku_name||x.name||x.spec_text||x.sku_id)}</option>`).join('')}</select></label>
    <label class="field">Ozon 支持的稳定 HTTPS 视频地址<input class="flowVideoUrl" value="${esc(chosen?.url||'')}" placeholder="VK / Yandex Disk / Rutube 分享链接"></label>
    <label class="field">视频标题<input class="flowVideoTitle" value="${esc(chosen?.title||v.title||'')}"></label></div>`;
}

renderProduct = function renderStepwiseProduct() {
    productEditor.step='form';
    legacyProductRenderer();
    if(!state.guided||!state.product)return;
    const g=state.guided,w=g.workflow||{},main=$('#main'),content=$('#productFormContent');
    const oldPanels=[...$('#contentWorkflow').children], take=text=>oldPanels.find(x=>x.querySelector('h2')?.textContent.includes(text));
    const panes={specs:$('#skuSection'),keywords:take('本商品关键词'),copy:take('俄文标题'),plan:take('图片规划'),qc:take('图片校验'),group:take('SKU 分组'),fields:take('Ozon 上架字段')};
    const category=$('#officialCategory'),official=$('#officialFieldsSection'),information=$('#productInformation'),details=$('#productDetails'),media=$('#productMedia');
    const preview=$('#productPreviewContent');preview.hidden=false;
    const active=flowStep(), complete={specs:state.skus.has_selection&&state.skus.active_count>0,analysis:w.analysis?.confirmed,keywords:(g.selected_keywords?.keywords||[]).some(x=>x.role==='core')&&g.category_selection?.confirmed_by_user,copy:w.copy?.confirmed,media:g.review?.sections.images.approved,card:g.review?.sections.fields.approved};
    const nav=main.querySelector('.product-steps');nav.className='listing-stepstrip';nav.setAttribute('aria-label','上架流程步骤');
    nav.innerHTML=listingFlowSteps.map(([id,label],i)=>`<button class="${active===id?'on':complete[id]?'done':''}" data-flow-action="step" data-step="${id}" aria-current="${active===id?'step':'false'}"><span class="step-number">${complete[id]&&active!==id?'✓':i+1}</span>${label}</button>`).join('');
    content.innerHTML=listingFlowSteps.map(([id])=>`<div class="flow-pane" data-pane="${id}" ${active===id?'':'hidden'}></div>`).join('');
    const pane=id=>content.querySelector(`[data-pane="${id}"]`);
    if(panes.specs)pane('specs').append(panes.specs);
    if(listingFlow.skuDrafts.has(state.product)){
        const selection=new Set(listingFlow.skuDrafts.get(state.product));
        for(const input of pane('specs').querySelectorAll('.skuChoice'))input.checked=selection.has(input.value);
        pane('specs').insertAdjacentHTML('afterbegin','<p class="hint warn">规格勾选尚未确认。确认后才会更新摘要及下游内容。</p>');
    }
    const save=pane('specs').querySelector('[data-action="save-skus"]');if(save){save.removeAttribute('data-action');save.dataset.flowAction='save-skus';save.textContent='确认规格并自动总结';save.disabled=listingFlow.busy}
    pane('analysis').innerHTML=flowAnalysisHtml(g);
    if(panes.keywords)pane('keywords').append(panes.keywords);
    pane('keywords').insertAdjacentHTML('beforeend',`<section class="panel"><h2>从数据库选择关键词</h2><p class="flow-lead">先按研究类目读取已采集词。搜索量只帮助排序，不能证明商品具备对应功能。</p><label class="field">研究类目<select id="flowKeywordCategory"><option value="">选择已采集类目</option>${state.categories.map(x=>`<option value="${esc(x.key)}" ${(g.selected_keywords?.category_key||state.category)===x.key?'selected':''}>${esc(x.label)}</option>`).join('')}</select></label><div class="flow-actionrow">${flowButton('读取类目词库','load-words')}</div><div id="flowKeywordLibrary">${flowWordsHtml()}</div></section>`);
    if(category)pane('keywords').append(category);
    pane('copy').innerHTML=flowCandidatesHtml(g);
    if(panes.copy){pane('copy').append(panes.copy);for(const button of panes.copy.querySelectorAll('[data-action="prepare"],[data-action="approve"]'))button.remove();const edit=panes.copy.querySelector('[data-action="save-copy"]');if(edit){edit.removeAttribute('data-action');edit.dataset.flowAction='save-copy'}
        if(g.copy?.title_ru){panes.copy.insertAdjacentHTML('beforeend',`<label class="field" style="margin-top:18px">主题标签（空格分隔）<textarea id="flowCopyTags">${esc(productDraft()?.edits.flowCopyTags??(g.copy.hashtags||[]).join(' '))}</textarea></label><div class="flow-actionrow">${flowButton('确认当前标题、简介和标签','confirm-copy',!w.copy?.selected||listingFlow.busy)}</div>`)}
    }
    pane('media').insertAdjacentHTML('beforeend',`<section class="panel"><h2>整套图片规划</h2><p class="flow-lead">根据所选规格和已确认卖点，规划主图、细节图与场景图。你可以修改提示词、选择参考图，再逐张确认付费生成。</p>${w.plan?.status==='stale'?'<div class="hint warn">旧图片计划已过期，不能直接生图或提交。</div>':''}<div class="flow-actionrow">${flowButton('规划图片卖点与提示词','plan',!w.copy?.confirmed||listingFlow.busy)}</div></section>`);
    for(const element of [media,panes.plan,panes.qc])if(element)pane('media').append(element);
    if(panes.plan){
        const specs=[...(g.image_plan?.main_images||[]),...(g.image_plan?.detail_images||[])];
        const generated=new Set(g.generated_image_paths||[]);
        for(const [i,row] of [...panes.plan.querySelectorAll('.slot')].entries()){
            const id=row.querySelector('.slotPrompt')?.dataset.slot,spec=specs.find(x=>x.slot===id)||{};
            if(!generated.has(spec.output_path)){const img=row.querySelector('img');if(img){const empty=document.createElement('div');empty.className='flow-image-placeholder';empty.textContent='尚未生成';img.replaceWith(empty)}}
            const wrap=document.createElement('details');wrap.className='flow-slot';wrap.open=i===0;
            const label=document.createElement('summary');label.textContent=`${id||'图位'} · ${spec.purpose||'展开查看提示词与参考图'}`;
            row.replaceWith(wrap);wrap.append(label,row);
        }
    }
    pane('media').insertAdjacentHTML('beforeend',flowVideosHtml(g));
    for(const element of [information,details,official,panes.group,panes.fields])if(element)pane('card').append(element);
    for(const button of pane('card').querySelectorAll('[data-action="prepare"]'))button.remove();
    pane('card').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>自动填充与编译卡片</h2><p class="flow-lead">只使用已经选定的文案、所选规格及有来源的事实。未知品牌、条码、售价和包装信息不会编造。这一步不调用 AI。</p><div class="flow-actionrow">${flowButton('填充并检查上架卡片','prepare-card',!w.copy?.confirmed||listingFlow.busy)}</div></section>`);
    pane('preview').append(preview);
    if(g.copy?.hashtags?.length)pane('preview').insertAdjacentHTML('beforeend',`<section class="panel"><h2>主题标签</h2>${flowTags(g.copy.hashtags)}</section>`);
    pane('preview').querySelector('[data-action="product-step"]')?.remove();
    pane('preview').insertAdjacentHTML('beforeend',`<section class="panel"><h2>提交官方 API / 导出 Excel</h2><p class="flow-lead">两种输出使用同一份已确认资料。接口受理不等于审核通过或可售，不自动填写库存。</p><div class="flow-actionrow">${flowButton('公开已确认图片的 HTTPS 地址','publish-media',!g.review?.sections.images.approved||listingFlow.busy)}${flowButton('刷新 Ozon 提交结果','verify')}</div><p class="field-help">图片发布到你配置的存储，只有公开可访问的地址才能用于上架。</p><div class="flow-actionrow">${flowButton('确认并提交到 Ozon','submit',!g.review?.ready_to_preflight||listingFlow.busy)}</div><h3>最新类目模板</h3><p class="field-help">从 Ozon 下载当前类目的最新模板。类目编号不一致时会阻止导出，不修改模板的隐藏配置。</p><input id="flowTemplateFile" type="file" accept=".xlsx"><div class="flow-actionrow">${flowButton('上传模板','upload-template',listingFlow.busy)}${flowButton('导出已确认商品 Excel','export',listingFlow.busy)}</div><div id="flowPublishResult" class="flow-publish-result"></div></section>`);
    const i=listingFlowSteps.findIndex(x=>x[0]===active);
    pane(active).insertAdjacentHTML('beforeend',`<div class="flow-footer">${i?flowButton('上一步','step',false,`data-step="${listingFlowSteps[i-1][0]}"`):'<span></span>'}${i<listingFlowSteps.length-1?flowButton('下一步','step',false,`data-step="${listingFlowSteps[i+1][0]}"`):'<span></span>'}</div>`);
    const error=listingFlow.errors.get(state.product);if(error)pane(active).insertAdjacentHTML('afterbegin',`<div class="flow-error" role="alert">${esc(error)}</div>`);
    if(listingFlow.busy)pane(active).insertAdjacentHTML('afterbegin','<p class="flow-status" role="status">正在处理，请勿重复点击。不会自动提交商品或批量付费生图。</p>');
    hydrateProductFields();filterListingFields();renderProductSupport();
};

function flowWordsHtml() {
    const rows=listingFlow.words.get(state.product)||[];
    return rows.length?`<div class="flow-keywords">${rows.map(x=>`<div class="flow-keyword-row"><span>${esc(x.key||x.keyword)}<br><small class="muted">${esc(x.label||'')}</small></span><span>机会分 ${n(x.score)}</span><div>${flowButton('作主词','use-primary',false,`data-word="${esc(x.key||x.keyword)}"`)} ${flowButton('加辅词','use-secondary',false,`data-word="${esc(x.key||x.keyword)}"`)}</div></div>`).join('')}</div>`:'<p class="muted">请选择研究类目后读取词库；也可在上方手动输入。</p>';
}
async function flowRequest(suffix,method='POST',body={}) {return api(`/api/workbench/products/${state.product}/${suffix}`,json(method,body))}
function flowShowResult(result) {const host=$('#flowPublishResult');if(host)host.textContent=JSON.stringify(result,null,2)}

document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-flow-action]');if(!button)return;
    const action=button.dataset.flowAction;
    if(action==='step'){if(!listingFlow.busy)flowSetStep(button.dataset.step);return}
    if(listingFlow.busy)return;
    const id=state.product;
    listingFlow.errors.delete(id);
    try {
        if(['analyze','confirm-analysis','candidates','choose-copy','save-copy','confirm-copy','plan','prepare-card','publish-media','submit','export'].includes(action)&&listingFlow.skuDrafts.has(id))throw Error('规格勾选尚未确认，请返回第一步确认规格');
        if(['confirm-analysis','candidates','choose-copy','confirm-copy','plan','prepare-card','submit','export'].includes(action)&&listingFlow.factDrafts.has(id))throw Error('补充事实尚未保存，请返回商品摘要保存并重新分析');
        if(['prepare-card','submit','export'].includes(action)&&listingFlow.videoDrafts.has(id))throw Error('视频选择尚未保存，请先保存上架视频选择');
        button.disabled=true;listingFlow.busy=true;
        if(action==='save-skus'){
            const include=[...document.querySelectorAll('.skuChoice:checked')].map(x=>x.value);
            if(!include.length)throw Error('请至少选择一个上架规格');
            await flowRequest('skus','POST',{include});listingFlow.skuDrafts.delete(id);listingFlow.steps.set(id,'analysis');
            notice('规格已保存，正在使用豆包总结所选商品信息');
            await flowRequest('guided/analyze');await refreshProduct();
        }
        if(action==='analyze'){notice('正在分析所选规格，相同资料优先使用缓存');await flowRequest('guided/analyze');await refreshProduct()}
        if(action==='facts'){
            const material=$('#flowMaterial').value.trim(),quantity=$('#flowQuantity').value;
            const facts={};if(material)facts.material=material;if(quantity)facts.package_quantity=Number(quantity);
            if(!Object.keys(facts).length)throw Error('请填写有依据的事实；清空旧值请到卡片资料中保存');
            await flowRequest('guided/facts','PUT',facts);listingFlow.factDrafts.delete(id);await flowRequest('guided/analyze');await refreshProduct();
        }
        if(action==='confirm-analysis'){await flowRequest('guided/analysis/confirm','POST',{input_fingerprint:state.guided.workflow.analysis.fingerprint});listingFlow.steps.set(id,'keywords');await refreshProduct()}
        if(action==='load-words'){
            const category=$('#flowKeywordCategory').value;if(!category)throw Error('请选择已采集的研究类目');
            const result=await api('/api/research/keywords?'+new URLSearchParams({category_key:category}));listingFlow.words.set(id,result.items||[]);$('#flowKeywordLibrary').innerHTML=flowWordsHtml();
        }
        if(action==='use-primary'||action==='use-secondary'){
            const input=action==='use-primary'?$('#productPrimary'):$('#productSecondary');
            input.value=action==='use-primary'?button.dataset.word:[...new Set([...input.value.split(',').map(x=>x.trim()).filter(Boolean),button.dataset.word])].join(', ');input.dispatchEvent(new Event('input',{bubbles:true}));notice('已加入输入框，请保存商品关键词');
        }
        if(action==='candidates'){
            if([...productDraft()?.pending||[]].some(x=>['productPrimary','productSecondary'].includes(x)))throw Error('请先保存最新关键词');
            if(!state.guided.category_selection?.confirmed_by_user)throw Error('先在关键词步骤确认真实 Ozon 类目和类型');
            notice('正在生成三组俄文候选，不会自动应用或上架');await flowRequest('guided/candidates');await refreshProduct();
        }
        if(action==='choose-copy'){
            await flowRequest('guided/candidates/choose','PUT',{candidate_id:button.dataset.candidate});
            const d=productDraft();for(const key of ['copyTitle','copyDescription']){delete d.edits[key];d.pending?.delete(key)}await refreshProduct();
        }
        if(action==='save-copy'){
            const candidate=state.guided.workflow.copy.selected_id;if(!candidate)throw Error('请先选择一组候选文案');
            await flowRequest('guided/candidates/choose','PUT',{candidate_id:candidate,title_ru:$('#copyTitle').value,description_ru:$('#copyDescription').value,hashtags:$('#flowCopyTags').value.trim().split(/\s+/).filter(Boolean)});
            const d=productDraft();for(const key of ['copyTitle','copyDescription','flowCopyTags']){delete d.edits[key];d.pending?.delete(key)}await refreshProduct();notice('文案修改已保存，请确认当前版本');
        }
        if(action==='confirm-copy'){
            if([...productDraft()?.pending||[]].some(x=>['copyTitle','copyDescription','flowCopyTags'].includes(x)))throw Error('请先保存文案和标签修改');
            await flowRequest('guided/copy/confirm','POST',{input_fingerprint:state.guided.workflow.copy.fingerprint});listingFlow.steps.set(id,'media');await refreshProduct();
        }
        if(action==='plan'){notice('正在规划每张图的卖点和提示词，不会付费生图');await flowRequest('guided/plan');await refreshProduct()}
        if(action==='prepare-card'){
            if(productDraft()?.dirty||listingDraft()?.dirty)throw Error('请先保存商品资料和官方属性，再编译卡片');
            const result=await flowRequest('guided/prepare-card','POST',{store:selectedReadStore()?.id});await refreshProduct();if(!result.report.ok)throw Error(result.report.blockers.join('；'));notice('卡片已编译，未调用 AI；请核对分组与必填字段');
        }
        if(action==='download-video'){notice('正在获取原视频到私有资料库，不会自动用于上架');await flowRequest(`videos/${encodeURIComponent(button.dataset.video)}/download`);await refreshProduct()}
        if(action==='upload-video'){
            const file=$('#flowVideoFile').files[0];if(!file)throw Error('请选择 MP4/MOV 文件');if(file.size>100*1024*1024)throw Error('首版视频不得超过 100 MB');
            await api(`/api/workbench/products/${id}/videos/upload?filename=${encodeURIComponent(file.name)}`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});await refreshProduct();
        }
        if(action==='save-videos'){
            const videos=[...document.querySelectorAll('.flow-video')].filter(x=>x.querySelector('.flowVideoUse').checked).map(x=>({video_id:x.dataset.video,url:x.querySelector('.flowVideoUrl').value.trim(),title:x.querySelector('.flowVideoTitle').value.trim(),source_sku_id:x.querySelector('.flowVideoSku').value||null}));
            await flowRequest('videos/selection','PUT',{rights_confirmed:$('#flowVideoRights').checked,videos});listingFlow.videoDrafts.delete(id);await refreshProduct();notice('视频选择已保存，尚未公开或提交 Ozon');
        }
        if(action==='publish-media'){
            if(!confirm('确认把已审核图片发布到配置的公开存储？这是生成 Ozon 可读取图片地址，不会创建商品卡。'))return;
            const result=await flowRequest('guided/publish-media','POST',{confirm:'PUBLISH_MEDIA'});flowShowResult(result);notice('已生成公开图片地址，请运行提交前预检');
        }
        if(action==='upload-template'){
            const file=$('#flowTemplateFile').files[0];if(!file)throw Error('请选择最新 Ozon 类目模板');
            const result=await api(`/api/workbench/products/${id}/listing-template`,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});flowShowResult(result);notice('模板已保存，导出时会核对真实类目和字段');
        }
        if(action==='export'){
            const result=await flowRequest('listing-export','POST',{store:selectedReadStore()?.id});flowShowResult(result);
            const link=document.createElement('a');link.href=result.download_url;link.textContent='下载商品 Excel';link.className='btn';$('#flowPublishResult').append(document.createElement('br'),link);
        }
        if(action==='submit'){
            if(productDraft()?.dirty||listingDraft()?.dirty||productDraft()?.pending?.size)throw Error('请先保存所有未保存的修改');
            const shop=selectedReadStore();if(!shop)throw Error('请选择已授权店铺');
            if(!confirm(`确认向店铺「${shop.display_name||shop.id}」提交已审核商品？会真实创建商品卡；不提交库存、不启用付费集评。`))return;
            const result=await flowRequest('guided/submit','POST',{store:shop.id,confirm:'SUBMIT'});flowShowResult(result);notice('已收到提交结果，请回读审核状态；受理不等于可售');
        }
        if(action==='verify'){const result=await flowRequest('verify','POST',{store:selectedReadStore()?.id});flowShowResult(result)}
    } catch(error) {
        listingFlow.errors.set(id,error.message);notice(error.message,true);
        if(id===state.product){await refreshProduct().catch(()=>{});if(state.view==='product')renderProduct()}
    } finally {
        listingFlow.busy=false;
        if(button.isConnected)button.disabled=false;
        else if(id===state.product&&state.view==='product')renderProduct();
        // Refresh buttons without erasing upload/preflight result panes or local input.
        if(id===state.product)document.querySelectorAll('.flow-status[role=status]').forEach(x=>x.remove());
    }
});
document.addEventListener('input',event=>{
    if(event.target.id==='flowCopyTags'){
        const draft=productDraft();draft.edits.flowCopyTags=event.target.value;(draft.pending||=new Set()).add('flowCopyTags');
    }
    if(['flowMaterial','flowQuantity'].includes(event.target.id))listingFlow.factDrafts.set(state.product,{material:$('#flowMaterial').value,quantity:$('#flowQuantity').value});
    if(event.target.closest('.flow-video')||event.target.id==='flowVideoRights')flowRememberVideoDraft();
});
function flowRememberVideoDraft(){
    if(!$('#flowVideoRights'))return;
    listingFlow.videoDrafts.set(state.product,{rights_confirmed:$('#flowVideoRights').checked,videos:[...document.querySelectorAll('.flow-video')].map(x=>({video_id:x.dataset.video,use:x.querySelector('.flowVideoUse').checked,url:x.querySelector('.flowVideoUrl').value,title:x.querySelector('.flowVideoTitle').value,source_sku_id:x.querySelector('.flowVideoSku')?.value||null}))});
}
document.addEventListener('change',event=>{
    if(event.target.matches('.skuChoice')){
        const values=[...document.querySelectorAll('.skuChoice:checked')].map(x=>x.value);
        const saved=state.skus.selected||[];
        if(JSON.stringify([...values].sort())===JSON.stringify([...saved].sort()))listingFlow.skuDrafts.delete(state.product);
        else listingFlow.skuDrafts.set(state.product,values);
    }
    if(event.target.closest('.flow-video')||event.target.id==='flowVideoRights')flowRememberVideoDraft();
});
