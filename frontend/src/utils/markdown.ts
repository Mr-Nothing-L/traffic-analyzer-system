/** 极小 markdown → html(移植自 legacy markdown.js,逻辑逐行对应):
 * 标题/加粗/表格/列表/代码块/图片/引用/分割线。
 * XSS 纪律:输入先整体 esc,再按白名单规则替换出标签,不引入原始 HTML。 */

export function esc(s: unknown): string {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

type ImgResolver = (src: string) => string

export function mdInline(text: string, resolveImg?: ImgResolver): string {
  let s = esc(text)
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_m, alt: string, src: string) => {
    const url = /^https?:|^data:/.test(src) ? src : resolveImg ? resolveImg(src) : src
    return '<img alt="' + alt + '" src="' + esc(url) + '">'
  })
  s = s.replace(/`([^`]+)`/g, '<code>$1</code>')
  s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
  return s
}

export function mdToHtml(md: string, resolveImg?: ImgResolver): string {
  const lines = String(md || '').split('\n')
  const out: string[] = []
  let i = 0
  let listStack: 'ul' | 'ol' | null = null
  const closeList = () => {
    if (listStack) {
      out.push('</' + listStack + '>')
      listStack = null
    }
  }

  while (i < lines.length) {
    const line = lines[i]

    // 代码块
    if (/^```/.test(line)) {
      closeList()
      const buf: string[] = []
      i++
      while (i < lines.length && !/^```/.test(lines[i])) {
        buf.push(lines[i])
        i++
      }
      i++
      out.push('<pre><code>' + esc(buf.join('\n')) + '</code></pre>')
      continue
    }
    // 表格
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList()
      const parseRow = (l: string) => l.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim())
      const head = parseRow(line)
      i += 2
      const rows: string[][] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        rows.push(parseRow(lines[i]))
        i++
      }
      out.push(
        '<table><thead><tr>' +
          head.map((h) => '<th>' + mdInline(h, resolveImg) + '</th>').join('') +
          '</tr></thead><tbody>' +
          rows.map((r) => '<tr>' + r.map((c) => '<td>' + mdInline(c, resolveImg) + '</td>').join('') + '</tr>').join('') +
          '</tbody></table>',
      )
      continue
    }
    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/)
    if (h) {
      closeList()
      out.push('<h' + h[1].length + '>' + mdInline(h[2], resolveImg) + '</h' + h[1].length + '>')
      i++
      continue
    }
    // 分割线
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
      closeList()
      out.push('<hr>')
      i++
      continue
    }
    // 引用
    if (/^>\s?/.test(line)) {
      closeList()
      out.push('<blockquote>' + mdInline(line.replace(/^>\s?/, ''), resolveImg) + '</blockquote>')
      i++
      continue
    }
    // 列表
    let m = line.match(/^\s*[-*+]\s+(.*)$/)
    if (m) {
      if (listStack !== 'ul') {
        closeList()
        out.push('<ul>')
        listStack = 'ul'
      }
      out.push('<li>' + mdInline(m[1], resolveImg) + '</li>')
      i++
      continue
    }
    m = line.match(/^\s*\d+[.)]\s+(.*)$/)
    if (m) {
      if (listStack !== 'ol') {
        closeList()
        out.push('<ol>')
        listStack = 'ol'
      }
      out.push('<li>' + mdInline(m[1], resolveImg) + '</li>')
      i++
      continue
    }
    // 空行 / 段落
    closeList()
    if (line.trim() === '') {
      i++
      continue
    }
    out.push('<p>' + mdInline(line, resolveImg) + '</p>')
    i++
  }
  closeList()
  return '<div class="md">' + out.join('\n') + '</div>'
}
