/* Read-only post-listing ledger. Seller and advertising identities stay separate. */
(function (global) {
    'use strict';
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const array = value => Array.isArray(value) ? value : [];
    function initialShopId(value) {
        const shops=array(value);
        const chosen=shops.find(shop=>shop?.enabled===true && shop.credentials_ready===true && shop.is_default===true)
            || shops.find(shop=>shop?.enabled===true && shop.credentials_ready===true)
            || shops.find(shop=>shop?.enabled===true)
            || shops[0];
        return chosen?.id || '';
    }
    const number = value => (typeof value==='number' || (typeof value==='string' && value.trim()!=='')) && Number.isFinite(Number(value)) ? Number(value) : null;
    const metric = (value, digits = 0) => {const n = number(value); return n === null ? '暂无数据' : new Intl.NumberFormat('zh-CN', {maximumFractionDigits:digits}).format(n)};
    const finiteRatio = (top, bottom) => {const a=number(top), b=number(bottom);return a !== null && b !== null && b > 0 ? a / b : null};
    const safeSource = value => {try {const u=new URL(String(value));return u.protocol==='https:' && u.hostname==='detail.1688.com' && !u.username && !u.password && /^\/offer\/\d+\.html$/.test(u.pathname) ? u.origin+u.pathname : ''} catch (_) {return ''}};
    const date = value => {if (!value) return '未同步'; const parsed=new Date(value);return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleString('zh-CN', {hour12:false})};
    const statusLabels = {queued:'排队中',running:'处理中',retrying:'等待重试',retry_wait:'等待重试',partial:'部分完成',failed:'同步失败',completed:'已完成',succeeded:'已完成',ready:'可用',pending:'处理中',submitting:'正在提交',submission_uncertain:'提交结果不确定',unavailable:'暂无权限',not_configured:'未授权',enabled:'已启用',disabled:'未启用',imported:'已导入',processed:'已处理',ok:'正常',success:'成功',cancelled:'已取消',expired:'已过期',error:'失败',no_data:'暂无数据',permission_required:'需要数据权限',available:'已取得数据',deferred:'等待接口配额'};
    const normalizedStatus = value => ({NOT_STARTED:'pending',IN_PROGRESS:'running',OK:'ready',ERROR:'error',SUBMISSION_UNCERTAIN:'submission_uncertain',SUBMITTING:'submitting'}[String(value)] || String(value || '').toLowerCase());
    const status = value => statusLabels[normalizedStatus(value)] || String(value || '未知');
    const button = (label, action, extra='', disabled=false) => `<button type="button" data-ops-action="${action}" ${extra}${disabled?' disabled':''}>${escape(label)}</button>`;
    const badge = value => `<span class="ops-state" data-status="${escape(normalizedStatus(value) || 'unknown')}">${escape(status(value))}</span>`;
    const empty = text => `<p class="ops-empty">${escape(text)}</p>`;
    const pick = (row, keys) => {for (const key of keys) {const value=row?.metrics?.[key] ?? row?.[key];if (value!==undefined && value!==null && value!=='') return value}return null};
    function latestSnapshot(items, kind) {return array(items).filter(row=>row.kind===kind).sort((a,b)=>String(b.fetched_at || b.updated_at || '').localeCompare(String(a.fetched_at || a.updated_at || '')))[0] || null}
    function metricObservations(items) {const latest=latestSnapshot(items,'metrics');return latest?array(latest.data?.items).map(row=>({...row,currency:row.currency || latest.data?.currency || latest.currency || null,source:latest.endpoint || 'Seller API',date_from:latest.date_from,date_to:latest.date_to,status:latest.status})):array(items).filter(row=>row.kind!=='queries' && row.kind!=='health')}
    function queryObservations(value) {return Array.isArray(value)?value:array(value?.items).map(row=>({...row,date_from:row.date_from || value.date_from,date_to:row.date_to || value.date_to,status:row.status || value.status}))}
    const daysLabel = row => [row?.date_from || row?.from, row?.date_to || row?.to].filter(Boolean).join(' 至 ') || row?.period || '周期未提供';
    const terminal = value => ['ready','completed','succeeded','partial','failed','error','cancelled','expired','submission_uncertain'].includes(normalizedStatus(value));
    const canCreateReport = report => !report || (terminal(report.status) && normalizedStatus(report.status)!=='submission_uncertain');
    const historyBlocksReport = rows => array(rows).some(row=>!canCreateReport({status:row.status || row.state}));
    const pendingJob = row => ['queued','running','retrying','retry_wait'].includes(normalizedStatus(row.status));
    const jobError = row => row.last_error || row.error || ({permission_required:'需要开通对应数据权限',quota_deferred:'等待接口调用配额恢复',seller_rate_limit:'官方接口限流，将按队列重试',credentials_missing:'店铺授权缺失，请重新授权',no_credentials:'店铺授权缺失，请重新授权',shop_disabled:'店铺已停用，未访问官方接口',sku_not_found:'还没有回读到 Ozon SKU',network_error:'网络请求失败，可重新同步',network_unavailable:'网络暂不可用，将按队列重试'}[row.error_code] || (row.error_code?'接口说明：'+row.error_code:''));
    let host=null, state=null, generation=0, detailGeneration=0, productsGeneration=0, reportHistoryGeneration=0, controllers=new Set(), pendingChannels=new Map(), jobsTimer=null, reportTimer=null;
    const live = () => Boolean(host && host.isConnected !== false && state);
    const current = revision => live() && generation===revision;
    function stopTimers() {if(jobsTimer)clearTimeout(jobsTimer);if(reportTimer)clearTimeout(reportTimer);jobsTimer=reportTimer=null}
    function cancelRequests() {for(const c of controllers)c.abort();controllers.clear();pendingChannels.clear()}
    function unmount() {generation++;detailGeneration++;stopTimers();cancelRequests();if(host){host.removeEventListener('click',onClick);host.removeEventListener('change',onChange);host.removeEventListener('submit',onSubmit);host.classList.remove('operations-center')}host=null;state=null}
    function requestError(response, data) {if(response.status===401 || response.status===403)return new Error(data?.detail?.message || (typeof data?.detail==='string'?data.detail:'当前店铺尚未开通此数据权限，请检查授权和分析套餐。'));return new Error(typeof data?.detail==='string'?data.detail: data?.error?.message || data?.message || `请求失败（${response.status}），请稍后重试。`)}
    async function request(path, options={}, channel='') {
        if(channel && pendingChannels.has(channel))return pendingChannels.get(channel);
        const controller=new AbortController();controllers.add(controller);
        const timer=setTimeout(()=>controller.abort(),25000);
        const running=(async()=>{
            const response=await global.fetch(path, {...options,signal:controller.signal,credentials:'same-origin',headers:{Accept:'application/json',...(options.body?{'Content-Type':'application/json'}:{}),...options.headers}});
            let data;try{data=await response.json()}catch(_){throw new Error('服务未返回可读取的数据，请检查工作台连接。')}
            if(!response.ok)throw requestError(response,data);return data;
        })();
        if(channel)pendingChannels.set(channel,running);
        try{return await running}finally{clearTimeout(timer);controllers.delete(controller);if(channel && pendingChannels.get(channel)===running)pendingChannels.delete(channel)}
    }
    const json = value => ({method:'POST',body:JSON.stringify(value)});
    const qs = values => new URLSearchParams(Object.entries(values).filter(([,v])=>v!==null && v!==undefined && v!=='')).toString();
    function announce(text, error=false) {if(!live())return;const box=host.querySelector('[data-ops-notice]');box.textContent=text;box.classList.toggle('is-error',error);box.hidden=!text}
    function region(name, html) {const target=host?.querySelector(`[data-ops-region="${name}"]`);if(target)target.innerHTML=html}
    function controls() {
        const options=state.shops.map(shop=>`<option value="${escape(shop.id)}" ${state.shop===shop.id?'selected':''}>${escape(shop.name || shop.display_name || shop.id)}</option>`).join('');
        region('toolbar', `<label>店铺<select data-ops-input="shop">${options || '<option value="">先添加店铺授权</option>'}</select></label><label class="ops-search">搜索货号 / 规格<input type="search" data-ops-input="query" value="${escape(state.query)}" placeholder="货号、商品编号或货源备注" maxlength="100"></label><label>观察周期<select data-ops-input="days"><option value="7" ${state.days===7?'selected':''}>最近 7 天</option><option value="30" ${state.days===30?'selected':''}>最近 30 天</option></select></label>${button('搜索 / 刷新记录','search','',!state.shop)}${button('读取新上架记录','discover','',!state.shop)}${button('任务与定时监测','settings','',!state.shop)}${button('广告授权与报表','advertising','',!state.shop)}<label class="ops-check ops-traffic-choice"><input type="checkbox" data-ops-input="include-traffic" ${state.includeTraffic?'checked':''}>同步时读取曝光 / 展示指标（需对应权限）</label>`);
    }
    function productRows(items) {
        return array(items).map(row=>{
            const source=safeSource(row.source_url), offer=String(row.offer_id || ''), identifier=row.ozon_sku || row.sku || '待回读';
            return `<tr><td><button class="ops-offer" type="button" data-ops-action="detail" data-offer="${escape(offer)}">${escape(offer || '货号未提供')}</button><small>${escape(row.product_id || '本地商品未绑定')}</small></td><td>${escape(row.source_note || row.sku_name || '规格未记录')}<small>${source?`<a href="${escape(source)}" target="_blank" rel="noopener noreferrer">查看 1688 货源</a>`:'无有效货源链接'}</small></td><td>${badge(row.import_status || row.status)}<small>${escape(row.stock_status?status(row.stock_status):'库存尚未核对')}</small></td><td>${escape(row.warehouse_name || '未记录仓库')}<small>${escape(row.warehouse_id || '')}</small></td><td>${escape(identifier)}<small>${escape(row.ozon_product_id?`商品 ID ${row.ozon_product_id}`:'商品 ID 待回读')}</small></td><td>${escape(date(row.last_synced_at || row.updated_at))}${button('同步此商品','sync',` data-offer="${escape(offer)}"`,!offer)}</td></tr>`;
        }).join('');
    }
    function products() {
        const s=state;
        region('products', `<div class="ops-section-title"><h2>按货号监测</h2><span>${s.total} 条记录 · 仅当前店铺</span></div>${s.loading?empty('正在读取已保存记录…'):s.items.length?`<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>货号 / 本地商品</th><th>规格 / 货源</th><th>上架 / 库存状态</th><th>仓库</th><th>Ozon SKU</th><th>最近记录 / 操作</th></tr></thead><tbody>${productRows(s.items)}</tbody></table></div>`:empty('还没有匹配的上架记录。确认店铺后点击“读取新上架记录”；此操作只整理工作台已有记录，不会创建商品或开启广告。')}<div class="ops-pagination"><span>${s.total?s.offset+1:0}–${Math.min(s.offset+s.items.length,s.total)} / ${s.total}</span>${button('上一页','previous','',s.loading || !s.offset)}${button('下一页','next','',s.loading || s.offset+s.limit>=s.total)}</div>`);
    }
    function trendSvg(rows, keys, label) {
        const source=array(rows).map(row=>({date:row.date || row.day || row.date_to || row.collected_at,value:number(pick(row,keys))})).filter(row=>row.value!==null).sort((a,b)=>String(a.date).localeCompare(String(b.date)));
        if(source.length<2)return empty(`${label}：至少需要两个有效数据点才能显示趋势。`);
        const width=640,height=124,left=38,bottom=99,top=12,max=Math.max(...source.map(x=>x.value)),min=Math.min(0,...source.map(x=>x.value)),range=max-min || 1;
        const points=source.map((row,i)=>`${(left+i*(width-left-14)/(source.length-1)).toFixed(2)},${(bottom-(row.value-min)*(bottom-top)/range).toFixed(2)}`).join(' ');
        return `<div class="ops-trend"><strong>${escape(label)}</strong><svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escape(label)}趋势，${escape(source[0].date)} 至 ${escape(source.at(-1).date)}"><path d="M38 12 V99 H626" fill="none" stroke="#edf0f4"/><polyline points="${points}" fill="none" stroke="#1557e8" stroke-width="2.5"/><text x="2" y="18">${escape(metric(max))}</text><text x="38" y="119">${escape(String(source[0].date || '').slice(0,10))}</text><text x="540" y="119">${escape(String(source.at(-1).date || '').slice(0,10))}</text></svg><p>只使用有值的观测点；缺失值不会替换成 0，也不合并不同统计来源。</p></div>`;
    }
    function snapshotRows(items) {
        return array(items).map(row=>`<tr><td>${escape(row.date || row.day || daysLabel(row))}</td><td>${escape(metric(pick(row,['hits_view_search'])))}</td><td>${escape(metric(pick(row,['hits_view_pdp'])))}</td><td>${escape(metric(pick(row,['ordered_units'])))}</td><td>${escape(metric(pick(row,['revenue']),2))}<small>${escape(row.currency || '币种未提供')}</small></td><td>${escape(row.source || row.kind || '来源未提供')}<small>${escape(status(row.status || row.capability))}</small></td></tr>`).join('');
    }
    function queryRows(items) {
        return array(items).map(row=>`<tr><td><strong lang="ru">${escape(row.query || row.phrase || row.search_query || '关键词未提供')}</strong><small>${escape(daysLabel(row))}</small></td><td>${escape(metric(pick(row,['unique_search_users'])))}</td><td>${escape(metric(pick(row,['unique_view_users'])))}</td><td>${escape(metric(pick(row,['order_count'])))}</td><td>${escape(metric(pick(row,['gmv']),2))}<small>${escape(row.currency || '币种未提供')}</small></td><td>${escape(metric(pick(row,['position','average_position']),1))}</td><td>${escape(row.source || 'Seller API')}<small>${escape(row.note || status(row.status))}</small></td></tr>`).join('');
    }
    function diagnostics(items) {
        if(!array(items).length)return empty('暂时没有足够证据生成诊断。同步后先核对可售状态、库存与数据覆盖周期。');
        const titles={health_not_verified:'先核实商品可售状态',listing_not_ready:'上架状态需处理',no_stock:'平台未返回可用库存',no_price:'平台价格状态异常',availability_unknown:'库存或价格仍待核实',metrics_not_available:'流量与销量数据暂不可用',queries_not_available:'关键词明细暂不可用',query_sales_observed:'搜索词已产生订单',query_zero_views:'搜索词展示用户数为零',query_observation:'搜索词观察记录'};
        return array(items).map(row=>`<article class="ops-diagnosis" data-severity="${escape(row.severity || 'info')}"><h4>${escape(row.title || titles[row.code] || '监测说明')}</h4><p>${escape(row.message || row.description || '')}</p>${row.evidence?`<p class="ops-muted">依据：${escape(typeof row.evidence==='string'?row.evidence:JSON.stringify(row.evidence))}</p>`:''}</article>`).join('');
    }
    function healthHtml(snapshots) {
        const snapshot=latestSnapshot(snapshots,'health'),health=snapshot?.data || {};
        const booleanLabel=value=>value===true?'平台返回有库存':value===false?'平台返回无库存':'待核实';
        const reviewLabels={approved:'已通过',approve:'已通过',declined:'已拒绝',rejected:'已拒绝',pending:'待审核',awaiting_moderation:'待审核',moderating:'审核中',none:'未提供'};
        const review=health.moderate_status?reviewLabels[String(health.moderate_status).toLowerCase()] || health.moderate_status:'待核实';
        const price=number(health.price),currency=health.currency || '币种未提供';
        return `<section class="ops-health"><h3>平台当前状态</h3><dl class="ops-facts"><dt>审核状态</dt><dd>${escape(review)}</dd><dt>平台是否有库存</dt><dd>${escape(booleanLabel(health.has_stock))}</dd><dt>平台价格</dt><dd>${price===null?'待核实':`${escape(metric(price,2))} ${escape(currency)}`}${health.has_price===false?'（平台返回无有效价格）':''}</dd><dt>状态回读时间</dt><dd>${escape(date(snapshot?.fetched_at))}</dd></dl><p class="ops-muted">库存标记是平台返回的总体可用状态，不代表所选仓库的库存数量。${snapshot && snapshot.status!=='available'?`本次状态：${escape(status(snapshot.status))}。`:''}</p></section>`;
    }
    function detail() {
        const d=state.detail;
        if(!d){region('detail','');return}
        if(d.loading){region('detail',`<section class="ops-detail">${empty('正在读取商品监测记录…')}</section>`);return}
        const row=d.product || {}, source=safeSource(row.source_url), snapshots=metricObservations(d.snapshots), queries=queryObservations(d.queries), snapshotsMeta=array(d.snapshots);
        const traffic=latestSnapshot(snapshotsMeta,'metrics');
        region('detail', `<section class="ops-detail"><div class="ops-section-title"><div><h2>${escape(state.offer)}</h2><p>${escape(row.source_note || '规格未记录')}</p></div><div>${button('同步最近 '+state.days+' 天','sync',` data-offer="${escape(state.offer)}"`)}${button('关闭详情','close-detail')}</div></div><dl class="ops-facts"><dt>本地商品</dt><dd>${escape(row.product_id || '未绑定')}</dd><dt>Ozon SKU</dt><dd>${escape(row.ozon_sku || row.sku || '待回读')}</dd><dt>货源</dt><dd>${source?`<a href="${escape(source)}" target="_blank" rel="noopener noreferrer">${escape(source)}</a>`:'未记录'}</dd><dt>最近同步</dt><dd>${escape(date(row.last_synced_at || row.updated_at))}</dd></dl>${healthHtml(snapshotsMeta)}<h3>诊断与缺项</h3>${diagnostics(d.diagnostics)}<h3>数据观测</h3><p class="ops-muted">搜索曝光、商品卡展示与搜索用户分别呈现；广告归因与自然流量不可直接相减。仅显示最近一次同来源观测，不累加重叠日期的报告。</p>${traffic?`<p class="ops-muted">周期：${escape(daysLabel(traffic))} · 数据状态：${escape(status(traffic.status))} · 抓取时间：${escape(date(traffic.fetched_at))}</p>`:''}${trendSvg(snapshots,['ordered_units'],'下单商品件数')}${snapshots.length?`<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>日期 / 周期</th><th>搜索曝光次数</th><th>商品卡展示次数</th><th>下单商品件数</th><th>销售金额</th><th>来源 / 权限</th></tr></thead><tbody>${snapshotRows(snapshots)}</tbody></table></div>`:empty('尚无观测数据。点击“同步”读取官方接口；权限不足、数据延迟和真实 0 会分别保存。')}<h3>商品搜索词</h3><p class="ops-muted">这是监测词表，与员工手工关键词库和 Seerfar 词库独立。数据可能有 1–2 天延迟，每个 SKU 最多读取 15 个查询词；不会据此认为其余查询词不存在。</p>${queries.length?`<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>关键词 / 周期</th><th>搜索用户数</th><th>看到商品的用户数</th><th>订单数</th><th>归因销售金额</th><th>平均位置</th><th>来源 / 说明</th></tr></thead><tbody>${queryRows(queries)}</tbody></table></div>`:empty('未取得关键词明细。暂无数据不能判定关键词无效，请查看上方诊断与接口权限。')}<h3>此商品同步任务</h3>${jobsHtml(d.jobs)}</section>`);
    }
    function jobsHtml(items) {
        if(!array(items).length)return empty('没有同步任务。');
        return `<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>任务</th><th>货号</th><th>状态</th><th>尝试次数</th><th>最近更新 / 说明</th></tr></thead><tbody>${array(items).map(row=>`<tr><td>${escape(row.kind || row.type || '商品监测')}<small>${escape(row.id || row.job_id || '')}</small></td><td>${escape(row.offer_id || '店铺任务')}</td><td>${badge(row.status)}</td><td>${escape(metric(row.attempts ?? row.attempt))}</td><td>${escape(date(row.updated_at))}<small>${escape(jobError(row))}</small>${normalizedStatus(row.status)==='failed' && row.offer_id?button('重新同步','sync',` data-offer="${escape(row.offer_id)}"`):''}</td></tr>`).join('')}</tbody></table></div>`;
    }
    function settings() {
        if(!state.settingsOpen){region('settings','');return}
        const schedule=state.scheduleDraft || state.schedule || {};
        region('settings', `<section class="ops-settings"><div class="ops-section-title"><h2>任务与定时监测</h2>${button('收起','settings')}</div><form data-ops-form="schedule"><label class="ops-check"><input type="checkbox" data-ops-input="schedule-enabled" ${schedule.enabled?'checked':''}>启用当前店铺定时监测</label><label>监测周期<select data-ops-input="schedule-days"><option value="7" ${Number(schedule.days || 7)===7?'selected':''}>最近 7 天</option><option value="30" ${Number(schedule.days)===30?'selected':''}>最近 30 天</option></select></label><span>每 24 小时只读同步。仅已登记的上架商品，不修改商品、不创建广告。</span><button type="submit" ${state.scheduleSaving?'disabled':''}>${state.scheduleSaving?'保存中…':'保存监测设置'}</button></form>${schedule.next_run_at?`<p class="ops-muted">预计下次：${escape(date(schedule.next_run_at))}</p>`:''}${jobsHtml(state.jobs)}</section>`);
    }
    function adStatusHtml() {
        const ad=state.ad || {}, secure=state.security;
        return `<div class="ops-auth-status">${badge(ad.ready?'ready':ad.status || 'not_configured')}<span>${escape(ad.masked_client_id || ad.client_id_masked || (ad.ready?'独立广告服务账号已加密授权':'独立广告服务账号，尚未配置'))}</span></div><p class="ops-muted">在 Ozon 卖家后台「设置 → API 密钥 → Performance API」创建服务账号，填写该账号的 client_id 和 client_secret；不是现有 Seller API 密钥。密钥加密保存，不显示明文。</p>${secure?.can_submit_credentials===false?`<p class="ops-error">${escape(secure.reason || '当前连接不能安全提交密钥，请使用 HTTPS 或本机 SSH 隧道。')}</p>`:''}<form data-ops-form="authorization" autocomplete="off"><label>广告 client_id<input name="client_id" autocomplete="off" spellcheck="false" required maxlength="500" ${secure?.can_submit_credentials===false?'disabled':''}></label><label>广告 client_secret<input type="password" name="client_secret" autocomplete="new-password" required maxlength="2000" ${secure?.can_submit_credentials===false?'disabled':''}></label><button type="submit" ${state.authorizing || secure?.can_submit_credentials===false?'disabled':''}>${state.authorizing?'验证中…':'验证并加密保存'}</button></form>`;
    }
    function campaignRows(items) {
        return array(items).map(row=>{const id=String(row.id || row.campaign_id || '');return `<tr><td><input type="checkbox" data-ops-campaign="${escape(id)}" aria-label="选择广告 ${escape(row.title || row.name || id)}" ${state.campaignsSelected.has(id)?'checked':''}></td><td><strong>${escape(row.title || row.name || id)}</strong><small>${escape(id)}</small></td><td>${escape(row.state || row.status || '未提供')}</td><td>${escape(row.paymentType || row.payment_type || row.type || '未提供')}</td><td>${escape(row.placement || row.objectType || '未提供')}</td></tr>`}).join('');
    }
    function campaignPager() {
        if(!state.campaignsLoaded)return '';
        return `<div class="ops-pagination"><span>第 ${state.campaignPage} 页 · 每页最多 50 个活动 · 跨页已选 ${state.campaignsSelected.size} / 10</span>${button('上一页活动','campaigns-previous','',state.campaignsLoading || state.campaignPage<=1)}${button('下一页活动','campaigns-next','',state.campaignsLoading || !state.campaignHasMore)}</div>`;
    }
    function reportRows(items) {
        const rows=array(items).slice(0,100).map(row=>row.raw && typeof row.raw==='object'?{...(row.campaign_id!==undefined?{'活动 ID':row.campaign_id}:{}),...(row.file!==undefined?{'报表文件':row.file}:{}),...(row.row_number!==undefined?{'报表行号':row.row_number}:{}),...row.raw,...Object.fromEntries(Object.entries(row.derived || {}).map(([key,value])=>['计算 · '+key,value]))}:row), columns=[...new Set(rows.flatMap(row=>Object.keys(row)))].slice(0,24);
        return `<div class="ops-table-scroll"><table class="ops-table"><thead><tr>${columns.map(k=>`<th>${escape(k)}</th>`).join('')}</tr></thead><tbody>${rows.map(row=>`<tr>${columns.map(k=>`<td>${escape(row[k]===null || row[k]===undefined?'暂无数据':typeof row[k]==='object'?JSON.stringify(row[k]):row[k])}</td>`).join('')}</tr>`).join('')}</tbody></table></div>${items.length>100?'<p class="ops-muted">仅预览前 100 行，完整数据已保存于后台。</p>':''}`;
    }
    function reportHtml() {
        const report=state.report;if(!report)return '';
        const uncertain=normalizedStatus(report.status)==='submission_uncertain';
        const recovery=report.recovery_instructions || '提交结果不确定，已停止轮询和重复提交。请核对官方报表任务或等待管理员恢复。';
        return `<div class="ops-report"><div class="ops-section-title"><h3>广告报表</h3><div>${badge(report.status || 'pending')}${button('刷新报表状态','report-refresh','',state.reportPolling)}${button('停止等待','report-stop','',!report.waiting)}</div></div><p class="ops-muted">任务 ${escape(report.uuid || '')} · ${escape(report.message || '')}</p>${report.error?`<p class="ops-error">${escape(report.error)}</p>`:''}${uncertain?`<p class="ops-error">${escape(Array.isArray(recovery)?recovery.join('；'):recovery)}</p>`:array(report.rows).length?reportRows(report.rows):empty(terminal(report.status)?'接口未返回报表行，请检查活动类型、日期或广告权限。':'Ozon 正在生成报表。本页每 10 秒查询一次，最多等待 5 分钟；停止等待不会取消官方报表，也不会丢失任务编号。')}<p class="ops-muted">广告归因销售额不是广告带来的新增利润。报表为原始官方字段，不把缺失值当作 0。</p></div>`;
    }
    function reportHistoryHtml() {
        if(!state.savedReports.length)return empty('没有已保存的广告报表任务。任务保存在后台，重新打开工作台后可从此处继续读取。');
        return `<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>任务编号</th><th>统计周期</th><th>状态</th><th>操作</th></tr></thead><tbody>${state.savedReports.map(row=>{const id=row.uuid || row.UUID || row.report_id || '';return `<tr><td>${escape(id)}</td><td>${escape(daysLabel(row))}</td><td>${badge(row.status || row.state)}</td><td>${button('查看 / 继续读取','report-resume',` data-report="${escape(id)}"`,!id || state.reportPolling)}</td></tr>`}).join('')}</tbody></table></div>`;
    }
    function advertising() {
        if(!state.adOpen){region('advertising','');return}
        region('advertising', `<section class="ops-advertising"><div class="ops-section-title"><h2>广告授权与报表</h2>${button('收起','advertising')}</div>${adStatusHtml()}<div class="ops-section-title"><h3>广告活动（只读）</h3>${button(state.campaignsLoading?'读取中…':'从 Ozon 读取活动','campaigns','',!state.ad?.ready || state.campaignsLoading)}</div>${state.campaignsLoaded && !state.campaigns.length?empty('当前账号没有可读活动，或未获得对应店铺的广告权限。'):state.campaigns.length?`<div class="ops-table-scroll"><table class="ops-table"><thead><tr><th>选择</th><th>活动 / ID</th><th>官方状态</th><th>计费类型</th><th>投放位置</th></tr></thead><tbody>${campaignRows(state.campaigns)}</tbody></table></div>`:empty('验证独立广告授权后，手动读取活动。进入本页不会请求广告数据或开启广告。')}${campaignPager()}<form data-ops-form="report"><label>报表起始日期<input type="date" name="date_from" value="${escape(state.reportFrom)}" required></label><label>截止日期<input type="date" name="date_to" value="${escape(state.reportTo)}" required></label><span>已选择 ${state.campaignsSelected.size} / 10 个活动</span><button type="submit" ${!state.ad?.ready || !state.campaignsSelected.size || state.reportCreating || !canCreateReport(state.report) || historyBlocksReport(state.savedReports)?'disabled':''}>${state.reportCreating?'提交中…':'生成只读广告报表'}</button></form>${reportHtml()}<div class="ops-section-title"><h3>已保存报表任务</h3>${button('刷新任务记录','report-history')}</div>${reportHistoryHtml()}${historyBlocksReport(state.savedReports)?'<p class="ops-muted">当前店铺有未完成或结果不确定的报表，请先继续读取该任务或按恢复说明处理，不重复创建报表。</p>':''}<p class="ops-ad-guard">首版仅验证授权和读取报表。不会创建、开启广告，调整出价、预算，或修改线上商品。</p></section>`);
    }
    function render() {if(!live())return;controls();products();detail();settings();advertising()}
    async function loadProducts() {
        const revision=generation,listRevision=++productsGeneration;state.loading=true;products();
        try{const data=await request('/api/operations/products?'+qs({shop:state.shop,q:state.query,limit:state.limit,offset:state.offset}));if(!current(revision) || productsGeneration!==listRevision)return;state.items=array(data.items);state.total=Number(data.total) || 0}
        catch(error){if(current(revision) && productsGeneration===listRevision && error.name!=='AbortError')announce(error.message,true)}finally{if(current(revision) && productsGeneration===listRevision){state.loading=false;products()}}
    }
    async function loadJobs() {
        const revision=generation;
        try {const hadPending=state.jobs.some(pendingJob);const data=await request('/api/operations/jobs?'+qs({shop:state.shop}),{},'jobs');if(!current(revision))return;state.jobs=array(data.items);settings();if(state.detail){state.detail.jobs=state.jobs.filter(j=>j.offer_id===state.offer);detail()}
            if(state.jobs.some(pendingJob)){if(jobsTimer)clearTimeout(jobsTimer);jobsTimer=setTimeout(()=>{jobsTimer=null;if(live())loadJobs()},10000)}else{if(jobsTimer)clearTimeout(jobsTimer);jobsTimer=null}
            if(hadPending && !state.jobs.some(pendingJob)){await loadProducts();if(current(revision) && state.offer)await loadDetail(state.offer)}
        } catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}
    }
    async function loadAdStatus() {
        const revision=generation;try{const data=await request('/api/operations/advertising/status?'+qs({shop:state.shop}),{},'ad-status');if(current(revision)){state.ad=data;advertising()}}catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}
    }
    async function loadCampaigns(page=1, reset=false) {
        if(!live() || state.campaignsLoading || !state.ad?.ready || page<1 || page>10000)return;
        const revision=generation;state.campaignsLoading=true;advertising();
        try{const data=await request('/api/operations/advertising/campaigns?'+qs({shop:state.shop,page}),{},'campaigns');if(!current(revision))return;state.campaigns=array(data.items || data.list);state.campaignsLoaded=true;state.campaignPage=Number(data.page) || page;state.campaignHasMore=data.has_more===true;if(reset)state.campaignsSelected.clear()}
        catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}finally{if(current(revision)){state.campaignsLoading=false;advertising()}}
    }
    async function loadSchedule() {
        const revision=generation;try{const data=await request('/api/operations/schedule?'+qs({shop:state.shop}),{},'schedule');if(current(revision)){state.schedule=data.schedule || data;settings()}}catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}
    }
    async function loadReportHistory() {
        const revision=generation,historyRevision=++reportHistoryGeneration;try{const data=await request('/api/operations/advertising/reports?'+qs({shop:state.shop,limit:30}));if(current(revision) && reportHistoryGeneration===historyRevision){state.savedReports=array(data.items);advertising()}}catch(error){if(current(revision) && reportHistoryGeneration===historyRevision && error.name!=='AbortError')announce(error.message,true)}
    }
    async function loadDetail(offer) {
        const revision=generation, detailRevision=++detailGeneration;state.offer=offer;state.detail={loading:true};detail();
        try{const data=await request('/api/operations/product?'+qs({shop:state.shop,offer_id:offer}));if(!current(revision) || detailGeneration!==detailRevision)return;state.detail=data;detail()}
        catch(error){if(current(revision) && detailGeneration===detailRevision && error.name!=='AbortError'){state.detail=null;detail();announce(error.message,true)}}
    }
    async function refresh() {if(!live() || !state.shop)return;await Promise.all([loadProducts(),loadJobs(),loadAdStatus(),loadSchedule(),loadReportHistory()]);if(state.offer)await loadDetail(state.offer)}
    function switchShop(shop) {
        generation++;detailGeneration++;stopTimers();cancelRequests();const old=state;
        state={...old,shop,items:[],total:0,offset:0,detail:null,offer:'',ad:null,jobs:[],schedule:null,scheduleDraft:null,campaigns:[],campaignsSelected:new Set(),campaignsLoaded:false,campaignsLoading:false,campaignPage:1,campaignHasMore:false,report:null,savedReports:[],reportCreating:false,reportPolling:false,authorizing:false,scheduleSaving:false};announce('');render();refresh();
    }
    async function sync(offer) {
        if(!offer || !state.shop)return;const revision=generation,shop=state.shop,days=state.days,include_traffic=state.includeTraffic;
        try{await request('/api/operations/sync',json({shop,offer_id:offer,days,include_traffic}),`sync-${shop}-${offer}`);if(!current(revision))return;announce('已加入只读同步队列。可继续查看其他商品。');await loadJobs()}
        catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}
    }
    async function pollReport(automatic=true) {
        if(!live() || !state.report?.uuid || state.reportPolling)return;
        const revision=generation,uuid=state.report.uuid;state.reportPolling=true;advertising();
        try{const data=await request('/api/operations/advertising/reports/'+encodeURIComponent(uuid)+'?'+qs({shop:state.shop}));if(!current(revision) || state.report?.uuid!==uuid)return;const old=state.report;state.report={...old,...data,uuid,polls:(old.polls || 0)+1};
            if(terminal(state.report.status)){state.report.waiting=false;await loadReportHistory();if(!current(revision) || state.report?.uuid!==uuid)return}
            if(automatic && state.report.waiting && state.report.polls<30 && !terminal(state.report.status)){if(reportTimer)clearTimeout(reportTimer);reportTimer=setTimeout(()=>{reportTimer=null;if(live())pollReport(true)},10000)}
            else if(state.report.polls>=30){state.report.waiting=false;state.report.message='已停止自动等待；可手动刷新，任务编号仍保留。'}
        }catch(error){if(current(revision) && state.report?.uuid===uuid && error.name!=='AbortError'){state.report.error=error.message;state.report.waiting=false}}
        finally{if(current(revision)){state.reportPolling=false;advertising()}}
    }
    async function onClick(event) {
        const target=event.target.closest('[data-ops-action]');if(!target || target.disabled || !live())return;
        const action=target.dataset.opsAction;const revision=generation;
        if(action==='settings'){state.settingsOpen=!state.settingsOpen;settings();return}
        if(action==='advertising'){state.adOpen=!state.adOpen;advertising();return}
        if(action==='detail'){await loadDetail(target.dataset.offer);return}
        if(action==='close-detail'){detailGeneration++;state.detail=null;state.offer='';detail();return}
        if(action==='sync'){await sync(target.dataset.offer);return}
        if(action==='search'){state.query=host.querySelector('[data-ops-input="query"]').value.trim();state.offset=0;await loadProducts();return}
        if(action==='previous' || action==='next'){state.offset=Math.max(0,state.offset+(action==='previous'?-state.limit:state.limit));await loadProducts();return}
        if(action==='report-stop'){if(reportTimer)clearTimeout(reportTimer);reportTimer=null;if(state.report)state.report.waiting=false;advertising();return}
        if(action==='report-refresh'){await pollReport(false);return}
        if(action==='report-history'){await loadReportHistory();return}
        if(action==='report-resume'){const row=state.savedReports.find(x=>(x.uuid || x.UUID || x.report_id)===target.dataset.report);if(!row)return;if(reportTimer)clearTimeout(reportTimer);reportTimer=null;state.report={...row,uuid:target.dataset.report,status:row.status || row.state || 'pending',waiting:false,polls:0};await pollReport(false);return}
        if(action==='campaigns'){await loadCampaigns(1,true);return}
        if(action==='campaigns-previous'){if(state.campaignPage>1)await loadCampaigns(state.campaignPage-1);return}
        if(action==='campaigns-next'){if(state.campaignHasMore)await loadCampaigns(state.campaignPage+1);return}
        try{
            if(action==='discover'){target.disabled=true;await request('/api/operations/discover',json({}),'discover');if(!current(revision))return;announce('已整理工作台上架记录，未修改任何线上商品。');await loadProducts()}
        }catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}finally{if(current(revision)){if(action==='discover' && target.isConnected)target.disabled=false}}
    }
    function onChange(event) {
        if(!live())return;const input=event.target;
        if(input.dataset.opsInput==='shop'){switchShop(input.value);return}
        if(input.dataset.opsInput==='days'){state.days=Number(input.value)===30?30:7;detail();return}
        if(input.dataset.opsInput==='include-traffic'){state.includeTraffic=input.checked;return}
        if(input.dataset.opsInput==='schedule-enabled' || input.dataset.opsInput==='schedule-days'){state.scheduleDraft={...(state.scheduleDraft || state.schedule || {}),[input.dataset.opsInput==='schedule-enabled'?'enabled':'days']:input.dataset.opsInput==='schedule-enabled'?input.checked:Number(input.value)};return}
        if(input.dataset.opsCampaign!==undefined){const id=input.dataset.opsCampaign;if(input.checked){if(state.campaignsSelected.size>=10){input.checked=false;announce('单次报表最多选择 10 个广告活动。',true);return}state.campaignsSelected.add(id)}else state.campaignsSelected.delete(id);advertising()}
        if(input.name==='date_from')state.reportFrom=input.value;
        if(input.name==='date_to')state.reportTo=input.value;
    }
    async function onSubmit(event) {
        const form=event.target;if(!form.dataset.opsForm || !live())return;event.preventDefault();
        const revision=generation,shop=state.shop,kind=form.dataset.opsForm;
        try{
            if(kind==='authorization'){
                if(state.authorizing || state.security?.can_submit_credentials!==true)return;
                const clientInput=form.querySelector('[name="client_id"]'), secretInput=form.querySelector('[name="client_secret"]');
                const body={shop,client_id:clientInput.value.trim(),client_secret:secretInput.value};clientInput.value='';secretInput.value='';state.authorizing=true;advertising();
                try{await request('/api/operations/advertising/authorize',json(body));if(current(revision)){announce('广告授权已验证并加密保存。尚未开启任何广告。');await loadAdStatus()}}
                catch(error){if(current(revision) && error.name!=='AbortError')announce('广告授权验证失败。请确认服务账号、店铺权限和安全连接；密钥不会保留在页面中。',true)}
                finally{body.client_id='';body.client_secret='';if(current(revision)){state.authorizing=false;advertising()}}return;
            }
            if(kind==='schedule'){
                if(state.scheduleSaving)return;state.scheduleSaving=true;
                const body={shop,enabled:form.querySelector('[data-ops-input="schedule-enabled"]').checked,days:Number(form.querySelector('[data-ops-input="schedule-days"]').value),interval_hours:24};settings();
                const data=await request('/api/operations/schedule',{method:'PUT',body:JSON.stringify(body)});if(current(revision)){state.schedule=data.schedule || data;state.scheduleDraft=null;announce(body.enabled?'已启用当前店铺的只读定时监测。':'已关闭当前店铺定时监测。')}
            }
            if(kind==='report'){
                if(state.reportCreating || !state.ad?.ready || !state.campaignsSelected.size || !canCreateReport(state.report) || historyBlocksReport(state.savedReports))return;
                const from=form.querySelector('[name="date_from"]').value,to=form.querySelector('[name="date_to"]').value,span=(new Date(to+'T00:00:00Z')-new Date(from+'T00:00:00Z'))/86400000;
                if(!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to) || !Number.isFinite(span) || span<0 || span>61)throw new Error('报表日期需前后有效且不超过 62 天。');
                state.reportFrom=from;state.reportTo=to;state.reportCreating=true;advertising();
                const data=await request('/api/operations/advertising/reports',json({shop,campaigns:[...state.campaignsSelected],date_from:from,date_to:to}));if(!current(revision))return;
                const uuid=data.uuid || data.UUID;if(!uuid)throw new Error('官方接口未返回报表任务编号，请稍后重试。');state.report={...data,uuid,waiting:true,polls:0};announce('广告报表已提交，只读取统计，不改动投放。');await pollReport(true);if(current(revision))await loadReportHistory();
            }
        }catch(error){if(current(revision)){if(error.name!=='AbortError')announce(error.message,true);if(kind==='report')await loadReportHistory()}}finally{if(current(revision)){if(kind==='schedule'){state.scheduleSaving=false;settings()}if(kind==='report'){state.reportCreating=false;advertising()}}}
    }
    async function mount(container) {
        unmount();host=typeof container==='string'?global.document.querySelector(container):container;if(!host)return;
        const today=new Date(),end=today.toISOString().slice(0,10),start=new Date(today.getTime()-6*86400000).toISOString().slice(0,10);
        state={shops:[],shop:'',security:null,days:7,includeTraffic:true,query:'',offset:0,limit:30,items:[],total:0,loading:false,offer:'',detail:null,jobs:[],settingsOpen:false,schedule:null,scheduleDraft:null,scheduleSaving:false,ad:null,adOpen:false,authorizing:false,campaigns:[],campaignsSelected:new Set(),campaignsLoaded:false,campaignsLoading:false,campaignPage:1,campaignHasMore:false,reportFrom:start,reportTo:end,report:null,savedReports:[],reportCreating:false,reportPolling:false};
        host.classList.add('operations-center');host.innerHTML='<header class="ops-heading"><h1>商品运营中心</h1><p>按店铺与货号追踪上架状态、搜索词和广告数据，先诊断，再优化。</p></header><p class="ops-stage-note">当前阶段：只读监测与广告报表。不会自动修改链接、开启广告或消耗广告预算。</p><p data-ops-notice class="ops-notice" role="status" aria-live="polite" hidden></p><div class="ops-toolbar" data-ops-region="toolbar"></div><div data-ops-region="products"></div><div data-ops-region="detail"></div><div data-ops-region="settings"></div><div data-ops-region="advertising"></div>';
        host.addEventListener('click',onClick);host.addEventListener('change',onChange);host.addEventListener('submit',onSubmit);render();const revision=generation;
        try{const config=await request('/api/operations/config',{},'config');if(!current(revision))return;state.shops=array(config.shops);state.security=config.credential_security;state.shop=initialShopId(state.shops);render();if(state.shop)await refresh();else announce('先在“店铺授权”添加 Ozon 店铺，再读取上架记录。')}
        catch(error){if(current(revision) && error.name!=='AbortError')announce(error.message,true)}
    }
    const api=Object.freeze({mount,refresh,unmount});global.OperationsCenter=api;
    if(typeof module!=='undefined' && module.exports)module.exports={escape,initialShopId,metric,finiteRatio,safeSource,status,productRows,queryRows,snapshotRows,reportRows,trendSvg,diagnostics,healthHtml,latestSnapshot,metricObservations,queryObservations,normalizedStatus,terminal,canCreateReport,historyBlocksReport,mount,refresh,unmount};
})(typeof window!=='undefined'?window:globalThis);
