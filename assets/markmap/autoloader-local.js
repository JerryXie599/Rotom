/* autoloader-local.js — markmap 装载器的本地替身(零 CDN)。
 *
 * 背景:站点档案里的 `报告/思维导图.html` 是从 CDN 拉 markmap-autoloader 渲染的:
 *   <script src="https://cdn.jsdelivr.net/npm/markmap-autoloader@0.18"></script>
 * 现场机器不保证能出网,直接打开那个 html 就是白屏。这里把官方装载器的逻辑照搬成
 * 本地版(渲染流程一字不差:去公共缩进 → Transformer → Markmap.create → setData → fit),
 * 但库本身用同目录的 d3 / markmap-lib / markmap-view,不碰网络。
 *
 * 用法:先引 d3.js、markmap-lib.iife.js、markmap-view.js,再引本文件。
 * 页面结构("" .markmap"" 容器 + <script type="text/template"> 里的 markdown)与官方一致。 */
(function () {
  'use strict';

  /* 官方装载器在渲染前把库自带的 globalCSS 注进页面(所以 Markmap.create 才传
     embedGlobalCSS:false)。少了这一步,节点没有基础样式、尺寸量成 0,导图会被压成
     左侧一条细缝(实测过)。这里照做。 */
  function injectGlobalCSS() {
    if (document.getElementById('markmap-global-css')) return;
    var mk = window.markmap;
    if (!mk || !mk.globalCSS) return;
    var style = document.createElement('style');
    style.id = 'markmap-global-css';
    style.textContent = mk.globalCSS;
    // 装载器可能在 head 里就执行了,此时 body 还没有 —— 退到 documentElement,一样生效
    (document.body || document.documentElement).prepend(style);
  }

  function fail(el, e) {
    // 静默的空白导图最难查(这个项目已经踩过好几次):把错误直接写在页面上,别让它无声无息
    window.__markmapError = String((e && e.stack) || e);
    el.innerHTML = '<pre style="color:#f88;font:12px/1.6 Menlo,monospace;padding:16px;white-space:pre-wrap">'
      + '思维导图渲染失败:' + String((e && e.message) || e) + '</pre>';
    if (window.console) console.error('[mindmap]', e);
  }

  function render(el) {
    try { renderInto(el); } catch (e) { fail(el, e); }
  }

  function renderInto(el) {
    var mk = window.markmap;
    if (!mk || !mk.Transformer || !mk.Markmap) return;
    var text = el.textContent || '';
    var lines = text.split('\n');
    var indent = Infinity;
    lines.forEach(function (line) {
      var m = line.match(/^\s*/);
      var spaces = (m && m[0].length) || 0;
      if (spaces < line.length) indent = Math.min(indent, spaces);
    });
    if (!isFinite(indent)) indent = 0;
    var content = lines.map(function (l) { return l.slice(indent); }).join('\n').trim();

    /* 不传插件参数 = 用 markmap-lib 的默认插件集(含 frontmatter)。
       之前传了空数组 [] 想"避开联网插件",结果把 frontmatter 也关了 —— 源文头里的
       maxWidth: 420 失效,节点文字不换行、单个节点撑到 8000+px,整张图巨大,
       适配后缩到 0.08 倍、一个字都看不清(实测踩过)。
       这里只做 transform(不调 getAssets/loadJS),所以默认插件也不会去 CDN 取东西。 */
    var transformer = new mk.Transformer();
    el.innerHTML = '<svg></svg>';
    var svg = el.firstChild;
    var mm = mk.Markmap.create(svg, { embedGlobalCSS: false });

    var result = transformer.transform(content);
    var fmOpts = (result.frontmatter && result.frontmatter.markmap) || {};
    /* 与面板内渲染同一套修正(见 assets/markmap/render.js 的注释):
       ① 没有 frontmatter 的源文拿不到 maxWidth,库默认 9999px → 节点不换行、整图巨大
       ② 关掉淡入动画,免得 transition 卡在中间表现为空白 */
    if (fmOpts.maxWidth == null) fmOpts.maxWidth = 420;
    fmOpts.duration = 0;
    var opts = mk.deriveOptions ? mk.deriveOptions(fmOpts) : fmOpts;

    /* 「适应窗口」对几百节点的导图会把图缩到 0.05 倍 —— 全貌是看到了,字一个也读不了
       (实测 404 节点的档案导图就是这样)。所以:fit 完看比例,太小就改用可读比例打开,
       并给「适应窗口 / 100%」两个按钮让人自己切换。
       注意 fit() / rescale() 都是 async —— 不 await 就读比例,读到的是上一帧的值,
       判断会失效(实测踩过:比例已经是 0.056,却以为还 >= 0.5 就跳过了)。 */
    function currentScale() {
      var d3 = window.d3;
      try { return d3 ? d3.zoomTransform(svg).k : 1; } catch (e) { return 1; }
    }
    async function fitOrReadable() {
      try {
        await mm.fit();
        if (currentScale() < 0.5) await mm.rescale(0.6);
      } catch (e) { /* 尺寸为 0 时 fit 会失败,等 ResizeObserver 再来 */ }
    }
    mm.setData(result.root, opts).then(function () {
      return fitOrReadable();
    }).then(function () {
      el.appendChild(toolbar(mm, fitOrReadable));
    }, function (e) { fail(el, e); });

    // 容器尺寸变了(面板里 iframe 拉伸)要重新贴合,否则图会缩在角落
    var ro = new ResizeObserver(function () { fitOrReadable(); });
    ro.observe(el);
  }

  // 极简工具条:官方那套 markmap-toolbar 是另一个包,这里只要两个动作就够用
  function toolbar(mm, fitFn) {
    var box = document.createElement('div');
    box.className = 'markmap-local-tools';
    [['适应窗口', fitFn], ['100%', function () { mm.rescale(1); }]].forEach(function (pair) {
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = pair[0];
      b.addEventListener('click', function () { try { pair[1](); } catch (e) {} });
      box.appendChild(b);
    });
    return box;
  }

  function renderAll() {
    injectGlobalCSS();
    Array.prototype.forEach.call(document.querySelectorAll('.markmap'), render);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', renderAll);
  else renderAll();
})();