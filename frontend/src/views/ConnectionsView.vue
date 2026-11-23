<!-- 数据源与资产管理：连接器能力矩阵抽屉、连接 CRUD/探活、Secret 引用掩码、只读元数据快照、Drift Diff、敏感标记与 MinIO 文件上传 -->
<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ApiClientError } from '@/api/client'
import { api } from '@/api'
import { useAuthStore } from '@/stores/auth'
import type {
  ConnectionItem,
  ConnectorRegistryEntry,
  MetadataSnapshot,
  SchemaDriftEvent,
  ViewDataState,
} from '@/types/api'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'
import Drawer from '@/components/ui/Drawer.vue'
import StateView from '@/components/ui/StateView.vue'

const auth = useAuthStore()
const state = ref<ViewDataState>('loading')
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

const connections = ref<ConnectionItem[]>([])
const registry = ref<ConnectorRegistryEntry[]>([])
const matrixDrawerOpen = ref(false)
const createFormOpen = ref(false)

// 新建连接表单（仅接受 SecretProvider 引用，不保存明文密码）
const newConn = ref({
  name: '',
  connector_type: 'mysql',
  host: 'demo-mysql',
  port: 3306,
  database: 'demo_db',
  secret_ref: 'vault://secret/data/etl/demo-mysql',
})

// 选中连接的元数据与 Drift 状态
const selectedConnection = ref<ConnectionItem | null>(null)
const snapshots = ref<MetadataSnapshot[]>([])
const selectedSnapshotId = ref<string>('')
const driftEvents = ref<SchemaDriftEvent[]>([])
const actionFeedback = ref<string | null>(null)

// MinIO 文件上传状态
const uploadingFile = ref(false)
const uploadedObjectKey = ref<string | null>(null)

const activeSnapshot = computed(
  () => snapshots.value.find((s) => s.id === selectedSnapshotId.value) ?? snapshots.value[0] ?? null,
)

function registryBadgeVariant(status: ConnectorRegistryEntry['verification_status']) {
  if (status === 'END_TO_END_VERIFIED') return 'success'
  if (status === 'INTEGRATION_TESTED') return 'info'
  if (status === 'CONFIGURED') return 'warning'
  return 'outline'
}

async function loadData(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) {
    state.value = 'empty'
    return
  }
  state.value = 'loading'
  errorInfo.value = null
  try {
    const [connRes, regRes] = await Promise.all([
      api.listConnections(projectId),
      api.getConnectorRegistry(),
    ])
    connections.value = connRes.items
    registry.value = regRes.items
    state.value = connRes.items.length === 0 ? 'empty' : 'success'
  } catch (err) {
    state.value = 'error'
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'CONNECTIONS_LOAD_ERROR',
        message: err instanceof Error ? err.message : '加载连接列表失败',
        traceId: 'client-local',
      }
    }
  }
}

