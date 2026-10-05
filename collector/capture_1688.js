/* 1688 商品页抓取脚本（在商品详情页的开发者工具 Console 里粘贴运行，或存成书签）
 *
 * 作用：把当前 1688 商品页的「标题 / SKU / 主图 / 详情图 / SKU 图」整理成一个 JSON 并**下载**，
 * 交给本机脚本继续处理（两步都把数据留在你自己机器上）：
 *
 *   1) 本脚本 → 得到 capture-<offerId>.json
 *   2) python -m collector.fetch_images --json capture-<offerId>.json --out D:\capture\p1
 *   3) python -m collector.push_capture --folder D:\capture\p1 --keyword "..." --category-id ... --type-id ...
 *
 * 书签版（把下面整段压成一行，前面加 javascript:）：
 *   javascript:(()=>{ ...整段... })()
 *
 * ⚠️ 1688 的 DOM 会改版：脚本对每个字段都做了多选择器兜底，抓不到的字段会**明确告诉你**，
 *    而不是编一个值。抓不到就手工补齐（JSON 里字段名见下方 TEMPLATE）。
 */
(() => {
  const TEMPLATE = {
    source_url: "",
    title_zh: "",
    skus: [],
    images: { main: [], sku: [], detail: [] },
    keywords: [],
    note: "由 collector/capture_1688.js 生成；keywords 请填你在工作台选的词",
  };

  const abs = (url) => {
    if (!url) return "";
    try {
      return new URL(url, location.href).href;
    } catch (e) {
      return "";
    }
  };
  // 1688 的图片地址常带 .jpg_300x300.jpg 之类的后缀，去掉尺寸段拿原图
  const original = (url) => abs(url).replace(/_\d+x\d+(xz)?\.(jpg|png|webp)$/i, "").replace(/\.(jpg|png|webp)_\.(webp|jpg)$/i, ".$1");

  const pick = (selectors) => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      if (node) {
        const text = (node.textContent || "").trim();
        if (text) return text;
      }
    }
    return "";
  };

  const offerId = (() => {
    const match = location.href.match(/offer\/(\d+)/);
    return match ? match[1] : "";
  })();

  const result = JSON.parse(JSON.stringify(TEMPLATE));
  result.source_url = offerId ? `https://detail.1688.com/offer/${offerId}.html` : location.href;
  result.title_zh = pick([
    "h1.d-title",
    ".title-text",
    ".offer-title",
    "h1",
    "title",
  ]).replace(/\s*-\s*阿里巴巴.*$/, "");

  // ---- SKU：优先读页面的 SKU 表格（规格 × 价格），读不到就留空让用户补
  const skus = [];
  document.querySelectorAll("table tr").forEach((row) => {
    const cells = Array.from(row.querySelectorAll("td, th")).map((cell) => (cell.textContent || "").trim());
    if (cells.length >= 2) {
      const id = cells[0];
      const price = cells.find((text) => /^[0-9]+(\.[0-9]+)?$/.test(text));
      if (id && price && !/规格|颜色|价格/.test(id)) {
        skus.push({ sku_id: id.slice(0, 40), purchase_price_cny: Number(price), raw: cells.join(" | ").slice(0, 120) });
      }
    }
  });
  result.skus = skus.slice(0, 10); // 工作台规定：一次最多 10 个 SKU

  // ---- 图片：主图 / SKU 图 / 详情图
  const seen = new Set();
  const collect = (nodes) => {
    const urls = [];
    nodes.forEach((node) => {
      const raw = node.getAttribute("data-src") || node.getAttribute("data-lazy-src") || node.src || node.getAttribute("href") || "";
      const url = original(raw);
      if (url && /\.(jpg|jpeg|png|webp)/i.test(url) && !seen.has(url)) {
        seen.add(url);
        urls.push(url);
      }
    });
    return urls;
  };

  result.images.main = collect(
    document.querySelectorAll(".detail-gallery img, .tab-content-container img, .img-list img, [class*=gallery] img")
  ).filter((url) => !/detail|desc/i.test(url)).slice(0, 10);
  result.images.sku = collect(document.querySelectorAll("[class*=sku] img, [class*=prop] img")).slice(0, 20);
  result.images.detail = collect(
    document.querySelectorAll("[class*=detail] img, [class*=desc] img, #detail-content img")
  ).slice(0, 30);

  const missing = [];
  if (!result.title_zh) missing.push("title_zh");
  if (!result.skus.length) missing.push("skus");
  if (!result.images.main.length) missing.push("images.main");
  if (!result.images.detail.length) missing.push("images.detail");
  result.note = missing.length
    ? `⚠️ 这些字段没抓到，请手工补：${missing.join(", ")}（1688 改版时选择器需要调整）`
    : "字段抓取完整；keywords 请填工作台选好的俄文关键词";

  // ---- 下载 JSON
  const blob = new Blob([JSON.stringify(result, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `capture-${offerId || "unknown"}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();

  console.log("%c已导出 " + link.download, "color:#0a0;font-weight:bold");
  console.log(result);
  console.log(
    "%c下一步：python -m collector.fetch_images --json " +
      link.download +
      " --out D:\\capture\\p1",
    "color:#06c"
  );
  if (missing.length) console.warn("没抓到的字段：", missing);
  return result;
})();
