<!-- 身份认证视图：密码登录 + 邮件二次验证 (Email OTP) -->
<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Mail, ShieldAlert } from 'lucide-vue-next'
import { ApiClientError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import Badge from '@/components/ui/Badge.vue'
import Card from '@/components/ui/Card.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const email = ref('')
const password = ref('')
const otpCode = ref('')
const submitting = ref(false)
const errorInfo = ref<{ code: string; message: string; traceId: string } | null>(null)

async function handlePasswordLogin(): Promise<void> {
  errorInfo.value = null
  submitting.value = true
  try {
    await auth.loginWithPassword(email.value.trim(), password.value)
  } catch (err) {
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'NETWORK_ERROR',
        message: err instanceof Error ? err.message : '登录请求失败',
        traceId: 'client-local',
      }
    }
  } finally {
    submitting.value = false
  }
}

async function handleVerifyOtp(): Promise<void> {
  errorInfo.value = null
  submitting.value = true
  try {
    await auth.verifyEmailOtp(otpCode.value.trim())
    const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : '/overview'
    await router.push(redirect)
  } catch (err) {
    if (err instanceof ApiClientError) {
      errorInfo.value = { code: err.errorCode, message: err.message, traceId: err.traceId }
    } else {
      errorInfo.value = {
        code: 'OTP_VERIFY_FAILED',
        message: err instanceof Error ? err.message : '验证码校验失败',
        traceId: 'client-local',
      }
    }
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center bg-background p-4">
    <div class="w-full max-w-md space-y-4">
      <div class="text-center">
        <Badge variant="info">Control Plane Auth</Badge>
        <h1 class="mt-2 text-xl font-bold tracking-tight">ETL-Agent 企业数据集成控制台</h1>
        <p class="mt-1 text-xs text-muted-foreground">
          密码登录 + 邮件二次验证（本地开发由 Mailpit 捕获验证邮件）
        </p>
      </div>

      <Card>
        <div
          v-if="errorInfo"
          role="alert"
          class="mb-4 rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-xs text-rose-300"
        >
          <div class="font-semibold">{{ errorInfo.code }}: {{ errorInfo.message }}</div>
          <div class="mt-1 font-mono text-[11px] opacity-80">Trace ID: {{ errorInfo.traceId }}</div>
        </div>

        <!-- 步骤 1：账号密码登录 -->
        <form v-if="!auth.otpChallenge" class="space-y-4" @submit.prevent="handlePasswordLogin">
          <div class="space-y-1.5">
            <label class="block text-xs font-medium">邮箱账号</label>
            <input
              v-model="email"
              type="email"
              required
              placeholder="maker@demo.local"
              class="w-full rounded-md border bg-background px-3 py-2 text-sm"
            />
          </div>

          <div class="space-y-1.5">
            <label class="block text-xs font-medium">登录密码</label>
            <input
              v-model="password"
              type="password"
              required
              placeholder="请输入密码"
              class="w-full rounded-md border bg-background px-3 py-2 text-sm"
            />
          </div>

          <button
            type="submit"
            :disabled="submitting"
            class="w-full rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
          >
            {{ submitting ? '正在验证并发送邮件验证码…' : '下一步：发送邮件二次验证' }}
          </button>
        </form>

        <!-- 步骤 2：邮件二次验证 (Email OTP) -->
        <form v-else class="space-y-4" @submit.prevent="handleVerifyOtp">
          <div class="flex items-start gap-2 rounded-md border border-sky-500/30 bg-sky-500/10 p-3 text-xs">
            <Mail class="mt-0.5 h-4 w-4 shrink-0 text-sky-400" />
            <div>
              <div class="font-semibold text-sky-300">邮件二次验证 (Email OTP)</div>
              <p class="mt-0.5 text-muted-foreground">
                验证码已发送至 <span class="font-mono">{{ auth.otpChallenge.masked_email }}</span>，
                有效期 {{ auth.otpChallenge.expires_in_seconds }} 秒。
              </p>
            </div>
          </div>

          <div class="space-y-1.5">
            <label class="block text-xs font-medium">6 位邮件验证码</label>
            <input
              v-model="otpCode"
              type="text"
              required
              maxlength="12"
              placeholder="输入邮件中的验证码"
              class="w-full rounded-md border bg-background px-3 py-2 font-mono text-sm tracking-widest"
            />
          </div>

          <div class="flex gap-2">
            <button
              type="button"
              class="rounded-md border px-3 py-2 text-xs hover:bg-accent"
              @click="auth.otpChallenge = null"
            >
              返回修改账号
            </button>
            <button
              type="submit"
              :disabled="submitting"
              class="flex-1 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-50"
            >
              {{ submitting ? '正在签发 JWT 会话…' : '完成邮件二次验证并进入控制台' }}
            </button>
          </div>
        </form>

        <div class="mt-4 flex items-start gap-2 border-t pt-3 text-[11px] text-muted-foreground">
          <ShieldAlert class="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-400" />
          <span>
            合规说明：当前采用 JWT + 邮件二次验证（Email OTP），TOTP / WebAuthn 强认证列为后续增强项。
          </span>
        </div>
      </Card>
    </div>
  </div>
</template>
