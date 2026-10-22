<script setup lang="ts">
/** 历史会话栏:工作区分组卡片纵列(当前工作区卡最上恒展开;其他工作区/未
 * 分组卡默认折叠,点击卡头展开);条目 title + 相对/绝对时间,点击切换(跨工作
 * 区先切工作区),悬停出重命名/删除;重命名原位输入框(Enter/blur 确认,Esc
 * 取消)。自 ChatView 拆出,store 直连;选中后父视图滚动时间线到底(emit select)。 */
import { computed, nextTick, reactive, ref, watch } from 'vue'
import { NButton, NScrollbar, useMessage } from 'naive-ui'
import type { AgentSessionInfo } from '../../stores/agentchat'
import { useAgentChatStore } from '../../stores/agentchat'
import { useWorkspaceStore } from '../../stores/workspace'
import { groupSessionsByWorkspace, wsBasename } from '../../utils/sessionGroups'
import type { SessionGroup } from '../../utils/sessionGroups'
import { relTime, absTime } from '../../utils/chatDisplay'
import UiIcon from '../UiIcon.vue'

const emit = defineEmits<{ select: [] }>()
const agent = useAgentChatStore()
const ws = useWorkspaceStore()
const message = useMessage()

/* ---- 历史会话栏:按最近活跃倒序;相对时间(后端为 epoch ms) ---- */
const sortedSessions = computed(() =>
  [...agent.sessions].sort((a, b) => (b.lastActiveAt ?? 0) - (a.lastActiveAt ?? 0)),
)

/* 会话按工作区分组(utils/sessionGroups,参考 kimi-code 侧栏:工作区文件夹为组,
 * 组下挂会话):当前工作区组最上(无标题、恒展开);其余工作区各一组(标题 =
 * basename,悬停显示完整路径,默认折叠、点击展开);无 workspaceDir 的旧会话
 * 归「未分组」组沉底;组内按 lastActiveAt 倒序。 */
const sessionGroups = computed(() => groupSessionsByWorkspace(agent.sessions, ws.path))
/** 当前工作区组会话(空态提示与进入页面续接最近会话用)。 */
const ownSessions = computed(
  () => sessionGroups.value.find((g) => g.key === 'own')?.items ?? [],
)

/** 其他工作区/未分组组的展开态(默认折叠;当前工作区组恒展开,不在此列)。 */
const expandedGroups = reactive(new Set<string>())
function toggleGroup(key: string) {
  if (expandedGroups.has(key)) expandedGroups.delete(key)
  else expandedGroups.add(key)
}

/** 卡片式分组:卡头标题兜底(当前工作区组 label 为空,显示当前工作区 basename)。 */
const ownLabel = computed(() => wsBasename(ws.path ?? '') || '当前工作区')
function headLabel(g: SessionGroup): string {
  return g.label || ownLabel.value
}

/** 悬停提示:其他工作区组为完整路径;当前工作区组也补全路径,便于同名 basename 辨认。 */
function headTitle(g: SessionGroup): string | undefined {
  return g.title ?? (g.key === 'own' ? (ws.path ?? undefined) : undefined)
}

function onGroupHeadClick(g: SessionGroup) {
  if (g.collapsible) toggleGroup(g.key)
}

/** 点击会话条目:跨工作区会话由 openSession 先切工作区再选会话;
 * 工作区切换失败(目录不存在/不在白名单)提示「工作区不可用」,不切换。 */
async function onSelect(s: AgentSessionInfo) {
  try {
    await agent.openSession(s.id)
  } catch (e) {
    message.error(`工作区不可用:${(e as Error).message}`)
    return
  }
  await nextTick()
  emit('select')
}

async function onDelete(id: string) {
  try {
    await agent.deleteSession(id)
  } catch (e) {
    message.error(`删除会话失败:${(e as Error).message}`)
  }
}

/* ---- 会话重命名:hover 显现铅笔 → 标题原位变输入框;Enter/blur 确认,Esc 取消 ---- */
const renamingId = ref<string | null>(null)
const renamingText = ref('')
let renamingOriginal = ''

function onRenameStart(s: AgentSessionInfo) {
  renamingId.value = s.id
  renamingOriginal = sessionTitle(s)
  renamingText.value = renamingOriginal
}
async function onRenameConfirm(id: string) {
  if (renamingId.value !== id) return
  const title = renamingText.value.trim()
  renamingId.value = null
  if (!title || title === renamingOriginal) return
  try {
    await agent.renameSession(id, title)
  } catch (e) {
    message.error(`重命名失败:${(e as Error).message}`)
  }
}
function onRenameCancel() {
  renamingId.value = null
}

