<script setup lang="ts">
/** SFT 详情卡·关键帧面板:上排 10 张候选帧(hover 右上角「+」加入),下排事件
 * 关键帧(hover「−」删除、HTML5 原生拖拽排序、「智能挑选」)。增删改即时调 API,
 * 响应里的新 file_sig 回写 sft store 乐观锁指纹(store.stem 未变时),避免后续
 * 文本保存 409。图片点击放大由 NImage/NImageGroup 自带,不加依赖。 */
import { ref, watch } from 'vue'
import { NButton, NImage, NImageGroup, useDialog, useMessage } from 'naive-ui'
import {
  addKf,
  autoPickKf,
  deleteKf,
  getKfCandidates,
  getKfList,
  reorderKf,
} from '../../../api/keyframes'
import type { CandidateFrame, KeyframeEntry } from '../../../api/keyframes'
import { frameUrl, resultFileUrl } from '../../../api/results'
import { useSftStore } from '../../../stores/sft'

const props = defineProps<{ stem: string }>()
const store = useSftStore()
const message = useMessage()
const dialog = useDialog()

const candidates = ref<CandidateFrame[]>([])
const frames = ref<KeyframeEntry[]>([])

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

/** 变更成功后的统一收尾:本地列表替换 + file_sig 回写(仅当仍是同一视频)。 */
function applyMutation(data: { keyframes?: KeyframeEntry[]; file_sig?: string | null }) {
  if (Array.isArray(data.keyframes)) frames.value = data.keyframes
  if (data.file_sig && store.stem === props.stem) store.baseSig = data.file_sig
}

async function reload() {
  const stem = props.stem
  candidates.value = []
  frames.value = []
  try {
    const [cands, list] = await Promise.all([
      getKfCandidates(stem),
      getKfList(stem).catch(() => [] as KeyframeEntry[]), // 尚未标注过:空列表不报错
    ])
    if (props.stem !== stem) return // 期间已切换视频:丢弃过期响应
    candidates.value = cands
    frames.value = list
  } catch (e) {
    message.error(`关键帧候选加载失败:${errText(e)}`)
  }
}

watch(() => props.stem, reload, { immediate: true })

async function onAdd(c: CandidateFrame) {
  try {
    applyMutation(await addKf(props.stem, c.index, c.time_sec))
  } catch (e) {
    message.error(`加入关键帧失败:${errText(e)}`)
  }
}

async function onDelete(k: KeyframeEntry) {
  try {
    applyMutation(await deleteKf(props.stem, k.filename))
  } catch (e) {
    message.error(`删除关键帧失败:${errText(e)}`)
  }
}

/* ---- 拖拽排序:HTML5 原生事件,drop 后按新顺序调 PUT order ---- */
const dragFrom = ref<number | null>(null)
const dragOver = ref<number | null>(null)
let dragSnapshot: KeyframeEntry[] = []

function onDragStart(i: number, e: DragEvent) {
  dragFrom.value = i
  dragSnapshot = [...frames.value]
  // dataTransfer 必须有数据,Safari/Firefox 才会真正触发 drop
  e.dataTransfer?.setData('text/plain', String(i))
  if (e.dataTransfer) e.dataTransfer.effectAllowed = 'move'
}

function onDragLeave(i: number) {
  if (dragOver.value === i) dragOver.value = null
}

async function onDrop(to: number) {
  const from = dragFrom.value
  dragFrom.value = null
  dragOver.value = null
  if (from == null || from === to) return
  const names = frames.value.map((f) => f.filename)
  const [moved] = names.splice(from, 1)
  names.splice(to, 0, moved)
  try {
    applyMutation(await reorderKf(props.stem, names))
  } catch (e) {
    frames.value = dragSnapshot
    message.error(`排序保存失败:${errText(e)}`)
  }
}

/* ---- 智能挑选:已有关键帧时先确认覆盖 ---- */
const picking = ref(false)

function onAutoPick() {
  const run = async () => {
    picking.value = true
    try {
      const r = await autoPickKf(props.stem, true)
      applyMutation(r)
      message.success(`智能挑选完成:选中 ${r.picked?.length ?? r.keyframes.length} 帧`)
    } catch (e) {
      message.error(`智能挑选失败:${errText(e)}`)
    } finally {
      picking.value = false
    }
  }
  if (!frames.value.length) return void run()
  dialog.warning({
    title: '覆盖已有关键帧',
    content: `当前已有 ${frames.value.length} 个关键帧,智能挑选将全部替换。确定继续?`,
    positiveText: '覆盖重挑',
    negativeText: '取消',
    onPositiveClick: run,
  })
}
</script>

