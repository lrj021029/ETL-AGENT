<!-- 根组件：公开路由（登录页）直接渲染，其余受保护路由包裹在 AppShell 中；使用原生 Vue Transition 避免异步路由与 AnimatePresence mode="wait" 卡死白屏 -->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { MotionConfig } from 'motion-v'
import AppShell from '@/layouts/AppShell.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const auth = useAuthStore()

onMounted(() => {
  if (auth.isAuthenticated && !auth.userProfile) {
    void auth.fetchCurrentUser().catch(() => {
      // Token 失效时由路由守卫或请求错误引导重新登录
    })
  }
})
</script>

<template>
  <MotionConfig reduced-motion="user">
    <RouterView v-if="route.meta.public" v-slot="{ Component, route: r }">
      <Transition name="page-route" mode="out-in">
        <div v-if="Component" :key="r.path" class="page-route-shell">
          <component :is="Component" />
        </div>
      </Transition>
    </RouterView>
    <AppShell v-else>
      <RouterView v-slot="{ Component, route: r }">
        <Transition name="page-route" mode="out-in">
          <div v-if="Component" :key="r.path" class="page-route-shell">
            <component :is="Component" />
          </div>
        </Transition>
      </RouterView>
    </AppShell>
  </MotionConfig>
</template>
