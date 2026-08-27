<script setup lang="ts">
/** 工具条目:参数摘要 + 结果折叠(文本/图片/子代理),track_suspects 附叠加
 * 视频与取证目录路径,失败红字。 */
import { computed, ref, watch } from 'vue'
import type { AgentEntry } from '../../stores/agentchat'
import type { AgentToolEntry } from '../../stores/agentchat'
import UiIcon from '../UiIcon.vue'
import { copyText, toolLabel, toolErrorSummary, trackSuspectsView } from '../../utils/chatDisplay'

const props = defineProps<{
  entry: AgentEntry
  toolOpen: boolean
  subThinkOpen: Set<string>
}>()

const emit = defineEmits<{
  'toggle-tool': []
  'toggle-sub-think': [key: string]
  preview: [url: string]
}>()

const entry = computed(() => props.entry as AgentToolEntry)

/** track_suspects 取证产物(trackSuspectsView 解析;其他工具恒 null):
 * clip 推导出 stream 地址则渲染可播放叠加视频,推不出/渲染报错只留路径文本。 */
const trackView = computed(() =>
  entry.value.name === 'track_suspects' ? trackSuspectsView(entry.value.result) : null,
)

/** 叠加视频加载失败标记:置真后隐藏 video 换路径文本;换新结果(新 src)时复位。 */
const overlayBroken = ref(false)
watch(
  () => trackView.value?.videoSrc,
  () => {
    overlayBroken.value = false
  },
)

/** 取证目录点击复制:成功图标变 ✓ 一秒,失败静默保持原样(低调调试辅助)。 */
const dirCopied = ref(false)
let dirCopiedTimer: ReturnType<typeof setTimeout> | null = null
async function onCopyArtifactsDir(): Promise<void> {
  const dir = trackView.value?.dir
  if (!dir || dirCopied.value) return
  try {
    await copyText(dir)
  } catch {
    return
  }
  dirCopied.value = true
  if (dirCopiedTimer !== null) clearTimeout(dirCopiedTimer)
  dirCopiedTimer = setTimeout(() => {
    dirCopied.value = false
    dirCopiedTimer = null
  }, 1000)
}

