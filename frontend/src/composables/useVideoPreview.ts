/** 视频预览降级链(迁移自 legacy video_preview.js):
 * <video> Range 流播放 → error 事件 → 逐帧滑块(GET meta 取 frame_count)
 * → meta 也读不到 → 错误态 + 「重试播放」。 */
import { ref } from 'vue'
import { apiFetch } from '../api/client'
import type { VideoMeta, VideoSource } from '../api/results'
import { metaPath, streamUrl } from '../api/results'

export type PreviewMode = 'video' | 'frames' | 'error'

export function useVideoPreview() {
  const mode = ref<PreviewMode>('video')
  const hint = ref('') // 降级原因提示
  const frameCount = ref(0)
  const url = ref('') // 当前流地址
  let source: VideoSource | null = null

  function mount(src: VideoSource) {
    source = src
    url.value = streamUrl(src)
    mode.value = 'video'
    hint.value = ''
  }

  /** 逐帧降级:先取真实帧数元数据,失败进错误态(同 legacy mountFrameStepper)。 */
  async function degradeToFrames(h: string) {
    hint.value = h
    if (!source) return
    const src = source // 闭包固定,期间切换视频则丢弃过期响应
    try {
      const meta = await apiFetch<VideoMeta>(metaPath(src))
      if (source !== src) return
      frameCount.value = Math.max(1, meta.frame_count || 0)
      mode.value = 'frames'
    } catch {
      if (source !== src) return
      mode.value = 'error'
    }
  }

  /** <video> error:编码不受支持或转码不可用 → 逐帧预览。 */
  function onVideoError() {
    degradeToFrames('浏览器无法直接播放该视频(编码不受支持或转码服务不可用),已切换为逐帧预览。')
  }

  /** 「重试播放」:回到 <video> 重新加载。 */
  function retry() {
    if (source) mount(source)
  }

  return { mode, hint, frameCount, url, mount, onVideoError, retry }
}
