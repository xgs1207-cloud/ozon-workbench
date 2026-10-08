/* In-page media inspection. Opening a preview never generates or edits media. */
function listingPreviewUrl(value) {
    try {
        const url = new URL(value, location.href);
        return url.origin === location.origin &&
            /^\/api\/workbench\/products\/P\d{6}\/media\/.+\.(?:png|jpe?g|webp|gif|avif)$/i.test(url.pathname)
            ? url.href : '';
    } catch { return ''; }
}
(function () {
    let viewer, opener, previousOverflow, currentImage, previewScope;
    const mediaRegions = '.studio-reference-grid, .studio-result, .product-source-images, .studio-slot-editor';

    function releasePreview() {
        if (previousOverflow !== undefined) document.body.style.overflow = previousOverflow;
        previousOverflow = undefined;
        currentImage?.remove();
        currentImage = null;
        if (opener?.isConnected) opener.focus({preventScroll: true});
        opener = null;
    }
    function closePreview() {
        if (viewer?.open) viewer.close();
        releasePreview();
    }

    function ensureViewer() {
        if (viewer) return viewer;
        viewer = document.createElement('dialog');
        viewer.className = 'listing-image-preview';
        viewer.setAttribute('aria-labelledby', 'listingPreviewCaption');
        viewer.innerHTML = `<header><h2 id="listingPreviewCaption">图片预览</h2>
            <button type="button" class="listing-preview-close" aria-label="关闭图片预览">关闭 <span aria-hidden="true">×</span></button></header>
            <div class="listing-preview-stage"><p class="listing-preview-status" role="status">正在加载图片…</p></div>`;
        document.body.append(viewer);
        viewer.querySelector('button').addEventListener('click', closePreview);
        viewer.addEventListener('cancel', event => { event.preventDefault(); closePreview(); });
        viewer.addEventListener('click', event => {
            const box = viewer.getBoundingClientRect();
            if (event.target === viewer && (event.clientX < box.left || event.clientX > box.right ||
                event.clientY < box.top || event.clientY > box.bottom)) closePreview();
        });
        viewer.addEventListener('close', () => {
            // Native close events are queued: never tear down an already reopened viewer.
            if (!viewer.open) releasePreview();
        });
        return viewer;
    }

    function openPreview(url, label, trigger) {
        const dialog = ensureViewer();
        if (dialog.open) closePreview();
        const stage = dialog.querySelector('.listing-preview-stage');
        const status = stage.querySelector('.listing-preview-status');
        dialog.querySelector('h2').textContent = label || '图片预览';
        status.hidden = false;
        status.textContent = '正在加载图片…';
        const img = document.createElement('img');
        img.alt = label || '商品图片';
        img.draggable = false;
        currentImage = img;
        img.addEventListener('load', () => { if (currentImage === img) status.hidden = true; });
        img.addEventListener('error', () => {
            if (currentImage !== img) return;
            img.hidden = true;
            status.textContent = '图片暂时无法加载，请关闭后重试。';
        });
        opener = trigger;
        previewScope = {product:state.product,step:flowStep(),view:state.view};
        previousOverflow = document.body.style.overflow;
        stage.append(img);
        img.src = url;
        document.body.style.overflow = 'hidden';
        dialog.showModal();
        dialog.querySelector('button').focus();
    }

    function previewTarget(target) {
        if (!target?.closest || !target.closest('#main') || !target.closest(mediaRegions)) return null;
        const link = target.closest('a[href]');
        const img = target.closest('img');
        const url = listingPreviewUrl(link?.getAttribute('href') || img?.getAttribute('src'));
        if (!url) return null;
        const nearbyImage = img || link?.querySelector('img') || link?.parentElement?.querySelector('img');
        return {url, label: nearbyImage?.alt || '商品图片', trigger: link || img?.closest('.studio-reference')?.parentElement?.querySelector('.studio-reference-preview') || img};
    }

    function decorateLinks() {
        for (const link of document.querySelectorAll('#main a[href]')) {
            if (!link.closest(mediaRegions) || !listingPreviewUrl(link.getAttribute('href'))) continue;
            link.removeAttribute('target');
            link.title = '点击放大预览';
        }
    }
    // Capture also prevents a reference-image click from toggling its checkbox.
    document.addEventListener('click', event => {
        const item = previewTarget(event.target);
        if (!item) return;
        event.preventDefault();
        event.stopPropagation();
        openPreview(item.url, item.label, item.trigger);
    }, true);
    document.addEventListener('auxclick', event => {
        if (previewTarget(event.target)) event.preventDefault();
    }, true);
    const render = renderProduct;
    renderProduct = function (...args) {
        if (viewer?.open && (previewScope.product !== state.product || previewScope.step !== flowStep() || previewScope.view !== state.view)) closePreview();
        const result = render(...args);
        decorateLinks();
        return result;
    };
    const main = document.querySelector('#main');
    if (main) new MutationObserver(decorateLinks).observe(main, {childList: true, subtree: true});
    decorateLinks();
})();
