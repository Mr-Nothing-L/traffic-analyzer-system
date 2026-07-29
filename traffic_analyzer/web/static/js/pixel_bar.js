/* ------------------------------------------------------------ 像素进度条(公共) */
// 主区专家工作间(preview/expert_panel)与侧栏迷你条(tree)共用的像素条实现:
// 格数(cells)/子格数(subsPerCell)/frontier 脉冲(opts.running)均参数化。

// 分段像素条 HTML:cells 列,每列 subsPerCell 个均等方块像素(从上到下堆叠,点亮顺序亦从上到下)
export function pixelBarHtml(cells, subsPerCell) {
  const subs = subsPerCell || 3;
  const cell = '<span class="pixel-cell">'
    + '<span class="pixel-sub"></span>'.repeat(subs) + '</span>';
  return cell.repeat(cells);
}

// 按 displayed(0..1)点亮像素条:displayed×列数 = 整列数 + 列内小数;
// 整列子格全亮,frontier 列按小数×子格数 从上到下点亮;
// 下一个待点亮像素加 .frontier 明暗脉冲(仅 opts.running 时;不传 opts 即无脉冲)
export function paintPixelBar(cells, displayed, opts) {
  const n = cells.length;
  if (!n) return;
  const subs = cells[0].children.length || 3;
  const pos = Math.max(0, Math.min(1, displayed)) * n;
  const full = Math.min(n, Math.floor(pos));
  const litInFrontier = Math.min(subs - 1, Math.floor((pos - full) * subs));
  const running = !!(opts && opts.running);
  for (let i = 0; i < n; i++) {
    const lit = i < full ? subs : (i === full ? litInFrontier : 0);
    const cellSubs = cells[i].children;
    for (let s = 0; s < cellSubs.length; s++) {
      const frontier = running && i === full && full < n && s === lit;
      cellSubs[s].classList.toggle('on', s < lit || frontier);
      cellSubs[s].classList.toggle('frontier', frontier);
    }
  }
}
