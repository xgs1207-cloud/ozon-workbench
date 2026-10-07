/* Isolated generation workspaces, shared explicitly selected publishing gallery. */
listingBench.activeSlots=new Map();listingBench.newDrafts=new Map();
function benchWorkspace(){return listingBench.workspaces.get(state.product)||'single'}
function benchSets(g=state.guided){return g?.media_sets||[]}
function benchSetId(g=state.guided){const saved=listingBench.sets.get(state.product);return benchSets(g).find(x=>x.id===saved)?.id||benchSets(g)[0]?.id||''}
function benchWorkspaceKey(){return `${state.product}:${benchWorkspace()}:${benchWorkspace()==='set'?benchSetId():''}`}
function benchWorkspaceSlots(g=state.guided){return flowStudioSlots(g).filter(x=>(x.workspace||'single')===benchWorkspace()&&(benchWorkspace()!=='set'||x.set_id===benchSetId(g)))}
flowStudioActive=function(g=state.guided){const visible=benchWorkspaceSlots(g),saved=listingFlow.studioSlots.get(state.product),remembered=listingBench.activeSlots.get(benchWorkspaceKey());return saved==='new'?'new':visible.find(x=>x.slot===saved)?.slot||visible.find(x=>x.slot===remembered)?.slot||visible[0]?.slot||'new'};
function benchRememberWorkspace(){
    const key=benchWorkspaceKey(),active=flowStudioActive();listingBench.activeSlots.set(key,active);
    if(active==='new')listingBench.newDrafts.set(key,{prompt:document.querySelector('[data-studio-slot="new"] .slotPrompt')?.value||'',refs:flowStudioReferenceIds('new')});
}
function benchRestoreWorkspace(){const key=benchWorkspaceKey(),draft=productDraft(),fresh=listingBench.newDrafts.get(key);listingFlow.studioSlots.set(state.product,listingBench.activeSlots.get(key)||benchWorkspaceSlots()[0]?.slot||'new');if(draft){draft.edits['prompt:new']=fresh?.prompt||'';draft.edits['refs:new']=(fresh?.refs||[]).join(', ');draft.pending?.delete('prompt:new');draft.pending?.delete('refs:new')}}
function benchMergeMedia(result,product=state.product){if(product!==state.product||!state.guided)return;for(const key of ['image_plan','image_generation','generated_image_paths','captured_reference_images'])if(result[key]!=null)state.guided[key]=result[key];state.guided.image_jobs=result.jobs||[];state.guided.media_sets=result.sets||[];state.guided.media_workspaces=result.workspaces||{}}
async function benchReadMedia(product=state.product){const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/media`);benchMergeMedia(result,product);return result}
const mediaFetchProduct=fetchProduct;
fetchProduct=async function(...args){const result=await mediaFetchProduct(...args),product=state.product;if(product){try{await benchReadMedia(product);if(state.product===product&&state.view==='product')renderProduct()}catch(error){listingBench.errors.set(product,error.message);notice(`读取图库失败：${error.message}`,true)}}return result};
const mediaStudioHtml=flowImageStudioHtml;
flowImageStudioHtml=function(g){
    const slots=benchWorkspaceSlots(g),filtered={...g,image_plan:{...g.image_plan,main_images:slots.filter(x=>x.role==='variant_main'),detail_images:slots.filter(x=>x.role!=='variant_main')}},template=document.createElement('template');
    template.innerHTML=mediaStudioHtml(filtered);
    const root=template.content.querySelector('.image-studio'),mode=benchWorkspace(),setId=benchSetId(g),sets=benchSets(g),count=listingBench.counts.get(`${state.product}:${mode}`)||6;
    const toolbar=root.querySelector('.studio-tools');toolbar.querySelector('[data-flow-action="plan"]')?.remove();
    const add=toolbar.querySelector('[data-flow-action="studio-new"]');if(add){add.textContent=mode==='set'?'另加一张套图':'新增单张图片';add.disabled=mode==='set'&&!setId||!state.skus?.active_count}
    const header=root.querySelector('.studio-toolbar');header.insertAdjacentHTML('afterend',`<nav class="bench-tabs" aria-label="生图工作区"><button data-bench-action="workspace" data-mode="single" class="${mode==='single'?'on':''}" aria-pressed="${mode==='single'}">单张生成</button><button data-bench-action="workspace" data-mode="set" class="${mode==='set'?'on':''}" aria-pressed="${mode==='set'}">套图生成</button></nav>${mode==='set'?`<div class="bench-set-controls"><label class="field">套图数量<input id="benchSetCount" type="number" min="1" max="50" step="1" value="${count}"></label><label class="field">当前套图<select id="benchSetSelect"><option value="">新建套图</option>${sets.map((set,index)=>`<option value="${esc(set.id)}" ${set.id===setId?'selected':''}>${esc(set.name||`套图 ${index+1}`)} · ${set.completed||0}/${set.total||set.slots?.length||0}</option>`).join('')}</select></label>${benchButton('按当前参考图建立新套图','create-set','',!state.skus?.active_count)}${benchButton('生成当前套图未启动图片','generate-set',`data-set="${esc(setId)}"`,!setId||!imageBackendReady())}<p class="field-help">建立套图不扣费；点击生成才加入队列。不会重做已有成功图片；失败项需逐张确认重试。每张图均可独立改提示词。</p></div>`:'<p class="field-help">每次生成一张；可以同时启动其他图片。单张任务和套图任务分别保存，不覆盖另一工作区。</p>'}`);
    const refs=root.querySelector('.studio-references');refs.insertAdjacentHTML('afterbegin',`<div class="studio-new-tools"><label class="field">原图用于哪一规格<select id="benchOriginalSku">${(g.source?.skus||[]).filter(x=>(state.skus?.selected||[]).includes(x.sku_id)).map(x=>`<option value="${esc(x.sku_id)}">${esc(x.sku_name||x.name_zh||x.name_cn||x.name||x.sku_id)}</option>`).join('')}</select></label><label class="field">原图用途<select id="benchOriginalRole"><option value="detail">详情 / 场景</option><option value="variant_main">规格主图</option></select></label></div>`);
    for(const ref of refs.querySelectorAll('.studio-reference'))ref.parentElement.insertAdjacentHTML('beforeend',benchButton('直接使用原图','adopt-original',`data-reference="${esc(ref.querySelector('input').dataset.reference)}"`,!state.skus?.active_count||mode==='set'&&!setId));
    // benchButton already owns class; assign the extra visual class without duplicate HTML attrs.
    for(const button of refs.querySelectorAll('[data-bench-action="adopt-original"]'))button.classList.add('bench-adopt');
    const active=flowStudioActive(g),spec=flowStudioSlots(g).find(x=>x.slot===active);
    if(spec?.origin==='captured'){
        const editor=root.querySelector(`[data-studio-slot="${CSS.escape(active)}"]`);
        if(editor)editor.innerHTML=`<h3>采集原图 · 未经 AI 修改</h3><img src="${flowMediaUrl(spec.output_path)}" alt="已入库原图" style="max-height:360px;max-width:100%;object-fit:contain"><p class="field-help">原图与封存采集文件一致。要改图时另建 AI 图位，原图不会被覆盖。</p>${benchButton('以这张原图另建 AI 图片','edit-original',`data-slot="${esc(active)}"`)}`;
    }
    return template.innerHTML;
};
flowStudioQueueHtml=function(jobs){
    const labels={queued:'排队中',running:'生成中',completed:'已完成',failed:'失败',unknown:'结果未知',stale:'资料变化，结果未替换'},phases={queued:'等待生成',provider_request:'模型处理中',verifying:'校验文件',committing:'保存结果',complete:'完成',completed:'完成'};
    const ids=new Set(benchWorkspaceSlots().map(x=>x.slot)),latest=new Map();for(const job of jobs)if(ids.has(job.slot))latest.set(job.slot,job);
    const rows=[...latest.values()].map(job=>({...job,phase:job.stage||job.phase,message:['failed','unknown','stale'].includes(job.status)?job.error||job.message:job.stage_label||job.message})),completed=rows.filter(x=>x.status==='completed').length,running=rows.filter(x=>['queued','running'].includes(x.status)).length;
    if(!rows.length)return '';
    return `<div class="bench-progress"><span>当前工作区 ${completed}/${rows.length} 项完成${running?` · ${running} 项进行中`:''}</span><progress value="${completed}" max="${rows.length}" aria-label="图片任务完成数量"></progress></div><div class="bench-queue">${[...rows].reverse().map(job=>`<div class="studio-job-row"><b>${esc(labels[job.status]||job.status)}</b><span>${esc(flowStudioSlotLabel(flowStudioSlots().find(x=>x.slot===job.slot)||job,flowStudioSlots().findIndex(x=>x.slot===job.slot)))}</span><span>${esc(phases[job.phase]||job.message||job.error||(job.status==='running'?'模型处理中，无精确百分比':''))}</span>${['failed','unknown','stale'].includes(job.status)?benchButton('核对后重试','retry-image',`data-slot="${esc(job.slot)}"`):''}</div>`).join('')}</div>`;
};
function benchImageTile(spec,g,selected=false,publishing=false){
    const selection=flowStudioSelection(g),file=(g.image_generation?.files||[]).find(x=>x.slot===spec.slot)||{},src=`${flowMediaUrl(spec.output_path)}?v=${encodeURIComponent(file.generation_id||file.sha256||'saved')}`;
    return `<article class="studio-result ${selected?'selected':''}" data-result-slot="${esc(spec.slot)}" draggable="${publishing}"><span class="bench-origin">${spec.origin==='captured'?'采集原图':spec.workspace==='set'?'套图结果':'单张结果'}</span><a href="${src}" target="_blank" rel="noopener noreferrer" draggable="false"><img draggable="false" src="${src}" alt="${esc(spec.purpose||'商品图片')}" loading="lazy"></a><div class="studio-result-heading">${publishing?`<span>第 ${selection.order.indexOf(spec.slot)+1} 张</span><span class="studio-drag-handle" aria-hidden="true">⠿</span>`:`<input type="checkbox" id="use-image-${esc(spec.slot)}" class="studioImageUse" data-slot="${esc(spec.slot)}" ${selected?'checked':''}><label for="use-image-${esc(spec.slot)}">用于上架</label>`}</div><p>${esc(spec.purpose||'商品图片')}</p><div class="studio-result-actions">${benchButton(spec.origin==='captured'?'另建 AI 改图':'修改 / 重做','edit-image',`data-slot="${esc(spec.slot)}"`)}${publishing?flowButton('左移','studio-move',selection.order.indexOf(spec.slot)===0,`data-slot="${esc(spec.slot)}" data-direction="-1"`)+flowButton('右移','studio-move',selection.order.indexOf(spec.slot)===selection.order.length-1,`data-slot="${esc(spec.slot)}" data-direction="1"`):''}</div></article>`;
}
flowStudioResultsHtml=function(g){
    // Editors are filtered to a workspace. Publishing is always the full
    // document: otherwise hidden selections could be submitted unexpectedly.
    g=state.guided||g;
    const generated=new Set(g.generated_image_paths||[]),selection=flowStudioSelection(g),ready=flowStudioSlots(g).filter(x=>generated.has(x.output_path)),workspaceReady=benchWorkspaceSlots(g).filter(x=>generated.has(x.output_path)),selected=new Set(selection.order),ordered=selection.order.map(id=>ready.find(x=>x.slot===id)).filter(Boolean);
    return `<section class="studio-results"><div class="studio-results-head"><h3>${benchWorkspace()==='set'?'本套图结果':'单张图片结果'} <span class="tag">${workspaceReady.length} 张</span></h3><span class="muted">每张独立编辑；勾选后加入下方上架图库。</span></div>${workspaceReady.length?`<div class="studio-result-grid">${workspaceReady.map(spec=>benchImageTile(spec,g,selected.has(spec.slot))).join('')}</div>`:'<div class="empty">可以生成图片，也可以直接使用左侧原图。</div>'}<section class="bench-publish-contact"><h3>已选上架图片 · ${ordered.length} 张</h3><p class="field-help">来自单图、套图或原图，统一按下方顺序上传；拖拽调整顺序。只发布勾选图片，不要求固定套图数量。</p>${ordered.length?`<div class="studio-result-grid">${ordered.map(spec=>benchImageTile(spec,g,true,true)).join('')}</div>`:'<p class="muted">尚未选择图片，可以先保存空草稿。实际提交仍需满足 Ozon 主图要求。</p>'}<div class="studio-confirm-row"><p>已选 ${ordered.length} 张${selection.dirty?' · 选择或排序待确认':''}</p>${flowButton('确认图片并下一步','studio-confirm')}</div></section></section>`;
};
flowStudioSaveSlot=async function(slot,product=state.product){
    if(listingFlow.skuDrafts.has(product))throw Error('请先确认最新规格');
    const editor=[...document.querySelectorAll('.slotPrompt')].find(x=>x.dataset.slot===slot),prompt=editor?.value.trim()||'',reference_ids=flowStudioReferenceIds(slot),spec=flowStudioSlots().find(x=>x.slot===slot);
    const workspace=benchWorkspace(),set_id=workspace==='set'?benchSetId():null,workspaceKey=benchWorkspaceKey(),activeBefore=flowStudioActive(),draft=productEditor.drafts.get(product),beforeEdits={};
    listingBench.activeSlots.set(workspaceKey,activeBefore);
    for(const key of [`prompt:${slot}`,`refs:${slot}`])beforeEdits[key]=draft?.edits?.[key];
    if(spec?.origin==='captured')throw Error('原图不会被直接覆盖，请另建 AI 图片');
    if(!prompt)throw Error('请填写图片提示词');if(reference_ids.length<1||reference_ids.length>3)throw Error('请选择 1–3 张参考图');
    const base=`/api/workbench/products/${encodeURIComponent(product)}/guided/media`;
    const result=slot==='new'?await api(`${base}/slots`,json('POST',{prompt,reference_ids,source_sku_id:$('#flowNewImageSku')?.value||state.skus.selected[0],role:$('#flowNewImageRole')?.value||'detail',workspace,set_id})):await api(`${base}/slots/${encodeURIComponent(slot)}`,json('PUT',{prompt,reference_ids}));
    let changed=false;for(const key of [`prompt:${slot}`,`refs:${slot}`]){if(draft?.edits?.[key]===beforeEdits[key]){draft?.pending?.delete(key);if(draft)delete draft.edits[key]}else changed=true}
    const resolved=typeof result.slot==='object'?result.slot.slot:result.slot||slot;
    if(product===state.product&&result.image_plan)state.guided.image_plan=result.image_plan;
    const activeUnchanged=listingBench.activeSlots.get(workspaceKey)===activeBefore;
    if(activeUnchanged)listingBench.activeSlots.set(workspaceKey,changed&&slot==='new'?'new':resolved);
    if(activeUnchanged&&product===state.product&&benchWorkspaceKey()===workspaceKey)listingFlow.studioSlots.set(product,changed&&slot==='new'?'new':resolved);
    return resolved;
};
const mediaStudioAction=flowStudioAction;
flowStudioAction=async function(action,button){
    if(!['studio-generate','studio-confirm'].includes(action))return mediaStudioAction(action,button);
    const product=state.product,slot=button.dataset.slot||flowStudioActive(),key=`${product}:${slot}`;
    if(listingFlow.studioRequests.has(key)||action==='studio-generate'&&flowStudioSlotBusy(slot))return;
    listingFlow.studioRequests.add(key);button.disabled=true;
    try{
        if(action==='studio-generate'){
            if(!imageBackendReady())throw Error('请先配置正式生图模型');
            const previous=[...(state.guided.image_jobs||[])].reverse().find(x=>x.slot===slot),regeneration=(state.guided.generated_image_paths||[]).includes(flowStudioSlots().find(x=>x.slot===slot)?.output_path);
            const retry=['failed','unknown','stale'].includes(previous?.status);
            if((retry||regeneration)&&!confirm(`${retry?'上次任务失败或结果未知，请先核对调用记录。':'重新生成会替换本张成功图片，其他图片不变。'}确认再次调用付费模型？`))return;
            const resolved=await flowStudioSaveSlot(slot,product),result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/media/slots/${encodeURIComponent(resolved)}/generate`,json('POST',{confirm_new_charge:retry}));
            if(product===state.product)state.guided.image_jobs=[...(state.guided.image_jobs||[]),result.job];listingFlow.jobProducts.add(product);flowStudioStartPolling();notice('本张已加入队列，可继续生成其他图片');
        }else{
            const selected_slots=[...flowStudioSelection().order];
            await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/media/selection`,json('PUT',{selected_slots}));
            await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/media/confirm`,json('POST',{}));
            const changed=JSON.stringify(listingFlow.mediaDrafts.get(product)?.order||selected_slots)!==JSON.stringify(selected_slots);
            if(!changed){listingFlow.mediaDrafts.delete(product);listingFlow.steps.set(product,'card')}
            if(product===state.product)await refreshProduct();
            notice(changed?'已确认发送时的图片；操作期间的新选择仍保留，请再次确认':`已确认 ${selected_slots.length} 张图片`);
        }
        if(product===state.product)renderProduct();
    }catch(error){listingFlow.errors.set(product,{step:'media',action,message:error.message});notice(error.message,true);if(product===state.product)renderProduct()}
    finally{listingFlow.studioRequests.delete(key);if(button.isConnected)button.disabled=false;if(product===state.product)flowStudioRefreshReferences()}
};
flowStudioPollJobs=async function(){
    listingFlow.jobTimer=null;if(listingFlow.jobPolling)return;listingFlow.jobPolling=true;
    try{await Promise.all([...listingFlow.jobProducts].map(async product=>{
        try{const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/media`),jobs=result.jobs||[],running=jobs.some(x=>['queued','running'].includes(x.status));
            if(product===state.product){benchMergeMedia(result,product);const queue=$('#flowStudioQueue');if(queue)queue.innerHTML=flowStudioQueueHtml(jobs);flowStudioRefreshResults();flowStudioRefreshReferences();for(const spec of flowStudioSlots()){const label=document.querySelector(`[data-slot-status="${CSS.escape(spec.slot)}"]`);if(label)label.textContent=flowStudioSlotStatus(spec,state.guided)}}
            if(!running)listingFlow.jobProducts.delete(product);
        }catch(error){const queue=product===state.product?$('#flowStudioQueue'):null;if(queue)queue.innerHTML=`<p class="studio-action-error" role="alert">任务刷新失败：${esc(error.message)}。不会重新调用模型，稍后自动重读任务。</p>`}
    }))}finally{listingFlow.jobPolling=false;if(listingFlow.jobProducts.size)flowStudioStartPolling()}
};
document.addEventListener('input',event=>{if(event.target.id==='benchSetCount')listingBench.counts.set(`${state.product}:set`,event.target.value)});
document.addEventListener('change',event=>{if(event.target.id==='benchSetSelect'){benchRememberWorkspace();listingBench.sets.set(state.product,event.target.value);benchRestoreWorkspace();renderProduct()}});
document.addEventListener('click',async event=>{
    const button=event.target.closest('[data-bench-action]');if(!button||['reserve-offers','save-operational','reload-document'].includes(button.dataset.benchAction))return;
    const action=button.dataset.benchAction,product=state.product,base=`/api/workbench/products/${encodeURIComponent(product)}/guided/media`,workspace=benchWorkspace(),setId=benchSetId(),workspaceKey=benchWorkspaceKey(),activeBefore=flowStudioActive(),selectionBefore=[...flowStudioSelection().order];
    listingBench.activeSlots.set(workspaceKey,activeBefore);
    if(action==='workspace'){benchRememberWorkspace();listingBench.workspaces.set(product,button.dataset.mode);benchRestoreWorkspace();renderProduct();return}
    if(action==='edit-image'||action==='edit-original'){
        const spec=flowStudioSlots().find(x=>x.slot===button.dataset.slot);if(!spec)return;
        benchRememberWorkspace();listingBench.workspaces.set(product,spec.workspace||'single');if(spec.set_id)listingBench.sets.set(product,spec.set_id);
        listingFlow.studioSlots.set(product,spec.origin==='captured'?'new':spec.slot);
        if(spec.origin==='captured'){const refs=spec.reference_image_ids||[],draft=productDraft();draft.edits['refs:new']=refs.join(', ');draft.edits['prompt:new']='保留参考图片中商品的真实造型和颜色。'+flowStudioGuidance(state.guided,spec)}
        renderProduct();document.querySelector('.studio-slot-editor:not([hidden]) .slotPrompt')?.focus({preventScroll:true});return;
    }
    button.disabled=true;
    try{
        if(action==='create-set'){
            const count=Number($('#benchSetCount').value),reference_ids=flowStudioReferenceIds(flowStudioActive()),prompt=document.querySelector('.studio-slot-editor:not([hidden]) .slotPrompt')?.value.trim()||'';
            if(!Number.isInteger(count)||count<1||count>50)throw Error('套图数量请填 1–50 的整数');if(reference_ids.length<1||reference_ids.length>3)throw Error('请选择 1–3 张参考图');
            const result=await api(`${base}/sets`,json('POST',{count,prompt,reference_ids,source_sku_id:$('#flowNewImageSku')?.value||state.skus.selected[0]}));
            const showNew=state.product===product&&benchWorkspaceKey()===workspaceKey&&listingBench.activeSlots.get(workspaceKey)===activeBefore;
            if(showNew)listingBench.sets.set(product,result.set_id);await benchReadMedia(product);
            const first=result.slots?.[0]||'new';listingBench.activeSlots.set(`${product}:set:${result.set_id}`,first);
            if(showNew)listingFlow.studioSlots.set(product,first);notice(`已建立 ${count} 张套图规划，尚未调用生图模型`);
        }
        if(action==='generate-set'){
            benchPaidPrerequisite(product);
            for(const slot of flowStudioSlots().filter(x=>x.set_id===button.dataset.set))if(productDraft()?.pending?.has(`prompt:${slot.slot}`)||productDraft()?.pending?.has(`refs:${slot.slot}`))throw Error('本套图有未保存的提示词或参考图，请先逐张保存再批量生成');
            const set=benchSets().find(x=>x.id===button.dataset.set),unstarted=Number(set?.unstarted||0);if(!unstarted)throw Error('本套图没有未启动图片。失败项请逐张核对后重试');
            if(!confirm(`确认把本套图 ${unstarted} 张未启动图片加入付费生成队列？已经完成或失败的图片不会重新调用。`))return;
            const result=await api(`${base}/sets/${encodeURIComponent(button.dataset.set)}/generate`,json('POST',{}));await benchReadMedia(product);
            listingFlow.jobProducts.add(product);flowStudioStartPolling();notice(`已排队 ${result.accepted?.length||0} 张${result.rejected?.length?`，${result.rejected.length} 张未入队，请检查任务` : ''}`);
        }
        if(action==='adopt-original'){
            const result=await api(`${base}/adopt`,json('POST',{reference_id:button.dataset.reference,source_sku_id:$('#benchOriginalSku').value,role:$('#benchOriginalRole').value,workspace,set_id:workspace==='set'?setId:null}));
            await benchReadMedia(product);const slot=typeof result.slot==='object'?result.slot.slot:result.slot,selection=listingFlow.mediaDrafts.get(product)?.order||selectionBefore;
            listingFlow.mediaDrafts.set(product,{order:[...new Set([...selection,slot])],dirty:true});
            if(listingBench.activeSlots.get(workspaceKey)===activeBefore){listingBench.activeSlots.set(workspaceKey,slot);if(product===state.product&&benchWorkspaceKey()===workspaceKey)listingFlow.studioSlots.set(product,slot)}
            notice('原图已加入候选图库，未调用 AI；请确认上架顺序');
        }
        if(action==='retry-image'){
            benchPaidPrerequisite(product);
            if(productDraft()?.pending?.has(`prompt:${button.dataset.slot}`)||productDraft()?.pending?.has(`refs:${button.dataset.slot}`))throw Error('请先保存本张提示词和参考图，再确认付费重试');
            if(!confirm('失败或结果未知的任务可能已经计费。请核对模型调用记录；确认再次付费重试这张图？其他图片不变。'))return;
            await api(`${base}/slots/${encodeURIComponent(button.dataset.slot)}/retry`,json('POST',{confirm_new_charge:true}));await benchReadMedia(product);listingFlow.jobProducts.add(product);flowStudioStartPolling();notice('本张重试已加入队列');
        }
        if(action==='readback'){
            const result=await api(`/api/workbench/products/${encodeURIComponent(product)}/guided/readback`,json('POST',{store:benchShop()}));flowShowResult(result);return;
        }
        if(state.product===product&&state.view==='product')renderProduct();
    }catch(error){listingFlow.errors.set(product,{step:flowStep(),action,message:error.message});notice(error.message,true);if(state.product===product)renderProduct()}
    finally{if(button.isConnected)button.disabled=false}
});
const benchShowResult=flowShowResult;
const benchReadbackLabels={not_selected:'未选择上架视频',expectation_unknown:'缺少提交快照，无法核对视频',awaiting_submission:'尚未提交商品卡',awaiting_readback:'等待接口回读',readback_failed:'读取接口失败',processing_or_not_yet_readable:'处理中或尚未回读到',readable_in_api:'官方接口可回读',rehosted_in_api_unverified_identity:'平台已转存视频，内容身份仍需核对',readback_mismatch:'回读内容不一致',failed:'媒体回读失败'};
flowShowResult=function(result){
    const media=result.report?.media_readback||result.media_readback;benchShowResult(result);const host=$('#flowPublishResult');if(!host)return;
    if(media){const states=benchReadbackLabels,report=result.report||result;host.innerHTML=`<section class="bench-readback"><h3>官方卡片媒体回读</h3>${report.ok===false?'<p class="bad" role="alert">整张卡片的对账尚未全部通过，请查看下方原因。</p>':''}${report.message?`<p>${esc(report.message)}</p>`:''}<p>${esc(states[media.status]||media.status)}${Number.isFinite(media.video_expected)?` · 预期 ${media.video_expected} 段视频 / 可回读 ${media.video_readable??'—'} 段`:''}</p>${(report.failed||[]).length?`<ul>${(report.checks||[]).filter(check=>!check.ok&&!check.soft).map(check=>`<li>${esc(check.detail||check.name)}</li>`).join('')}</ul>`:''}<table><thead><tr><th>货号</th><th>视频回读</th><th>结果</th></tr></thead><tbody>${(media.items||[]).map(row=>`<tr><td>${esc(row.offer_id)}</td><td>${row.observed_count||0} / ${row.expected_count||0}</td><td>${esc(states[row.status]||row.status)}</td></tr>`).join('')}</tbody></table><p class="field-help">接口可回读不代表买家页已可播放。该操作只读取已有提交任务，不会重复发布或填写库存。</p></section><details><summary>查看接口检查明细</summary><pre>${esc(JSON.stringify(result,null,2))}</pre></details>`}
    const checked=result.report?.checked_at||result.checked_at;if(checked)host.querySelector('.bench-readback')?.insertAdjacentHTML('beforeend',`<p class="field-help">回读时间：${esc(checked)}</p>`);
};
const mediaRenderProduct=renderProduct;
renderProduct=function(){mediaRenderProduct();const verify=document.querySelector('[data-flow-action="verify"]');if(verify){verify.removeAttribute('data-flow-action');verify.dataset.benchAction='readback';verify.textContent='回读已提交卡片与视频'}const preview=document.querySelector('[data-pane="preview"]');if(preview)for(const section of preview.querySelectorAll(':scope>.panel'))if(section.querySelector('h2')?.textContent==='主题标签')section.remove()};
productPreviewHtml=function(){
    const g=state.guided||{},doc=benchDocument(),copy=doc?.copy||g.copy||{},order=flowStudioSelection(g).order,generated=new Set(g.generated_image_paths||[]),images=order.map(id=>flowStudioSlots(g).find(x=>x.slot===id)).filter(x=>x&&generated.has(x.output_path)),rows=doc?.selected_skus||[];
    return `<section class="panel"><h2>商品卡预览</h2><h3>${esc(copy.title_ru||'尚未确认标题')}</h3><p style="white-space:pre-wrap;line-height:1.8">${esc(copy.description_ru||'尚未确认简介')}</p>${flowTags(copy.hashtags)}<div class="studio-result-grid">${images.map((spec,index)=>`<div><img src="${flowMediaUrl(spec.output_path)}" style="max-width:100%;aspect-ratio:3/4;object-fit:contain" alt="第 ${index+1} 张上架图片" loading="lazy"><p class="field-help">第 ${index+1} 张 · ${spec.origin==='captured'?'原图':'生成图'}</p></div>`).join('')}</div>${!images.length?'<p class="muted">没有选择上架图片。草稿可保存；真实提交前会检查官方媒体要求。</p>':''}<h3>本次上架规格</h3>${rows.map(row=>`<p>${esc(row.name)} · ${esc(row.offer_id||'货号未分配')} · ${esc(row.manual_price?.price??'售价未填写')} ${esc(row.manual_price?.currency||'')}</p>`).join('')}<p class="field-help">只展示选定顺序中的图片。卡片和 Excel 使用同一份已确认资料。</p></section>`;
};
function benchCanonicalPreviewHtml(){
    const doc=benchDocument(),card=doc?.card||{},packageValues=doc?.operational_fields?.package||{},fields=new Map((card.form?.fields||[]).map(field=>[String(field.attribute_id),field.name||field.attribute_name])),selected=doc?.selected_skus||[];
    const attributesHtml=values=>Object.entries(values||{}).map(([id,values])=>`<dt>${esc(fields.get(id)||`属性 ${id}`)}</dt><dd>${esc((values||[]).map(value=>String(value.value??'')).join(' / '))}</dd>`).join('');
    const videos=state.guided?.media_selection?.videos||[];
    return `<section class="panel"><h3>含包装信息</h3><dl class="flow-facts">${[['weight_g','含包装重量','克'],['length_mm','包装长度','毫米'],['width_mm','包装宽度','毫米'],['height_mm','包装高度','毫米']].map(([key,label,unit])=>`<dt>${label}</dt><dd>${packageValues[key]?`${esc(packageValues[key])} ${unit}`:'待填写'}</dd>`).join('')}</dl><details><summary>官方属性明细</summary><h3>商品共用属性</h3><dl class="flow-facts">${attributesHtml(card.attributes)}</dl>${selected.filter(row=>Object.keys(card.per_sku_attributes?.[row.sku_id]||{}).length).map(row=>`<h3>${esc(row.name)} · 规格属性</h3><dl class="flow-facts">${attributesHtml(card.per_sku_attributes[row.sku_id])}</dl>`).join('')}</details><h3>上架视频 · ${videos.length} 段</h3>${videos.length?videos.map(video=>`<p>${esc(video.title||'Видео товара')} · ${video.source_sku_id?`规格 ${esc(video.source_sku_id)}`:'商品共用'}</p>`).join(''):'<p class="muted">未选择上架视频。</p>'}</section>`;
}
const benchMediaPreview=productPreviewHtml;
productPreviewHtml=function(){const copy=benchDocument()?.copy,status=copy?.status;return `${!copy?.confirmed?`<p class="hint warn" role="status">${status==='stale'?'文案资料已过期，请重新核对摘要、文案和卡片。':'下面是草稿预览，文案尚未确认。'}</p>`:''}`+benchMediaPreview()+benchCanonicalPreviewHtml()};
function benchPaidPrerequisite(product){
    if(listingFlow.skuDrafts.has(product))throw Error('规格勾选尚未保存，请返回第一步确认规格；本次未调用模型');
    if(listingFlow.factDrafts.has(product))throw Error('商品事实尚未保存，请先保存并核对摘要；本次未调用模型');
}
document.addEventListener('click',event=>{
    const button=event.target.closest('[data-flow-action]');
    if(button&&['studio-slot','studio-new'].includes(button.dataset.flowAction))listingBench.activeSlots.set(benchWorkspaceKey(),button.dataset.slot||'new');
    if(button&&['prepare-card','submit','export','publish-media'].includes(button.dataset.flowAction)&&listingFlow.mediaDrafts.get(state.product)?.dirty){event.preventDefault();event.stopImmediatePropagation();notice('图片选择或顺序尚未确认，请先回到图片区确认，不会按旧图库发布',true)}
},true);
