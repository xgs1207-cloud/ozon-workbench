/* Unified shop editor. Credentials live only in the submit fields and request. */
(function(global){
    'use strict';
    const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const rows=value=>Array.isArray(value)?value:[];
    const currencies={CNY:'CNY · 人民币',RUB:'RUB · 卢布',USD:'USD · 美元',EUR:'EUR · 欧元'};
    const credentialFields=['seller_client_id','seller_api_key','advertising_client_id','advertising_client_secret'];
    const fieldNames={mode:'保存方式',shop_id:'店铺标识',display_name:'店铺名称',default_currency_code:'售价币种',seller_client_id:'Seller Client-ID',seller_api_key:'Seller API Key',advertising_client_id:'广告 client_id',advertising_client_secret:'广告 client_secret',make_default:'默认店铺'};
    class FormError extends Error{constructor(message,fields=[]){super(message);this.fields=fields}}
    function initialShop(shops){return rows(shops).find(s=>s.enabled===true&&s.credentials_ready===true&&s.is_default===true)||rows(shops).find(s=>s.enabled===true&&s.credentials_ready===true)||rows(shops)[0]||null}
    function buildPayload({mode,shop,draft,credentials,existingIds=[]}){
        if(!['create','update'].includes(mode))throw new FormError('请重新选择需要编辑的店铺。');
        const id=String(mode==='update'?shop?.id??'':draft.shop_id??'').trim(),name=String(draft.display_name??'').trim();
        if(!/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(id))throw new FormError('店铺标识需为 1–64 位字母、数字、点、短横线或下划线，且以字母或数字开头。',['shop_id']);
        if(mode==='create'&&existingIds.includes(id))throw new FormError('此店铺标识已存在，请在左侧选择店铺进行编辑。',['shop_id']);
        if(!name||name.length>100)throw new FormError('请输入 1–100 个字符的店铺名称。',['display_name']);
        if(!Object.hasOwn(currencies,draft.default_currency_code))throw new FormError('请选择 CNY、RUB、USD 或 EUR。',['default_currency_code']);
        const body={mode,shop_id:id,display_name:name,default_currency_code:draft.default_currency_code};
        const sellerId=String(credentials?.seller_client_id??'').trim(),sellerKey=String(credentials?.seller_api_key??'').trim(),adId=String(credentials?.advertising_client_id??'').trim(),adSecret=String(credentials?.advertising_client_secret??'').trim();
        if(Boolean(sellerId)!==Boolean(sellerKey))throw new FormError('Seller Client-ID 与 API Key 必须一起填写；不更新时两项都留空。',['seller_client_id','seller_api_key']);
        if(mode==='create'&&(!sellerId||!sellerKey))throw new FormError('新增店铺需要填写 Seller Client-ID 与 API Key。',['seller_client_id','seller_api_key']);
        if(sellerId&&!/^\d+$/.test(sellerId))throw new FormError('Seller Client-ID 应为 Ozon 提供的数字编号。',['seller_client_id']);
        if(Boolean(adId)!==Boolean(adSecret))throw new FormError('广告 client_id 与 client_secret 必须一起填写；不更新时两项都留空。',['advertising_client_id','advertising_client_secret']);
        if(sellerId){body.seller_client_id=sellerId;body.seller_api_key=sellerKey}
        if(adId){body.advertising_client_id=adId;body.advertising_client_secret=adSecret}
        if(draft.make_default===true)body.make_default=true;
        return body;
    }
    function clearObjectSecrets(value){for(const key of credentialFields)if(Object.hasOwn(value,key))value[key]=''}
    function sellerStatus(shop){if(!shop?.credentials_ready)return '未配置 Seller';return ['connected','valid','verified','ok','ready'].includes(shop.connection_status)?'Seller 已连接':shop.connection_status==='error'||shop.connection_status==='failed'?'Seller 验证异常':'Seller 已保存，待验证'}
    function advertisingStatus(shop){const ad=shop?.advertising||{};return !ad.configured?'广告未授权':ad.connection_status==='connected'?'广告已授权':['failed','error','expired'].includes(ad.connection_status)?'广告授权异常':'广告已保存，待验证'}
    function resultMessage(result){
        if(result?.partial===true){
            const messages={cache_refresh_failed:'类目读取缓存尚未刷新，请刷新工作台后重新读取。',advertising_save_failed:'广告授权未更新，原有广告授权保持不变（若有）。请核对广告服务账号后重新填写广告凭据。',default_save_failed:'未能设置默认店铺，请先启用并验证 Seller 后单独设置。',advertising_status_unavailable:'广告授权状态暂时无法读取，请稍后刷新。'};
            const notes=rows(result.warning_codes).map(code=>messages[code]).filter(Boolean);
            return {text:'店铺资料已保存，未全部完成。'+(result.advertising_saved===true?'广告授权已保存。':'')+(notes.length?notes.join(' '):'广告授权未更新，原有广告授权保持不变（若有）。请刷新状态核对。'),warning:true};
        }
        return {text:result?.advertising_saved===true?'店铺信息、Seller 与广告授权已保存。广告仅完成授权，未开启投放。':'店铺信息已保存。未填写的密钥保持不变。',warning:false};
    }
    function errorMessage(http,data){
        if(http===422){const details=rows(data?.detail||data?.errors),labels=[...new Set(details.map(e=>fieldNames[rows(e?.loc).at(-1)]).filter(Boolean))];return labels.length?`请检查${labels.join('、')}。凭据需要成对填写，字段长度与格式须符合要求。`:'保存校验未通过。请检查店铺标识是否已存在、名称与币种，以及两组凭据是否成对填写。'}
        if(http===401)return 'Seller 授权验证未通过或已过期，请检查 Client-ID 与 API Key；原有凭据未被替换。';
        if(http===403)return '当前连接不安全或 Ozon API 权限不足；请使用 HTTPS 或本机 SSH 隧道，并检查密钥的只读权限。';
        if(http===409)return '店铺标识或账户信息发生冲突，请刷新记录核对；更换 Seller 账户需新增店铺，不能替换已有账户。';
        if(http===429)return '接口请求过于频繁，请稍后重试。';
        return `请求未完成（HTTP ${http}）。请检查工作台连接和 Ozon 授权；不会显示请求中的密钥。`;
    }
    const button=(label,action,extra='',disabled=false)=>`<button type="button" data-shop-action="${action}" ${extra}${disabled?' disabled':''}>${esc(label)}</button>`;
    let host=null,state=null,options={},epoch=0,readSeq=0,controllers=new Set();
    const live=()=>Boolean(host&&host.isConnected!==false&&state),current=mark=>live()&&mark===epoch;
    function selected(){return state?.shops.find(s=>s.id===state.selectedId)||null}
    function allowed(){return state?.security?.can_submit_credentials===true}
    function region(name,html){const target=host?.querySelector(`[data-shop-region="${name}"]`);if(target)target.innerHTML=html}
    function notice(text,error=false){if(!live())return;state.notice={text,error};const box=host.querySelector('[data-shop-notice]');box.textContent=text;box.hidden=!text;box.classList.toggle('is-error',error)}
    function clearFields(){if(!host)return;for(const key of credentialFields){const input=host.querySelector(`[name="${key}"]`);if(input)input.value=''}}
    function isBusy(){return state?.busy===true}
    function canLeave(){if(!isBusy())return true;notice('正在验证并保存店铺，完成后再切换页面。');return false}
    async function request(url,settings={}){
        const controller=new AbortController();controllers.add(controller);const timer=setTimeout(()=>controller.abort(),90000);
        try{const response=await global.fetch(url,{...settings,signal:controller.signal,credentials:'same-origin',headers:{Accept:'application/json',...(settings.body?{'Content-Type':'application/json'}:{}),...settings.headers}});let data;try{data=await response.json()}catch(_){throw new Error('工作台未返回可读取的数据，请检查服务连接。')}
            if(!response.ok)throw new Error(errorMessage(response.status,data));if(data?.ok===false)throw new Error('授权或保存未完成，请检查店铺凭据与权限。');return data;
        }catch(error){if(error.name==='AbortError')throw error;throw new Error(error.message||'工作台连接失败，请稍后重试。')}
        finally{clearTimeout(timer);controllers.delete(controller)}
    }
    function listHtml(){
        const shown=state.shops.filter(s=>[s.id,s.display_name,s.name].some(value=>String(value||'').toLowerCase().includes(state.query.toLowerCase())));
        return shown.length?shown.map(shop=>`<button type="button" class="sm-shop ${state.mode==='update'&&state.selectedId===shop.id?'is-selected':''}" data-shop-action="select" data-shop-id="${esc(shop.id)}" ${state.busy?'disabled':''}><span class="sm-shop-name">${esc(shop.display_name||shop.name||shop.id)}${shop.is_default?'<span class="sm-default">默认</span>':''}</span><span class="sm-shop-id">${esc(shop.id)} · ${shop.enabled?'已启用':'已停用'}</span><span class="sm-shop-status"><span class="${shop.credentials_ready?'is-ready':''}">${esc(sellerStatus(shop))}</span><span class="${shop.advertising?.configured?'is-ready':''}">${esc(advertisingStatus(shop))}</span></span></button>`).join(''):`<p class="sm-empty">${state.loading?'正在读取店铺…':state.shops.length?'没有匹配的店铺。':'没有已保存的店铺。点击“新增店铺”开始授权。'}</p>`;
    }
    function renderList(){region('list',listHtml());const add=host?.querySelector('[data-shop-action="new"]'),refresh=host?.querySelector('[data-shop-action="refresh"]');if(add)add.disabled=state.busy||state.loading;if(refresh)refresh.disabled=state.busy||state.loading;const count=host?.querySelector('[data-shop-count]');if(count)count.textContent=String(state.shops.length)}
    function editorStatus(){
        const shop=selected();if(state.mode!=='update'||!shop){region('editor-status','');return}
        const roles=rows(shop.roles).map(role=>typeof role==='string'?role:role?.name||role?.role||'').filter(Boolean);
        region('editor-status',`<div class="sm-status-line"><span>${esc(sellerStatus(shop))}</span><span>${esc(advertisingStatus(shop))}</span><span>${shop.enabled?'已启用':'已停用'}</span>${shop.is_default?'<span class="sm-default">默认店铺</span>':''}</div><p class="sm-meta">最近 Seller 检查：${esc(shop.checked_at||'尚未检查')}<br>Seller 密钥到期：${esc(shop.expires_at||'接口未返回')}${roles.length?`<br>角色：${esc(roles.join(' / '))}`:''}</p><div class="sm-shop-actions">${button('测试 Seller 连接','test','',state.busy||!allowed()||!shop.credentials_ready)}${button(shop.enabled?'停用此店铺':'启用此店铺','enabled','',state.busy||!allowed())}${button('设为默认店铺','default','',state.busy||!allowed()||shop.is_default||!shop.enabled||!shop.credentials_ready)}</div>`);
    }
    function renderSecurity(){const reason=allowed()?'密钥由服务器加密保存，不回显、不保存到浏览器。':state.security?.reason||'正在检查安全连接…';region('security',`<p class="sm-security ${allowed()?'':'is-warning'}">${esc(reason)}</p>`);const fieldset=host?.querySelector('[data-shop-fields]');if(fieldset)fieldset.disabled=state.busy||state.loading||!allowed()}
    function renderEditor(){
        const d=state.draft,create=state.mode==='create',blocked=state.busy||state.loading||!allowed();
        region('editor',`<div class="sm-editor-heading"><h2>${create?'新增店铺':'编辑店铺'}</h2><p>${create?'一次填写 Seller 与可选广告授权。':'名称与币种可直接修改；不更新密钥时留空即可。'}</p></div><div data-shop-region="editor-status"></div><div data-shop-region="security"></div><form data-shop-form autocomplete="off"><fieldset data-shop-fields ${blocked?'disabled':''}><section class="sm-form-section"><h3>店铺信息</h3><div class="sm-field-grid"><label>店铺名称<input name="display_name" value="${esc(d.display_name)}" maxlength="100" required autocomplete="off" placeholder="例如 俄罗斯一店"></label><label>店铺标识<input name="shop_id" value="${esc(d.shop_id)}" maxlength="64" ${create?'required':'readonly'} autocomplete="off" spellcheck="false" placeholder="例如 ozon_store_1"><small>${create?'字母、数字、点、短横线、下划线；保存后固定。':'此标识固定，不随店铺名称或密钥变化。'}</small></label><label>默认售价币种<select name="default_currency_code">${Object.entries(currencies).map(([key,label])=>`<option value="${key}" ${d.default_currency_code===key?'selected':''}>${label}</option>`).join('')}</select><small>用于后续新草稿，不修改已上架商品价格。</small></label><label class="sm-check"><input type="checkbox" name="make_default" ${d.make_default||selected()?.is_default?'checked':''} ${selected()?.is_default?'disabled':''}>${selected()?.is_default?'当前已是默认店铺':'保存后设为默认店铺'}</label></div></section><section class="sm-form-section"><div class="sm-group-title"><h3>Seller API</h3><span>${create?'新增必填':'更新可选 · 空白保留'}</span></div><p class="sm-help">在 <a href="https://seller.ozon.ru/app/settings/api-keys" target="_blank" rel="noopener noreferrer">Ozon 后台 API 密钥</a> 获取数字 Client-ID 与 API Key。</p><div class="sm-field-grid"><label>Seller Client-ID<input name="seller_client_id" inputmode="numeric" autocomplete="off" spellcheck="false" maxlength="500" ${create?'required':''} placeholder="${create?'Ozon 提供的数字编号':'不更新则留空'}"></label><label>Seller API Key<input name="seller_api_key" type="password" autocomplete="new-password" maxlength="2000" ${create?'required':''} placeholder="${create?'输入 Seller 密钥':'不更新则留空'}"></label></div></section><section class="sm-form-section"><div class="sm-group-title"><h3>广告 Performance API</h3><span>可选 · 独立服务账号</span></div><p class="sm-help">同一后台的 Performance API 页签创建服务账号。这里仅验证并保存授权，不创建广告或开启投放。</p><div class="sm-field-grid"><label>广告 client_id<input name="advertising_client_id" autocomplete="off" spellcheck="false" maxlength="500" placeholder="服务账号 client_id；不更新则留空"></label><label>广告 client_secret<input name="advertising_client_secret" type="password" autocomplete="new-password" maxlength="2000" placeholder="服务账号密钥；不更新则留空"></label></div></section><div class="sm-form-footer"><button type="submit" class="sm-primary">${state.busy?'验证并保存中…':create?'验证并新增店铺':'保存店铺信息'}</button><span>${state.busy?'请等待完成；保存过程中不能切换店铺或页面。':'只验证接口权限，不提交商品，不开启广告。'}</span></div></fieldset></form>`);
        editorStatus();renderSecurity();
    }
    function draftFor(shop){return {shop_id:shop?.id||'',display_name:shop?.display_name||shop?.name||'',default_currency_code:Object.hasOwn(currencies,shop?.default_currency_code)?shop.default_currency_code:'CNY',make_default:false}}
    function selectShop(id){if(state.busy||state.loading)return;const shop=state.shops.find(s=>s.id===id);if(!shop)return;clearFields();state.scope++;state.mode='update';state.selectedId=id;state.draft=draftFor(shop);state.dirty=false;notice('');renderList();renderEditor()}
    function newShop(){if(state.busy||state.loading)return;clearFields();state.scope++;state.mode='create';state.selectedId='';state.draft=draftFor(null);state.dirty=false;notice('');renderList();renderEditor();host.querySelector('[name="display_name"]')?.focus()}
    async function refresh({selectId,resetEditor=false}={}){
        if(!live())return;const mark=epoch,seq=++readSeq,scope=state.scope;state.loading=true;renderList();renderSecurity();
        try{const data=await request('/api/workbench/shop-management');if(!current(mark)||readSeq!==seq)return;state.shops=rows(data.shops);state.security=data.authorization_context||{can_submit_credentials:false,reason:'服务尚未启用安全授权入口。'};
            if(!state.initialized){state.initialized=true;const shop=initialShop(state.shops);state.selectedId=shop?.id||'';state.mode=shop?'update':'create';state.draft=draftFor(shop);renderEditor()}
            else if(resetEditor&&state.scope===scope){const shop=state.shops.find(s=>s.id===selectId)||selected();if(shop){state.mode='update';state.selectedId=shop.id;state.draft=draftFor(shop);state.dirty=false;renderEditor()}}
            renderList();editorStatus();renderSecurity();
        }catch(error){if(current(mark)&&readSeq===seq&&error.name!=='AbortError')notice(error.message,true)}finally{if(current(mark)&&readSeq===seq){state.loading=false;renderList();renderSecurity()}}
    }
    async function notifySaved(result,mark){if(typeof options.onSaved!=='function')return;try{await options.onSaved(result)}catch(_){if(current(mark))notice((state.notice?.text?state.notice.text+'\n':'')+'店铺已保存，但外部店铺列表刷新未完成，请点击刷新状态。',true)}}
    function lock(value){state.busy=value;renderList();editorStatus();renderSecurity();const submit=host.querySelector('button[type="submit"]');if(submit)submit.textContent=value?'验证并保存中…':state.mode==='create'?'验证并新增店铺':'保存店铺信息'}
    async function onSubmit(event){
        if(!event.target.matches?.('[data-shop-form]')&&!event.target.hasAttribute?.('data-shop-form'))return;event.preventDefault();if(!live()||state.busy||state.loading)return;if(!allowed()){notice('当前连接不能安全提交，请使用 HTTPS 或本机 SSH 隧道。',true);return}
        const form=event.target,credentials={},mark=epoch,scope=state.scope;
        let payload;for(const key of credentialFields)credentials[key]=form.querySelector(`[name="${key}"]`)?.value||'';
        try{payload=buildPayload({mode:state.mode,shop:selected(),draft:state.draft,credentials,existingIds:state.shops.map(s=>s.id)})}catch(error){clearObjectSecrets(credentials);notice(error.message,true);form.querySelector(`[name="${error.fields?.[0]||'display_name'}"]`)?.focus();return}
        const body=JSON.stringify(payload),id=payload.shop_id;clearObjectSecrets(credentials);clearObjectSecrets(payload);clearFields();lock(true);notice('');
        try{const result=await request('/api/workbench/shop-management',{method:'POST',body});if(!current(mark)||state.scope!==scope)return;if(result.shop?.id===id){const existing=state.shops.findIndex(shop=>shop.id===id);if(existing>=0)state.shops[existing]=result.shop;else state.shops.push(result.shop);state.selectedId=id;state.mode='update';state.draft=draftFor(result.shop);state.dirty=false;renderEditor();renderList()}await refresh({selectId:id,resetEditor:true});if(!current(mark)||state.scope!==scope)return;const message=resultMessage(result);notice(message.text,message.warning);await notifySaved(result,mark)}
        catch(error){if(current(mark)&&state.scope===scope){notice(error.name==='AbortError'?'保存等待超时，服务器可能仍在处理。请刷新状态核对，不要立即重复提交。':error.message,true)}}
        finally{clearObjectSecrets(credentials);clearObjectSecrets(payload);if(current(mark)&&state.scope===scope){clearFields();lock(false)}}
    }
    function onInput(event){if(!live()||state.busy)return;const input=event.target;if(input.dataset.shopFilter!==undefined){state.query=input.value;renderList();return}if(['shop_id','display_name','default_currency_code','make_default'].includes(input.name)){if(input.name==='shop_id'&&state.mode!=='create')return;state.draft[input.name]=input.type==='checkbox'?input.checked:input.value;state.dirty=true}}
    async function onClick(event){
        const target=event.target.closest('[data-shop-action]');if(!target||target.disabled||!live())return;const action=target.dataset.shopAction;if(state.busy){notice('正在保存，请等待完成后再操作。');return}
        if(action==='select'){selectShop(target.dataset.shopId);return}if(action==='new'){newShop();return}if(action==='refresh'){await refresh();return}
        const shop=selected();if(!shop||!allowed())return;const mark=epoch,scope=state.scope;lock(true);notice('');
        try{let result;if(action==='test')result=await request(`/api/workbench/stores/${encodeURIComponent(shop.id)}/test`,{method:'POST',body:'{}'});else if(action==='enabled'||action==='default')result=await request(`/api/workbench/stores/${encodeURIComponent(shop.id)}/settings`,{method:'PUT',body:JSON.stringify(action==='default'?{make_default:true}:{enabled:!shop.enabled})});else return;
            if(!current(mark)||state.scope!==scope)return;await refresh();if(!current(mark)||state.scope!==scope)return;notice(action==='test'?'Seller 只读连接已验证。':action==='default'?'已设置为默认店铺。':shop.enabled?'店铺已停用。':'店铺已启用。');await notifySaved(result,mark)
        }catch(error){if(current(mark)&&state.scope===scope)notice(error.name==='AbortError'?'操作等待超时，请刷新状态核对结果。':error.message,true)}finally{if(current(mark)&&state.scope===scope)lock(false)}
    }
    function guardNavigation(event){if(!isBusy())return;const navigation=event.target.closest?.('[data-view]');if(navigation){event.preventDefault();event.stopImmediatePropagation();canLeave()}}
    function beforeUnload(event){if(isBusy()){event.preventDefault();event.returnValue=''}}
    function unmount(){epoch++;readSeq++;if(host){clearFields();host.removeEventListener('click',onClick);host.removeEventListener('input',onInput);host.removeEventListener('change',onInput);host.removeEventListener('submit',onSubmit);host.classList.remove('shop-manager')}global.document?.removeEventListener('click',guardNavigation,true);global.removeEventListener?.('beforeunload',beforeUnload);global.document?.body?.classList.remove('shop-manager-mode');for(const controller of controllers)controller.abort();controllers.clear();if(state){state.draft=draftFor(null);state.notice=null}state=null;host=null;options={}}
    async function mount(container,settings={}){
        unmount();host=typeof container==='string'?global.document.querySelector(container):container;if(!host)return;options=settings;state={shops:[],security:null,selectedId:'',mode:'create',draft:draftFor(null),query:'',dirty:false,busy:false,loading:true,initialized:false,scope:0,notice:null};host.classList.add('shop-manager');global.document?.body?.classList.add('shop-manager-mode');
        host.innerHTML='<header class="sm-heading"><h1>店铺管理</h1><p>在同一个页面管理店铺资料、Seller 授权与独立广告授权。</p></header><p class="sm-shared-note">店铺由本工作台成员共用；当前入口不是员工权限隔离。修改授权会影响使用该店铺的其他成员。</p><p data-shop-notice class="sm-notice" role="status" aria-live="polite" hidden></p><div class="sm-layout"><aside class="sm-list-pane"><div class="sm-list-heading"><h2>店铺 <span data-shop-count>0</span></h2><button type="button" data-shop-action="new">新增店铺</button></div><label class="sm-search">搜索店铺<input type="search" data-shop-filter placeholder="名称或店铺标识" maxlength="100"></label><div data-shop-region="list" class="sm-list"></div><button type="button" data-shop-action="refresh" class="sm-refresh">刷新状态</button></aside><section class="sm-editor" data-shop-region="editor"></section></div>';
        host.addEventListener('click',onClick);host.addEventListener('input',onInput);host.addEventListener('change',onInput);host.addEventListener('submit',onSubmit);global.document?.addEventListener('click',guardNavigation,true);global.addEventListener?.('beforeunload',beforeUnload);renderList();renderEditor();await refresh();
    }
    global.ShopManager=Object.freeze({mount,unmount,refresh,isBusy,canLeave});
    if(typeof module!=='undefined'&&module.exports)module.exports={esc,initialShop,buildPayload,clearObjectSecrets,sellerStatus,advertisingStatus,resultMessage,errorMessage,FormError,mount,unmount,refresh,isBusy,canLeave};
})(typeof window!=='undefined'?window:globalThis);