async function handleCreateConnection(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  actionFeedback.value = null
  try {
    await api.createConnection(projectId, {
      name: newConn.value.name,
      connector_type: newConn.value.connector_type,
      environment: auth.activeEnvironment,
      config: {
        host: newConn.value.host,
        port: newConn.value.port,
        database: newConn.value.database,
      },
      secret_ref: newConn.value.secret_ref,
    })
    createFormOpen.value = false
    newConn.value.name = ''
    await loadData()
  } catch (err) {
    actionFeedback.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleTestConnection(conn: ConnectionItem): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  actionFeedback.value = null
  try {
    const res = await api.testConnection(projectId, conn.id)
    actionFeedback.value = `探活结果 [${conn.name}]: ${res.reachable ? '连通正常' : '不可达'} (${res.message})`
    await loadData()
  } catch (err) {
    actionFeedback.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function selectConnection(conn: ConnectionItem): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId) return
  selectedConnection.value = conn
  actionFeedback.value = null
  try {
    const [snapRes, driftRes] = await Promise.all([
      api.listMetadataSnapshots(projectId, conn.id),
      api.getSchemaDrift(projectId, conn.id),
    ])
    snapshots.value = snapRes.items || []
    selectedSnapshotId.value = snapshots.value[0]?.id || ''
    driftEvents.value = driftRes.items || []
  } catch (err) {
    actionFeedback.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleProbeMetadata(): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !selectedConnection.value) return
  actionFeedback.value = '正在执行受管只读 Metadata Probe…'
  try {
    const snap = await api.probeMetadata(projectId, selectedConnection.value.id)
    snapshots.value = [snap, ...snapshots.value]
    selectedSnapshotId.value = snap.id
    const driftRes = await api.getSchemaDrift(projectId, selectedConnection.value.id)
    driftEvents.value = driftRes.items || []
    actionFeedback.value = `只读探查成功：共发现 ${snap.tables?.length ?? 0} 张表，Schema 指纹 ${(snap.schema_fingerprint || '').slice(0, 16)}…`
  } catch (err) {
    actionFeedback.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleTagSensitivity(
  tableName: string,
  columnName: string,
  tag: string,
): Promise<void> {
  const projectId = auth.activeProject?.id
  if (!projectId || !activeSnapshot.value) return
  try {
    const updated = await api.updateColumnClassification(projectId, activeSnapshot.value.id, {
      table_name: tableName,
      column_name: columnName,
      sensitivity_tag: tag,
    })
    const idx = snapshots.value.findIndex((s) => s.id === activeSnapshot.value?.id)
    if (idx >= 0 && updated.tables) snapshots.value[idx] = { ...snapshots.value[idx], ...updated }
    actionFeedback.value = `已更新敏感分级：${tableName}.${columnName} → ${tag}`
  } catch (err) {
    actionFeedback.value = err instanceof ApiClientError ? `${err.errorCode}: ${err.message}` : String(err)
  }
}

async function handleFileUpload(event: Event): Promise<void> {
  const projectId = auth.activeProject?.id
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!projectId || !file) return
  uploadingFile.value = true
  try {
    const res = await api.uploadFile(projectId, file)
    uploadedObjectKey.value = `${res.bucket}/${res.object_key} (${res.size_bytes} bytes)`
  } finally {
    uploadingFile.value = false
  }
}

onMounted(() => {
  void loadData()
})

watch(
  () => auth.activeProject?.id,
  () => {
    void loadData()
  },
)
</script>

<template>
  <div class="space-y-5">
    <div class="flex flex-wrap items-center justify-between gap-3">
      <div>
        <h1 class="text-lg font-bold tracking-tight">数据源与资产管理</h1>
        <p class="text-xs text-muted-foreground">
          Secret 引用严格掩码，只读 Metadata Probe 生成不可变快照与 Schema Fingerprint
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2 text-xs">
        <button
          type="button"
          class="rounded-md border px-3 py-1.5 hover:bg-accent"
          @click="matrixDrawerOpen = true"
        >
          连接器能力矩阵 ({{ registry.length }})
        </button>
        <label class="cursor-pointer rounded-md border px-3 py-1.5 hover:bg-accent">
          <span>{{ uploadingFile ? '上传 MinIO 中…' : '上传文件至 MinIO' }}</span>
          <input type="file" class="hidden" @change="handleFileUpload" />
        </label>
        <button
          type="button"
          class="rounded-md bg-primary px-3 py-1.5 font-medium text-primary-foreground hover:opacity-90"
          @click="createFormOpen = !createFormOpen"
        >
          + 新建连接
        </button>
      </div>
    </div>

    <div
      v-if="uploadedObjectKey"
      class="rounded-md border border-success/30 bg-success-soft px-3 py-2 font-mono text-xs text-success-text"
    >
      MinIO 租户隔离对象已上传：{{ uploadedObjectKey }}
    </div>

    <div
      v-if="actionFeedback"
      class="rounded-md border border-info/30 bg-info-soft px-3 py-2 text-xs text-info-text"
    >
      {{ actionFeedback }}
    </div>

    <!-- 新建连接表单 -->
    <Card v-if="createFormOpen" title="登记受管连接（SecretProvider 引用）">
      <form class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" @submit.prevent="handleCreateConnection">
        <input
          v-model="newConn.name"
          required
          placeholder="连接名称（如 demo-mysql-orders）"
          class="rounded-sm border bg-background px-3 py-1.5 text-xs"
        />
        <select v-model="newConn.connector_type" class="rounded-sm border bg-background px-3 py-1.5 text-xs">
          <option value="mysql">MySQL</option>
          <option value="postgres">PostgreSQL</option>
          <option value="doris">Apache Doris</option>
          <option value="minio">MinIO / S3</option>
          <option value="rest_api">REST API Profile</option>
        </select>
        <input
          v-model="newConn.host"
          required
          placeholder="Host"
          class="rounded-sm border bg-background px-3 py-1.5 text-xs"
        />
        <input
          v-model.number="newConn.port"
          type="number"
          required
          placeholder="Port"
          class="rounded-sm border bg-background px-3 py-1.5 text-xs"
        />
        <input
          v-model="newConn.database"
          required
          placeholder="Database"
          class="rounded-sm border bg-background px-3 py-1.5 text-xs"
        />
        <input
          v-model="newConn.secret_ref"
          type="password"
          required
          placeholder="Secret 引用（如 vault://secret/...）"
          class="rounded-sm border bg-background px-3 py-1.5 font-mono text-xs"
        />
        <div class="sm:col-span-2 lg:col-span-3 flex justify-end gap-2">
          <button
            type="button"
            class="rounded-sm border px-3 py-1.5 text-xs"
            @click="createFormOpen = false"
          >
            取消
          </button>
          <button
            type="submit"
            class="rounded-sm bg-primary px-3 py-1.5 text-xs font-medium text-primary-foreground"
          >
            保存连接
          </button>
        </div>
      </form>
    </Card>

    <!-- 连接列表 -->
    <StateView
      :state="state"
      :error-code="errorInfo?.code"
      :error-message="errorInfo?.message"
      :trace-id="errorInfo?.traceId"
      empty-title="暂无数据源连接"
      empty-description="点击右上角「+ 新建连接」登记 MySQL / PostgreSQL / Doris / MinIO 等数据源。"
      @retry="loadData"
    >
      <div class="grid gap-4 lg:grid-cols-2">
        <Card
          v-for="conn in connections"
          :key="conn.id"
          :title="conn.name"
          :subtitle="`${(conn.connector_type || 'unknown').toUpperCase()} · ${conn.host_summary || '-'}`"
        >
          <template #header>
            <Badge
              :variant="
                conn.last_test_status === 'OK'
                  ? 'success'
                  : conn.last_test_status === 'FAILED'
                    ? 'danger'
                    : 'outline'
              "
            >
              {{ conn.last_test_status || 'UNTESTED' }}
            </Badge>
          </template>
          <div class="space-y-2 text-xs">
            <div class="flex justify-between">
              <span class="text-muted-foreground">Secret 引用（掩码）:</span>
              <span class="font-mono">{{ conn.secret_masked }}</span>
            </div>
            <div class="flex gap-2 pt-2">
              <button
                type="button"
                class="rounded-sm border px-2.5 py-1 hover:bg-accent"
                @click="handleTestConnection(conn)"
              >
                只读探活
              </button>
              <button
                type="button"
                class="rounded-sm border border-primary/40 bg-primary/10 px-2.5 py-1 text-primary hover:bg-primary/20"
                @click="selectConnection(conn)"
              >
                Metadata 快照 & Drift
              </button>
            </div>
          </div>
        </Card>
      </div>
    </StateView>

    <!-- Metadata Snapshot 与 Schema Drift 审查区 -->
    <Card
      v-if="selectedConnection"
      :title="`只读元数据资产：${selectedConnection.name}`"
      subtitle="不可变 MetadataSnapshot、Schema Fingerprint、敏感分级标记与脱敏样本"
    >
      <template #header>
        <div class="flex items-center gap-2">
          <select
            v-model="selectedSnapshotId"
            class="rounded-sm border bg-background px-2 py-1 font-mono text-xs"
          >
            <option v-for="snap in snapshots" :key="snap.id" :value="snap.id">
              {{ snap.captured_at }} ({{ (snap.schema_fingerprint || '').slice(0, 12) }})
            </option>
          </select>
          <button
            type="button"
            class="rounded-sm bg-primary px-3 py-1 text-xs text-primary-foreground"
            @click="handleProbeMetadata"
          >
            发起只读 Probe
          </button>
        </div>
      </template>

      <div v-if="activeSnapshot" class="space-y-4 text-xs">
        <div class="rounded-sm border bg-muted/40 p-2 font-mono">
          Schema Fingerprint: {{ activeSnapshot.schema_fingerprint }}
        </div>

        <div v-for="tbl in activeSnapshot.tables" :key="`${tbl.schema_name}.${tbl.table_name}`" class="space-y-2">
          <div class="font-semibold">
            {{ tbl.schema_name }}.{{ tbl.table_name }}
            <span class="font-normal text-muted-foreground">(约 {{ tbl.approx_rows ?? '-' }} 行)</span>
          </div>
          <table class="w-full text-left text-xs">
            <thead class="border-b text-muted-foreground">
              <tr>
                <th class="py-1.5 pr-3">列名</th>
                <th class="py-1.5 pr-3">类型</th>
                <th class="py-1.5 pr-3">敏感分级</th>
                <th class="py-1.5">脱敏样本</th>
              </tr>
            </thead>
            <tbody class="divide-y">
              <tr v-for="col in tbl.columns" :key="col.name">
                <td class="py-1.5 pr-3 font-mono">
                  {{ col.name }}
                  <Badge v-if="col.is_primary_key" variant="info">PK</Badge>
                </td>
                <td class="py-1.5 pr-3 font-mono">{{ col.data_type }}</td>
                <td class="py-1.5 pr-3">
                  <select
                    :value="col.sensitivity_tag || 'NONE'"
                    class="rounded-sm border bg-background px-1.5 py-0.5 text-xs"
                    @change="
                      handleTagSensitivity(
                        tbl.table_name,
                        col.name,
                        ($event.target as HTMLSelectElement).value,
                      )
                    "
                  >
                    <option value="NONE">NONE</option>
                    <option value="PII">PII</option>
                    <option value="PCI">PCI</option>
                    <option value="CONFIDENTIAL">CONFIDENTIAL</option>
                    <option value="RESTRICTED">RESTRICTED</option>
                  </select>
                </td>
                <td class="py-1.5 font-mono text-muted-foreground">
                  {{ (col.masked_samples || []).join(', ') || '***' }}
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Schema Drift Diff -->
        <div class="border-t pt-3">
          <h4 class="mb-2 font-semibold">Schema Drift 历史对比</h4>
          <div v-if="driftEvents.length === 0" class="text-muted-foreground">
            未检测到跨快照 Schema 漂移。
          </div>
          <div
            v-for="ev in driftEvents"
            :key="ev.id"
            class="mb-2 rounded-sm border border-warning/40 bg-warning-soft p-2.5"
          >
            <div class="flex items-center justify-between">
              <Badge variant="warning">Drift Severity: {{ ev.severity }}</Badge>
              <span class="font-mono text-xs text-muted-foreground">{{ ev.detected_at }}</span>
            </div>
            <ul class="mt-1.5 space-y-1 font-mono text-xs">
              <li v-for="(d, idx) in ev.diffs" :key="idx">
                [{{ d.change_type }}] {{ d.table }}.{{ d.column }}: {{ d.before || '∅' }} →
                {{ d.after || '∅' }}
              </li>
            </ul>
          </div>
        </div>
      </div>
      <div v-else class="text-xs text-muted-foreground">
        暂无 Metadata 快照，请点击「发起只读 Probe」。
      </div>
    </Card>

    <!-- Connector Registry 能力矩阵抽屉 -->
    <Drawer
      :open="matrixDrawerOpen"
      title="Connector Registry 能力矩阵"
      subtitle="实时读取 /api/v1/connectors/registry，严禁虚标未验证连接器"
      @close="matrixDrawerOpen = false"
    >
      <div class="space-y-3">
        <div
          v-for="entry in registry"
          :key="entry.connector_type"
          class="rounded-md border p-3 text-xs"
        >
          <div class="flex items-center justify-between">
            <span class="font-semibold">{{ entry.display_name }} ({{ entry.connector_type }})</span>
            <Badge :variant="registryBadgeVariant(entry.verification_status)">
              {{ entry.verification_status }}
            </Badge>
          </div>
          <div class="mt-1.5 flex flex-wrap gap-2 text-muted-foreground">
            <span>模式: {{ (entry.modes || []).join(' / ') }}</span>
            <span>方向: {{ (entry.directions || []).join(' / ') }}</span>
          </div>
          <p v-if="entry.notes" class="mt-1 text-xs text-muted-foreground">{{ entry.notes }}</p>
        </div>
      </div>
    </Drawer>
  </div>
</template>
