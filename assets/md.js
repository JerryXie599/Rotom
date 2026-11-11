/* md.js — 控制台自包含 markdown 渲染器(零依赖,服务本地 /assets/md.js)。
 * 覆盖档案里实际出现的语法:标题/加粗/斜体/行内码/表格/列表(单层+缩进续行)/引用/分隔线/代码围栏/链接。
 * 先整体 HTML 转义再渲染,不执行原文里的任何 HTML。 */
(function () {
  'use strict';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }

  function inline(s) {
    return esc(s)
      .replace(/`([^`]+)`/g, function (_, c) { return '<code>' + c + '</code>'; })
      .replace(/\*\*([^*]+)\*\*/g, function (_, c) { return '<b>' + c + '</b>'; })
      .replace(/(^|[\s(])\*([^*\n]+)\*/g, function (_, p, c) { return p + '<i>' + c + '</i>'; })
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_, t, u) {
        var safe = /^(https?:|\/|#)/i.test(u) ? u : '#';
        return '<a href="' + esc(safe) + '" target="_blank" rel="noopener">' + t + '</a>';
      })
      .replace(/\*\*/g, '')
      .replace(/\n/g, '<br>');
  }

  function parseRow(row) {
    return row.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(function (c) { return c.trim(); });
  }

  function isTableRow(l) { return /^\s*\|/.test(l); }
  function isTableSep(l) { return /^\s*\|[\s:|-]+\|?\s*$/.test(l) && /-/.test(l); }
  function isUl(l) { return /^\s*[-*]\s+/.test(l); }
  function isOl(l) { return /^\s*\d+[.、)]\s*/.test(l); }
  function isQuote(l) { return /^\s*>/.test(l); }
  function isFence(l) { return /^\s*```/.test(l); }
  function isHr(l) { return /^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l); }
  function headingOf(l) { var m = l.match(/^(#{1,5})\s+(.*)$/); return m ? m : null; }

  function render(src) {
    if (!src) return '';
    var lines = String(src).replace(/\r/g, '').split('\n');
    var out = [];
    var i = 0;
    while (i < lines.length) {
      var L = lines[i];
      if (/^\s*$/.test(L)) { i++; continue; }
      var h = headingOf(L);
      if (h) {
        var n = Math.min(h[1].length + 1, 6);
        out.push('<h' + n + ' class="md-h">' + inline(h[2]) + '</h' + n + '>');
        i++; continue;
      }
      if (isHr(L)) { out.push('<hr class="md-hr">'); i++; continue; }
      if (isFence(L)) {
        i++; var buf = [];
        while (i < lines.length && !isFence(lines[i])) { buf.push(lines[i]); i++; }
        i++;
        out.push('<pre class="md-pre">' + esc(buf.join('\n')) + '</pre>');
        continue;
      }
      if (isTableRow(L) && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        var head = parseRow(L);
        i += 2;
        var rows = '';
        while (i < lines.length && isTableRow(lines[i])) {
          rows += '<tr>' + parseRow(lines[i]).map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
          i++;
        }
        out.push('<div class="md-tbl"><table><thead><tr>' +
          head.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') +
          '</tr></thead><tbody>' + rows + '</tbody></table></div>');
        continue;
      }
      if (isQuote(L)) {
        var q = [];
        while (i < lines.length && isQuote(lines[i])) { q.push(lines[i].replace(/^\s*>\s?/, '')); i++; }
        out.push('<blockquote class="md-quote">' + render(q.join('\n')) + '</blockquote>');
        continue;
      }
      if (isUl(L) || isOl(L)) {
        var ordered = isOl(L) && !isUl(L);
        var items = [];
        while (i < lines.length && (isUl(lines[i]) || isOl(lines[i]))) {
          var txt = lines[i].replace(/^\s*([-*]|\d+[.、)])\s+/, '');
          i++;
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !isUl(lines[i]) && !isOl(lines[i])) {
            txt += ' ' + lines[i].trim(); i++;
          }
          items.push('<li>' + inline(txt) + '</li>');
        }
        out.push(ordered ? '<ol>' + items.join('') + '</ol>' : '<ul>' + items.join('') + '</ul>');
        continue;
      }
      var para = [L]; i++;
      while (i < lines.length && !/^\s*$/.test(lines[i]) && !headingOf(lines[i]) &&
             !isTableRow(lines[i]) && !isUl(lines[i]) && !isOl(lines[i]) &&
             !isQuote(lines[i]) && !isFence(lines[i]) && !isHr(lines[i])) {
        para.push(lines[i]); i++;
      }
      out.push('<p class="md-p">' + inline(para.join('\n')) + '</p>');
    }
    return out.join('');
  }

  window.mdRender = render;
})();
