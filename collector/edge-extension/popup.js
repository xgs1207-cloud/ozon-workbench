const DEFAULT_FACTORY_URL = "http://43.132.190.110:8088";
const COMMAND_CENTER_QUERY_VERSION = "2026-08-01-ui-state-v1";
// 旧的本机/局域网地址：检测到这些旧配置时回退到新的公网默认地址
const LEGACY_LOCAL_FACTORY_URLS = new Set([
    "http://127.0.0.1:8765",
    "http://localhost:8765"
]);
let factoryConfig = { baseUrl: DEFAULT_FACTORY_URL, authHeader: null, deviceId: "" };
async function ensureFactoryDeviceId() {
    const stored = await chrome.storage.local.get(["factoryDeviceId"]);
    let deviceId = String(stored.factoryDeviceId || "").trim();
    if (!deviceId) {
        deviceId = globalThis.crypto?.randomUUID?.() || `device-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
        await chrome.storage.local.set({ factoryDeviceId: deviceId });
    }
    return deviceId;
}
// 解析工作台地址：允许局域网地址与公网服务器地址（manifest 已声明 http://*/* host 权限）。
// URL 里可带 Basic Auth 用户信息（http://user:pass@host:port），返回供请求头使用。
function parseFactoryUrl(value) {
    const url = new URL(String(value || DEFAULT_FACTORY_URL).trim());
    if (!['http:', 'https:'].includes(url.protocol))
        throw new Error('工作台地址协议不支持');
    const origin = `${url.protocol}//${url.host}`;
    let authHeader = null;
    if (url.username || url.password) {
        const credentials = `${decodeURIComponent(url.username)}:${decodeURIComponent(url.password)}`;
        authHeader = 'Basic ' + btoa(credentials);
    }
    return { origin, authHeader };
}
function workbenchEntryUrl(kind, extra = {}) {
    const path = kind === "ozon" ? "/ozon-reference" : "/1688-collection";
    const params = new URLSearchParams({ v: COMMAND_CENTER_QUERY_VERSION });
    if (extra.product_id)
        params.set("product_id", String(extra.product_id));
    if (extra.task_id)
        params.set("task_id", String(extra.task_id));
    return `${factoryConfig.baseUrl}${path}?${params.toString()}`;
}
function cleanFactoryUrlText(value) {
    return String(value || "").trim().replace(/\/+$/, "");
}
function isLegacyLocalFactoryUrl(value) {
    return LEGACY_LOCAL_FACTORY_URLS.has(cleanFactoryUrlText(value));
}
function factoryUrlOrDefault(value) {
    const text = cleanFactoryUrlText(value);
    if (!text || isLegacyLocalFactoryUrl(text))
        return DEFAULT_FACTORY_URL;
    return text;
}
async function ensureWorkbenchHostPermission(origin) {
    const address = new URL(origin);
    if (address.protocol !== "https:")
        return;
    const pattern = `https://${address.hostname}/*`;
    if (await chrome.permissions.contains({ origins: [pattern] }))
        return;
    if (!await chrome.permissions.request({ origins: [pattern] }))
        throw new Error("未获得 HTTPS 工作台的站点访问权限，请重新点保存并连接授权");
}
function isSafeMarketDestination(origin) {
    const address = new URL(origin);
    return address.protocol === "https:"
        || (address.protocol === "http:" && ["localhost", "127.0.0.1"].includes(address.hostname));
}
async function loadFactoryConfig() {
    const stored = await chrome.storage.local.get(["factoryBaseUrl"]);
    const text = factoryUrlOrDefault(stored.factoryBaseUrl);
    const access = parseFactoryUrl(text);
    if (cleanFactoryUrlText(stored.factoryBaseUrl) !== text) {
        await chrome.storage.local.set({ factoryBaseUrl: text });
    }
    factoryConfig = {
        baseUrl: access.origin,
        authHeader: access.authHeader,
        deviceId: await ensureFactoryDeviceId()
    };
    return factoryConfig;
}
async function factoryFetch(path, options = {}) {
    await loadFactoryConfig();
    const headers = { ...(options.headers || {}) };
    if (headers["X-Market-Ingest-Token"] && !isSafeMarketDestination(factoryConfig.baseUrl))
        throw new Error("市场数据和令牌不能通过公网 HTTP 传输");
    headers["X-Factory-Device-Id"] = factoryConfig.deviceId;
    if (factoryConfig.authHeader)
        headers["Authorization"] = factoryConfig.authHeader;
    return fetch(`${factoryConfig.baseUrl}${path}`, { ...options, headers });
}
function uniqueMarketRecords(records, seen) {
    return records.filter((record) => {
        const key = JSON.stringify(record);
        if (seen.has(key))
            return false;
        seen.add(key);
        return true;
    });
}
function marketRecordChunks(records, maxBytes = 850000) {
    const encoder = new TextEncoder();
    const chunks = [];
    let chunk = [];
    let bytes = 0;
    for (const record of records) {
        const size = encoder.encode(JSON.stringify(record)).length + 1;
        if (size > maxBytes)
            throw new Error("单条报表数据超过安全上传大小，请反馈该行内容");
        if (chunk.length >= 200 || (chunk.length && bytes + size > maxBytes)) {
            chunks.push(chunk);
            chunk = [];
            bytes = 0;
        }
        chunk.push(record);
        bytes += size;
    }
    if (chunk.length)
        chunks.push(chunk);
    return chunks;
}
function isRollingMarketPage(url) {
    try {
        const address = new URL(url);
        return ["seerfar.cn", "www.seerfar.cn"].includes(address.hostname)
            && /^\/admin\/market(?:\.html)?\/?$/.test(address.pathname);
    }
    catch {
        return false;
    }
}
function marketCaptureMonth(now = new Date()) {
    return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}`;
}
const els = {
    status: document.getElementById("page-status"),
    productCaptureUi: document.getElementById("product-capture-ui"),
    title: document.getElementById("title"),
    mainCount: document.getElementById("main-count"),
    skuCount: document.getElementById("sku-count"),
    detailCount: document.getElementById("detail-count"),
    capture: document.getElementById("capture"),
    previewToggle: document.getElementById("preview-toggle"),
    debugExport: document.getElementById("debug-export"),
    openInbox: document.getElementById("open-inbox"),
    preview: document.getElementById("preview"),
    skuList: document.getElementById("sku-list"),
    mainThumbs: document.getElementById("main-thumbs"),
    detailThumbs: document.getElementById("detail-thumbs"),
    duplicate: document.getElementById("duplicate"),
    duplicateMessage: document.getElementById("duplicate-message"),
    openExisting: document.getElementById("open-existing"),
    createVersion: document.getElementById("create-version"),
    progress: document.getElementById("progress"),
    result: document.getElementById("result"),
    factoryUrl: document.getElementById("factory-url"),
    saveConnection: document.getElementById("save-connection"),
    testConnection: document.getElementById("test-connection"),
    connectionResult: document.getElementById("connection-result"),
    marketCapture: document.getElementById("market-capture"),
    marketRollingPeriod: document.getElementById("market-rolling-period"),
    marketNaturalPeriod: document.getElementById("market-natural-period"),
    marketCaptureMonth: document.getElementById("market-capture-month"),
    marketPeriod: document.getElementById("market-period"),
    marketToken: document.getElementById("market-token"),
    saveMarketToken: document.getElementById("save-market-token"),
    clearMarketToken: document.getElementById("clear-market-token"),
    marketTokenStatus: document.getElementById("market-token-status"),
    marketMaxPages: document.getElementById("market-max-pages"),
    captureMarket: document.getElementById("capture-market"),
    stopMarket: document.getElementById("stop-market")
};
const chainEls = {
    auto: document.getElementById('market-auto-keywords'),
    pages: document.getElementById('market-keyword-pages'),
    categories: document.getElementById('market-max-categories'),
    resume: document.getElementById('resume-market-chain'),
    status: document.getElementById('market-chain-status')
};
const productPeriodBox = document.getElementById('market-product-period');
const productPeriodKind = document.getElementById('market-product-period-kind');
productPeriodKind?.addEventListener('change', () => {
    const rolling = productPeriodKind.value === 'rolling_30d';
    els.marketRollingPeriod.hidden = !rolling;
    els.marketNaturalPeriod.hidden = rolling;
});
let chainJob = null;
async function chainMessage(type, extra = {}) {
    const response = await chrome.runtime.sendMessage({ type, ...extra });
    if (!response?.ok) throw new Error(response?.error || '后台采集连接失败，请重新加载插件');
    return response.job;
}
function showChainJob(job) {
    chainJob = job;
    if (!job || !chainEls.status) return;
    const running = job.status === 'running';
    chainEls.status.textContent = `${job.message || ''} · 关键词类目 ${Math.min(job.targetIndex, job.categoryCount)}/${job.categoryCount}`
        + (job.currentCategory && running ? ` · ${job.currentCategory}` : '')
        + (job.skipped ? ` · ${job.skipped} 行缺少类目 ID，未自动采词` : '')
        + (job.error ? ` · ${job.error}` : '');
    els.captureMarket.disabled = running || marketCaptureRunning;
    els.stopMarket.disabled = !running && !marketCaptureRunning;
    chainEls.resume.hidden = !['error', 'stopped'].includes(job.status);
}
async function refreshChainJob() {
    try { showChainJob(await chainMessage('SEERFAR_CHAIN_STATUS')); } catch { /* Older plugin needs reload. */ }
}
let latestCapture = null;
let duplicateProductId = null;
let activePageKind = "unsupported";
let marketCaptureRunning = false;
let marketStopRequested = false;
function safeMarketTokenError(error, verifiedToken = "") {
    let message = String(error?.message || error);
    for (const token of new Set([verifiedToken, els.marketToken.value.trim()])) {
        if (token)
            message = message.split(token).join("[已隐藏令牌]");
    }
    return message;
}
async function verifyAndSaveMarketToken() {
    const token = els.marketToken.value.trim();
    if (!token)
        throw new Error("请填写市场数据写入令牌");
    await loadFactoryConfig();
    if (!isSafeMarketDestination(factoryConfig.baseUrl))
        throw new Error("令牌不能通过公网 HTTP 传输：请配置 HTTPS 工作台或本机 SSH 隧道");
    els.marketTokenStatus.textContent = "正在验证工作台令牌…";
    let response;
    try {
        response = await factoryFetch("/api/market-data/stats", {
            headers: { "X-Market-Ingest-Token": token }
        });
    }
    catch {
        throw new Error("无法连接工作台验证令牌，请检查地址与 SSH 隧道");
    }
    if (!response.ok) {
        if (response.status === 401)
            throw new Error("令牌无效，请确认与服务器配置一致");
        if (response.status === 503)
            throw new Error("服务器尚未配置市场数据写入令牌");
        throw new Error(`令牌验证失败（HTTP ${response.status}）`);
    }
    let result;
    try {
        result = await response.json();
    }
    catch {
        throw new Error("工作台验证响应格式异常，令牌未保存");
    }
    if (result?.ok !== true)
        throw new Error("工作台未确认令牌有效，令牌未保存");
    await chrome.storage.local.set({ marketIngestToken: token });
    els.marketTokenStatus.textContent = "令牌已验证并保存在本机浏览器";
    return token;
}
function setResult(value) {
    els.result.textContent = typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
function skuImagePreflight(capture) {
    const debug = capture?.raw_snapshot?.sku_debug || {};
    const total = Number(capture?.sku_image_preflight?.total_skus || debug.total_skus || capture?.skus?.length || 0);
    const withImages = Number(capture?.sku_image_preflight?.sku_with_images || debug.sku_with_images || 0);
    const missing = capture?.sku_image_preflight?.missing_sku_ids || debug.missing_image_skus || [];
    return {
        total,
        withImages,
        missing,
        complete: total > 0 && missing.length === 0 && withImages === total
    };
}
function showSkuImageWarning(capture) {
    const check = skuImagePreflight(capture);
    if (check.complete)
        return false;
    const sample = check.missing.slice(0, 5).join("、") || "未识别SKU";
    els.status.textContent = `可采集：SKU图片${check.withImages}/${check.total}，缺图将保留标记`;
    els.progress.textContent = "缺图SKU可继续选择；生图前需要人工确认参考图";
    setResult({
        code: "SKU_IMAGES_INCOMPLETE_WARNING",
        message: `1688页面识别到${check.withImages}/${check.total}个SKU图片，缺少${check.missing.length}个。仍可采集；系统会保留真实缺图状态，生图前再由你确认共用哪张同外观SKU实拍图。`,
        missing_sku_ids: check.missing,
        examples: sample
    });
    return false;
}
async function getActiveTab() {
    const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
    return tabs[0];
}
function sendToTab(tabId, message) {
    if (message?.type?.startsWith('SEERFAR_MARKET_'))
        return sendSeerfarTabMessage(tabId, message);
    if (PRODUCT_PAGE_COMMANDS.has(message?.type))
        return sendProductTabMessage(tabId, message);
    return new Promise((resolve, reject) => {
        chrome.tabs.sendMessage(tabId, message, (response) => {
            const err = chrome.runtime.lastError;
            if (err)
                reject(new Error(err.message));
            else
                resolve(response);
        });
    });
}
async function waitForSkuSelection(tabId, capture, previousSelectedSkuIds = []) {
    const result = await sendToTab(tabId, {
        type: "OPEN_SKU_SELECTOR",
        capture,
        previous_selected_sku_ids: previousSelectedSkuIds
    });
    if (result?.opened !== true) throw new Error(result?.error || '规格选择器未打开，请刷新商品页后重试');
    window.close();
    return null;
}
async function loadPreview() {
    try {
        const tab = await getActiveTab();
        const is1688Page = productPageKind(tab?.url) === '1688';
        const isOzonPage = productPageKind(tab?.url) === 'ozon';
        const isSeerfarPage = Boolean(tab?.url && /^https:\/\/(?:www\.)?seerfar\.cn\//.test(tab.url));
        els.marketCapture.hidden = !isSeerfarPage;
        els.productCaptureUi.hidden = !(is1688Page || isOzonPage);
        if (isSeerfarPage) {
            activePageKind = "seerfar";
            const rolling = isRollingMarketPage(tab.url);
            if (productPeriodBox) productPeriodBox.hidden = !/\/admin\/product-search(?:\.html)?(?:\?|\/?$)/.test(tab.url);
            els.marketRollingPeriod.hidden = !rolling;
            els.marketNaturalPeriod.hidden = rolling;
            els.marketCaptureMonth.textContent = `${marketCaptureMonth()}（按本机时间自动生成）`;
            els.status.textContent = "Seerfar 报表页：可手动采集当前可见表格";
            return;
        }
        if (!tab || !tab.url || (!is1688Page && !isOzonPage)) {
            activePageKind = "unsupported";
            els.status.textContent = "当前页面不是可采集的1688商品页或Ozon商品页";
            return;
        }
        activePageKind = isOzonPage ? "ozon" : "1688";
        latestCapture = await sendToTab(tab.id, { type: isOzonPage ? "COLLECTOR_OZON_PREVIEW" : "COLLECTOR_PREVIEW" });
        if (!latestCapture || !latestCapture.is_collectable) {
            els.status.textContent = latestCapture?.reason || "当前页面不可采集";
            setResult(latestCapture || {});
            return;
        }
        if (isOzonPage) {
            els.status.textContent = "可采集Ozon参考页（浏览器已打开，绕开307重定向）";
            els.title.textContent = latestCapture.title || latestCapture.title_ru || "unknown";
            els.mainCount.textContent = latestCapture.image_urls?.length || latestCapture.main_images?.length || 0;
            els.skuCount.textContent = 1;
            els.detailCount.textContent = latestCapture.detail_images?.length || 0;
            els.capture.textContent = "采集当前Ozon参考页";
            els.capture.disabled = false;
            els.previewToggle.disabled = false;
            els.debugExport.disabled = true;
            renderPreview(latestCapture);
            setResult({
                source_url: latestCapture.source_url,
                title: latestCapture.title,
                image_count: latestCapture.image_urls?.length || 0,
                warnings: latestCapture.capture_warnings,
            });
            return;
        }
        const skuCheck = skuImagePreflight(latestCapture);
        els.status.textContent = skuCheck.complete
            ? "可采集（SKU图片已完整加载）"
            : `可采集（SKU图片${skuCheck.withImages}/${skuCheck.total}，缺图已标记）`;
        els.title.textContent = latestCapture.title_cn || "unknown";
        els.mainCount.textContent = latestCapture.main_images.length;
        els.skuCount.textContent = latestCapture.skus.length;
        els.detailCount.textContent = latestCapture.detail_images.length;
        els.capture.disabled = false;
        els.previewToggle.disabled = false;
        els.debugExport.disabled = false;
        renderPreview(latestCapture);
        setResult({
            warnings: latestCapture.capture_warnings,
            diagnostics: latestCapture.field_diagnostics,
            sku_debug: latestCapture.raw_snapshot?.sku_debug || null
        });
    }
    catch (error) {
        els.status.textContent = `无法读取页面：${error.message}`;
        setResult(error.message);
    }
}
function renderPreview(capture) {
    els.skuList.innerHTML = "";
    els.mainThumbs.innerHTML = "";
    els.detailThumbs.innerHTML = "";
    (capture.skus || []).slice(0, 300).forEach((sku) => {
        const li = document.createElement("li");
        const dimensions = (sku.option_values || []).map((item) => `${item.name_cn || "规格"}:${item.value_cn || "unknown"}`).join("；");
        const imageState = sku.sku_image_missing ? "无SKU图" : "有SKU图";
        li.textContent = [sku.sku_name, dimensions, sku.purchase_price ? `¥${sku.purchase_price}` : "¥unknown", sku.price_source || "unknown", imageState, sku.sku_id || "unknown"].filter(Boolean).join(" / ") || "unknown";
        els.skuList.appendChild(li);
    });
    (capture.main_images || []).slice(0, 80).forEach((item) => {
        const img = document.createElement("img");
        img.loading = 'lazy';
        img.src = item.url;
        img.title = item.url;
        els.mainThumbs.appendChild(img);
    });
    (capture.detail_images || []).slice(0, 80).forEach((item) => {
        const img = document.createElement("img");
        img.loading = 'lazy';
        img.src = item.url;
        img.title = item.url;
        els.detailThumbs.appendChild(img);
    });
}
async function exportSkuDebug() {
    els.debugExport.disabled = true;
    els.progress.textContent = "正在导出SKU诊断...";
    try {
        const tab = await getActiveTab();
        const debug = await sendToTab(tab.id, { type: "EXPORT_SKU_DEBUG" });
        els.progress.textContent = "已导出 sku-debug.json";
        setResult({
            total_skus: debug.total_skus,
            real_sku_ids: debug.real_sku_ids,
            sku_with_images: debug.sku_with_images,
            sku_with_prices: debug.sku_with_prices,
            data_sources: debug.data_sources
        });
    }
    catch (error) {
        els.progress.textContent = "导出失败";
        setResult(error.message);
    }
    finally {
        els.debugExport.disabled = false;
    }
}
async function checkDuplicate(sourceUrl) {
    const response = await factoryFetch(`/api/collector/duplicates?source_url=${encodeURIComponent(sourceUrl)}`);
    if (!response.ok)
        return { exists: false };
    return response.json();
}
async function postCapture(capture, allowNewVersion = false) {
    const body = { ...capture };
    if (allowNewVersion)
        body.allow_new_version = true;
    const response = await factoryFetch(`/api/collector/products${allowNewVersion ? '?allow_new_version=true' : ''}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok) {
        const error = new Error(result.detail ? JSON.stringify(result.detail) : `HTTP ${response.status}`);
        error.status = response.status;
        error.body = result;
        throw error;
    }
    return result;
}
async function postOzonReferencePage(capture) {
    const response = await factoryFetch("/api/collector/ozon-reference-page", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(capture)
    });
    const result = await response.json();
    if (!response.ok) {
        const error = new Error(result.detail?.message || result.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.body = result;
        throw error;
    }
    return result;
}
async function captureCurrentProduct(allowNewVersion = false) {
    if (activePageKind === "ozon") {
        await captureCurrentOzonReference();
        return;
    }
    els.capture.disabled = true;
    els.duplicate.hidden = true;
    els.progress.textContent = "读取页面数据...";
    try {
        const tab = await getActiveTab();
        const capture = await sendToTab(tab.id, { type: "COLLECTOR_CAPTURE" });
        if (!capture?.is_collectable) throw new Error(capture?.reason || '商品信息未加载，无法采集');
        showSkuImageWarning(capture);
        if (!allowNewVersion) {
            const duplicate = await checkDuplicate(capture.source_url);
            if (duplicate.exists) {
                duplicateProductId = duplicate.product_id;
                els.progress.textContent = "该产品已经采集过。";
                els.duplicateMessage.textContent = `已有商品：${duplicate.product_id}`;
                els.duplicate.hidden = false;
                setResult(duplicate);
                return;
            }
        }
        els.progress.textContent = `正在保存全部 ${capture.skus?.length || 0} 个规格和图片，请保持弹窗打开…`;
        const result = await postCapture({ ...capture, collection_mode: 'all_skus' }, allowNewVersion);
        els.progress.textContent = `采集完成：${result.counts?.skus || 0} 个规格，请到工作台选择上架规格`;
        setResult(result);
        await loadFactoryConfig();
        chrome.tabs.create({ url: workbenchEntryUrl('1688', { product_id: result.product_id }), active: true });
    }
    catch (error) {
        els.progress.textContent = "采集失败";
        setResult(error.message);
    }
    finally {
        els.capture.disabled = false;
    }
}
async function captureCurrentOzonReference() {
    els.capture.disabled = true;
    els.duplicate.hidden = true;
    els.progress.textContent = "正在读取当前Ozon页面...";
    try {
        const tab = await getActiveTab();
        const capture = await sendToTab(tab.id, { type: "COLLECTOR_OZON_CAPTURE" });
        if (!capture?.is_collectable)
            throw new Error(capture?.reason || "当前页面不是Ozon商品页");
        els.progress.textContent = "正在提交到共享工作台...";
        const result = await postOzonReferencePage(capture);
        els.progress.textContent = result.status === "waiting_ai_design"
            ? "Ozon参考页已采集，已进入AI商品卡生成"
            : "Ozon参考页已采集，正在打开工作台";
        setResult(result);
        await loadFactoryConfig();
        chrome.tabs.create({ url: workbenchEntryUrl("ozon", { task_id: result.task?.task_id }), active: true });
    }
    catch (error) {
        els.progress.textContent = "Ozon参考页采集失败";
        setResult(error.message);
    }
    finally {
        els.capture.disabled = false;
    }
}
els.capture.addEventListener("click", () => captureCurrentProduct(false));
els.captureMarket.addEventListener("click", async () => {
    if (marketCaptureRunning)
        return;
    if (chainJob?.status === 'running') return;
    if (chainEls.auto?.checked) {
        els.captureMarket.disabled = true;
        try {
            await verifyAndSaveMarketToken();
            const tab = await getActiveTab();
            const job = await chainMessage('SEERFAR_CHAIN_START', { tabId: tab.id, period: els.marketPeriod.value,
                maxPages: Number(els.marketMaxPages.value), keywordPages: Number(chainEls.pages.value),
                maxCategories: Number(chainEls.categories.value) });
            showChainJob(job);
            els.progress.textContent = '类目联动采集已启动，可关闭弹窗；重开可查看进度或停止';
        } catch (error) {
            setResult({ error: safeMarketTokenError(error) });
            els.captureMarket.disabled = false;
        }
        return;
    }
    marketCaptureRunning = true;
    marketStopRequested = false;
    els.captureMarket.disabled = true;
    els.stopMarket.disabled = false;
    let pages = 0;
    let received = 0;
    let inserted = 0;
    let verifiedToken = "";
    els.saveMarketToken.disabled = true;
    els.clearMarketToken.disabled = true;
    try {
        const maxPages = Number(els.marketMaxPages.value);
        if (!Number.isInteger(maxPages) || maxPages < 1 || maxPages > 20)
            throw new Error("本次采集页数须在 1–20 之间");
        const token = await verifyAndSaveMarketToken();
        verifiedToken = token;
        const tab = await getActiveTab();
        if (!/^https:\/\/(?:www\.)?seerfar\.cn\//.test(tab?.url || ""))
            throw new Error("当前不是 Seerfar 页面");
        let snapshot = await sendToTab(tab.id, { type: "SEERFAR_MARKET_CAPTURE" });
        if (!snapshot?.records?.length)
            throw new Error(snapshot?.reason || "当前页面没有识别到类目、关键词或商品报表表格");
        const dataset = snapshot.dataset;
        if (dataset === 'products' && !['rolling_30d', 'calendar_month'].includes(productPeriodKind?.value))
            throw new Error('请按商品报表实际选择最近 30 天或完整自然月，不能混用销量口径');
        const rolling = dataset === 'products' ? productPeriodKind.value === 'rolling_30d' : isRollingMarketPage(tab.url);
        const period = rolling ? marketCaptureMonth() : els.marketPeriod.value;
        if (!period) throw new Error("请填写报表实际所属自然月");
        els.marketCaptureMonth.textContent = `${period}（按本机时间自动生成）`;
        const seen = new Set();
        let stopReason = "已到本次页数上限";
        for (let page = 1; page <= maxPages; page++) {
            if (marketStopRequested) {
                stopReason = "用户已停止";
                break;
            }
            if (snapshot.dataset !== dataset)
                throw new Error("翻页后报表类型变化，已停止避免混合入库");
            const unique = uniqueMarketRecords(snapshot.records, seen);
            if (!unique.length)
                throw new Error("下一页与已采集数据完全重复，已停止避免循环");
            const chunks = marketRecordChunks(unique);
            for (let part = 0; part < chunks.length; part++) {
                els.progress.textContent = `正在提交第 ${page}/${maxPages} 页，第 ${part + 1}/${chunks.length} 批…`;
                const response = await factoryFetch("/api/collector/market-snapshots", {
                    method: "POST",
                    headers: { "Content-Type": "application/json", "X-Market-Ingest-Token": token },
                    body: JSON.stringify({ ...snapshot, records: chunks[part], source: "seerfar",
                        capture_method: "browser_extension", period,
                        ...(rolling ? { period_kind: "rolling_30d" } : {}) })
                });
                const result = await response.json();
                if (!response.ok)
                    throw new Error(result.detail?.message || result.detail || `HTTP ${response.status}`);
                received += chunks[part].length;
                inserted += Number(result.inserted || 0);
            }
            pages += 1;
            els.progress.textContent = `已完成 ${pages} 页，接收 ${received} 行，新入库 ${inserted} 行`;
            if (page === maxPages || marketStopRequested) {
                if (marketStopRequested)
                    stopReason = "用户已停止";
                break;
            }
            const next = await sendToTab(tab.id, { type: "SEERFAR_MARKET_NEXT_PAGE" });
            if (next?.done || next?.stopped) {
                stopReason = next.reason || "已到最后一页";
                break;
            }
            if (!next?.advanced || !next.snapshot?.records?.length)
                throw new Error(next?.error || "翻页后未取得有效数据，已停止");
            snapshot = next.snapshot;
        }
        els.progress.textContent = `采集结束：${pages} 页，接收 ${received} 行，新入库 ${inserted} 行；${stopReason}`;
        setResult({ pages, received, inserted, stop_reason: stopReason });
    }
    catch (error) {
        const safeError = safeMarketTokenError(error, verifiedToken);
        els.progress.textContent = received
            ? `采集中断：已提交 ${received} 行、新入库 ${inserted} 行；后续页面未处理`
            : "市场报表入库失败";
        setResult({ error: safeError, pages, received, inserted });
    }
    finally {
        marketCaptureRunning = false;
        els.captureMarket.disabled = false;
        els.stopMarket.disabled = true;
        els.saveMarketToken.disabled = false;
        els.clearMarketToken.disabled = false;
    }
});
els.saveMarketToken.addEventListener("click", async () => {
    els.saveMarketToken.disabled = true;
    els.clearMarketToken.disabled = true;
    try {
        await verifyAndSaveMarketToken();
    }
    catch (error) {
        els.marketTokenStatus.textContent = safeMarketTokenError(error);
    }
    finally {
        els.saveMarketToken.disabled = false;
        els.clearMarketToken.disabled = false;
    }
});
els.clearMarketToken.addEventListener("click", async () => {
    els.saveMarketToken.disabled = true;
    els.clearMarketToken.disabled = true;
    try {
        await chrome.storage.local.remove("marketIngestToken");
        els.marketToken.value = "";
        els.marketTokenStatus.textContent = "本机保存的令牌已清除";
    }
    catch {
        els.marketTokenStatus.textContent = "清除失败，请重试";
    }
    finally {
        els.saveMarketToken.disabled = false;
        els.clearMarketToken.disabled = false;
    }
});
els.stopMarket.addEventListener("click", async () => {
    if (chainJob?.status === 'running') {
        try { await chainMessage('SEERFAR_CHAIN_STOP'); els.progress.textContent = '正在停止后台任务，已入库数据保留'; }
        catch (error) { setResult({ error: safeMarketTokenError(error) }); }
        return;
    }
    marketStopRequested = true;
    els.stopMarket.disabled = true;
    els.progress.textContent = "正在停止；当前页提交完成后结束…";
    try {
        const tab = await getActiveTab();
        if (tab?.id)
            await sendToTab(tab.id, { type: "SEERFAR_MARKET_STOP" });
    }
    catch {
        // The current tab may have navigated; popup loop still stops after its pending request.
    }
});
chainEls.resume?.addEventListener('click', async () => {
    try { await verifyAndSaveMarketToken(); showChainJob(await chainMessage('SEERFAR_CHAIN_RESUME')); }
    catch (error) { setResult({ error: safeMarketTokenError(error) }); }
});
void refreshChainJob();
globalThis.setInterval?.(refreshChainJob, 1800);
els.previewToggle.addEventListener("click", () => {
    els.preview.hidden = !els.preview.hidden;
});
els.debugExport.addEventListener("click", exportSkuDebug);
els.openInbox.addEventListener("click", async () => {
    try {
        await loadFactoryConfig();
        const target = activePageKind === "ozon"
            ? workbenchEntryUrl("ozon")
            : workbenchEntryUrl("1688");
        chrome.tabs.create({ url: target });
    }
    catch (error) {
        els.connectionResult.textContent = `工作台地址无效：${error.message}`;
    }
});
els.openExisting.addEventListener("click", () => {
    if (duplicateProductId) {
        els.progress.textContent = `已有商品目录：products/${duplicateProductId}`;
        setResult({ existing_product_id: duplicateProductId, path: `products/${duplicateProductId}` });
    }
});
els.createVersion.addEventListener("click", () => captureCurrentProduct(true));
async function saveConnection() {
    try {
        const text = cleanFactoryUrlText(els.factoryUrl.value);
        const access = parseFactoryUrl(text);
        await ensureWorkbenchHostPermission(access.origin);
        await chrome.storage.local.set({ factoryBaseUrl: text });
        factoryConfig = { baseUrl: access.origin, authHeader: access.authHeader, deviceId: await ensureFactoryDeviceId() };
        els.connectionResult.textContent = "工作台地址已保存";
        await testConnection();
    }
    catch (error) {
        els.connectionResult.textContent = error.message;
    }
}
async function testConnection() {
    els.connectionResult.textContent = "正在自动识别并连接主电脑...";
    try {
        const response = await factoryFetch("/api/workbench/summary");
        const result = await response.json().catch(() => ({}));
        if (!response.ok)
            throw new Error(result.detail?.message || result.detail || `HTTP ${response.status}`);
        els.connectionResult.textContent = "已连接共享工作台（无需访问码）";
    }
    catch (error) {
        els.connectionResult.textContent = `连接失败：${error.message}`;
    }
}
els.saveConnection.addEventListener("click", saveConnection);
els.testConnection.addEventListener("click", testConnection);
async function initialize() {
    await loadFactoryConfig();
    const stored = await chrome.storage.local.get(["factoryBaseUrl"]);
    els.factoryUrl.value = cleanFactoryUrlText(stored.factoryBaseUrl) || factoryConfig.baseUrl;
    const marketSettings = await chrome.storage.local.get(["marketIngestToken"]);
    els.marketToken.value = marketSettings.marketIngestToken || "";
    if (marketSettings.marketIngestToken)
        els.marketTokenStatus.textContent = "已从本机浏览器读取令牌；采集前会重新验证";
    await loadPreview();
}
initialize().catch((error) => {
    els.status.textContent = "插件初始化失败";
    setResult(error.message);
});
