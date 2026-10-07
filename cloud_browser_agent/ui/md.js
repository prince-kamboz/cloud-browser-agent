// Small, safe markdown renderer for the agent's answers (HTML is escaped first). Same as v1.
const esc = (t) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
function inline(t) {
  const codes = [];
  t = t.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
       .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>')
       .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>').replace(/(^|[^*])\*([^*\s][^*]*)\*/g, '$1<em>$2</em>');
  return t.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[i]}</code>`);
}
export function md(src) {
  const out = [];
  const lines = esc(src.replace(/\r/g, '')).split('\n');
  for (let i = 0; i < lines.length;) {
    const l = lines[i];
    if (/^```/.test(l)) {                                   // code block
      const buf = []; i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++; out.push(`<pre><code>${buf.join('\n')}</code></pre>`); continue;
    }
    if (/^\s*\|.*\|\s*$/.test(l) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || '')) {   // table
      const cells = (r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
      const head = cells(l); i += 2; const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
      continue;
    }
    const h = /^(#{1,3})\s+(.*)$/.exec(l);
    if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i++; continue; }
    const li = /^\s*([-*]|\d+\.)\s+/.exec(l);
    if (li) {                                               // list
      const tag = /\d/.test(li[1]) ? 'ol' : 'ul'; const items = [];
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) items.push(`<li>${inline(lines[i++].replace(/^\s*([-*]|\d+\.)\s+/, ''))}</li>`);
      out.push(`<${tag}>${items.join('')}</${tag}>`); continue;
    }
    if (!l.trim()) { i++; continue; }
    const para = [];                                        // paragraph
    while (i < lines.length && lines[i].trim() && !/^(```|#{1,3}\s|\s*([-*]|\d+\.)\s+|\s*\|.*\|\s*$)/.test(lines[i])) para.push(inline(lines[i++]));
    out.push(`<p>${para.join('<br>')}</p>`);
  }
  return out.join('');
}