<template>
  <div class="kf-panel">
    <div class="kf-row-title">候选帧(hover 右上角「+」加入事件关键帧)</div>
    <div v-if="!candidates.length" class="kf-empty">候选帧加载中…</div>
    <n-image-group v-else>
      <div class="kf-strip">
        <div v-for="c in candidates" :key="c.index" class="kf-cell">
          <n-image
            class="kf-thumb"
            :src="frameUrl({ stem: props.stem }, c.index)"
            width="120"
            lazy
          />
          <button class="kf-op" title="加入事件关键帧" @click="onAdd(c)">+</button>
          <span class="kf-t">{{ c.time_sec.toFixed(1) }}s</span>
        </div>
      </div>
    </n-image-group>

    <div class="kf-row-title">
      <span>事件关键帧({{ frames.length ? `${frames.length} 帧` : '2–5 帧' }},拖拽调整时间顺序)</span>
      <n-button size="tiny" type="primary" secondary :loading="picking" @click="onAutoPick">
        智能挑选
      </n-button>
    </div>
    <div v-if="!frames.length" class="kf-empty">
      暂无关键帧;从上方候选帧加入,或点「智能挑选」自动选取
    </div>
    <n-image-group v-else>
      <div class="kf-strip">
        <div
          v-for="(k, i) in frames"
          :key="k.filename"
          class="kf-cell kf-draggable"
          :class="{ 'drag-src': dragFrom === i, 'drag-over': dragOver === i }"
          draggable="true"
          @dragstart="onDragStart(i, $event)"
          @dragenter.prevent="dragOver = i"
          @dragover.prevent
          @dragend="
            dragFrom = null;
            dragOver = null;
          "
          @dragleave="onDragLeave(i)"
          @drop.prevent="onDrop(i)"
        >
          <n-image
            class="kf-thumb"
            :src="resultFileUrl(props.stem, `关键帧/${k.filename}`)"
            width="120"
            :img-props="{ draggable: false }"
          />
          <button class="kf-op del" title="删除该关键帧" @click="onDelete(k)">−</button>
          <span class="kf-t">{{ k.time_sec.toFixed(1) }}s</span>
        </div>
      </div>
    </n-image-group>
  </div>
</template>

<style scoped>
.kf-panel {
  display: flex;
  flex-direction: column;
  gap: var(--space-xs);
  margin-bottom: var(--space-sm);
}

.kf-row-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-sm);
  font-size: var(--text-xs);
  color: var(--color-text2);
}

.kf-strip {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-sm);
}

.kf-cell {
  position: relative;
  width: 120px;
}

.kf-thumb {
  width: 120px;
  display: block;
}

.kf-thumb :deep(img) {
  width: 120px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-border);
}

.kf-op {
  position: absolute;
  top: 4px;
  right: 4px;
  z-index: 1;
  width: 22px;
  height: 22px;
  padding: 0;
  border: none;
  border-radius: 50%;
  font-size: 15px;
  line-height: 1;
  cursor: pointer;
  background: var(--color-accent);
  color: var(--color-on-accent);
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease-out);
}

.kf-op.del {
  background: var(--color-red);
}

.kf-cell:hover .kf-op {
  opacity: 0.85;
}

.kf-op:hover {
  opacity: 1 !important;
}

.kf-t {
  position: absolute;
  left: 4px;
  bottom: 4px;
  font-size: var(--text-xs);
  line-height: 1.4;
  padding: 0 5px;
  border-radius: var(--radius-sm);
  background: var(--color-stage-bg);
  color: var(--color-paper);
  opacity: 0.8;
  pointer-events: none;
}

.kf-draggable {
  cursor: grab;
}

.drag-src {
  opacity: 0.35;
}

.drag-over {
  outline: 2px dashed var(--color-accent);
  outline-offset: 2px;
}

.kf-empty {
  font-size: var(--text-xs);
  color: var(--color-text2);
  padding: var(--space-xs) 0;
}
</style>
