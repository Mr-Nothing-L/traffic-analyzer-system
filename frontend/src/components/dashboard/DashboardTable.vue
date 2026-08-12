<script setup lang="ts">
/** 逐视频明细表 + 翻页条(自建表格:NDataTable 对行内 chip 组/presence 徽章/整行
 * 点击跳得不顺手,自建更贴 legacy 密度)。迁移自 legacy renderTable/pagerHtml/bindPager。
 * 跳转:整行不可点(避免误触 chip 时跳走),仅最右列「打开」按钮进分析详情。 */
import { computed, nextTick, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { useAppStore } from '../../stores/app'
import { useDashboardStore } from '../../stores/dashboard'
import type { DashboardRow } from '../../stores/dashboard'
import { usePresenceStore } from '../../stores/presence'
import { useWorkspaceStore } from '../../stores/workspace'
import ReviewChip from './ReviewChip.vue'
import UiIcon from '../UiIcon.vue'

const dash = useDashboardStore()
const ws = useWorkspaceStore()
const presence = usePresenceStore()
const app = useAppStore()
const router = useRouter()
const message = useMessage()

const rows = computed(() => dash.rowsData?.rows || [])
const d = computed(() => dash.rowsData)
// 空态二选一:工作区无视频 / 过滤后无匹配(同 legacy)
const wsEmpty = computed(() => !(dash.data?.summary || {}).total)

/** 一致性徽章:有差异时附「漏:x;误:x」明细(同 legacy)。 */
function consistencyOf(r: DashboardRow): { cls: string; label: string; detail: string | null } {
  if (r.status === 'consistent') return { cls: 'ok', label: '一致', detail: null }
  if (r.status === 'diff') {
    return {
      cls: 'warn',
      label: '有差异',
      detail: `漏:${(r.missing || []).length};误:${(r.extra || []).length}`,
    }
  }
  if (r.status === 'no_gt') return { cls: 'mute', label: '无 GT', detail: null }
  return { cls: 'mute', label: '未推理', detail: null }
}

/** 「人工已改」徽章 title:原始检出 → 现在 + 人工补充/删除(同 legacy editedBadge)。 */
function editedTitle(r: DashboardRow): string {
  const raw = dash.namesText(r.pred_raw_ids) || '(空)'
  const cur = dash.namesText(r.pred_ids) || '(空)'
  let title = `原始检出:「${raw}」→ 现在:「${cur}」`
  if ((r.edit_extra || []).length) title += `;人工补充:「${dash.namesText(r.edit_extra)}」`
  if ((r.edit_missing || []).length) title += `;人工删除:「${dash.namesText(r.edit_missing)}」`
  return title
}

/** 翻页:点击时读最新 rowsData(重拉/筛选可能已更新页码),加载中忽略(按钮已禁用)。 */
function goPage(delta: number) {
  const cur = d.value
  if (!cur || dash.rowsFetching) return
  const target = cur.page + delta
  if (target < 1 || target > (cur.total_pages || 0)) return
  dash.fetchRows(target)
}

/** 行点击 → 选中并进详情:树未加载先加载,逐层展开祖先目录保证节点可见(同 TreeNode.onSelect)。 */
async function openRow(rel: string, stem: string) {
  try {
    if (!ws.loaded) await ws.loadTree()
    const parts = rel.split('/')
    for (let i = 1; i < parts.length; i += 1) {
      const dir = parts.slice(0, i).join('/')
      if (!ws.expanded.has(dir)) await ws.toggleDir(dir)
    }
    ws.currentRel = rel
    router.push({ name: 'detail', params: { stem }, query: { rel } })
  } catch (e) {
    message.error(`打开视频失败:${(e as Error).message}`)
  }
}

/** 编辑时间戳格式化:ISO → YYYY-MM-DD HH:MM。 */
function formatTime(iso: string): string {
  try {
    const d = new Date(iso)
    const pad = (n: number) => String(n).padStart(2, '0')
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  } catch { return '' }
}

/** 侧边树点击视频 → 看板模式下滚动到对应行并高亮(由 dashboard.scrollToStem 驱动)。 */
watch(() => dash.scrollToStem, (stem) => {
  if (!stem) return
  nextTick(() => {
    const el = document.querySelector(`tr[data-stem="${CSS.escape(stem)}"]`) as HTMLElement | null
    if (el) {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' })
      el.classList.add('dash-row-flash')
      setTimeout(() => el.classList.remove('dash-row-flash'), 2000)
    }
  })
})
</script>

<template>
  <div v-if="dash.rowsError" class="dash-empty">明细加载失败:{{ dash.rowsError }}</div>
  <template v-else>
    <div v-if="!rows.length" class="dash-empty">
      {{ wsEmpty ? '工作区内暂无视频。' : '当前过滤条件下没有匹配的视频。' }}
    </div>
    <div v-else class="dash-table-wrap">
      <table class="dash-table dash-rows-table">
        <thead>
          <tr>
            <th>视频</th>
            <th>GT 事件</th>
            <th>模型检出</th>
            <th>一致性</th>
            <th class="dash-col-edit">人工</th>
            <th class="dash-col-review">审核</th>
            <th class="dash-col-open"></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.rel" :data-stem="r.stem">
            <td class="dash-v" :title="r.rel">
              <span class="file-name">{{ r.rel }}</span>
              <span
                v-for="b in presence.badgesFor(r.rel, app.user)"
                :key="b.kind + b.name"
                class="presence-badge"
                :class="b.kind === 'editing' ? 'presence-editing' : 'presence-viewing'"
                :title="b.name + (b.kind === 'editing' ? ' 正在编辑' : ' 正在查看')"
              >
                <UiIcon v-if="b.kind === 'editing'" name="edit" :size="11" /> {{ b.name }}
              </span>
            </td>
            <!-- GT 事件:漏检(在 missing)的事件 chip 用暖色,其余中性底(同 legacy) -->
            <td>
              <template v-if="r.status !== 'no_gt'">
                <span v-if="r.gt_ids.length === 1 && r.gt_ids[0] === 9" class="dash-none">无异常事件</span>
                <template v-else-if="(r.gt_ids || []).length">
                  <span
                    v-for="id in r.gt_ids"
                    :key="id"
                    class="dash-ev-chip"
                    :class="{ 'dash-ev-chip-warm': (r.missing || []).includes(id) }"
                    >{{ dash.eventName(id) }}</span
                  >
                </template>
                <span v-else class="dash-none">无事件</span>
              </template>
              <span v-else class="dash-none">—</span>
            </td>
            <!-- 模型检出:误检(在 extra)的事件 chip 用暖色 -->
            <td>
              <template v-if="r.status !== 'no_results'">
                <span v-if="r.pred_ids.length === 1 && r.pred_ids[0] === 9" class="dash-none">无异常事件</span>
                <template v-else-if="(r.pred_ids || []).length">
                  <span
                    v-for="id in r.pred_ids"
                    :key="id"
                    class="dash-ev-chip"
                    :class="{ 'dash-ev-chip-warm': (r.extra || []).includes(id) }"
                    >{{ dash.eventName(id) }}</span
                  >
                </template>
                <span v-else class="dash-none">无异常事件</span>
              </template>
              <span v-else class="dash-none">—</span>
            </td>
            <td class="dash-nowrap dash-col-consistency">
              <span class="dash-badge" :class="`dash-badge-${consistencyOf(r).cls}`">
                {{ consistencyOf(r).label }}
              </span>
              <span v-if="consistencyOf(r).detail" class="dash-diff-detail">
                {{ consistencyOf(r).detail }}
              </span>
            </td>
            <td class="dash-nowrap">
              <span v-if="r.edited" class="dash-badge dash-badge-edit" :title="editedTitle(r)">
                人工已改
              </span>
              <span v-if="r.edited && r.edited_at" class="dash-edited-time">
                {{ formatTime(r.edited_at) }}
              </span>
            </td>
            <td class="dash-nowrap"><ReviewChip :stem="r.stem" :review="r.review" /></td>
            <td class="dash-nowrap">
              <button type="button" class="dash-open-btn" @click="openRow(r.rel, r.stem)">
                打开 →
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <!-- 翻页条:加载中禁用两按钮 + 「加载中…」提示(大工作区可达 ~11s,同 legacy) -->
    <div class="dash-pager">
      <button
        type="button"
        class="dash-chip"
        :disabled="dash.rowsFetching || !d || d.page <= 1"
        @click="goPage(-1)"
      >
        上一页
      </button>
      <span class="dash-card-sub">第 {{ d?.page ?? 1 }} / {{ Math.max(d?.total_pages ?? 0, 1) }} 页</span>
      <button
        type="button"
        class="dash-chip"
        :disabled="dash.rowsFetching || !d || (d.total_pages || 0) < 1 || d.page >= d.total_pages"
        @click="goPage(1)"
      >
        下一页
      </button>
      <span v-if="dash.rowsFetching" class="dash-card-sub">加载中…</span>
    </div>
  </template>
</template>

<style scoped>
.dash-edited-time {
  font-size: var(--text-xs);
  color: var(--color-text2);
  display: block;
  margin-top: 2px;
}
.dash-row-flash {
  animation: dash-flash 2s ease;
}
@keyframes dash-flash {
  0% { background: var(--color-accent-soft); }
  100% { background: transparent; }
}
</style>
