/** Naive UI 主题映射到 design.md token。
 * naive-ui 会在 JS 里对主色做 rgba() 派生(seemly),既不认 var(--token) 也不认 oklch();
 * 因此在模块加载时读 tokens.css 的计算值并换算为 sRGB。源仍是 var(--token),
 * 组件内依旧零 raw hex/oklch。 */
import type { GlobalThemeOverrides } from 'naive-ui'

/** oklch(L C H) → 'rgb(r, g, b)';非 oklch 输入(如 hex)原样返回。
 * 近无色度(白/黑/灰)生成器会省略 hue,此时按 h=0 处理(C≈0,hue 无影响)。 */
export function oklchToRgb(input: string): string {
  const m = input.match(
    /oklch\(\s*([\d.]+)\s+([\d.]+)(?:\s+([\d.]+))?\s*(?:\/\s*([\d.]+%?)\s*)?\)/,
  )
  if (!m) return input
  const L = parseFloat(m[1])
  const C = parseFloat(m[2])
  const h = ((m[3] ? parseFloat(m[3]) : 0) * Math.PI) / 180
  const a = C * Math.cos(h)
  const b = C * Math.sin(h)
  // OKLab → 线性 sRGB(标准矩阵)
  const l3 = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
  const m3 = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
  const s3 = (L - 0.0894841775 * a - 1.291485548 * b) ** 3
  const lin = [
    4.0767416621 * l3 - 3.3077115913 * m3 + 0.2309699292 * s3,
    -1.2684380046 * l3 + 2.6097574011 * m3 - 0.3413193965 * s3,
    -0.0041960863 * l3 - 0.7034186147 * m3 + 1.707614701 * s3,
  ]
  const chan = lin.map((v) => {
    const g = v <= 0.0031308 ? 12.92 * v : 1.055 * Math.pow(Math.max(v, 0), 1 / 2.4) - 0.055
    return Math.round(Math.min(1, Math.max(0, g)) * 255)
  })
  if (m[4]) {
    const alpha = m[4].endsWith('%') ? parseFloat(m[4]) / 100 : parseFloat(m[4])
    return `rgba(${chan[0]}, ${chan[1]}, ${chan[2]}, ${alpha})`
  }
  return `rgb(${chan[0]}, ${chan[1]}, ${chan[2]})`
}

/** 把 var(--x) 读成计算色并转 sRGB(seemly 可解析);失败原样返回。 */
function c(token: string): string {
  const probe = document.createElement('span')
  probe.style.color = `var(--color-${token})`
  probe.style.display = 'none'
  document.body.appendChild(probe)
  const resolved = getComputedStyle(probe).color
  probe.remove()
  return resolved ? oklchToRgb(resolved) : `var(--color-${token})`
}

export const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: c('accent'),
    primaryColorHover: c('accent-hover'),
    primaryColorPressed: c('accent-hover'),
    primaryColorSuppl: c('accent'),
    successColor: c('sage'),
    successColorHover: c('sage'),
    successColorPressed: c('sage'),
    successColorSuppl: c('sage'),
    warningColor: c('gold'),
    warningColorHover: c('gold'),
    warningColorPressed: c('gold'),
    warningColorSuppl: c('gold'),
    errorColor: c('red'),
    errorColorHover: c('red'),
    errorColorPressed: c('red'),
    errorColorSuppl: c('red'),
    infoColor: c('blue'),
    infoColorHover: c('blue'),
    infoColorPressed: c('blue'),
    infoColorSuppl: c('blue'),
    textColorBase: c('text'),
    textColor1: c('text'),
    textColor2: c('text2'),
    bodyColor: 'var(--color-paper)',
    cardColor: 'var(--color-card)',
    modalColor: 'var(--color-card)',
    popoverColor: 'var(--color-card)',
    borderColor: 'var(--color-border)',
    borderRadius: 'var(--radius-sm)',
    borderRadiusSmall: 'var(--radius-sm)',
    fontFamily: 'var(--font-sans)',
    fontFamilyMono: 'var(--font-mono)',
  },
  Button: {
    textColorPrimary: c('on-accent'),
    textColorHoverPrimary: c('on-accent'),
    textColorPressedPrimary: c('on-accent'),
    textColorFocusPrimary: c('on-accent'),
  },
}