/** 重命名输入框自动聚焦并全选(便于直接覆盖输入)。 */
const renameInputEl = ref<HTMLInputElement | null>(null)
watch(renamingId, async (id) => {
  if (id === null) return
  await nextTick()
  renameInputEl.value?.focus()
  renameInputEl.value?.select()
})

async function onNewSession() {
  await agent.newSession()
}

function sessionTitle(s: AgentSessionInfo): string {
  return s.title?.trim() || '新会话'
}

</script>

<template>
    <aside class="session-col">
      <div class="session-head">
        <UiIcon name="chat" :size="14" />
        <span class="session-head-title">历史会话</span>
        <span class="session-spacer" />
        <!-- 新建:运行中也可点(断连不杀服务端轮次,切回时恢复轮询续跑) -->
        <n-button size="small" @click="onNewSession">新建</n-button>
      </div>
      <n-scrollbar class="session-scroll">
        <div v-if="!sortedSessions.length" class="session-empty">暂无历史会话</div>
        <div v-else-if="!ownSessions.length" class="session-empty">当前工作区暂无会话</div>
        <section v-for="g in sessionGroups" :key="g.key" class="session-card">
          <!-- 卡片头:工作区名 + 会话数;可折叠组是按钮(caret 折叠),当前工作区组是静态头 -->
          <component
            :is="g.collapsible ? 'button' : 'div'"
            class="session-card-head"
            :title="headTitle(g)"
            @click="onGroupHeadClick(g)"
          >
            <span
              v-if="g.collapsible"
              class="session-group-caret"
              :class="{ open: expandedGroups.has(g.key) }"
            >
              ▸
            </span>
            <span class="session-card-label">{{ headLabel(g) }}</span>
            <span class="session-card-count">{{ g.items.length }}</span>
          </component>
          <div v-if="!g.collapsible || expandedGroups.has(g.key)" class="session-card-body">
            <div
              v-for="s in g.items"
              :key="s.id"
              class="session-item"
              :class="{ active: s.id === agent.sessionId }"
              tabindex="0"
              @click="onSelect(s)"
              @keydown.enter="onSelect(s)"
            >
              <div v-if="renamingId === s.id" class="session-item-rename" @click.stop>
                <input
                  ref="renameInputEl"
                  v-model="renamingText"
                  class="session-rename-input"
                  maxlength="30"
                  placeholder="会话名称"
                  @keydown.enter.prevent="onRenameConfirm(s.id)"
                  @keydown.esc.prevent="onRenameCancel"
                  @blur="onRenameConfirm(s.id)"
                />
              </div>
              <template v-else>
                <div class="session-item-title" :title="sessionTitle(s)">{{ sessionTitle(s) }}</div>
                <div class="session-item-meta">
                  <span class="session-item-meta-left">
                    <span class="session-item-time">{{ relTime(s.lastActiveAt) }}</span>
                    <span v-if="s.createdAt" class="session-item-time session-item-time-abs">
                      {{ absTime(s.createdAt) }}
                    </span>
                  </span>
                  <span class="session-item-actions">
                    <button class="session-edit" title="重命名会话" @click.stop="onRenameStart(s)">
                      <UiIcon name="edit" :size="11" />
                    </button>
                    <button class="session-del" title="删除会话" @click.stop="onDelete(s.id)">
                      <UiIcon name="trash" :size="11" />
                    </button>
                  </span>
                </div>
              </template>
            </div>
          </div>
        </section>
      </n-scrollbar>
    </aside>
</template>

<style scoped>
/* ---- 历史会话栏:工作区分组卡片纵列 ----
   会话栏整体是 UI 骨架(design.md §2),像素字体在此声明逐层继承;
   仅数值(会话数/相对时间)按角色改回等宽 */
.session-col {
  width: 220px;
  flex: 0 0 220px;
  display: flex;
  flex-direction: column;
  min-height: 0;
  font-family: var(--font-pixel);
}

.session-head {
  display: flex;
  align-items: center;
  gap: var(--space-sm);
  padding: var(--space-xs) var(--space-xs);
  color: var(--color-text2);
}

.session-head-title {
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text);
}

.session-spacer {
  flex: 1;
}

.session-scroll {
  flex: 1;
  min-height: 0;
}

