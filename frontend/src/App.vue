<!-- 根组件：公开路由（如登录页）直接渲染，其余受保护路由包裹在 AppShell 中 -->
<script setup lang="ts">
import { onMounted } from 'vue'
import { useRoute } from 'vue-router'
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
  <RouterView v-if="route.meta.public" />
  <AppShell v-else>
    <RouterView />
  </AppShell>
</template>
