/** 在线状态(presence):名册 + 心跳 + 行内徽章数据。迁移自 legacy presence.js。 */
import { ref } from 'vue'
import { defineStore } from 'pinia'
import { apiFetch } from '../api/client'

export interface PresenceUser {
  username: string
  viewing: string | null
  editing: string | null
}

/** 某视频行的 presence 徽章:编辑中(橙)/查看中(灰)。 */
export interface PresenceBadge {
  name: string
  kind: 'editing' | 'viewing'
}

const PRESENCE_INTERVAL_MS = 10000

export const usePresenceStore = defineStore('presence', () => {
  const roster = ref<PresenceUser[]>([])
  let timer: ReturnType<typeof setInterval> | null = null

  /** 名册归一化:兼容数组 / {users:[...]} / SSE 的 {roster:[...]};username 缺省取 user。 */
  function setRoster(data: unknown) {
    const raw = Array.isArray(data)
      ? data
      : ((data as { users?: unknown[]; roster?: unknown[] })?.users ||
        (data as { roster?: unknown[] })?.roster ||
        [])
    roster.value = (raw as Record<string, unknown>[])
      .map((u) => ({
        username: String(u?.username || u?.user || ''),
        viewing: (u?.viewing as string | null) ?? null,
        editing: (u?.editing as string | null) ?? null,
      }))
      .filter((u) => u.username)
  }

  /** 每 10s 上报 viewing/editing(留在名册里的必要动作);页面隐藏时暂停。 */
  function startHeartbeat(getViewing: () => string | null) {
    if (timer) return
    const beat = () => {
      apiFetch('/presence', {
        method: 'POST',
        body: JSON.stringify({ viewing: getViewing(), editing: null }),
      }).catch(() => {
        // 后端未就绪:静默,下个周期重试
      })
    }
    beat()
    timer = setInterval(() => {
      if (document.hidden) return
      beat()
    }, PRESENCE_INTERVAL_MS)
  }

  /** 某视频 rel 的徽章列表;不显示自己。 */
  function badgesFor(rel: string, meName: string | null): PresenceBadge[] {
    if (!rel || !roster.value.length) return []
    const out: PresenceBadge[] = []
    roster.value.forEach((u) => {
      if (meName && u.username === meName) return
      if (u.editing === rel) out.push({ name: u.username, kind: 'editing' })
      else if (u.viewing === rel) out.push({ name: u.username, kind: 'viewing' })
    })
    return out
  }

  return { roster, setRoster, startHeartbeat, badgesFor }
})