/* 卡片与卡片之间留缝(滚动内容整体左右收进,不贴边) */
.session-scroll :deep(.n-scrollbar-content) {
  padding: 0 var(--space-xs) var(--space-xs);
}

.session-empty {
  padding: var(--space-xl) var(--space-xs);
  text-align: center;
  color: var(--color-text2);
  font-size: var(--text-sm);
}

.session-card + .session-card {
  margin-top: var(--space-sm);
}

.session-card {
  background: var(--color-card);
  border: 1px solid var(--color-border);
  border-radius: var(--radius-sm);
  box-shadow: var(--shadow);
  overflow: hidden;
}

/* 卡片头(可折叠组是 button,当前工作区组是 div,基础样式共用) */
.session-card-head {
  display: flex;
  align-items: center;
  gap: var(--space-xs);
  width: 100%;
  padding: var(--space-xs) var(--space-sm);
  border: none;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface-2);
  color: var(--color-text2);
  font-size: var(--text-xs);
  text-align: left;
}

button.session-card-head {
  cursor: pointer;
}

button.session-card-head:hover {
  color: var(--color-text);
}

button.session-card-head:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: -2px;
}

/* caret 同 TreeNode 模式:▸ 展开时旋转 90° */
.session-group-caret {
  display: inline-block;
  width: 12px;
  flex: 0 0 12px;
  text-align: center;
  font-size: 10px;
  transition: transform var(--dur-fast) var(--ease-out);
}

.session-group-caret.open {
  transform: rotate(90deg);
}

.session-card-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 会话数是数值 → 等宽 */
.session-card-count {
  margin-left: auto;
  font-family: var(--font-mono);
}

.session-card-body {
  display: flex;
  flex-direction: column;
}

.session-item {
  padding: var(--space-xs) var(--space-sm);
  border-bottom: 1px solid var(--color-border);
  cursor: pointer;
}

.session-item:last-child {
  border-bottom: none;
}

.session-item:hover {
  background: var(--color-hover-bg);
}

.session-item:focus-visible {
  outline: 2px solid var(--color-accent);
  outline-offset: -2px;
}

.session-item.active {
  background: var(--color-accent-soft);
}

.session-item-title {
  font-size: var(--text-sm);
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session-item-meta {
  margin-top: 2px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-xs);
}

/* 操作按钮组(重命名/删除)固定靠右,不随标题/时间挤压 */
.session-item-actions {
  flex: 0 0 auto;
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.session-item-meta-left {
  display: inline-flex;
  align-items: center;
  gap: var(--space-xs);
  min-width: 0;
}

/* 相对时间是时间戳 → 等宽 */
.session-item-time {
  font-size: var(--text-xs);
  color: var(--color-text2);
  font-family: var(--font-mono);
}

/* 绝对发起时间:与相对时间同行,前置分隔点、再弱一档(透明度),不占第二行 */
.session-item-time-abs {
  opacity: 0.65;
}

.session-item-time-abs::before {
  content: '·\00a0';
}

/* 删除按钮:点击即删(store 乐观移除 + 失败回滚);默认淡隐,悬停/聚焦条目或自身时出现 */
.session-del {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  opacity: 0;
}

.session-item:hover .session-del,
.session-item:focus-within .session-del {
  opacity: 1;
}

.session-del:hover {
  color: var(--color-red);
  background: var(--color-red-soft);
}

.session-del:focus-visible {
  opacity: 1;
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

/* 重命名按钮:与删除按钮同款淡隐/显现,hover 用强调色而非危险色 */
.session-edit {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: none;
  border-radius: 50%;
  background: none;
  color: var(--color-text2);
  cursor: pointer;
  opacity: 0;
}

.session-item:hover .session-edit,
.session-item:focus-within .session-edit {
  opacity: 1;
}

.session-edit:hover {
  color: var(--color-accent);
}

.session-edit:focus-visible {
  opacity: 1;
  outline: 2px solid var(--color-accent);
  outline-offset: 1px;
}

/* 重命名原位输入框:替换标题与 meta 行,沿用条目内边距 */
.session-item-rename {
  padding: 2px 0;
}

.session-rename-input {
  width: 100%;
  box-sizing: border-box;
  font: inherit;
  font-size: var(--text-sm);
  color: var(--color-text);
  background: var(--color-card);
  border: 1px solid var(--color-accent);
  border-radius: calc(var(--radius) / 2);
  padding: 4px 6px;
  outline: none;
}

</style>
