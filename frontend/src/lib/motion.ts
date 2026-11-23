// Motion for Vue 动效预设：统一时长、缓动与弹簧参数（DESIGN_SYSTEM §8）。
// 全局由 App.vue 的 <MotionConfig reduced-motion="user"> 尊重系统“减少动态效果”设置。
export const easeOut: [number, number, number, number] = [0.22, 1, 0.36, 1]

/** 弹簧：snappy 用于指示器/按压，gentle 用于面板与布局过渡，bouncy 仅用于少量强调反馈 */
export const springs = {
  snappy: { type: 'spring' as const, stiffness: 520, damping: 38, mass: 0.7 },
  gentle: { type: 'spring' as const, stiffness: 260, damping: 30 },
  bouncy: { type: 'spring' as const, stiffness: 420, damping: 20 },
}

/** 页面进入/退出（路由切换，AnimatePresence mode="wait"） */
export const pageMotion = {
  initial: { opacity: 0, y: 8 },
  animate: { opacity: 1, y: 0 },
  exit: { opacity: 0, y: -4 },
}
export const pageTransition = { duration: 0.2, ease: easeOut }

/** 浮层（Dialog / Sheet / Popover）进入/退出 */
export const overlayTransition = { duration: 0.18, ease: easeOut }

/** Hover / Press 交互 */
export const pressMotion = {
  whileHover: { y: -1 },
  whilePress: { scale: 0.97 },
}

/** 滚动触发（whileInView，只播放一次） */
export const revealInView = { once: true, margin: '0px 0px -8% 0px' }
