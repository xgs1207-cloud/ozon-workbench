/* Seerfar 关键词采集适配层（浏览器控制台 / 油猴脚本 / 扩展 content script 均可运行）
 *
 * 作用：把 Seerfar 关键词表格里当前可见的行抓成我们关键词库的入库载荷，POST 到本地工作台。
 *
 * ⚠️ 使用前必读
 * 1. 这是"旁路"链路：Seerfar 是第三方站点，DOM 一改版选择器就会失效，
 *    所以不要让它成为主流程的硬依赖；抓不到就在界面里手动补/用 CSV 导入。
 * 2. 只抓你自己账号有权查看的页面数据，并自行确认符合该站点的使用条款。
 * 3. 类目维度以 **Ozon 真实 category_id / type_id** 为准；Seerfar 的类目名只用于展示，
 *    请先把 Ozon 类目填进 WB.categoryId / WB.typeId（或运行时传入）。
 *
 * 用法：
 *   const rows = await window.ozonWb.collect();          // 抓当前页
 *   await window.ozonWb.push(rows);                      // 直接入库
 *   await window.ozonWb.collectAndPush();                // 一步到位
 */
(function () {
  "use strict";

  const WB = {
    endpoint: "http://127.0.0.1:8766/api/keywords/ingest", // 关键词库服务（不是 8765 那个工作台）
    source: "seerfar",
    categoryId: "", // 必填：Ozon category_id
    typeId: "",     // 必填：Ozon type_id
    categoryPathZh: "",
    // 指标列名候选（按表头文本匹配，命中即用；不区分大小写、允许包含关系）
    columnHints: {
      keyword: ["keyword", "关键词", "ключев", "запрос", "phrase", "фраза"],
      searchVolume: ["search volume", "searches", "搜索量", "показы", "частота", "volume"],
      competitorCount: ["competition", "competitors", "竞争", "конкурент", "products"],
      adsCount: ["ads", "广告", "реклам"],
      cpc: ["cpc", "bid", "点击", "ставка"],
      trend: ["trend", "增长", "динамик"],
    },
    // DOM 选择器（优先用 table 结构，命中不到再退回 columnHints 猜列）
    selectors: {
      table: "table",
      row: "tbody tr",
      cell: "td",
      headerCell: "thead th",
      keyword: "", // 可留空：留空时用 columnHints.keyword 在该行里找第一个像关键词的单元格
    },
    maxRowsPerPush: 500,
  };

  const numericCleanup = (text) => {
    if (!text) return null;
    const normalized = String(text)
      .replace(/\u00a0/g, " ")
      .replace(/[,\s](?=\d{3}\b)/g, "")
      .replace(/[^\d.\-]/g, "");
    if (!normalized) return null;
    const value = Number(normalized);
    return Number.isFinite(value) ? value : null;
  };

  const isLikelyKeyword = (text) => {
    if (!text) return false;
    const value = String(text).trim();
    if (value.length < 2 || value.length > 200) return false;
    // 纯数字/纯符号不算关键词（往往是排名或图表列）
    return /[A-Za-z\u0400-\u04FF\u4e00-\u9fff]/.test(value) && !/^\d+([.,]\d+)?$/.test(value);
  };

  const findColumnIndexes = (headers) => {
    const lowered = headers.map((header) => String(header || "").trim().toLowerCase());
    const found = {};
    for (const [field, hints] of Object.entries(WB.columnHints)) {
      found[field] = lowered.findIndex((header) =>
        header && hints.some((hint) => header.includes(String(hint).toLowerCase()))
      );
    }
    return found;
  };

  const collectFromTable = (table) => {
    const headers = Array.from(table.querySelectorAll(WB.selectors.headerCell)).map((cell) =>
      cell.textContent.trim()
    );
    const columns = findColumnIndexes(headers);
    const rows = Array.from(table.querySelectorAll(WB.selectors.row));
    const out = [];
    for (const row of rows) {
      const cells = Array.from(row.querySelectorAll(WB.selectors.cell)).map((cell) =>
        cell.textContent.trim()
      );
      if (!cells.length) continue;

      let keyword = columns.keyword >= 0 ? cells[columns.keyword] : "";
      if (!keyword) keyword = cells.find(isLikelyKeyword) || "";
      if (!isLikelyKeyword(keyword)) continue;

      out.push({
        keyword,
        search_volume: columns.searchVolume >= 0 ? numericCleanup(cells[columns.searchVolume]) : null,
        competitor_count:
          columns.competitorCount >= 0 ? numericCleanup(cells[columns.competitorCount]) : null,
        ads_count: columns.adsCount >= 0 ? numericCleanup(cells[columns.adsCount]) : null,
        cpc: columns.cpc >= 0 ? numericCleanup(cells[columns.cpc]) : null,
        trend: columns.trend >= 0 ? numericCleanup(cells[columns.trend]) : null,
        extra: { table_headers: headers, raw_cells: cells },
      });
    }
    return out;
  };

  const collect = async (options = {}) => {
    const config = { ...WB, ...options };
    const tables = Array.from(document.querySelectorAll(config.selectors.table));
    if (!tables.length) {
      console.warn("[ozon-wb] 没找到表格，请把 WB.selectors.table 改成实际容器选择器");
      return [];
    }
    const byKeyword = new Map();
    for (const table of tables) {
      for (const item of collectFromTable(table)) {
        const key = item.keyword.trim().toLowerCase();
        const previous = byKeyword.get(key);
        // 同一关键词出现多次时保留指标更全的一条
        const score = (row) =>
          [row.search_volume, row.competitor_count, row.ads_count].filter((v) => v !== null).length;
        if (!previous || score(item) > score(previous)) byKeyword.set(key, item);
      }
    }
    return Array.from(byKeyword.values());
  };

  const push = async (rows, options = {}) => {
    const config = { ...WB, ...options };
    if (!config.categoryId || !config.typeId) {
      throw new Error("[ozon-wb] 缺少 Ozon category_id / type_id，请先设置 WB.categoryId / WB.typeId");
    }
    if (!rows || !rows.length) return { ok: false, reason: "没有可推送的关键词" };
    const chunk = rows.slice(0, config.maxRowsPerPush);
    const response = await fetch(config.endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        source: config.source,
        category: {
          category_id: String(config.categoryId),
          type_id: String(config.typeId),
          category_path_zh: config.categoryPathZh || null,
        },
        keywords: chunk,
      }),
    });
    if (!response.ok) {
      const text = await response.text().catch(() => "");
      throw new Error(`[ozon-wb] 入库失败 ${response.status}: ${text.slice(0, 300)}`);
    }
    return await response.json();
  };

  const collectAndPush = async (options = {}) => push(await collect(options), options);

  window.ozonWb = { WB, collect, push, collectAndPush, collectFromTable, findColumnIndexes };
  console.log(
    "[ozon-wb] 已就绪。用法：ozonWb.WB.categoryId='<Ozon category_id>'; ozonWb.WB.typeId='<type_id>'; await ozonWb.collectAndPush();"
  );
})();
