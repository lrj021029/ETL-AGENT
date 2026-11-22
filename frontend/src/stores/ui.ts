// 全局 UI 状态 Store：管理主题切换（深色/浅色）与侧边栏折叠状态
import { ref } from 'vue'
import { defineStore } from 'pinia'

export const useUiStore = defineStore('ui', () => {
  const isDark = ref(true)
  const sidebarCollapsed = ref(false)

  function toggleTheme(): void {
    isDark.value = !isDark.value
    if (typeof document !== 'undefined') {
      document.documentElement.classList.toggle('dark', isDark.value)
    }
  }

  function toggleSidebar(): void {
    sidebarCollapsed.value = !sidebarCollapsed.value
  }

  return {
    isDark,
    sidebarCollapsed,
    toggleTheme,
    toggleSidebar,
  }
})
