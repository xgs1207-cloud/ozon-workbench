/* Rich content is a local, reviewable card draft. AI responses never apply themselves. */
const richContentBench = {entries:new Map(), dialog:null, opening:false, sequence:0};
function richScope(product=state.product,shop=benchShop()) {
    return typeof benchScopeKey==='function'?benchScopeKey(product,shop):`${product}:${shop}`;
}
function richEntry(product=state.product,shop=benchShop()) {
    const key=richScope(product,shop);
    if(!richContentBench.entries.has(key))richContentBench.entries.set(key,{key,product,shop,loaded:false,loading:false,error:'',data:null});
    return richContentBench.entries.get(key);
}
function richClone(value){return JSON.parse(JSON.stringify(value))}
function richUrl(entry,suffix=''){
    return `/api/workbench/products/${encodeURIComponent(entry.product)}/rich-content${suffix}${entry.shop?`?shop=${encodeURIComponent(entry.shop)}`:''}`;
}
function richImageUrl(entry,media){
    const value=String(media?.preview_url||''),prefix=`/api/workbench/products/${encodeURIComponent(entry.product)}/`;
    // Media IDs, not arbitrary remote URLs, enter the draft and the uploader.
    if(!value.startsWith(prefix)||value.includes('\\')||value.includes('#'))return '';
    const route=value.slice(prefix.length).split('?')[0];
    if(!/^(?:media\/|source-asset\/|rich-content\/images\/)/.test(route))return '';
    try{if(decodeURIComponent(route).split('/').some(part=>part==='.'||part==='..'))return ''}catch{return ''}
    return value;
}
function richBlock(type='text'){
    return {id:`rich-${Date.now()}-${++richContentBench.sequence}`,type,image_id:null,title:'',text:''};
}
function richBlockLabel(type){return ({image:'图片',image_text:'图片与文字',text:'文字'})[type]||'内容'}
function richMove(blocks,id,direction){
    const index=blocks.findIndex(block=>block.id===id),target=index+direction;
    if(index<0||target<0||target>=blocks.length)return blocks;
    const result=[...blocks];[result[index],result[target]]=[result[target],result[index]];return result;
}
function richValidBlocks(blocks,media){
    if(!Array.isArray(blocks))throw Error('富内容板块格式不正确');
    const mediaIds=new Set((media||[]).map(row=>String(row.id))),ids=new Set();
    for(const block of blocks){
        if(!block||!['image','image_text','text'].includes(block.type)||typeof block.id!=='string'||ids.has(block.id))throw Error('请检查板块类型与编号');
        ids.add(block.id);
        if(typeof block.title!=='string'||typeof block.text!=='string')throw Error('请检查标题和正文');
        if(block.type!=='text'&&(!block.image_id||!mediaIds.has(String(block.image_id))))throw Error('图片板块需要选择一张已导入或生成的图片');
        if(block.type!=='image'&&!block.title.trim()&&!block.text.trim())throw Error('文字板块需要填写标题或正文');
    }
    return blocks;
}
function richSessionCurrent(session){
    return state.product===session.entry.product&&benchShop()===session.entry.shop&&richScope()===session.entry.key;
}
function richRequireCurrent(session){if(!richSessionCurrent(session))throw Error('当前商品或类目已改变，请取消编辑并重新打开富内容')}
async function richLoad(entry=richEntry(),force=false){
    if(entry.loading){await entry.reading;return entry}
    if(entry.loaded&&!force||entry.error&&!force)return entry;
    entry.loading=true;entry.error='';
    entry.reading=(async()=>{
        try{entry.data=await api(richUrl(entry));entry.loaded=true}
        catch(error){entry.error=error.message}
        finally{entry.loading=false;entry.reading=null}
    })();
    await entry.reading;
    return entry;
}
function benchRichContentHtml(){
    const entry=richEntry(),data=entry.data,blocks=data?.blocks||[];
    const text=entry.loading?'读取富内容…':entry.error?'读取失败，点击重试':data&&!data.attribute_id?'当前类目不支持富内容':blocks.length?`${blocks.length} 个板块，点击编辑`:'图片与文字，可添加或由 AI 起草';
    return `<section class="panel bench-rich-field" id="benchRichContentField"><label class="field"><strong>JSON 富内容</strong><button type="button" class="rich-field-control" data-rich-action="open" ${entry.loading?'disabled':''}><span>${esc(text)}</span><span aria-hidden="true">${entry.error?'重试':'编辑'}</span></button></label><p class="field-help">与上架卡片共用一份草稿，可使用导入图片或已经生成的图片。</p></section>`;
}
function benchRichContentEnhance(){
    if(!state.product||!state.guided)return;
    if(typeof flowStep==='function'&&flowStep()!=='card')return;
    const card=document.querySelector('[data-pane="card"]');if(!card)return;
    const previous=card.querySelector('#benchRichContentField');if(previous)previous.remove();
    const holder=document.createElement('div');holder.innerHTML=benchRichContentHtml();
    const panel=holder.firstElementChild,check=card.querySelector('.bench-card-check');
    check?card.insertBefore(panel,check):card.append(panel);
    const entry=richEntry();
    if(!entry.loaded&&!entry.loading&&!entry.error)void richLoad(entry).then(()=>{if(state.product===entry.product&&richScope()===entry.key)benchRichContentEnhance()});
    if(richContentBench.dialog&&!richSessionCurrent(richContentBench.dialog.session))richEditorError(richContentBench.dialog.session,'当前商品或类目已改变，请取消编辑并重新打开富内容');
}
function richElement(tag,className,text){const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node}
function richButton(text,action,extra={}){
    const node=richElement('button','rich-button',text);node.type='button';node.dataset.richAction=action;
    for(const [key,value]of Object.entries(extra))node.dataset[key]=value;
    return node;
}
function richEditorError(session,message=''){
    session.error=message;
    const host=session.nodes?.error;if(host){host.hidden=!message;host.textContent=message}
}
function richDirty(session){session.dirty=true;session.localRevision++;richUpdateStatus(session)}
function richUpdateStatus(session){
    if(!session.nodes)return;
    session.nodes.status.textContent=session.saving?'正在保存草稿…':session.dirty?'有未应用的修改':'编辑不会自动保存或发布';
    session.nodes.apply.disabled=session.saving||session.uploading;
    session.nodes.cancel.disabled=session.saving;
    session.nodes.generate.disabled=session.aiBusy||session.saving;
    session.nodes.generate.textContent=session.aiBusy?'AI 正在生成…':'生成 / 按提示修改';
    session.nodes.import.disabled=session.uploading||session.saving;
}
function richRenderPreview(session,host=session.nodes.preview,blocks=session.blocks){
    if(!host)return;host.replaceChildren();
    if(!blocks.length){host.append(richElement('p','rich-empty','先添加图片或文字板块，也可以让 AI 起草。'));return}
    const media=new Map((session.media||[]).map(row=>[String(row.id),row]));
    for(const block of blocks){
        const article=richElement('article',`rich-preview-block rich-preview-${block.type}`);
        if(block.type!=='text'){
            const image=media.get(String(block.image_id)),url=richImageUrl(session.entry,image);
            if(url){const node=richElement('img');node.src=url;node.alt=image.label||block.title||'富内容图片';node.loading='lazy';article.append(node)}
            else article.append(richElement('p','rich-empty','尚未选择图片'));
        }
        if(block.type!=='image'){
            if(block.title)article.append(richElement('h3','',block.title));
            if(block.text)article.append(richElement('p','',block.text));
        }
        host.append(article);
    }
}
function richRenderBlocks(session){
    const list=session.nodes.blocks;list.replaceChildren();
    if(!session.blocks.length){list.append(richElement('p','rich-empty','添加一个板块开始编辑。'));richRenderPreview(session);return}
    session.blocks.forEach((block,index)=>{
        const row=richElement('article',`rich-block${session.activeBlock===block.id?' active':''}`);row.dataset.richBlock=block.id;
        const heading=richElement('div','rich-block-heading');
        heading.append(richElement('strong','',`${index+1}. ${richBlockLabel(block.type)}`));
        const buttons=richElement('div','rich-block-actions');
        for(const [text,action,disabled]of [['上移','up',index===0],['下移','down',index===session.blocks.length-1],['删除','delete',false]]){
            const button=richButton(text,action,{block:block.id});button.disabled=disabled||session.saving;buttons.append(button);
        }
        heading.append(buttons);row.append(heading);
        if(block.type!=='text'){
            const image=session.media.find(item=>String(item.id)===String(block.image_id)),url=richImageUrl(session.entry,image);
            const select=richButton(image?'更换图片':'选择图片','choose-image',{block:block.id});select.classList.add('rich-image-choice');
            if(url){const node=richElement('img');node.src=url;node.alt=image.label||'当前图片';select.prepend(node)}
            row.append(select);
        }
        if(block.type!=='image'){
            for(const [key,title,tag]of [['title','标题（俄文）','input'],['text','正文（俄文）','textarea']]){
                const label=richElement('label','rich-input-label',title),input=richElement(tag,'rich-content-input');
                input.value=block[key]||'';input.dataset.richInput=key;input.dataset.block=block.id;input.disabled=session.saving;
                if(tag==='textarea')input.rows=4;
                label.append(input);row.append(label);
            }
        }
        list.append(row);
    });
    richRenderPreview(session);richUpdateStatus(session);
}
function richRenderMedia(session){
    const host=session.nodes.media;host.replaceChildren();
    const media=session.media.filter(row=>session.mediaKind==='all'||row.kind===session.mediaKind);
    session.nodes.mediaCount.textContent=`${media.length} 张可选图片`;
    for(const item of media){
        const url=richImageUrl(session.entry,item);if(!url)continue;
        const button=richButton('','select-media',{image:String(item.id)});button.classList.add('rich-media-choice');
        const image=richElement('img');image.src=url;image.alt=item.label||richBlockLabel('image');image.loading='lazy';
        const label=richElement('span','',item.label||({captured:'采集原图',generated:'生成图片',imported:'导入图片'})[item.kind]||'图片');
        button.append(image,label);host.append(button);
    }
    if(!host.children.length)host.append(richElement('p','rich-empty','这一组还没有图片。可以导入本地图片，或先在生图步骤生成。'));
}
function richRenderConversation(session){
    const host=session.nodes.messages;host.replaceChildren();
    for(const turn of session.history){
        const article=richElement('article',`rich-chat-turn ${turn.role}`);
        article.append(richElement('strong','',turn.role==='user'?'你':'AI'),richElement('p','',turn.content));host.append(article);
    }
    if(session.candidate){
        const candidate=richElement('section','rich-candidate');
        candidate.append(richElement('strong','','AI 内容候选'),richElement('p','rich-help','请先预览，点击后才会替换编辑区。不会直接保存或发布。'));
        const preview=richElement('div','rich-candidate-preview');richRenderPreview(session,preview,session.candidate.blocks||[]);candidate.append(preview);
        const button=richButton('将候选用到编辑区','use-candidate');button.disabled=session.saving;candidate.append(button);host.append(candidate);
    }
    host.scrollTop=host.scrollHeight;
}
function richSelectCandidate(session){
    richRequireCurrent(session);
    if(!session.candidate)throw Error('请先生成富内容候选');
    if(session.dirty&&!confirm('这会替换当前未保存的板块。确认使用这份 AI 候选？'))return false;
    session.blocks=richClone(session.candidate.blocks||[]);session.activeBlock=session.blocks[0]?.id||'';
    richDirty(session);richRenderBlocks(session);richEditorError(session);return true;
}
async function richGenerate(session){
    richRequireCurrent(session);
    if(session.aiBusy||session.saving)throw Error('当前操作仍在处理中');
    const prompt=(session.nodes?.prompt.value??session.prompt??'').trim();
    if(!prompt)throw Error('请用中文说明希望生成或修改的内容');
    if(prompt.length>6000)throw Error('修改提示词最多 6000 个字');
    const snapshot={revision:session.revision,context_fingerprint:session.context,blocks:richClone(session.blocks),history:session.history.slice(-12).map(turn=>({role:turn.role,content:turn.content})),prompt};
    session.aiBusy=true;session.prompt=prompt;session.history.push({role:'user',content:prompt});richRenderConversation(session);richUpdateStatus(session);richEditorError(session);
    try{
        const result=await api(richUrl(session.entry,'/generate'),json('POST',snapshot));
        const candidate=result.candidate;
        if(!candidate||!Array.isArray(candidate.blocks))throw Error('AI 未返回可编辑的内容，请重新输入提示词');
        if(candidate.context_fingerprint&&candidate.context_fingerprint!==session.context)throw Error('AI 候选对应旧的商品资料，请重新打开后生成');
        session.candidate=richClone(candidate);session.history.push({role:'assistant',content:candidate.message_zh||'已生成一份俄文内容候选，请预览后选择是否使用。'});
        // Never mutate blocks, even if the user edited while this request was pending.
        if(session.open)richRenderConversation(session);
        return candidate;
    }catch(error){if(session.open)richEditorError(session,error.message);throw error}
    finally{session.aiBusy=false;if(session.open)richUpdateStatus(session)}
}
async function richSave(session){
    richRequireCurrent(session);
    if(session.saving||session.uploading)throw Error('当前操作仍在处理中');
    richValidBlocks(session.blocks,session.media);
    const submitted=richClone(session.blocks),localRevision=session.localRevision;
    session.saving=true;richUpdateStatus(session);richEditorError(session);
    try{
        const result=await api(richUrl(session.entry),json('PUT',{revision:session.revision,context_fingerprint:session.context,blocks:submitted}));
        session.entry.data=result;session.entry.loaded=true;session.entry.error='';
        session.revision=result.revision;session.context=result.context_fingerprint;
        if(localRevision===session.localRevision){session.dirty=false;session.blocks=richClone(result.blocks||submitted)}
        if(session.open&&localRevision===session.localRevision)richCloseEditor(session,true);
        if(state.product===session.entry.product&&richScope()===session.entry.key){
            if(typeof captureProductFields==='function')captureProductFields();
            if(typeof benchLoadDocument==='function')await benchLoadDocument(session.entry.product,session.entry.shop);
            if(typeof loadListingForm==='function')await loadListingForm();
            if(typeof fetchProduct==='function')await fetchProduct(session.entry.product);else benchRichContentEnhance();
        }
        if(typeof notice==='function')notice('富内容已应用到卡片草稿；尚未向 Ozon 发布');
        return result;
    }catch(error){richEditorError(session,error.message);throw error}
    finally{session.saving=false;if(session.open)richUpdateStatus(session)}
}
function richReadFile(file){return new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');reader.onerror=()=>reject(Error('无法读取图片，请重新选择'));reader.readAsDataURL(file)})}
async function richImportImages(session,files){
    richRequireCurrent(session);
    if(session.uploading||session.saving)throw Error('图片导入仍在处理中');
    const selected=[...files];
    if(!selected.length)return;
    if(selected.length>10)throw Error('每次最多导入 10 张图片');
    for(const file of selected){if(!['image/png','image/jpeg','image/webp'].includes(file.type))throw Error('支持 PNG、JPEG 和 WebP 图片');if(file.size>10*1024*1024)throw Error('单张图片不能超过 10 MB')}
    session.uploading=true;richUpdateStatus(session);richEditorError(session);
    try{
        for(const file of selected){
            const data_base64=await richReadFile(file);
            const result=await api(richUrl(session.entry,'/images'),json('POST',{filename:file.name,data_base64}));
            if(Array.isArray(result.media))session.media=richClone(result.media);
            else if(result.image&&!session.media.some(item=>item.id===result.image.id))session.media.push(richClone(result.image));
            if(result.image)session.lastImported=String(result.image.id);
            if(session.open){session.nodes.status.textContent=`已导入：${file.name}`;richRenderMedia(session)}
        }
        session.mediaKind='imported';if(session.nodes?.filter)session.nodes.filter.value='imported';
        if(session.open)richRenderMedia(session);
    }catch(error){if(session.open)richRenderMedia(session);richEditorError(session,error.message);throw error}
    finally{session.uploading=false;if(session.open)richUpdateStatus(session)}
}
function richCloseEditor(session,saved=false){
    if(session.saving&&!saved)return;
    session.open=false;
    if(session.nodes?.dialog){session.nodes.dialog.close();session.nodes.dialog.remove()}
    if(richContentBench.dialog?.session===session)richContentBench.dialog=null;
    if(session.opener?.isConnected)session.opener.focus();
}
async function richOpenEditor(opener=null){
    if(richContentBench.dialog){richContentBench.dialog.session.nodes.prompt.focus();return richContentBench.dialog.session}
    if(richContentBench.opening)return;
    richContentBench.opening=true;
    const entry=richEntry();
    try{
        if(typeof captureProductFields==='function')captureProductFields();
        await richLoad(entry,true);
        if(entry.error)throw Error(entry.error);
        if(!entry.data?.attribute_id)throw Error('当前官方类目没有富内容字段，请先确认类目');
        if(state.product!==entry.product||richScope()!==entry.key)return;
    }finally{richContentBench.opening=false}
    const candidates=(entry.data.candidates||[]).filter(row=>row.status==='candidate'&&row.revision===entry.data.revision&&row.context_fingerprint===entry.data.context_fingerprint);
    const history=candidates.slice(-6).flatMap(row=>[{role:'user',content:row.prompt_zh||'生成富内容'},{role:'assistant',content:row.message_zh||'已生成俄文内容候选。'}]);
    const session={entry,revision:entry.data.revision,context:entry.data.context_fingerprint,blocks:richClone(entry.data.blocks||[]),media:richClone(entry.data.media||[]),history,candidate:candidates.length?richClone(candidates.at(-1)):null,prompt:'',dirty:false,localRevision:0,aiBusy:false,saving:false,uploading:false,mediaKind:'all',activeBlock:'',open:true,opener};
    session.activeBlock=session.blocks[0]?.id||'';
    const dialog=richElement('dialog','rich-editor-dialog');dialog.setAttribute('aria-labelledby','richEditorTitle');
    // Static shell only; all supplier, user and AI strings are inserted using textContent.
    dialog.innerHTML=`<header class="rich-editor-header"><h2 id="richEditorTitle">编辑 JSON 富内容</h2><div class="rich-header-actions"><button type="button" class="rich-button" data-rich-action="cancel">取消</button><button type="button" class="rich-button primary" data-rich-action="apply">应用到卡片</button></div></header><p class="rich-editor-error" role="alert" hidden></p><div class="rich-editor-layout"><section class="rich-edit-column"><div class="rich-toolbar"><label>添加板块<select class="rich-add-type"><option value="image">图片</option><option value="image_text">图片与文字</option><option value="text">文字</option></select></label><button type="button" class="rich-button primary" data-rich-action="add">添加</button></div><div class="rich-block-list"></div><details class="rich-media-picker"><summary>选择图片 <span class="rich-media-count"></span></summary><div class="rich-media-tools"><select class="rich-media-filter" aria-label="图片来源"><option value="all">全部图片</option><option value="captured">采集原图</option><option value="generated">生成图片</option><option value="imported">导入图片</option></select><button type="button" class="rich-button" data-rich-action="import">导入图片</button><input class="rich-import-input" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden></div><p class="rich-help">点击图片应用到当前图片板块。导入图片不会修改生图结果。</p><div class="rich-media-grid"></div></details></section><section class="rich-preview-column"><h3>买家内容预览</h3><div class="rich-preview-content"></div></section><aside class="rich-ai-column"><h3>AI 对话修改</h3><p class="rich-help">结合所选规格、产品卖点和关键词生成俄文内容。中文提示词只用于修改，不会放入买家内容。</p><div class="rich-chat-messages" aria-live="polite"></div><label class="rich-input-label">修改提示词<textarea class="rich-ai-prompt" rows="4" maxlength="6000" placeholder="例如：突出产品设计和使用场景，保留已确认的尺寸，自然加入已选关键词。"></textarea></label><button type="button" class="rich-button primary" data-rich-action="generate">生成 / 按提示修改</button><p class="rich-help">点击才调用文本模型。AI 候选需要你确认后才进入编辑区。</p></aside></div><footer class="rich-editor-footer"><span class="rich-editor-status"></span><span>图片和文字按板块顺序显示</span></footer>`;
    const query=selector=>dialog.querySelector(selector);
    session.nodes={dialog,blocks:query('.rich-block-list'),preview:query('.rich-preview-content'),media:query('.rich-media-grid'),mediaCount:query('.rich-media-count'),picker:query('.rich-media-picker'),filter:query('.rich-media-filter'),file:query('.rich-import-input'),addType:query('.rich-add-type'),prompt:query('.rich-ai-prompt'),messages:query('.rich-chat-messages'),error:query('.rich-editor-error'),status:query('.rich-editor-status'),apply:query('[data-rich-action="apply"]'),cancel:query('[data-rich-action="cancel"]'),generate:query('[data-rich-action="generate"]'),import:query('[data-rich-action="import"]')};
    if(entry.data.warning||entry.data.legacy_json){
        const warning=richElement('p','rich-legacy-warning',entry.data.warning||'当前富内容来自旧版 JSON，原值仍保留；应用新板块会替换旧值。');
        query('.rich-edit-column').prepend(warning);
    }
    if(entry.data.source_context_changed)query('.rich-edit-column').prepend(richElement('p','rich-legacy-warning','商品规格、信息或关键词已有变化，请核对下方已保存内容，必要时让 AI 按新资料修改。'));
    dialog.addEventListener('cancel',event=>{event.preventDefault();richCloseEditor(session)});
    dialog.addEventListener('input',event=>{
        const input=event.target;if(input.matches('[data-rich-input]')){
            const block=session.blocks.find(row=>row.id===input.dataset.block);if(!block)return;
            block[input.dataset.richInput]=input.value;session.activeBlock=block.id;richDirty(session);richRenderPreview(session);
        }else if(input===session.nodes.prompt)session.prompt=input.value;
    });
    dialog.addEventListener('change',event=>{
        if(event.target===session.nodes.filter){session.mediaKind=event.target.value;richRenderMedia(session)}
        if(event.target===session.nodes.file){void richImportImages(session,event.target.files).catch(()=>{});event.target.value=''}
    });
    dialog.addEventListener('click',event=>{
        const button=event.target.closest('[data-rich-action]');if(!button||!dialog.contains(button))return;
        const action=button.dataset.richAction;
        const run=async()=>{
            if(action==='cancel'){richCloseEditor(session);return}
            if(action==='generate'){await richGenerate(session);return}
            if(action==='apply'){await richSave(session);return}
            if(action==='import'){session.nodes.file.click();return}
            richRequireCurrent(session);if(session.saving)throw Error('草稿正在保存');
            if(action==='add'){
                const block=richBlock(session.nodes.addType.value);session.blocks.push(block);session.activeBlock=block.id;richDirty(session);richRenderBlocks(session);
                if(block.type!=='text'){session.nodes.picker.open=true;richRenderMedia(session)}
            }else if(['up','down','delete'].includes(action)){
                const id=button.dataset.block;
                session.blocks=action==='delete'?session.blocks.filter(row=>row.id!==id):richMove(session.blocks,id,action==='up'?-1:1);
                session.activeBlock=session.blocks.find(row=>row.id===session.activeBlock)?.id||session.blocks[0]?.id||'';
                richDirty(session);richRenderBlocks(session);
            }else if(action==='choose-image'){
                session.activeBlock=button.dataset.block;session.nodes.picker.open=true;richRenderBlocks(session);richRenderMedia(session);session.nodes.picker.scrollIntoView({block:'nearest'});
            }else if(action==='select-media'){
                const block=session.blocks.find(row=>row.id===session.activeBlock),image=session.media.find(row=>String(row.id)===button.dataset.image);
                if(!block||block.type==='text')throw Error('请先添加图片板块，或点击要更换图片的板块');
                if(!image||!richImageUrl(session.entry,image))throw Error('该图片暂不可用，请重新读取');
                block.image_id=String(image.id);richDirty(session);richRenderBlocks(session);
            }else if(action==='use-candidate')richSelectCandidate(session);
        };
        void run().catch(error=>richEditorError(session,error.message));
    });
    document.body.append(dialog);richContentBench.dialog={session};
    richRenderBlocks(session);richRenderMedia(session);richRenderConversation(session);richUpdateStatus(session);
    dialog.showModal();session.nodes.addType.focus();return session;
}
document.addEventListener('click',event=>{
    const button=event.target.closest('[data-rich-action="open"]');if(!button)return;
    void richOpenEditor(button).catch(error=>{if(typeof notice==='function')notice(error.message,true);benchRichContentEnhance()});
});
const richBaseRenderer=renderProduct;
renderProduct=function(...args){const result=richBaseRenderer(...args);benchRichContentEnhance();return result};
