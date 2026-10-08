const DEFAULT_FACTORY_URL = "http://43.132.190.110:8088";
const COMMAND_CENTER_QUERY_VERSION = "2026-08-01-ui-state-v1";
// 旧的本机/局域网地址：检测到这些旧配置时回退到新的公网默认地址
const LEGACY_LOCAL_FACTORY_URLS = new Set([
    "http://127.0.0.1:8765",
    "http://localhost:8765"
]);
const OZON_IMAGE_HOST_SUFFIXES = ["ozone.ru", "ozon.ru", "ozonusercontent.com"];
let captureJobStorageLock = Promise.resolve();
function storeCaptureJob(record) {
    const pending = captureJobStorageLock.then(async () => {
        const stored = await chrome.storage.local.get(['collectorCaptureJobs']);
        const jobs = Array.isArray(stored.collectorCaptureJobs) ? stored.collectorCaptureJobs : [];
        const old = jobs.find(job => job.request_id === record.request_id);
        const merged = { ...old, ...record };
        await chrome.storage.local.set({ collectorCaptureJobs: [merged,
            ...jobs.filter(job => job.request_id !== record.request_id)].slice(0, 20) });
    });
    captureJobStorageLock = pending.catch(() => {});
    return pending;
}
async function ensureFactoryDeviceId() {
    const stored = await chrome.storage.local.get(['factoryDeviceId']);
    let deviceId = String(stored.factoryDeviceId || '').trim();
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
// 返回 { origin, authHeader }；旧/空配置时把默认地址写回存储（用户填写的完整 URL 含认证信息原样保留）
async function loadFactoryAccess() {
    const stored = await chrome.storage.local.get(['factoryBaseUrl']);
    const text = factoryUrlOrDefault(stored.factoryBaseUrl);
    const access = parseFactoryUrl(text);
    if (cleanFactoryUrlText(stored.factoryBaseUrl) !== text) {
        await chrome.storage.local.set({ factoryBaseUrl: text });
    }
    return access;
}
async function loadFactoryBaseUrl() {
    return (await loadFactoryAccess()).origin;
}
function commandCenterUrl(baseUrl, taskCenter, extra = {}) {
    const params = new URLSearchParams({ v: COMMAND_CENTER_QUERY_VERSION });
    if (extra.product_id)
        params.set("product_id", String(extra.product_id));
    if (extra.task_id)
        params.set("task_id", String(extra.task_id));
    const path = taskCenter === "reference"
        ? "/ozon-reference"
        : taskCenter === "inbox"
            ? "/1688-collection"
            : "/command-center";
    if (taskCenter && path === "/command-center")
        params.set("task_center", taskCenter);
    return `${baseUrl}${path}?${params.toString()}`;
}
function isAllowedOzonImageUrl(value) {
    try {
        const url = new URL(String(value || ""));
        const host = url.hostname.toLowerCase();
        return url.protocol === "https:" && OZON_IMAGE_HOST_SUFFIXES.some((suffix) => host === suffix || host.endsWith(`.${suffix}`));
    }
    catch {
        return false;
    }
}
function arrayBufferToBase64(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    const chunkSize = 0x8000;
    for (let index = 0; index < bytes.length; index += chunkSize) {
        binary += String.fromCharCode(...bytes.subarray(index, index + chunkSize));
    }
    return btoa(binary);
}
chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message?.type === 'FACTORY_FETCH_IMAGE_DATA_URL') {
        (async () => {
            try {
                const url = String(message.url || "");
                if (!isAllowedOzonImageUrl(url))
                    throw new Error("图片地址不属于 Ozon 图片域名");
                const response = await fetch(url, {
                    headers: {
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"
                    },
                    credentials: "omit",
                });
                if (!response.ok)
                    throw new Error(`图片读取失败 HTTP ${response.status}`);
                const contentType = String(response.headers.get("Content-Type") || "image/jpeg").split(";")[0].trim();
                if (!contentType.startsWith("image/"))
                    throw new Error("返回内容不是图片");
                const buffer = await response.arrayBuffer();
                if (buffer.byteLength > 8 * 1024 * 1024)
                    throw new Error("图片超过8MB");
                sendResponse({
                    ok: true,
                    url,
                    content_type: contentType,
                    byte_size: buffer.byteLength,
                    data_url: `data:${contentType};base64,${arrayBufferToBase64(buffer)}`,
                });
            }
            catch (error) {
                sendResponse({ ok: false, status: 0, error: error?.message || '图片读取失败' });
            }
        })();
        return true;
    }
    if (message?.type === 'FACTORY_OPEN_COMMAND_CENTER') {
        (async () => {
            try {
                const baseUrl = await loadFactoryBaseUrl();
                await chrome.tabs.create({
                    url: commandCenterUrl(baseUrl, message.task_center || "all", {
                        product_id: message.product_id,
                        task_id: message.task_id,
                    }),
                    active: true,
                });
                sendResponse({ ok: true });
            }
            catch (error) {
                sendResponse({ ok: false, status: 0, error: error?.message || '工作台打开失败' });
            }
        })();
        return true;
    }
    if (message?.type !== 'FACTORY_FETCH')
        return undefined;
    (async () => {
        let captureJob;
        try {
            const path = String(message.path || '');
            const allowedWorkbenchPath = path.startsWith('/api/workbench/market-intelligence/search-visibility/seerfar/');
            if (!path.startsWith('/api/collector/') && !allowedWorkbenchPath)
                throw new Error('无效的工作台接口');
            const access = await loadFactoryAccess();
            if (path === '/api/collector/jobs' && message.options?.method === 'POST') {
                const payload = JSON.parse(message.options.body || '{}');
                captureJob = { request_id: payload.request_id, title: payload.capture?.title_cn || '1688商品',
                    source_url: payload.capture?.source_url, base_url: access.origin, state: 'unconfirmed' };
                await storeCaptureJob(captureJob);
            }
            const headers = { ...(message.options?.headers || {}) };
            headers['X-Factory-Device-Id'] = await ensureFactoryDeviceId();
            if (access.authHeader)
                headers['Authorization'] = access.authHeader;
            const response = await fetch(`${access.origin}${path}`, {
                method: message.options?.method || 'GET',
                headers,
                body: message.options?.body
            });
            const body = await response.text();
            if (captureJob) {
                let result;
                try { result = JSON.parse(body); } catch { /* not an acknowledged job */ }
                await storeCaptureJob({ ...captureJob, ...(response.ok && result?.request_id === captureJob.request_id
                    ? result : { state: response.status >= 400 ? 'failed' : 'unconfirmed',
                        error: '后台未确认采集任务，请核对商品列表后重试' }) });
            }
            if (!captureJob && response.ok && /^\/api\/collector\/jobs\/[a-zA-Z0-9_-]{16,80}$/.test(path)) {
                let result;
                try { result = JSON.parse(body); } catch { /* not a job state */ }
                if (result?.request_id === path.split('/').pop())
                    await storeCaptureJob({ ...result, base_url: access.origin });
            }
            sendResponse({ ok: true, status: response.status, body });
        }
        catch (error) {
            // The snapshot may already have reached the server. Never retry a
            // POST blindly; re-open the popup and query this exact request ID.
            if (captureJob) await storeCaptureJob({ ...captureJob, state: 'unconfirmed' }).catch(() => {});
            sendResponse({ ok: false, status: 0, error: error?.message || '工作台连接失败' });
        }
    })();
    return true;
});
importScripts('seerfar-bridge.js', 'market-jobs.js');
