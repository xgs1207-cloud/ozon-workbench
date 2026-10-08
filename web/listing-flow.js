/* Desktop listing flow. Reuses the official form editor, not the legacy all-at-once runner. */
const listingFlow = {steps: new Map(), busy: false, errors: new Map(), words: new Map(), skuDrafts:new Map(), factDrafts:new Map(), videoDrafts:new Map(), studioSlots:new Map(), promptNames:new Map(), prompts:[], promptLibraryLoaded:false, promptLibraryLoading:false, promptLibraryError:'', mediaDrafts:new Map(), studioRequests:new Set(), jobProducts:new Set(), jobTimer:null, jobPolling:false, draggedSlot:null};
const listingFlowSteps = [
    ['specs', '选择规格'], ['analysis', '商品摘要'], ['keywords', '类目与关键词'],
    ['copy', '确认文案'], ['media', '图片与视频'], ['card', '填写卡片'], ['preview', '预览与发布']
];
const legacyProductRenderer = renderProduct;
const legacyProductSupportRenderer = renderProductSupport;
renderProductSupport=function(){legacyProductSupportRenderer();const host=$('#productSupport');if(!host)return;const note=[...host.querySelectorAll('p')].find(p=>p.textContent==='未确认的品牌、型号、售价等保持空白。');if(note)note.textContent='按你的设置应用默认值；型号名称由系统固定生成。售价、包装尺寸和未确认事实仍需核对。'};
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
 .flow-optional-library{border-top:1px solid #edf0f4;margin:22px 0;padding:16px 0}.flow-optional-library>summary{cursor:pointer;font-size:13px;color:#637389}.flow-optional-library .panel{margin:18px 0 0}
 .listing-studio .product-layout{grid-template-columns:minmax(0,1fr)}.listing-studio #productSupport{display:none}
 .product-page .image-studio h2{margin-bottom:12px}.studio-toolbar{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:18px}.studio-toolbar p{margin:0;font-size:12px;color:#637389;line-height:1.7}.studio-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.studio-tools .btn{min-height:34px}
 .studio-workspace{display:grid;grid-template-columns:minmax(235px,.8fr) minmax(330px,1.4fr);border:1px solid #e4e9f1;border-radius:12px;overflow:hidden;background:#fff}.studio-references{padding:20px;background:#f6f8fb;border-right:1px solid #e4e9f1;min-width:0}.studio-editor{padding:20px;min-width:0}.studio-references h3,.studio-editor h3{font-size:14px!important;margin:0 0 9px!important}.studio-reference-top{display:flex;justify-content:space-between;align-items:center;gap:8px}.studio-reference-count{font-size:12px;color:#1557e8;font-variant-numeric:tabular-nums}.studio-reference-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;max-height:560px;overflow:auto;overscroll-behavior:contain;margin-top:15px;padding:2px}.studio-reference{display:block;position:relative;cursor:pointer;min-width:0}.studio-reference input{position:absolute;top:6px;left:6px;width:16px;height:16px;margin:0;accent-color:#1557e8;z-index:1}.studio-reference img{display:block;aspect-ratio:1;width:100%;object-fit:contain;background:#fff;border:2px solid transparent;border-radius:7px}.studio-reference:has(input:checked) img{border-color:#1557e8;background:#f0f5ff}.studio-reference:has(input:focus-visible) img{outline:2px solid #1557e8;outline-offset:2px}.studio-reference span{display:block;font-size:10px;color:#637389;text-overflow:ellipsis;overflow:hidden;white-space:nowrap;margin-top:5px}.studio-reference-preview{font-size:11px;margin-top:4px;display:block;text-decoration:none}.studio-slot-nav{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 18px}.studio-slot-nav button{border:1px solid #e4e9f1;border-radius:6px;background:#fff;padding:7px 11px;font-size:12px;color:#637389}.studio-slot-nav button.on{color:#1557e8;border-color:#1557e8;background:#f5f8ff}.studio-slot-editor[hidden]{display:none!important}.product-page .studio-editor .slotPrompt{min-height:210px;padding:12px;font-size:13px;line-height:1.8;border-radius:8px;margin-bottom:10px}.product-page .studio-editor .field input,.product-page .studio-editor .field select{min-height:42px;padding:9px 10px;border-radius:7px}.studio-prompt-actions{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:15px 0}.studio-library{border-top:1px solid #e4e9f1;margin-top:18px;padding-top:14px}.studio-library summary{font-size:13px;color:#637389;cursor:pointer}.studio-library-tools{display:flex;gap:8px;margin:12px 0;align-items:end;flex-wrap:wrap}.studio-library-tools .field{flex:1;min-width:160px}.studio-library-items{max-height:220px;overflow:auto;margin-top:12px}.studio-library-item{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-bottom:1px solid #edf0f4}.studio-library-item>div:first-child{min-width:0}.studio-library-item b{display:block;font-size:12px;font-weight:600;overflow-wrap:anywhere}.studio-library-item p{font-size:11px;color:#637389;margin:4px 0 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:380px}.studio-library-item .row{flex:none;gap:5px}.studio-settings{font-size:11px;color:#637389;margin-top:18px}.studio-settings summary{cursor:pointer}.studio-generation-footer{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;border-top:1px solid #e4e9f1;padding-top:15px;margin-top:17px}.studio-generation-footer p{font-size:11px;color:#637389;max-width:420px;line-height:1.7;margin:0}.studio-generate{background:#1557e8!important;color:#fff!important}
 .studio-results{margin-top:25px}.studio-results-head{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.studio-results-head h3{margin:0!important;font-size:15px!important}.studio-result-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:14px}.studio-result{min-width:0}.studio-result img,.studio-result .flow-image-placeholder{width:100%;height:auto;aspect-ratio:3/4;object-fit:contain;background:#f6f8fb;border:1px solid #e9edf3;border-radius:8px}.studio-result h4{font-size:12px;font-weight:600;margin:8px 0 4px}.studio-result p{font-size:11px;color:#637389;margin:0 0 8px;line-height:1.6;overflow-wrap:anywhere}.studio-result .btn{padding:6px 9px;font-size:11px;min-height:30px}
 .official-advanced{margin:24px 0 0;border-top:1px solid #edf0f4;padding-top:16px}.official-advanced>summary{font-size:13px;color:#637389;cursor:pointer}.official-advanced[hidden]{display:none!important}.studio-video-publish-note{font-size:12px;color:#637389;line-height:1.7;margin:8px 0 16px}
 /* Desktop creation bench: supplier evidence, editor, then an ordered contact sheet. */
 .image-studio{--studio-ink:#111820;--studio-blue:#1557e8;--studio-line:#dfe5ef;--studio-pane:#f6f8fb;--studio-alert:#9b2331;background:#fff}
 .studio-workspace{grid-template-columns:300px minmax(0,1fr)}.studio-reference-grid{max-height:470px}.studio-editor{padding:22px}.studio-toolbar h2{font-size:24px!important}.studio-brief{border-top:1px solid var(--studio-line);border-bottom:1px solid var(--studio-line);padding:15px 0;margin:0 0 20px}.studio-brief-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.studio-brief h3{margin:0!important;font-size:14px!important}.studio-brief-grid{display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-top:12px}.studio-brief-grid p{font-size:12px;color:#637389;margin:0 0 8px}.studio-observations{margin:0;padding-left:18px;font-size:13px;line-height:1.8}.studio-observations li{margin:3px 0;overflow-wrap:anywhere}.studio-guidance{padding:12px 14px;background:#f6f8fb;border-left:3px solid #1557e8;margin:12px 0 16px}.studio-guidance p{font-size:12px;line-height:1.8;margin:0 0 9px;color:#44556e;white-space:pre-wrap}.studio-guidance summary{cursor:pointer;font-size:12px}.studio-guidance strong{display:block;font-size:12px;margin-bottom:6px}.studio-new-tools{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin:12px 0}.studio-new-tools .field{flex:1;min-width:130px;margin:0}.studio-new-tools select{width:100%}.studio-job-summary{font-size:12px;color:#637389;margin-top:8px}.studio-queue{margin:18px 0;padding:12px 14px;background:#f6f8fb}.studio-queue:empty{display:none}.studio-job-row{display:flex;align-items:center;gap:12px;font-size:12px;line-height:1.8;padding:5px 0;flex-wrap:wrap}.studio-job-row>span:last-child{color:#637389;flex:1;min-width:150px;overflow-wrap:anywhere}.studio-job-row b{font-weight:600;min-width:96px}.studio-result{position:relative;border:1px solid #e4e9f1;border-radius:8px;padding:10px;background:#fff}.studio-result.selected{border-color:#1557e8}.studio-result.drag-over{outline:2px dashed #1557e8;outline-offset:3px}.studio-result[draggable=true]{cursor:grab}.studio-result.dragging{opacity:.45}.studio-result-heading{display:flex;gap:8px;align-items:center;margin:8px 0}.studio-result-heading input{accent-color:#1557e8;width:16px;height:16px}.studio-result-heading label{font-size:12px;flex:1}.studio-drag-handle{font-size:16px;color:#637389;letter-spacing:-2px;user-select:none}.studio-result-actions{display:flex;gap:5px;align-items:center;flex-wrap:wrap}.studio-result-actions .btn.small{padding:5px 7px}.studio-result-grid{grid-template-columns:repeat(auto-fill,minmax(165px,1fr))}.studio-confirm-row{display:flex;justify-content:space-between;gap:14px;align-items:center;margin-top:20px;padding-top:16px;border-top:1px solid #e4e9f1}.studio-confirm-row p{font-size:12px;color:#637389;margin:0;line-height:1.8}.studio-action-error{font-size:12px;color:#9b2331;line-height:1.7}.image-studio button:focus-visible,.image-studio select:focus-visible{outline:2px solid #1557e8;outline-offset:3px}.studio-slot-nav button[aria-busy=true]{color:#637389;background:#f6f8fb}.studio-slot-nav{max-height:112px;overflow:auto}.studio-slot-editor .field{margin-bottom:0}.product-page .studio-editor .slotPrompt{min-height:180px}.studio-reference-grid img{min-height:80px}.studio-results-head{align-items:baseline}.studio-confirm-row .btn{flex:none}
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
function flowCategoryReady(g) {
    const category=g.category_selection||{};
    return category.confirmed_by_user===true&&category.source==='ozon_seller_api'&&['category_id','type_id'].every(key=>/^[1-9]\d*$/.test(String(category[key]??'')));
}
function flowCopyPrerequisite(g) {
    if(listingFlow.skuDrafts.has(state.product))return {step:'specs',label:'确认规格',message:'规格勾选尚未确认。先保存最新规格，再按最新商品资料生成文案。'};
    if(!state.skus?.has_selection||!(state.skus?.active_count>0))return {step:'specs',label:'选择并确认规格',message:'先选择要上架的商品规格，系统才知道需要总结和展示哪个商品。'};
    if(listingFlow.factDrafts.has(state.product))return {step:'analysis',label:'保存商品事实',message:'商品事实有未保存的修改。先保存并重新核对摘要，避免按旧资料生成文案。'};
    if(!g.workflow?.analysis?.confirmed){
        const status=g.workflow?.analysis?.status;
        return {step:'analysis',label:status==='ready'?'核对并确认商品摘要':'分析商品摘要',message:status==='ready'?'商品摘要已生成，但尚未确认。核对商品信息和卖点后，点击「确认摘要和卖点」。':status==='stale'?'商品摘要已过期。先按最新规格和商品事实重新分析，再确认摘要与卖点。':'还没有已确认的商品摘要。先分析所选规格，再确认可用于文案和图片的真实卖点。'};
    }
    if(!flowCategoryReady(g))return {step:'keywords',label:'确认 Ozon 官方类目',message:'先确认 Ozon 官方真实类目和商品类型。关键词可以留空，不必从类目词库选择。'};
    return null;
}
function flowMediaPrerequisite(g) {
    const prerequisite=flowCopyPrerequisite(g);if(prerequisite)return prerequisite;
    const copy=g.workflow?.copy||{};
    if(!copy.confirmed)return {step:'copy',label:copy.selected?'核对并确认文案':copy.candidates?.length&&copy.status!=='stale'?'选择并确认文案':'生成标题和简介',message:copy.status==='stale'?'文案已过期，请返回「确认文案」按最新资料重新生成、选择并确认。':copy.selected?'已经选择文案，但尚未确认。核对标题、简介和标签后，点击「确认当前标题、简介和标签」。':copy.candidates?.length?'先采用一组标题、简介和标签，并确认当前文案，再建立图片图位。':'先生成并确认标题、简介和标签，再建立图片图位。若上次生成失败，可返回文案步骤重试；不会自动调用模型。'};
    return null;
}
function flowPrerequisiteHtml(prerequisite,heading) {
    return prerequisite?`<div class="hint warn flow-prerequisite" role="status"><strong>${esc(heading)}</strong><p>${esc(prerequisite.message)}</p><div class="flow-actionrow">${flowButton(prerequisite.label,'step',listingFlow.busy,`data-step="${prerequisite.step}"`)}</div></div>`:'';
}
function flowOperationErrorHtml(step) {
    const error=listingFlow.errors.get(state.product);
    return error&&error.step===step?`<div class="flow-error" role="alert"><b>上次操作未完成：</b>${esc(error.message)}<div class="flow-actionrow">${flowButton('关闭提示','dismiss-error')}</div></div>`:'';
}
const flowFactLabels={title_cn:'商品名称',product_type:'商品类型',category_cn:'原始类目',material:'材质',materials:'材质',material_zh:'材质',material_ru:'材质（俄文）',brand:'品牌',pack_count:'包装数量',package_quantity:'包装内数量',skus:'所选规格',dimensions:'尺寸',weight:'重量',features:'已知特点',usage:'用途',load_capacity:'承重',certifications:'认证',functions:'功能',accessories:'配件',sku_id:'规格编号',name_cn:'规格名称',name_zh:'规格名称',properties:'规格参数',price_cny:'采购价（元）',image_refs:'图片来源'};
function flowScalar(value) {
    if (value == null || value === '' || value === 'unknown') return '未确认';
    if (Array.isArray(value)) return value.length?value.map(flowScalar).join('；'):'未确认';
    if (typeof value === 'object') return Object.entries(value).filter(([k])=>!['source_refs','metadata','schema_version','image_refs'].includes(k)).map(([k,v])=>`${flowFactLabels[k]||k}：${flowScalar(v)}`).join('；')||'未确认';
    return String(value);
}
function flowOperatorPointsHtml(g,listClass='flow-summary-list',emptyText='还没有可确认的卖点。') {
    const summary=g.workflow?.analysis||{},payload=summary.payload||{};
    const doc=typeof benchDocument==='function'?benchDocument():null;
    if(!doc&&typeof listingBench!=='undefined'&&listingBench.loading.has(benchScopeKey()))return '<p class="field-help" role="status">正在读取中文卖点…</p>';
    const display=doc?.summary?.display_zh||summary.display_zh;
    const rawPoints=payload.selling_points||payload.key_selling_points||[];
    const original=Array.isArray(rawPoints)?rawPoints:[];
    const local=original.map(point=>{
        const candidates=typeof point==='string'?[point]:point&&typeof point==='object'?[point.point_cn,point.text_zh,point.text_cn,point.title_zh,point.text,point.claim]:[];
        return candidates.find(text=>typeof text==='string'&&/[\u3400-\u9fff]/.test(text)&&!/[\u0400-\u04ff]/.test(text))||'';
    }).filter(Boolean);
    const points=display?.status==='ready'?(display.selling_points||[]).map(point=>point.text):local;
    const missing=original.length>points.length;
    const operation=typeof listingBench!=='undefined'?listingBench.summaryTranslations?.get(state.product):null;
    const translated=display?.is_translation;
    return `${points.length?`<ul class="${listClass}">${points.map(text=>`<li>${esc(text)}</li>`).join('')}</ul>`:`<p class="field-help">${esc(missing?'历史卖点尚未转换为中文。':emptyText)}</p>`}${translated?'<p class="field-help">中文释义仅用于阅读，原摘要和俄文上架文案保持不变。</p>':''}${missing?`<p class="field-help">只转换卖点语言，不重新分析商品；首次转换会调用文本模型，之后使用缓存。</p>${flowButton(operation?.busy?'正在转换中文…':'卖点转为中文','summary-zh',operation?.busy===true,`data-summary-fingerprint="${esc(display?.input_fingerprint||'')}"`)}${operation?.error?`<p class="inline-error" role="alert">${esc(operation.error)}</p>`:''}`:''}`;
}
function flowAnalysisHtml(g) {
    const a = g.workflow?.analysis || {}, data = a.payload || {}, facts = data.facts || {};
    const labels = flowFactLabels;
    const points = data.selling_points || data.key_selling_points || [];
    const unknowns = data.unknowns || data.missing_information || [];
    const stale = a.status === 'stale', extra=listingFlow.factDrafts.get(state.product)||{},preparation=g.workflow?.preparation_blockers||[],publication=g.workflow?.publication_blockers||[],deferred=g.workflow?.deferred_risks||[];
    return `<section class="panel"><h2>商品信息与卖点 <span class="tag ${a.confirmed?'ok':'warn'}">${a.confirmed?'已确认':stale?'内容已过期':a.status==='ready'?'待确认':'待分析'}</span></h2>
    <p class="flow-lead">豆包只总结本次所选规格。未知信息保持空白，卖点不能超出采集资料。修改规格或真实事实后，需要重新核对。</p>
    ${stale?'<div class="hint warn">下面是旧版本，仅供查看。请按最新规格重新分析，不能用于生成文案。</div>':''}
    ${Object.keys(facts).length?`<dl class="flow-facts">${Object.entries(facts).filter(([k])=>!['source_refs','selected_category'].includes(k)).map(([k,v])=>`<dt>${esc(labels[k]||k)}</dt><dd>${esc(flowScalar(v))}</dd>`).join('')}</dl>`:'<div class="empty">先确认要卖的规格，系统会自动开始商品分析；失败时可在这里重试。</div>'}
    <h3>可使用的卖点</h3>${flowOperatorPointsHtml(g)}
    ${unknowns.length?`<h3>待补充信息</h3><ul class="flow-summary-list">${unknowns.map(x=>`<li>${esc(typeof x==='string'?x:`${labels[x.field]||x.field||'未知字段'}：${x.reason||'采集资料未能确认'}`)}</li>`).join('')}</ul>`:''}
    ${preparation.length?`<div class="flow-error" role="alert"><b>需要先解决的信息冲突</b><ul class="flow-summary-list">${preparation.map(x=>`<li>${esc(typeof x==='string'?x:x.message||flowScalar(x))}</li>`).join('')}</ul></div>`:''}
    ${publication.length||deferred.length?`<details class="flow-optional-library"><summary>发布前待核对（${publication.length+deferred.length} 项，不影响先准备文案）</summary><p class="field-help">缺少的参数不编造；发布时仍需补齐实际必填项和合规资料。</p><ul class="flow-summary-list">${[...publication,...deferred].map(x=>`<li>${esc(typeof x==='string'?x:x.message||flowScalar(x))}</li>`).join('')}</ul></details>`:''}
    <details style="margin:20px 0"><summary>补充有依据的材质或包装内数量</summary><div class="grid2" style="margin-top:14px"><label class="field">材质<input id="flowMaterial" value="${esc(extra.material??g.human_confirmations?.material??'')}" placeholder="无法确认时留空"></label><label class="field">包装内数量<input id="flowQuantity" type="number" min="1" value="${esc(extra.quantity??g.human_confirmations?.package_quantity??'')}"></label></div>${flowButton('保存事实并重新分析','facts')}</details>
    <div class="flow-actionrow">${flowButton(a.status==='missing'?'分析所选规格':'按最新资料分析','analyze',!state.skus?.active_count||listingFlow.busy)}${flowButton('确认摘要和卖点','confirm-analysis',a.status!=='ready'||preparation.length>0||listingFlow.busy)}</div>
    <p class="field-help">相同输入优先使用已保存结果，不重复调用；此处不会生成图片或提交商品。</p></section>`;
}
function flowCandidatesHtml(g) {
    const c = g.workflow?.copy || {}, modes={search_first:'搜索匹配优先',conversion_first:'买家理解优先',differentiation_first:'真实差异优先'},prerequisite=flowCopyPrerequisite(g),generation=g.copy_generation_status||{},failed=generation.current===true&&generation.status==='invalid_response';
    return `<section class="panel"><h2>选择一组俄文文案</h2><p class="flow-lead">三组候选使用相同的已确认事实，只改变卖点的表达重点。选定之前不会填入正式卡片。</p>${flowCopyPolicyHtml(g)}
    ${flowPrerequisiteHtml(prerequisite,'生成文案前还需完成一步')}
    ${c.status==='stale'?'<div class="hint warn">规格、事实、类目或关键词已经改变。这些候选已过期，请重新生成。</div>':''}
    ${failed?`<div class="hint warn"><strong>上次模型结果未通过校验</strong><p>已保存失败结果，普通重试只重新校验，不会再次调用模型。需要新候选时，请主动点击付费重新生成。</p>${generation.errors?.length?`<ul class="flow-summary-list">${generation.errors.slice(0,5).map(error=>`<li>${esc(error)}</li>`).join('')}</ul>`:''}</div>`:''}
    <div class="flow-actionrow">${flowButton(failed?'重新校验已保存结果（不调用模型）':'生成三组标题、简介和标签','candidates',Boolean(prerequisite)||listingFlow.busy)}${failed?flowButton('重新生成候选（会调用模型）','candidates-force',Boolean(prerequisite)||listingFlow.busy):''}</div>
    ${(c.candidates||[]).map(x=>`<article class="flow-candidate ${c.selected_id===x.id?'selected':''}"><h3>${esc(modes[x.mode]||x.label||x.mode)}</h3><p class="flow-candidate-title">${esc(x.title_ru||x.title)}</p><p>${esc(x.description_ru||x.description)}</p>${flowTags(x.hashtags)}${(x.audit?.risk_flags||[]).length?`<p class="bad">${esc(x.audit.risk_flags.join('；'))}</p>`:''}${flowButton(c.selected_id===x.id?'已选择本组':'采用本组','choose-copy',c.status==='stale'||listingFlow.busy,`data-candidate="${esc(x.id)}"`)}</article>`).join('')||'<div class="empty">确认商品摘要和真实 Ozon 类目后即可生成；可自填关键词，不必读取类目词库。</div>'}</section>`;
}
function flowCopyPolicyHtml(g){
    const records=(g.selected_keywords?.keywords||[]).filter(row=>!['ad','reject','exclude','conflict'].includes(row.role));
    const core=records.find(row=>row.role==='core')||records[0],phrase=String(core?.keyword||'').trim().replace(/\s+/g,' ');
    const secondary=records.filter(row=>row!==core).map(row=>String(row.keyword||'').trim()).filter(Boolean);
    return `<div class="flow-copy-policy"><p><strong>标题：</strong>${phrase?`以完整主关键词 <span lang="ru">${esc(phrase)}</span> 开头；词组内不换序、不拆分，只规范大小写和空格。`:'未填写主关键词时，按真实类目与商品信息生成。'}属性词与真实卖点由 AI 自然融合，不机械拼接。</p><p><strong>简介：</strong>短段落、清晰分行与适量 emoji；${secondary.length?`副关键词 ${secondary.map(word=>`<span lang="ru">${esc(word)}</span>`).join('、')} 只在有事实依据时自然融入。`:'可填写副关键词，按商品事实自然融入。'}可在下方编辑排版。</p><p class="field-help">这能改善搜索相关性与买家理解，不保证排名。价格、履约、库存、转化与评价等因素也会影响流量。</p></div>`;
}
function flowStudioSlots(g=state.guided){return [...(g?.image_plan?.main_images||[]),...(g?.image_plan?.detail_images||[])]}
function flowStudioActive(g=state.guided){const slots=flowStudioSlots(g),saved=listingFlow.studioSlots.get(state.product);return saved==='new'?'new':slots.find(x=>x.slot===saved)?.slot||slots[0]?.slot||'new'}
function flowStudioReferenceIds(slot){const spec=flowStudioSlots().find(x=>x.slot===slot),draft=productDraft();return String(draft?.edits?.[`refs:${slot}`]??(spec?.reference_image_ids||[]).join(',')).split(',').map(x=>x.trim()).filter(Boolean)}
function flowMediaUrl(path){return `/api/workbench/products/${encodeURIComponent(state.product)}/media/${String(path).split('/').map(encodeURIComponent).join('/')}`}
function flowImageStudioHtml(g){
    const slots=flowStudioSlots(g),active=flowStudioActive(g)||'new',references=flowStudioReferences(g),selected=new Set(flowStudioReferenceIds(active)),backend=imageBackendInfo(g),ready=Boolean(state.skus?.has_selection&&state.skus?.active_count>0),prerequisite=flowMediaPrerequisite(g);
    const editors=active==='new'?[...slots,{slot:'new',prompt:'',purpose:'单张商品图片',reference_image_ids:[]}]:slots;
    return `<section class="panel image-studio"><div class="studio-toolbar"><div><h2>图片生成</h2><p>每张独立生成、修改和重做。生成过程中可以编辑或生成其他图片。</p></div><div class="studio-tools">${flowButton('新增一张图片','studio-new',!ready)}${flowButton('AI 辅助规划（可选）','plan',Boolean(prerequisite)||listingFlow.busy)}</div></div>
    ${!ready?'<div class="hint warn">请先在第一步确认要上架的规格，再选参考图生成图片。不需要先确认整套图片规划。</div>':''}
    ${flowStudioBriefHtml(g)}
    <div class="studio-workspace"><div class="studio-references"><div class="studio-reference-top"><h3>参考图片</h3><span class="studio-reference-count" id="flowStudioReferenceCount">已选 ${selected.size} / 3</span></div><p class="field-help">只传入本张勾选的 1–3 张原图。详情图可用于提取卖点；不同规格请分开核对。</p>${references.length?`<div class="studio-reference-grid">${references.map((ref,i)=>`<div><label class="studio-reference"><input type="checkbox" class="studioReferenceChoice" data-reference="${esc(ref.id)}" aria-label="参考图 ${i+1} ${esc(ref.id)}" ${selected.has(String(ref.id))?'checked':''}><img src="${flowMediaUrl(ref.path)}" alt="采集参考图 ${i+1}" loading="lazy"><span>${esc(ref.role==='detail'?'详情图':ref.role==='sku'?'规格图':'商品图')} ${i+1}</span></label><a class="studio-reference-preview" href="${flowMediaUrl(ref.path)}" target="_blank" rel="noopener noreferrer">查看原图</a></div>`).join('')}</div>`:'<div class="empty">没有采集原图，请先用插件采集商品。</div>'}<p id="flowStudioReferenceError" class="inline-error" role="alert" ${selected.size>3?'':'hidden'}>已选超过 3 张。请取消多余勾选，不会自动截断。</p></div>
    <div class="studio-editor"><nav class="studio-slot-nav" aria-label="选择生成图片">${editors.map((spec,i)=>`<button data-flow-action="studio-slot" data-slot="${esc(spec.slot)}" class="${spec.slot===active?'on':''}" aria-pressed="${spec.slot===active}" aria-busy="${flowStudioSlotBusy(spec.slot,g)}">${esc(flowStudioSlotLabel(spec,i))}<span data-slot-status="${esc(spec.slot)}">${flowStudioSlotStatus(spec,g)}</span></button>`).join('')}</nav>
    ${editors.map(spec=>`<div class="studio-slot-editor" data-studio-slot="${esc(spec.slot)}" ${spec.slot===active?'':'hidden'}><h3>${esc(spec.purpose||'商品图片')}</h3>${flowStudioGuidanceHtml(g,spec)}${spec.slot==='new'?flowStudioNewSettingsHtml(g):''}<label class="field">图片提示词（可自由修改）<textarea class="slotPrompt" data-slot="${esc(spec.slot)}" maxlength="4000" placeholder="例如：保留参考图中商品的真实造型和颜色，用近景侧光展示表面细节，背景简洁，画面只放本规格商品。">${esc(spec.prompt||'')}</textarea></label><input type="hidden" class="slotRefs" data-slot="${esc(spec.slot)}" value="${esc((spec.reference_image_ids||[]).join(', '))}"><div class="studio-prompt-actions"><span class="muted" data-studio-draft="${esc(spec.slot)}">点击生成会自动保存本张提示词和参考图。</span>${flowButton('保存本张设置','studio-save',false,`data-slot="${esc(spec.slot)}"`)}</div><div class="studio-generation-footer"><p>${esc(backend.label)} ${esc(backend.model)}<br>${esc(backend.aspect_ratio)}${backend.image_size?` / ${esc(backend.image_size.toUpperCase())}`:''} · 输出 ${esc(backend.normalized_size)}<br><span data-generation-reason="${esc(spec.slot)}">${!imageBackendReady(g)?'模型尚未配置，请展开下方配置说明。':'不要求先确认整套规划。'}</span></p><button class="btn studio-generate" data-flow-action="studio-generate" data-slot="${esc(spec.slot)}" ${ready&&imageBackendReady(g)&&!flowStudioSlotBusy(spec.slot,g)?'':'disabled'}>${flowStudioSlotBusy(spec.slot,g)?'本张生成中…':(g.generated_image_paths||[]).includes(spec.output_path)?'重新生成本张图片':'生成当前图片'}</button></div></div>`).join('')}
    <details class="studio-library"><summary>提示词库 · 保存与复用</summary><div class="studio-library-tools"><label class="field">保存名称<input id="flowPromptName" maxlength="80" value="${esc(listingFlow.promptNames.get(`${state.product}:${active}`)||'')}" placeholder="例如 白底主图 / 纹理特写"></label>${flowButton('保存当前提示词','save-image-prompt')}</div><div id="flowImagePromptLibrary">${flowPromptLibraryHtml()}</div><p class="field-help">载入提示词只修改编辑器，不会自动调用生图。</p></details><details class="studio-settings"><summary>模型配置与费用说明</summary>${imageBackendSummaryHtml(g)}</details></div></div>
    <div id="flowStudioQueue" class="studio-queue" aria-live="polite">${flowStudioQueueHtml(g.image_jobs||[])}</div><div id="flowStudioResults">${flowStudioResultsHtml(g)}</div></section>`;
}
function flowStudioReferences(g=state.guided){return g?.image_plan?.reference_images?.length?g.image_plan.reference_images:(g?.captured_reference_images||[])}
function flowStudioSlotLabel(spec,index){if(spec.slot==='new')return '新图片';return `${spec.role==='variant_main'||String(spec.slot).startsWith('main-')?'主图':'图片'} ${index+1}`}
function flowStudioSlotBusy(slot,g=state.guided){return listingFlow.studioRequests.has(`${state.product}:${slot}`)||(g?.image_jobs||[]).some(x=>x.slot===slot&&['queued','running'].includes(x.status))}
function flowStudioSlotStatus(spec,g){return flowStudioSlotBusy(spec.slot,g)?' · 生成中':(g.generated_image_paths||[]).includes(spec.output_path)?' ✓':''}
function flowStudioInsightText(value){return typeof value==='string'?value:[value.label_zh,value.description_zh||value.claim_zh||value.text_cn||value.text||value.observation_cn].filter(Boolean).join('：')||flowScalar(value)}
function flowStudioInsights(g){const insights=g?.image_insights||{};return {...(insights.payload||insights),status:insights.status}}
function flowStudioNewSettingsHtml(g){const selected=new Set(state.skus?.selected||[]),rows=(g.source?.skus||[]).filter(x=>selected.has(x.sku_id));return `<div class="studio-new-tools"><label class="field">这张图片对应的规格<select id="flowNewImageSku">${rows.map(x=>`<option value="${esc(x.sku_id)}">${esc(x.sku_name||x.name||x.name_cn||x.spec_text||x.sku_id)}</option>`).join('')}</select></label><label class="field">用途<select id="flowNewImageRole"><option value="detail">细节 / 卖点 / 场景图</option><option value="variant_main">商品主图</option></select></label></div>`}
function flowStudioBriefHtml(g){
    const a=g.workflow?.analysis||{},insights=flowStudioInsights(g),observations=insights.status==='stale'?[]:insights.visible_observations||[],claims=insights.status==='stale'?[]:insights.unverified_seller_claims||[];
    return `<section class="studio-brief"><div class="studio-brief-head"><h3>产品特征与卖点</h3>${flowButton(listingFlow.studioRequests.has(`${state.product}:insights`)?'正在识别图片…':'从勾选图片提取卖点','studio-insights',listingFlow.studioRequests.has(`${state.product}:insights`)||!state.skus?.active_count)}</div><div class="studio-brief-grid"><div><p>商品摘要${a.confirmed?'（已确认）':'（待核对）'}</p>${flowOperatorPointsHtml(g,'studio-observations','摘要尚无卖点。可直接选供应商图片提取，不影响手写提示词生图。')}</div><div><p>图片可见特征${insights.status==='stale'?'（资料已变更，请重新识别）':''}</p>${observations.length?`<ul class="studio-observations">${observations.slice(0,6).map(x=>`<li>${esc(flowStudioInsightText(x))}</li>`).join('')}</ul>`:'<p>选择详情图或规格图，识别画面和图中文字。无法确认的材质、参数或认证不会自动写入商品事实。</p>'}</div></div>${claims.length?`<details class="studio-guidance"><summary>图中文字中的供应商声明（${claims.length} 项，待核对）</summary>${claims.slice(0,6).map(x=>`<p>${esc(flowStudioInsightText(x))}</p>`).join('')}</details>`:''}</section>`;
}
function flowStudioGuidance(g,spec){const insights=flowStudioInsights(g),points=insights.status==='stale'?[]:insights.selling_points||[];return spec.guidance_cn||points.map(x=>x.image_howto_zh||x.visual_method_cn||x.visual_demo_cn||x.visual_guidance_cn||x.visual_guidance||x.prompt_cn||x.prompt_zh).filter(Boolean).slice(0,3).join('\n')||'造型与颜色：保留所选规格的真实外观，用简洁背景突出商品。\n表面与细节：使用近景和侧光，让买家看清纹理与细部。\n使用方式：只有资料能确认时才展示，不添加未知功能、认证或参数。'}
function flowStudioGuidanceHtml(g,spec){return `<div class="studio-guidance"><strong>中文参考提示词 · 如何体现卖点</strong><p>${esc(flowStudioGuidance(g,spec))}</p>${flowButton('填入提示词后编辑','studio-use-guidance',false,`data-slot="${esc(spec.slot)}"`)}</div>`}
function flowStudioQueueHtml(jobs){const labels={queued:'排队中',running:'生成中',completed:'已生成',failed:'生成失败',unknown:'结果待核对',stale:'资料已改变，未替换'};return [...jobs].slice(-12).reverse().map(x=>`<div class="studio-job-row"><b>${esc(labels[x.status]||x.status)}</b><span>${esc(x.slot)}</span><span>${esc(x.message||x.error||(['queued','running'].includes(x.status)?'可继续生成其他图片。':'completed'===x.status?'请检查结果并选择是否用于上架。':'旧图片保留；不会自动重复调用。'))}</span></div>`).join('')}
function flowStudioSelection(g=state.guided){const draft=listingFlow.mediaDrafts.get(state.product);if(draft)return draft;const selected=g?.image_plan?.selected_slots;return {order:Array.isArray(selected)?[...selected]:flowStudioSlots(g).filter(x=>(g?.generated_image_paths||[]).includes(x.output_path)).map(x=>x.slot),dirty:false}}
function flowStudioResultsHtml(g){const generated=new Set(g.generated_image_paths||[]),files=new Map((g.image_generation?.files||[]).map(x=>[x.slot,x])),selection=flowStudioSelection(g),selected=new Set(selection.order),ready=flowStudioSlots(g).filter(x=>generated.has(x.output_path)),ordered=[...selection.order.map(id=>ready.find(x=>x.slot===id)).filter(Boolean),...ready.filter(x=>!selected.has(x.slot))];return `<section class="studio-results"><div class="studio-results-head"><h3>生成结果 <span class="tag">${ready.length} 张</span></h3><span class="muted">勾选上架图片；拖拽调整顺序，也可用左右按钮移动。</span></div>${ordered.length?`<div class="studio-result-grid">${ordered.map((spec,i)=>`<article class="studio-result ${selected.has(spec.slot)?'selected':''}" data-result-slot="${esc(spec.slot)}" draggable="${selected.has(spec.slot)}"><a href="${flowMediaUrl(spec.output_path)}" target="_blank" rel="noopener noreferrer" draggable="false"><img draggable="false" src="${flowMediaUrl(spec.output_path)}?v=${encodeURIComponent(files.get(spec.slot)?.generation_id||files.get(spec.slot)?.sha256||'saved')}" alt="${esc(spec.purpose||'生成图片')}" loading="lazy"></a><div class="studio-result-heading"><input id="use-image-${esc(spec.slot)}" type="checkbox" class="studioImageUse" data-slot="${esc(spec.slot)}" ${selected.has(spec.slot)?'checked':''}><label for="use-image-${esc(spec.slot)}">${selected.has(spec.slot)?`第 ${selection.order.indexOf(spec.slot)+1} 张`:'不用于上架'}</label><span class="studio-drag-handle" aria-hidden="true">⠿</span></div><p>${esc(spec.purpose||'商品图片')}</p><div class="studio-result-actions">${flowButton('修改 / 重做','studio-slot',false,`data-slot="${esc(spec.slot)}"`)}${flowButton('左移','studio-move',!selected.has(spec.slot)||selection.order.indexOf(spec.slot)===0,`data-slot="${esc(spec.slot)}" data-direction="-1"`)}${flowButton('右移','studio-move',!selected.has(spec.slot)||selection.order.indexOf(spec.slot)===selection.order.length-1,`data-slot="${esc(spec.slot)}" data-direction="1"`)}</div></article>`).join('')}</div>`:'<div class="empty">可以只生成一张。完成后在这里查看和选择，不要求凑齐整套图片。</div>'}<div class="studio-confirm-row"><p>已选 ${selection.order.length} 张${selection.dirty?' · 顺序或选择尚未确认':''}<br>工作台不要求固定图片数量。Ozon 上架所需主图与规格对应关系在发布前单独检查。</p>${flowButton('确认图片并下一步','studio-confirm')}</div></section>`}
function flowPromptLibraryHtml(){
    if(listingFlow.promptLibraryLoading)return '<p class="muted" role="status">正在读取提示词库…</p>';
    const toolbar=`<div class="studio-library-tools"><label class="field">选择已保存提示词<select id="flowImagePromptSelect"><option value="">选择一条提示词</option>${listingFlow.prompts.map(item=>`<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('')}</select></label>${flowButton('载入编辑器','use-image-prompt',!listingFlow.prompts.length||!flowStudioActive())}${flowButton('刷新','reload-image-prompts',listingFlow.busy)}</div>`;
    if(listingFlow.promptLibraryError)return toolbar+`<p class="inline-error" role="alert">${esc(listingFlow.promptLibraryError)}</p>`;
    return toolbar+(listingFlow.prompts.length?`<div class="studio-library-items">${listingFlow.prompts.map(item=>`<div class="studio-library-item"><div><b>${esc(item.name)}</b><p title="${esc(item.prompt)}">${esc(item.prompt)}</p></div><div class="row">${flowButton('使用','use-image-prompt',!flowStudioActive(),`data-prompt="${esc(item.id)}"`)}${flowButton('删除','delete-image-prompt',listingFlow.busy,`data-prompt="${esc(item.id)}"`)}</div></div>`).join('')}</div>`:'<p class="muted">还没有保存的提示词。在编辑器写好提示词并起名，即可保存到库。</p>');
}
async function flowLoadPromptLibrary(force=false){
    if(listingFlow.promptLibraryLoading||listingFlow.promptLibraryLoaded&&!force)return;
    listingFlow.promptLibraryLoading=true;flowRefreshPromptLibrary();
    try{const result=await api('/api/workbench/image-prompts');listingFlow.prompts=result.items||[];listingFlow.promptLibraryError=''}catch(error){listingFlow.promptLibraryError=error.message}finally{listingFlow.promptLibraryLoading=false;listingFlow.promptLibraryLoaded=true;flowRefreshPromptLibrary()}
}
function flowRefreshPromptLibrary(){const host=$('#flowImagePromptLibrary');if(host)host.innerHTML=flowPromptLibraryHtml()}
function flowStudioRefreshReferences(){
    const slot=flowStudioActive(),selected=new Set(flowStudioReferenceIds(slot));
    for(const input of document.querySelectorAll('.studioReferenceChoice')){input.checked=selected.has(input.dataset.reference);input.disabled=!input.checked&&selected.size>=3}
    const count=$('#flowStudioReferenceCount');if(count){count.textContent=`已选 ${selected.size} / 3`;count.classList.toggle('bad',selected.size>3)}
    const error=$('#flowStudioReferenceError');if(error)error.hidden=selected.size<=3;
    for(const note of document.querySelectorAll('[data-studio-draft]'))note.textContent=productDraft()?.pending?.has(`prompt:${note.dataset.studioDraft}`)||productDraft()?.pending?.has(`refs:${note.dataset.studioDraft}`)?'有修改；点击生成会自动保存本张设置。':'可以直接生成本张，不需要确认整套规划。';
    for(const button of document.querySelectorAll('[data-flow-action="studio-generate"]')){const spec=flowStudioSlots().find(x=>x.slot===button.dataset.slot);button.textContent=flowStudioSlotBusy(button.dataset.slot)?'本张生成中…':(state.guided?.generated_image_paths||[]).includes(spec?.output_path)?'重新生成本张图片':'生成当前图片'}
    for(const button of document.querySelectorAll('[data-flow-action="studio-generate"]')){const current=button.dataset.slot,refs=flowStudioReferenceIds(current),editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===current),busy=flowStudioSlotBusy(current);button.disabled=!state.skus?.has_selection||!state.skus?.active_count||!imageBackendReady()||busy||refs.length<1||refs.length>3||!editor?.value.trim();const reason=document.querySelector(`[data-generation-reason="${CSS.escape(current)}"]`);if(reason)reason.textContent=busy?'本张正在生成，可以切换其他图片。':refs.length>3?'请取消多余参考图，只保留 1–3 张。':refs.length<1?'请在左侧勾选至少一张参考图。':!editor?.value.trim()?'请填写提示词，或载入中文参考提示词。':!imageBackendReady()?'模型尚未配置，请查看下方说明。':'已就绪。点击后只生成本张图片。'}
}

async function flowStudioSaveSlot(slot,product=state.product){
    if(listingFlow.skuDrafts.has(product))throw Error('请先确认最新规格，不会按未保存的规格调用模型');
    const editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===slot),prompt=editor?.value.trim()||'',reference_ids=flowStudioReferenceIds(slot);
    if(!prompt)throw Error('请填写图片提示词');if(reference_ids.length<1||reference_ids.length>3)throw Error('请选择 1–3 张参考图，不会自动截断勾选');
    const base=`/api/workbench/products/${encodeURIComponent(product)}/guided`;
    let result;
    if(slot==='new')result=await api(`${base}/image-slots`,json('POST',{prompt,reference_ids,source_sku_id:$('#flowNewImageSku')?.value||state.skus?.selected?.[0],role:$('#flowNewImageRole')?.value||'detail',purpose:$('#flowNewImageRole')?.value==='variant_main'?'商品主图':'商品细节与卖点展示'}));
    else result=await api(`${base}/image-plan/${encodeURIComponent(slot)}`,json('PUT',{prompt,reference_ids}));
    const d=productEditor.drafts.get(product);for(const key of [`prompt:${slot}`,`refs:${slot}`]){d?.pending?.delete(key);if(d)delete d.edits[key]}
    const resolved=typeof result.slot==='object'?result.slot.slot:result.slot||slot;
    if(product===state.product&&result.image_plan)state.guided.image_plan=result.image_plan;
    listingFlow.studioSlots.set(product,resolved);
    return resolved;
}
async function flowStudioAction(action,button){
    const product=state.product,slot=button.dataset.slot||flowStudioActive(),key=`${product}:${action==='studio-insights'?'insights':slot}`;
    if(action==='studio-new'){listingFlow.studioSlots.set(product,'new');renderProduct();document.querySelector('[data-studio-slot="new"] .slotPrompt')?.focus({preventScroll:true});return}
    if(action==='studio-use-guidance'){const editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===slot),spec=flowStudioSlots().find(x=>x.slot===slot)||{slot};if(editor){editor.value=flowStudioGuidance(state.guided,spec);editor.dispatchEvent(new Event('input',{bubbles:true}));editor.focus({preventScroll:true})}return}
    if(action==='studio-move'){flowStudioMove(slot,Number(button.dataset.direction));return}
    if(listingFlow.studioRequests.has(key)||action==='studio-generate'&&flowStudioSlotBusy(slot))return;
    listingFlow.studioRequests.add(key);button.disabled=true;
    if(action==='studio-insights')button.textContent='正在识别图片…';
    try{
        if(action==='studio-save'){await flowStudioSaveSlot(slot,product);notice('本张设置已保存，其他图片不变');if(product===state.product)renderProduct()}
        if(action==='studio-generate'){
            if(!imageBackendReady())throw Error('请先配置正式生图模型');
            const resolved=await flowStudioSaveSlot(slot,product);
            const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/generate-image`,json('POST',{slot:resolved}));
            if(product===state.product){state.guided.image_jobs=[...(state.guided.image_jobs||[]).filter(x=>x.id!==result.job?.id),...(result.job?[result.job]:[])];renderProduct()}
            listingFlow.jobProducts.add(product);flowStudioStartPolling();notice('本张已加入生成任务，可以继续生成其他图片');
        }
        if(action==='studio-insights'){
            const refs=flowStudioReferenceIds(flowStudioActive());if(refs.length<1||refs.length>3)throw Error('先在左侧选择 1–3 张要识别的详情图或规格图');
            const images=flowStudioReferences().filter(x=>refs.includes(String(x.id))).map(x=>x.path);
            const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/image-insights`,json('POST',{image_paths:images}));
            if(product===state.product){state.guided.image_insights=result.insights;renderProduct()}notice('图片特征已提取，请核对图中文字中的供应商声明');
        }
        if(action==='studio-confirm'){
            const selection=flowStudioSelection(),selected_slots=[...selection.order];
            await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/image-selection`,json('PUT',{selected_slots}));
            await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/approve`,json('POST',{section:'images'}));
            listingFlow.mediaDrafts.delete(product);listingFlow.steps.set(product,'card');if(product===state.product)await refreshProduct();notice(`已确认 ${selected_slots.length} 张图片，进入卡片填写`);
        }
    }catch(error){listingFlow.errors.set(product,{step:'media',action,message:error.message});notice(error.message,true);if(product===state.product){renderProduct();const host=$('#flowStudioQueue');if(host)host.insertAdjacentHTML('afterbegin',`<p class="studio-action-error" role="alert">${esc(error.message)}</p>`)}}
    finally{listingFlow.studioRequests.delete(key);if(button.isConnected)button.disabled=false;if(product===state.product){flowStudioRefreshReferences();const briefButton=document.querySelector('[data-flow-action="studio-insights"]');if(briefButton){briefButton.disabled=false;briefButton.textContent='从勾选图片提取卖点'}}}
}
function flowStudioMove(slot,direction){const selection=flowStudioSelection(),order=[...selection.order],index=order.indexOf(slot),target=index+direction;if(index<0||target<0||target>=order.length)return;[order[index],order[target]]=[order[target],order[index]];listingFlow.mediaDrafts.set(state.product,{order,dirty:true});flowStudioRefreshResults()}
function flowStudioRefreshResults(){const host=$('#flowStudioResults');if(!host)return;const focused=document.activeElement,focusSlot=focused?.dataset?.slot,action=focused?.dataset?.flowAction,wasChoice=focused?.classList?.contains('studioImageUse');host.innerHTML=flowStudioResultsHtml(state.guided);if(focusSlot){const query=wasChoice?`.studioImageUse[data-slot="${CSS.escape(focusSlot)}"]`:`[data-flow-action="${CSS.escape(action||'')}"][data-slot="${CSS.escape(focusSlot)}"]`;host.querySelector(query)?.focus({preventScroll:true})}}
function flowStudioStartPolling(){if(listingFlow.jobTimer||listingFlow.jobPolling)return;listingFlow.jobTimer=setTimeout(flowStudioPollJobs,document.hidden?10000:2000)}
async function flowStudioPollJobs(){
    listingFlow.jobTimer=null;if(listingFlow.jobPolling)return;listingFlow.jobPolling=true;
    try{await Promise.all([...listingFlow.jobProducts].map(async product=>{
        try{const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/image-jobs`),jobs=result.jobs||[],running=jobs.some(x=>['queued','running'].includes(x.status));
            if(product===state.product&&state.guided){const previous=new Set((state.guided.image_jobs||[]).filter(x=>['queued','running'].includes(x.status)).map(x=>x.id));state.guided.image_jobs=jobs;if(result.image_generation){state.guided.image_generation=result.image_generation;const paths=(result.image_generation.files||[]).map(x=>x.path);state.guided.generated_image_paths=[...new Set([...(state.guided.generated_image_paths||[]),...paths])]}
                const host=$('#flowStudioQueue');if(host)host.innerHTML=flowStudioQueueHtml(jobs);flowStudioRefreshReferences();for(const spec of flowStudioSlots()){const label=document.querySelector(`[data-slot-status="${CSS.escape(spec.slot)}"]`);if(label)label.textContent=flowStudioSlotStatus(spec,state.guided)}
                if(jobs.some(x=>previous.has(x.id)&&!['queued','running'].includes(x.status))){flowStudioRefreshResults();if(!running&&state.view==='product'){await refreshProduct()}}
            }if(!running)listingFlow.jobProducts.delete(product);
        }catch(error){if(product===state.product){const host=$('#flowStudioQueue');if(host&&!host.querySelector('.studio-action-error'))host.insertAdjacentHTML('afterbegin',`<p class="studio-action-error" role="alert">暂时无法刷新任务：${esc(error.message)}。未重新调用生图；刷新页面可继续查看。</p>`)} }
    }))}finally{listingFlow.jobPolling=false;if(listingFlow.jobProducts.size)flowStudioStartPolling()}
}
function flowVideosHtml(g) {
    const rows=g.video_library?.videos||[], selection=listingFlow.videoDrafts.get(state.product)||g.media_selection||{}, selected=new Map((selection.videos||[]).map(x=>[x.video_id,x]));
    const statuses={discovered:'已发现视频',ready:'文件已就绪',downloaded:'文件已就绪',unsupported:'暂不支持',expired:'地址已失效',failed:'获取失败',login_required:'需要登录',pending:'待获取',metadata_only:'已记录地址'};
    return `<section class="panel"><h2>1688 商品视频 <span class="tag">${rows.length} 段</span></h2><p class="flow-lead">直接使用采集到的供应商原视频。勾选后上传到你配置的对象存储，生成 Ozon 可读取的 HTTPS 地址，无需另填分享链接。</p>
    ${rows.map(v=>flowVideoRow(g,v,selected.get(v.video_id))).join('')||'<div class="empty">未发现商品视频。可以先在 1688 商品页正常播放后重新采集，或上传供应商原视频文件。</div>'}
    <details class="flow-optional-library"><summary>未采集到文件？手动补充供应商原视频</summary><label class="field">供应商原文件（MP4/MOV，最多 100 MB）<input id="flowVideoFile" type="file" accept=".mp4,.mov,video/mp4,video/quicktime"></label>${flowButton('上传到私有商品资料库','upload-video',listingFlow.busy)}</details>
    <p class="field-help">Ozon 普通视频为 8 秒至 5 分钟，最多 5 段；短视频封面不是 JPG 缩略图。需人工核对时长、画面和上传渠道支持情况。</p>
    <label class="checkrow" style="margin-top:20px"><input id="flowVideoRights" type="checkbox" ${selection.rights_confirmed?'checked':''}>确认有使用授权、无联系方式和误导内容，且与本次所选规格一致</label><p class="studio-video-publish-note">此操作会把勾选原视频公开到对象存储，并写入上架草稿；不会提交 Ozon 商品卡。取消所有勾选再保存，可清空视频选择。</p><div class="flow-actionrow">${flowButton('保存并发布所选原视频','save-videos',listingFlow.busy)}</div></section>`;
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
    ${chosen?.url?`<label class="field">已保存的视频 HTTPS 地址<input class="flowVideoUrl" readonly value="${esc(chosen.url)}"></label>`:''}
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
    const active=flowStep(), complete={specs:state.skus.has_selection&&state.skus.active_count>0,analysis:w.analysis?.confirmed,keywords:g.category_selection?.confirmed_by_user,copy:w.copy?.confirmed,media:g.review?.sections.images.approved,card:g.review?.sections.fields.approved};
    main.classList.toggle('listing-studio',active==='media');
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
    if(category)pane('keywords').append(category);
    if(panes.keywords){
        pane('keywords').append(panes.keywords);
        panes.keywords.querySelector('h2').textContent='关键词（可选）';
        panes.keywords.querySelector('p').textContent='已经自行确认 Ozon 类目时，无需读取类目词库。可直接填写关键词；留空时只使用真实类目名称与商品事实生成文案。';
        panes.keywords.querySelector('#productPrimary').placeholder='可自行输入，也可留空';
    }
    pane('keywords').insertAdjacentHTML('beforeend',`<details class="flow-optional-library"><summary>从已采集类目词库挑选（可选）</summary><section class="panel"><p class="flow-lead">仅在需要研究关键词时展开。搜索量帮助排序，不能证明商品具备对应功能。</p><label class="field">研究类目<select id="flowKeywordCategory"><option value="">选择已采集类目</option>${state.categories.map(x=>`<option value="${esc(x.key)}" ${(g.selected_keywords?.category_key||state.category)===x.key?'selected':''}>${esc(x.label)}</option>`).join('')}</select></label><div class="flow-actionrow">${flowButton('读取类目词库','load-words')}</div><div id="flowKeywordLibrary">${flowWordsHtml()}</div></section></details>`);
    pane('copy').innerHTML=flowCandidatesHtml(g);
    if(panes.copy){pane('copy').append(panes.copy);for(const button of panes.copy.querySelectorAll('[data-action="prepare"],[data-action="approve"]'))button.remove();const edit=panes.copy.querySelector('[data-action="save-copy"]');if(edit){edit.removeAttribute('data-action');edit.dataset.flowAction='save-copy'}
        const saveNote=[...panes.copy.querySelectorAll('p')].find(node=>node.textContent.includes('这会调用付费文本模型'));
        if(saveNote)saveNote.textContent='保存修改只做校验，不调用模型。修改后的标题、简介和标签需要重新确认。';
        if(g.copy?.title_ru){panes.copy.insertAdjacentHTML('beforeend',`<label class="field" style="margin-top:18px">主题标签（空格分隔）<textarea id="flowCopyTags">${esc(productDraft()?.edits.flowCopyTags??(g.copy.hashtags||[]).join(' '))}</textarea></label><div class="flow-actionrow">${flowButton('确认当前标题、简介和标签','confirm-copy',!w.copy?.selected||listingFlow.busy)}</div>`)}
    }
    pane('media').innerHTML=flowImageStudioHtml(g);
    if(panes.qc){for(const button of panes.qc.querySelectorAll('[data-action="approve"]'))button.remove();const details=document.createElement('details');details.className='flow-optional-library';details.innerHTML='<summary>查看图片技术检查与发布问题</summary>';details.append(panes.qc);pane('media').append(details)}
    pane('media').insertAdjacentHTML('beforeend',flowVideosHtml(g));
    for(const element of [information,details,official,panes.group,panes.fields])if(element)pane('card').append(element);
    for(const button of pane('card').querySelectorAll('[data-action="prepare"]'))button.remove();
    pane('card').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>自动填充与编译卡片</h2><p class="flow-lead">选定的标题、简介、标签与已知商品信息自动填入；默认值按你的设置处理。型号名称同一商品的多规格共用，售价及包装尺寸仍由你确认。这一步不调用 AI。</p><div class="flow-actionrow">${flowButton('填充并检查上架卡片','prepare-card',!w.copy?.confirmed||listingFlow.busy)}</div></section>`);
    pane('preview').append(preview);
    if(w.publication_blockers?.length)pane('preview').insertAdjacentHTML('afterbegin',`<section class="panel"><h2>发布前合规待核对</h2><div class="flow-error" role="alert">这些问题不会阻止准备文案与图片，但解决之前不能提交 Ozon 商品卡。<ul class="flow-summary-list">${w.publication_blockers.map(item=>`<li>${esc(typeof item==='string'?item:item.message||flowScalar(item))}</li>`).join('')}</ul></div><p class="field-help">补充有依据的合规资料并重新核对商品摘要，不能用默认值替代认证或安全证明。</p></section>`);
    if(g.copy?.hashtags?.length)pane('preview').insertAdjacentHTML('beforeend',`<section class="panel"><h2>主题标签</h2>${flowTags(g.copy.hashtags)}</section>`);
    pane('preview').querySelector('[data-action="product-step"]')?.remove();
    pane('preview').insertAdjacentHTML('beforeend',`<section class="panel"><h2>提交官方 API / 导出 Excel</h2><p class="flow-lead">两种输出使用同一份已确认资料。API 发布使用已保存的仓库和库存配置；商品导入、库存写入与审核结果分别记录。</p><div class="flow-actionrow">${flowButton('公开已确认图片的 HTTPS 地址','publish-media',!g.review?.sections.images.approved||listingFlow.busy)}${flowButton('刷新 Ozon 提交结果','verify')}</div><p class="field-help">图片发布到你配置的存储，只有公开可访问的地址才能用于上架。</p><div class="flow-actionrow">${flowButton('确认并提交到 Ozon','submit',!g.review?.ready_to_preflight||listingFlow.busy)}</div><h3>最新类目模板</h3><p class="field-help">从 Ozon 下载当前类目的最新模板。类目编号不一致时会阻止导出，不修改模板的隐藏配置。</p><input id="flowTemplateFile" type="file" accept=".xlsx"><div class="flow-actionrow">${flowButton('上传模板','upload-template',listingFlow.busy)}${flowButton('导出已确认商品 Excel','export',listingFlow.busy)}</div><div id="flowPublishResult" class="flow-publish-result"></div></section>`);
    const i=listingFlowSteps.findIndex(x=>x[0]===active);
    pane(active).insertAdjacentHTML('beforeend',`<div class="flow-footer">${i?flowButton('上一步','step',false,`data-step="${listingFlowSteps[i-1][0]}"`):'<span></span>'}${i<listingFlowSteps.length-1&&active!=='media'?flowButton('下一步','step',false,`data-step="${listingFlowSteps[i+1][0]}"`):'<span></span>'}</div>`);
    pane(active).insertAdjacentHTML('afterbegin',flowOperationErrorHtml(active));
    if(listingFlow.busy)pane(active).insertAdjacentHTML('afterbegin','<p class="flow-status" role="status">正在处理，请勿重复点击。不会自动提交商品或批量付费生图。</p>');
    hydrateProductFields();filterListingFields();renderProductSupport();flowStudioRefreshReferences();
    if(active==='media'){flowLoadPromptLibrary();if((g.image_jobs||[]).some(x=>['queued','running'].includes(x.status))){listingFlow.jobProducts.add(state.product);flowStudioStartPolling()}}
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
    if(action==='summary-zh')return; // Owned by the presentation-only card handler.
    if(action==='step'){if(!listingFlow.busy)flowSetStep(button.dataset.step);return}
    if(action==='dismiss-error'){listingFlow.errors.delete(state.product);renderProduct();return}
    if(action==='studio-slot'){listingFlow.studioSlots.set(state.product,button.dataset.slot);renderProduct();return}
    if(action.startsWith('studio-')){await flowStudioAction(action,button);return}
    if(listingFlow.busy)return;
    const id=state.product,operationStep=flowStep();
    listingFlow.errors.delete(id);
    try {
        if(['analyze','confirm-analysis','candidates','candidates-force','choose-copy','save-copy','confirm-copy','plan','prepare-card','publish-media','submit','export'].includes(action)&&listingFlow.skuDrafts.has(id))throw Error('规格勾选尚未确认，请返回第一步确认规格');
        if(['confirm-analysis','candidates','candidates-force','choose-copy','confirm-copy','plan','prepare-card','submit','export'].includes(action)&&listingFlow.factDrafts.has(id))throw Error('补充事实尚未保存，请返回商品摘要保存并重新分析');
        if(['prepare-card','submit','export'].includes(action)&&listingFlow.videoDrafts.has(id))throw Error('视频选择尚未保存，请先保存上架视频选择');
        button.disabled=true;listingFlow.busy=true;
        if(action==='reload-image-prompts'){await flowLoadPromptLibrary(true)}
        if(action==='save-image-prompt'){
            const slot=flowStudioActive(),editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===slot),name=$('#flowPromptName').value.trim(),prompt=editor?.value.trim()||'';
            if(!name)throw Error('请给这条提示词起一个名称');if(!prompt)throw Error('请先在编辑器填写图片提示词');
            await api('/api/workbench/image-prompts',json('POST',{name,prompt}));await flowLoadPromptLibrary(true);notice('提示词已保存到库，没有调用生图');
        }
        if(action==='use-image-prompt'){
            const selected=button.dataset.prompt||$('#flowImagePromptSelect')?.value,item=listingFlow.prompts.find(x=>String(x.id)===selected),slot=flowStudioActive(),editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===slot);
            if(!item)throw Error('请选择已保存的提示词');if(!editor)throw Error('请先规划图片并选择一个图位');
            editor.value=item.prompt;editor.dispatchEvent(new Event('input',{bubbles:true}));editor.focus({preventScroll:true});notice('提示词已载入；请核对并保存图位，不会自动生图');
        }
        if(action==='delete-image-prompt'){
            const item=listingFlow.prompts.find(x=>String(x.id)===button.dataset.prompt);if(!item)throw Error('这条提示词已不存在，请刷新提示词库');
            if(!confirm(`删除提示词「${item.name}」？不会改变已保存的商品图位。`))return;
            await api(`/api/workbench/image-prompts/${encodeURIComponent(item.id)}`,{method:'DELETE'});await flowLoadPromptLibrary(true);notice('提示词已从库中删除');
        }
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
        if(action==='candidates'||action==='candidates-force'){
            if([...productDraft()?.pending||[]].some(x=>['productPrimary','productSecondary'].includes(x)))throw Error('请先保存最新关键词');
            if(!state.guided.category_selection?.confirmed_by_user)throw Error('先在类目与关键词步骤确认真实 Ozon 类目和类型');
            const force=action==='candidates-force';
            if(force&&!(state.guided.copy_generation_status?.current===true&&state.guided.copy_generation_status?.status==='invalid_response'))throw Error('候选结果状态已变化，请刷新后再决定是否重新生成');
            if(force&&!confirm('确认重新调用豆包生成三组标题、简介和标签？这会产生模型费用；请求及有界修复次数以服务器配置为准。已有结果不会自动采用，也不会自动生图或上架。'))return;
            const revalidateOnly=!force&&state.guided.copy_generation_status?.current===true&&state.guided.copy_generation_status?.status==='invalid_response';
            notice(revalidateOnly?'正在重新校验已保存的结果，不调用模型':'正在生成三组俄文候选，不会自动应用或上架');await flowRequest('guided/candidates','POST',force?{force:true}:revalidateOnly?{revalidate_only:true}:{});await refreshProduct();
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
            const videos=[...document.querySelectorAll('.flow-video')].filter(x=>x.querySelector('.flowVideoUse').checked).map(x=>({video_id:x.dataset.video,title:x.querySelector('.flowVideoTitle').value.trim(),source_sku_id:x.querySelector('.flowVideoSku').value||null})),rights_confirmed=$('#flowVideoRights').checked;
            if(videos.length){
                if(!rights_confirmed)throw Error('请先确认视频使用授权、画面内容与规格一致');
                if(!confirm(`确认将 ${videos.length} 段供应商原视频公开上传到配置的对象存储？视频将可通过 HTTPS 地址读取；这一步不会提交 Ozon 商品卡。`))return;
                await flowRequest('guided/publish-videos','POST',{confirm:'PUBLISH_VIDEOS',rights_confirmed:true,videos});
            }else await flowRequest('videos/selection','PUT',{rights_confirmed,videos:[]});
            listingFlow.videoDrafts.delete(id);await refreshProduct();notice(videos.length?'原视频已发布到对象存储并填入上架草稿，尚未提交 Ozon':'已清空上架视频选择');
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
            if(typeof publicationRequireReady!=='function')throw Error('发布配置尚未加载，请刷新页面后选择仓库');
            const target=await publicationRequireReady();
            if(state.product!==id||selectedReadStore()?.id!==shop.id)throw Error('商品或店铺已切换，请重新确认发布目标');
            const stockLines=target.items.map(item=>`${item.offer_id}：${item.stock} 件`).join('\n');
            if(!confirm(`确认向店铺「${shop.display_name||shop.id}」提交已审核商品？\n仓库：${target.warehouse.name}（${target.warehouse.warehouse_id}）\n${stockLines}\n商品导入且价格处理完成后，会写入上述库存；未就绪时保存记录等待继续处理。不启用付费集评。`))return;
            const result=await api(`/api/workbench/products/${encodeURIComponent(id)}/guided/submit`,json('POST',{store:shop.id,confirm:'SUBMIT'}));
            if(state.product===id)flowShowResult(result);
            if(typeof publicationRefreshAfterSubmit==='function')await publicationRefreshAfterSubmit(id,shop.id);
            const report=result.report||{},outcome=report.state||report.outcome;
            const stockResult=report.stock_update||result.stock_update;
            if(stockResult?.pending)notice('商品导入已保存记录，库存仍待完成或回读。请从上架记录继续库存处理，不要重复导入商品',true);
            else if(!result.ok)notice(outcome==='rejected'?'Ozon 明确拒绝，请查看错误并修正资料后确认重试':'提交未确认成功，请回读现有任务；不要重复提交',true);
            else notice('已保存上架记录，请核对导入、库存与审核状态；接口受理不等于审核通过或可售');
        }
        if(action==='verify'){const result=await flowRequest('verify','POST',{store:selectedReadStore()?.id});flowShowResult(result)}
    } catch(error) {
        listingFlow.errors.set(id,{step:listingFlow.steps.get(id)||operationStep,action,message:error.message});notice(error.message,true);
        if(id===state.product){await refreshProduct().catch(()=>{});if(state.view==='product')renderProduct()}
    } finally {
        listingFlow.busy=false;
        if(button.isConnected)button.disabled=false;
        else if(id===state.product&&state.view==='product')renderProduct();
        // Refresh buttons without erasing upload/preflight result panes or local input.
        if(id===state.product)document.querySelectorAll('.flow-status[role=status]').forEach(x=>x.remove());
        if(action.includes('image-prompt'))flowRefreshPromptLibrary();
    }
});
document.addEventListener('input',event=>{
    if(event.target.id==='flowPromptName')listingFlow.promptNames.set(`${state.product}:${flowStudioActive()}`,event.target.value);
    if(event.target.classList.contains('slotPrompt'))flowStudioRefreshReferences();
    if(event.target.id==='flowCopyTags'){
        const draft=productDraft();draft.edits.flowCopyTags=event.target.value;(draft.pending||=new Set()).add('flowCopyTags');
    }
    if(['flowMaterial','flowQuantity'].includes(event.target.id))listingFlow.factDrafts.set(state.product,{material:$('#flowMaterial').value,quantity:$('#flowQuantity').value});
    if(event.target.closest('.flow-video')||event.target.id==='flowVideoRights')flowRememberVideoDraft();
});
function flowRememberVideoDraft(){
    if(!$('#flowVideoRights'))return;
    listingFlow.videoDrafts.set(state.product,{rights_confirmed:$('#flowVideoRights').checked,videos:[...document.querySelectorAll('.flow-video')].map(x=>({video_id:x.dataset.video,use:x.querySelector('.flowVideoUse').checked,url:x.querySelector('.flowVideoUrl')?.value||'',title:x.querySelector('.flowVideoTitle').value,source_sku_id:x.querySelector('.flowVideoSku')?.value||null}))});
}
document.addEventListener('change',event=>{
    if(event.target.matches('.studioImageUse')){const selection=flowStudioSelection(),slot=event.target.dataset.slot,order=selection.order.filter(x=>x!==slot);if(event.target.checked)order.push(slot);listingFlow.mediaDrafts.set(state.product,{order,dirty:true});flowStudioRefreshResults()}
    if(event.target.matches('.studioReferenceChoice')){
        const slot=flowStudioActive(),input=[...document.querySelectorAll('.slotRefs')].find(x=>x.dataset.slot===slot);
        if(input){input.value=[...document.querySelectorAll('.studioReferenceChoice:checked')].map(x=>x.dataset.reference).join(', ');input.dispatchEvent(new Event('input',{bubbles:true}));flowStudioRefreshReferences()}
    }
    if(event.target.matches('.skuChoice')){
        const values=[...document.querySelectorAll('.skuChoice:checked')].map(x=>x.value);
        const saved=state.skus.selected||[];
        if(JSON.stringify([...values].sort())===JSON.stringify([...saved].sort()))listingFlow.skuDrafts.delete(state.product);
        else listingFlow.skuDrafts.set(state.product,values);
    }
    if(event.target.closest('.flow-video')||event.target.id==='flowVideoRights')flowRememberVideoDraft();
});
document.addEventListener('dragstart',event=>{const tile=event.target.closest('.studio-result[draggable="true"]');if(!tile)return;listingFlow.draggedSlot=tile.dataset.resultSlot;event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',tile.dataset.resultSlot);tile.classList.add('dragging')});
document.addEventListener('dragover',event=>{const tile=event.target.closest('.studio-result[draggable="true"]');if(!tile||!listingFlow.draggedSlot)return;event.preventDefault();event.dataTransfer.dropEffect='move';tile.classList.add('drag-over')});
document.addEventListener('dragleave',event=>{const tile=event.target.closest('.studio-result');if(tile&&!tile.contains(event.relatedTarget))tile.classList.remove('drag-over')});
document.addEventListener('drop',event=>{const tile=event.target.closest('.studio-result[draggable="true"]');if(!tile||!listingFlow.draggedSlot)return;event.preventDefault();const source=listingFlow.draggedSlot,target=tile.dataset.resultSlot,selection=flowStudioSelection(),order=[...selection.order];if(source!==target&&order.includes(source)&&order.includes(target)){order.splice(order.indexOf(source),1);order.splice(order.indexOf(target),0,source);listingFlow.mediaDrafts.set(state.product,{order,dirty:true});flowStudioRefreshResults()}listingFlow.draggedSlot=null;document.querySelectorAll('.dragging,.drag-over').forEach(x=>x.classList.remove('dragging','drag-over'))});
document.addEventListener('dragend',()=>{listingFlow.draggedSlot=null;document.querySelectorAll('.dragging,.drag-over').forEach(x=>x.classList.remove('dragging','drag-over'))});
document.addEventListener('toggle',event=>{if(event.target.id==='listingAdvancedFields'){const draft=listingDraft();if(draft)draft.advancedOpen=event.target.open}},true);