function argsSummary(args: string): string {
  const cut = (s: string) => (s.length > 120 ? `${s.slice(0, 120)}…` : s)
  try {
    const obj = JSON.parse(args) as Record<string, unknown>
    const parts = Object.entries(obj).map(
      ([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`,
    )
    return cut(parts.join(', ') || '(无参数)')
  } catch {
    return cut(args)
  }
}
</script>

<template>
            <div class="tool">
              <button class="tool-head" @click="emit('toggle-tool')">
                <UiIcon
                  name="up"
                  :size="10"
                  class="think-caret"
                  :class="{ open: toolOpen }"
                />
                <span class="tool-title">工具调用:{{ toolLabel(entry.name) }}</span>
                <span class="tool-args">{{ argsSummary(entry.args) }}</span>
                <span v-if="!entry.done" class="tool-state">执行中…</span>
                <span
                  v-else-if="entry.isError"
                  class="tool-state err tool-err-text"
                  :title="entry.result"
                >{{ toolErrorSummary(entry.result) }}</span>
                <span v-else class="tool-state ok">完成</span>
              </button>
              <div v-if="toolOpen && (entry.done || entry.children.length)" class="tool-result">
                <!-- 子代理迷你时间线(spawn_subagent:think/text 聚合块 + 子工具一行小字) -->
                <template v-for="(c, j) in entry.children" :key="`${entry.id}:${j}`">
                  <div v-if="c.kind === 'think'" class="sub-think">
                    <button class="think-head" @click="emit('toggle-sub-think', `${entry.id}:${j}`)">
                      <UiIcon
                        name="up"
                        :size="10"
                        class="think-caret"
                        :class="{ open: subThinkOpen.has(`${entry.id}:${j}`) }"
                      />
                      <span>子代理思考</span>
                    </button>
                    <div v-if="subThinkOpen.has(`${entry.id}:${j}`)" class="think-text">{{ c.text }}</div>
                  </div>
                  <div v-else-if="c.kind === 'text'" class="sub-text">{{ c.text }}</div>
                  <div v-else class="sub-tool">
                    工具调用:{{ toolLabel(c.name) }}
                    <span class="tool-args">{{ argsSummary(c.args) }}</span>
                    <span v-if="!c.done" class="tool-state">执行中…</span>
                  </div>
                </template>
                <div v-if="entry.result" class="tool-result-text">{{ entry.result }}</div>
                <div v-else-if="entry.done && !entry.images.length" class="tool-result-text">(无输出)</div>
                <!-- load_video:视频 part 体积巨大,只显示静态提示,不做播放器 -->
                <div v-if="entry.hasVideo" class="tool-video-note">
                  <UiIcon name="video" :size="12" />
                  <span>已加载完整视频(降帧)</span>
                </div>
                <div v-if="entry.images.length" class="tool-imgs">
                  <img
                    v-for="(u, j) in entry.images"
                    :key="`${entry.id}:${j}`"
                    :src="u"
                    alt=""
                    loading="lazy"
                    @click="emit('preview', u)"
                  />
                </div>
                <!-- track_suspects 取证产物:叠加视频小播放器 + 可复制目录路径 -->
                <template v-if="trackView">
                  <video
                    v-if="trackView.videoSrc && !overlayBroken"
                    class="tool-track-video"
                    :src="trackView.videoSrc"
                    controls
                    preload="metadata"
                    @error="overlayBroken = true"
                  />
                  <button
                    v-if="trackView.dir"
                    type="button"
                    class="tool-artifacts-dir"
                    title="复制取证目录路径"
                    @click="onCopyArtifactsDir"
                  >
                    <UiIcon :name="dirCopied ? 'check' : 'copy'" :size="10" />
                    <span>{{ trackView.dir }}</span>
                  </button>
                </template>
              </div>
            </div>
</template>

<style scoped>
/* ---- 思考过程折叠 ---- */
.think {
  margin-bottom: var(--space-xs);
}

.think-head {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
}

.think-head:hover {
  color: var(--color-accent);
}

.think-caret {
  transform: rotate(180deg); /* 收起:向下 */
  transition: transform 0.15s ease;
}

.think-caret.open {
  transform: rotate(0deg); /* 展开:向上 */
}

.think-text {
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--color-text2);
  font-size: var(--text-sm);
  line-height: 1.6;
}
/* ---- 工具条目(弱化:无卡片边框/底色,小字 muted,与思考过程同款) ---- */
.tool {
  margin: var(--space-xs) 0;
}

.tool-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-size: var(--text-sm);
  cursor: pointer;
  text-align: left;
}

.tool-head:hover {
  color: var(--color-accent);
}

/* 工具名:正文 sans 小字,不突出;中文映射由 toolLabel 给出 */
.tool-title {
  flex: 0 0 auto;
}

.tool-args {
  flex: 1;
  min-width: 0;
  font-size: var(--text-xs);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.tool-state {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--color-blue);
}

.tool-state.ok {
  color: var(--color-sage);
}

.tool-state.err {
  color: var(--color-red);
}

/* 失败摘要:直接显示错误内容首行(红色),占满剩余宽度并省略截断 */
.tool-err-text {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  text-align: left;
}

.tool-result {
  padding: 2px 0 var(--space-xs) calc(10px + var(--space-xs));
  white-space: pre-wrap;
  word-break: break-word;
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  line-height: 1.6;
  color: var(--color-text2);
  max-height: 240px;
  overflow-y: auto;
}

/* 工具结果图片行(extract_frames/draw_boxes 抽帧/标注图,点击进画廊) */
.tool-imgs {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-xs);
}

.tool-result-text + .tool-imgs {
  margin-top: var(--space-xs);
}

.tool-imgs img {
  width: 160px;
  height: 120px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  cursor: zoom-in;
}

/* ---- 子代理迷你时间线(spawn_subagent 展开区内,同工具结果弱化风格) ---- */
.sub-think {
  margin: 2px 0;
}

.sub-text {
  margin: 2px 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.sub-tool {
  margin: 2px 0;
  display: flex;
  align-items: baseline;
  gap: var(--space-xs);
  min-width: 0;
}

/* load_video:视频 part 不做播放器,仅静态提示行 */
.tool-video-note {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  margin-top: var(--space-xs);
  color: var(--color-text2);
  font-family: var(--font-sans);
  font-size: var(--text-xs);
}

/* track_suspects:跟踪叠加片段小播放器(宽度规则同 user 气泡视频) */
.tool-track-video {
  display: block;
  width: min(320px, 100%);
  margin-top: var(--space-xs);
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
  background: var(--color-stage-bg);
}

/* 取证目录路径:低调等宽文本按钮,点击复制 */
.tool-artifacts-dir {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  max-width: 100%;
  margin-top: var(--space-xs);
  padding: 0;
  border: none;
  background: none;
  color: var(--color-text2);
  font-family: var(--font-mono);
  font-size: var(--text-xs);
  cursor: pointer;
  word-break: break-all;
  text-align: left;
}

.tool-artifacts-dir:hover {
  color: var(--color-accent);
}
</style>
