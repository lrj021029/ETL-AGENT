// 全局 UI 状态 Store：浅色单主题（TBD-07 已决策，不再提供深色切换），仅管理侧栏折叠状态（localStorage 持久化，纯 UI 状态）
import { ref } from 'vue'
import { defineStore } from 'pinia'

const SIDEBAR_KEY = 'etl-agent.sidebar-collapsed'

function readCollapsed(): boolean {
  try {
    return typeof localStorage !== 'undefined' && localStorage.getItem(SIDEBAR_KEY) === '1'
  } catch {
    return false
  }
}

export const useUiStore = defineStore('ui', () => {
  const sidebarCollapsed = ref(readCollapsed())

  function setSidebarCollapsed(value: boolean): void {
    sidebarCollapsed.value = value
    try {
      localStorage.setItem(SIDEBAR_KEY, value ? '1' : '0')
    } catch {
      // 无痕模式等无法写入时仅保留内存状态
    }
  }

  function toggleSidebar(): void {
    setSidebarCollapsed(!sidebarCollapsed.value)
  }

  return {
    sidebarCollapsed,
    setSidebarCollapsed,
    toggleSidebar,
  }
})
