/** 全局会话/连接状态:当前用户(/api/auth/me)+ SSE 连接标志。 */
import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface MeInfo {
  username: string
  login_ts: number | null
  login_ip: string | null
}

export const useAppStore = defineStore('app', () => {
  const user = ref<string | null>(null)
  const loginTs = ref<number | null>(null)
  const loginIp = ref<string | null>(null)
  const sseConnected = ref(false)

  function setUser(name: string | null) {
    user.value = name
  }
  function setMe(me: MeInfo | null) {
    user.value = me?.username ?? null
    loginTs.value = me?.login_ts ?? null
    loginIp.value = me?.login_ip ?? null
  }
  function setSseConnected(connected: boolean) {
    sseConnected.value = connected
  }

  return { user, loginTs, loginIp, sseConnected, setUser, setMe, setSseConnected }
})
