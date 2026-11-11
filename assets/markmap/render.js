/* render.js — 面板内直接用 markmap 官方 API 渲染导图(零依赖本地库,见 /assets/markmap/)。
 *   markmapInto(svgEl, markdownSrc) -> Promise
 * 用法就是 markmap 文档里的标准三步:Transformer.transform → Markmap.create → fit。
 * 节点文字走 foreignObject(库的固有实现),所以 SVG 必须在文档里、且容器有真实尺寸。 */
(function () {
  'use strict';

  function ready() {
    return !!(window.markmap && window.markmap.Transformer && window.markmap.Markmap);
  }

  function currentScale(svg) {
    try { return window.d3 ? window.d3.zoomTransform(svg).k : 1; } catch (e) { return 1; }
  }

  /* 大图 fit 会把整棵树缩到读不出字(实测 408 节点的档案导图只有 0.05 倍),
     所以适配后看一眼比例:太小就退回可读比例,让人自己用滚轮/按钮调。 */
  async function fitOrReadable(mm, svg) {
    try {
      await mm.fit();
      if (currentScale(svg) < 0.5) await mm.rescale(0.6);
    } catch (e) { /* 容器还没尺寸,交给 ResizeObserver 重试 */ }
  }

  window.markmapInto = function (svg, src) {
    if (!ready()) return Promise.reject(new Error('markmap 库没加载(检查 /assets/markmap/ 三个脚本)'));
    var transformer = new window.markmap.Transformer();
    var result = transformer.transform(String(src || ''));
    var fmOpts = (result.frontmatter && result.frontmatter.markmap) || {};
    /* 源文头里没写 maxWidth 的图(实测 r29 这类就没有 frontmatter),库的默认值是 9999px ——
       节点不换行、外层 div 直接撑到 9999px 宽,整棵树大到读不出内容。给个面板里合理的默认。 */
    if (fmOpts.maxWidth == null) fmOpts.maxWidth = 420;
    /* duration: 0 = 关掉淡入动画。库默认靠 d3 transition 把节点 opacity 从 0 拉到 1,
       而 transition 走 requestAnimationFrame —— 面板里切站点/刷新时这套动画会卡在中间,
       表现为"导图一片空白"(实测:节点都已生成、尺寸也算好了,就是 opacity 停在 0)。
       导图是给人看结构的,立即出现比淡入重要。 */
    fmOpts.duration = 0;
    var opts = window.markmap.deriveOptions ? window.markmap.deriveOptions(fmOpts) : fmOpts;
    // 标准用法:不传 options 时库自己注入样式;传了也保留 embedGlobalCSS 默认值
    var mm = window.markmap.Markmap.create(svg, opts ? Object.assign({}, opts) : undefined, result.root);
    return fitOrReadable(mm, svg).then(function () { return mm; });
  };

  window.markmapFit = function (mm, svg, mode) {
    return mode === 'full' ? mm.fit() : mm.rescale(1);
  };
  window.markmapReady = ready;
})();
