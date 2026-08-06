/** 「快捷选择」纯逻辑:由 /api/workspace/quick-dirs 响应构建平铺项 + 关键词过滤。
 * 抽成纯函数便于 vitest 直测;DirQuickPick.vue 只做渲染与交互。 */

export interface QuickDirRoot {
  path: string // 根绝对路径
  subs: string[] // 一层子目录名(后端已按 name.lower() 排序、不含隐藏目录)
}

export interface QuickItem {
  path: string // 完整路径(选中值)
  label: string // 主标签(子目录名;根本身为「根名 (根目录)」)
  rootPath: string // 所属根完整路径(分组键)
  rootName: string // 所属根名(组头)
  isRoot: boolean // 是否「根本身」项
}

/** 根名 = 路径末段;"/" 本身返回 "/"。 */
export function rootNameOf(path: string): string {
  return path.split('/').filter(Boolean).pop() ?? '/'
}

/** 拼子路径,根为 "/" 时不产生双斜杠。 */
function joinPath(dir: string, name: string): string {
  return dir === '/' ? `/${name}` : `${dir}/${name}`
}

/** 白名单根列表 → 平铺项:每根首项为根本身,其后为各子目录(保持后端顺序)。 */
export function buildQuickItems(roots: QuickDirRoot[]): QuickItem[] {
  const items: QuickItem[] = []
  for (const r of roots) {
    const rootName = rootNameOf(r.path)
    items.push({
      path: r.path,
      label: `${rootName} (根目录)`,
      rootPath: r.path,
      rootName,
      isRoot: true,
    })
    for (const s of r.subs) {
      items.push({ path: joinPath(r.path, s), label: s, rootPath: r.path, rootName, isRoot: false })
    }
  }
  return items
}

/** 子串过滤:命中 子目录名/根名/完整路径 任一即保留,忽略大小写;空关键词原样返回。 */
export function filterQuickItems(items: QuickItem[], q: string): QuickItem[] {
  const kw = q.trim().toLowerCase()
  if (!kw) return items
  return items.filter(
    (it) =>
      it.label.toLowerCase().includes(kw) ||
      it.rootName.toLowerCase().includes(kw) ||
      it.path.toLowerCase().includes(kw),
  )
}
