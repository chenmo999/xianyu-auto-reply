/**
 * 批量发布页面
 *
 * 功能：
 * 1. 选择多个闲鱼账号
 * 2. 从素材库选择多条素材
 * 3. 支持批量发布队列：可连续提交多个任务
 * 4. 等待中的任务可取消，正在执行中的任务可停止
 * 5. 轮询任务进度，展示完成状态
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { motion } from 'framer-motion'
import { Layers, CheckCircle, XCircle, Clock, Play, Loader2, Square, Trash2, ListOrdered } from 'lucide-react'
import { useUIStore } from '@/store/uiStore'
import {
  publishBatch,
  getBatchStatus,
  cancelBatchPublish,
  getMaterials,
  type ProductMaterial,
  type BatchAccountStatus,
} from '@/api/productPublish'
import { getAccountDetails, getAccountCategories } from '@/api/accounts'

interface BatchProgress {
  batch_id: string
  total: number
  success: number
  failed: number
  publishing: number
  pending: number
  finished: boolean
  cancelled?: boolean
  cancel_message?: string | null
  account_statuses: BatchAccountStatus[]
}

type BatchQueueStatus = 'waiting' | 'submitting' | 'running' | 'finished' | 'cancelled' | 'failed'

interface BatchQueueItem {
  local_id: string
  batch_id?: string
  account_ids: string[]
  material_ids: number[]
  account_count: number
  material_count: number
  total: number
  status: BatchQueueStatus
  created_at: number
  message?: string
  progress?: BatchProgress
}

// localStorage/sessionStorage 键名：保存进行中的 batch_id 与等待队列
// v1.4.6：修复刷新页面后，前端等待队列丢失的问题。
const BATCH_ID_STORAGE_KEY = 'batch_publish_active_batch_id'
const BATCH_QUEUE_STORAGE_KEY = 'batch_publish_queue_items_v2'
const QUEUE_RESTORE_MAX_AGE_MS = 24 * 60 * 60 * 1000
const MAX_STORED_QUEUE_ITEMS = 50
const ACTIVE_QUEUE_STATUSES: BatchQueueStatus[] = ['waiting', 'submitting', 'running']
const ALL_QUEUE_STATUSES: BatchQueueStatus[] = ['waiting', 'submitting', 'running', 'finished', 'cancelled', 'failed']

const isQueueStatus = (value: unknown): value is BatchQueueStatus =>
  typeof value === 'string' && ALL_QUEUE_STATUSES.includes(value as BatchQueueStatus)

const sanitizeStoredQueueItem = (raw: any): BatchQueueItem | null => {
  if (!raw || typeof raw !== 'object') return null
  const localId = typeof raw.local_id === 'string' && raw.local_id.trim()
    ? raw.local_id.trim()
    : `restored_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
  const status: BatchQueueStatus = isQueueStatus(raw.status)
    ? (raw.status === 'submitting' && !raw.batch_id ? 'waiting' : raw.status)
    : 'waiting'
  const accountIds = Array.isArray(raw.account_ids)
    ? raw.account_ids.map((id: any) => String(id)).filter(Boolean)
    : []
  const materialIds = Array.isArray(raw.material_ids)
    ? raw.material_ids.map((id: any) => Number(id)).filter((id: number) => Number.isFinite(id))
    : []
  const batchId = typeof raw.batch_id === 'string' && raw.batch_id.trim() ? raw.batch_id.trim() : undefined
  if (accountIds.length === 0 && !batchId) return null
  if (materialIds.length === 0 && !batchId) return null
  const accountCount = Number(raw.account_count || accountIds.length || 0)
  const materialCount = Number(raw.material_count || materialIds.length || 0)
  const total = Number(raw.total || (accountCount * materialCount) || 0)
  const createdAt = Number(raw.created_at || Date.now())
  return {
    local_id: localId,
    batch_id: batchId,
    account_ids: accountIds,
    material_ids: materialIds,
    account_count: Number.isFinite(accountCount) ? accountCount : accountIds.length,
    material_count: Number.isFinite(materialCount) ? materialCount : materialIds.length,
    total: Number.isFinite(total) ? total : 0,
    status,
    created_at: Number.isFinite(createdAt) ? createdAt : Date.now(),
    message: typeof raw.message === 'string' ? raw.message : undefined,
    progress: raw.progress && typeof raw.progress === 'object' ? raw.progress as BatchProgress : undefined,
  }
}

const loadStoredQueueItems = (): BatchQueueItem[] => {
  try {
    if (typeof window === 'undefined') return []
    const raw = window.localStorage.getItem(BATCH_QUEUE_STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    const now = Date.now()
    return parsed
      .map(sanitizeStoredQueueItem)
      .filter((item): item is BatchQueueItem => Boolean(item))
      .filter(item => ACTIVE_QUEUE_STATUSES.includes(item.status) || now - item.created_at <= QUEUE_RESTORE_MAX_AGE_MS)
      .slice(-MAX_STORED_QUEUE_ITEMS)
  } catch {
    return []
  }
}

const persistQueueItems = (items: BatchQueueItem[]) => {
  try {
    if (typeof window === 'undefined') return
    const now = Date.now()
    const storedItems = items
      .filter(item => ACTIVE_QUEUE_STATUSES.includes(item.status) || now - item.created_at <= QUEUE_RESTORE_MAX_AGE_MS)
      .slice(-MAX_STORED_QUEUE_ITEMS)
    window.localStorage.setItem(BATCH_QUEUE_STORAGE_KEY, JSON.stringify(storedItems))
  } catch { /* ignore */ }
}

const readStoredBatchId = (): string | null => {
  try {
    if (typeof window === 'undefined') return null
    return window.sessionStorage.getItem(BATCH_ID_STORAGE_KEY) || window.localStorage.getItem(BATCH_ID_STORAGE_KEY)
  } catch {
    return null
  }
}

export function BatchPublish() {
  const { addToast } = useUIStore()
  const [accounts, setAccounts] = useState<any[]>([])
  const [accountCategories, setAccountCategories] = useState<string[]>([])
  const [materials, setMaterials] = useState<ProductMaterial[]>([])
  const [selectedAccounts, setSelectedAccounts] = useState<Set<string>>(new Set())
  const [selectedMaterials, setSelectedMaterials] = useState<Set<number>>(new Set())
  const [loadingAccounts, setLoadingAccounts] = useState(true)
  const [loadingMaterials, setLoadingMaterials] = useState(true)
  const [submitting, setSubmitting] = useState(false)
  const [progress, setProgress] = useState<BatchProgress | null>(null)
  const [queueItems, setQueueItems] = useState<BatchQueueItem[]>(() => loadStoredQueueItems())
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const queueItemsRef = useRef<BatchQueueItem[]>([])
  const runningQueueIdRef = useRef<string | null>(null)
  const startQueueItemRef = useRef<(item: BatchQueueItem) => void>(() => undefined)
  const [materialSearch, setMaterialSearch] = useState('')

  const accountNameMap = new Map(accounts.map((account: any) => [account.id, account.note || account.id]))
  const normalizeCategory = (value?: string | null) => (value && value.trim()) ? value.trim() : '默认'
  const categoryAccountCountMap = new Map<string, number>()
  accounts.forEach((account: any) => {
    const category = normalizeCategory(account.category)
    categoryAccountCountMap.set(category, (categoryAccountCountMap.get(category) || 0) + 1)
  })
  const displayAccountCategories = Array.from(new Set([
    ...accountCategories.map(normalizeCategory),
    ...accounts.map((account: any) => normalizeCategory(account.category)),
  ])).filter(Boolean)

  const updateQueueItems = useCallback((updater: BatchQueueItem[] | ((prev: BatchQueueItem[]) => BatchQueueItem[])) => {
    setQueueItems(prev => {
      const next = typeof updater === 'function'
        ? (updater as (prev: BatchQueueItem[]) => BatchQueueItem[])(prev)
        : updater
      queueItemsRef.current = next
      persistQueueItems(next)
      return next
    })
  }, [])

  useEffect(() => {
    queueItemsRef.current = queueItems
  }, [queueItems])

  const getSyncStatusLabel = (status: BatchAccountStatus['sync_status']) => {
    if (status === 'success') return '已成功'
    if (status === 'failed') return '失败'
    if (status === 'running') return '获取中'
    if (status === 'skipped') return '未触发'
    if (status === 'unknown') return '状态未知'
    return '待执行'
  }

  const getSyncStatusClassName = (status: BatchAccountStatus['sync_status']) => {
    if (status === 'success') return 'badge-success'
    if (status === 'failed') return 'badge-danger'
    if (status === 'running') return 'badge-info'
    if (status === 'skipped') return 'badge-warning'
    if (status === 'unknown') return 'badge-warning'
    return 'badge-secondary'
  }

  const getQueueStatusLabel = (status: BatchQueueStatus) => {
    if (status === 'waiting') return '等待中'
    if (status === 'submitting') return '提交中'
    if (status === 'running') return '执行中'
    if (status === 'finished') return '已完成'
    if (status === 'cancelled') return '已取消'
    return '失败'
  }

  const getQueueStatusClassName = (status: BatchQueueStatus) => {
    if (status === 'waiting') return 'badge-secondary'
    if (status === 'submitting' || status === 'running') return 'badge-info'
    if (status === 'finished') return 'badge-success'
    if (status === 'cancelled') return 'badge-warning'
    return 'badge-danger'
  }

  /** 清除 storage 中的 batch_id */
  const clearStoredBatchId = useCallback(() => {
    try { sessionStorage.removeItem(BATCH_ID_STORAGE_KEY) } catch { /* ignore */ }
    try { localStorage.removeItem(BATCH_ID_STORAGE_KEY) } catch { /* ignore */ }
  }, [])

  /** 保存 batch_id 到 storage，刷新页面或关闭标签页后仍能恢复 */
  const storeBatchId = useCallback((batchId: string) => {
    try { sessionStorage.setItem(BATCH_ID_STORAGE_KEY, batchId) } catch { /* ignore */ }
    try { localStorage.setItem(BATCH_ID_STORAGE_KEY, batchId) } catch { /* ignore */ }
  }, [])

  const startNextWaitingQueue = useCallback(() => {
    if (runningQueueIdRef.current) return
    const next = queueItemsRef.current.find(item => item.status === 'waiting')
    if (next) {
      setTimeout(() => startQueueItemRef.current(next), 0)
    }
  }, [])

  /** 启动轮询批量发布进度 */
  const startPolling = useCallback((batchId: string, localQueueId?: string) => {
    if (pollingRef.current) clearInterval(pollingRef.current)
    pollingRef.current = setInterval(async () => {
      try {
        const res = await getBatchStatus(batchId)
        if (res.success) {
          const nextProgress = res.data
          setProgress(nextProgress)
          if (localQueueId) {
            updateQueueItems(prev => prev.map(item => item.local_id === localQueueId
              ? {
                  ...item,
                  batch_id: batchId,
                  progress: nextProgress,
                  status: nextProgress.finished
                    ? (nextProgress.cancelled ? 'cancelled' : 'finished')
                    : 'running',
                  message: nextProgress.cancelled
                    ? (nextProgress.cancel_message || '批量发布已手动停止')
                    : item.message,
                }
              : item,
            ))
          }

          if (nextProgress.finished) {
            if (pollingRef.current) clearInterval(pollingRef.current)
            pollingRef.current = null
            runningQueueIdRef.current = null
            setSubmitting(false)
            clearStoredBatchId()

            if (nextProgress.cancelled) {
              addToast({ type: 'warning', message: nextProgress.cancel_message || '批量发布已停止' })
            } else {
              const syncFailedCount = nextProgress.account_statuses.filter(item => item.sync_status === 'failed').length
              const syncUnknownCount = nextProgress.account_statuses.filter(item => item.sync_status === 'unknown').length
              const syncProblemCount = syncFailedCount + syncUnknownCount
              addToast({
                type: nextProgress.failed === 0 && syncProblemCount === 0 ? 'success' : 'warning',
                message: syncFailedCount > 0
                  ? `批量发布完成！成功 ${nextProgress.success} 条，失败 ${nextProgress.failed} 条，${syncFailedCount} 个账号自动获取商品失败`
                  : syncUnknownCount > 0
                    ? `批量发布完成！成功 ${nextProgress.success} 条，失败 ${nextProgress.failed} 条，${syncUnknownCount} 个账号自动获取商品状态未知`
                    : `批量发布完成！成功 ${nextProgress.success} 条，失败 ${nextProgress.failed} 条`,
              })
            }
            startNextWaitingQueue()
          }
        } else {
          if (pollingRef.current) clearInterval(pollingRef.current)
          pollingRef.current = null
          runningQueueIdRef.current = null
          setProgress(null)
          setSubmitting(false)
          if (localQueueId) {
            updateQueueItems(prev => prev.map(item => item.local_id === localQueueId
              ? { ...item, status: 'failed', message: res.message || '批量任务状态已失效' }
              : item,
            ))
          }
          addToast({ type: 'warning', message: res.message || '批量任务状态已失效，请重新提交任务' })
          startNextWaitingQueue()
        }
      } catch { /* 静默处理轮询错误 */ }
    }, 3000)
  }, [addToast, clearStoredBatchId, startNextWaitingQueue, updateQueueItems])

  const startQueueItem = useCallback(async (item: BatchQueueItem) => {
    if (runningQueueIdRef.current && runningQueueIdRef.current !== item.local_id) return
    runningQueueIdRef.current = item.local_id
    setSubmitting(true)
    updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
      ? { ...queueItem, status: 'submitting', message: '正在提交到后端队列' }
      : queueItem,
    ))
    try {
      const res = await publishBatch({
        account_ids: item.account_ids,
        material_ids: item.material_ids,
      })
      if (res.success) {
        const batchId = res.data?.batch_id
        const totalCount = res.data?.total ?? item.total
        const accountCount = item.account_count
        const materialCountPerAccount = accountCount > 0 ? Math.floor(totalCount / accountCount) : 0
        if (!batchId) {
          throw new Error('后端未返回 batch_id')
        }

        const currentItem = queueItemsRef.current.find(queueItem => queueItem.local_id === item.local_id)
        if (currentItem?.status === 'cancelled') {
          await cancelBatchPublish(batchId).catch(() => undefined)
          runningQueueIdRef.current = null
          setSubmitting(false)
          startNextWaitingQueue()
          return
        }

        const initialProgress: BatchProgress = {
          batch_id: batchId,
          total: totalCount,
          success: 0,
          failed: 0,
          publishing: 0,
          pending: totalCount,
          finished: false,
          cancelled: false,
          cancel_message: null,
          account_statuses: item.account_ids.map(accountId => ({
            account_id: accountId,
            total: materialCountPerAccount,
            success: 0,
            failed: 0,
            publishing: 0,
            pending: materialCountPerAccount,
            sync_status: 'pending',
            sync_message: '等待该账号发布完成后自动获取商品',
            sync_total_count: 0,
            sync_saved_count: 0,
          })),
        }
        storeBatchId(batchId)
        setProgress(initialProgress)
        updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
          ? { ...queueItem, status: 'running', batch_id: batchId, progress: initialProgress, message: '正在执行' }
          : queueItem,
        ))
        setSubmitting(false)
        addToast({ type: 'success', message: res.message || '批量发布任务已开始执行' })
        startPolling(batchId, item.local_id)
      } else {
        updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
          ? { ...queueItem, status: 'failed', message: res.message || '提交失败' }
          : queueItem,
        ))
        addToast({ type: 'error', message: res.message || '提交失败' })
        runningQueueIdRef.current = null
        setSubmitting(false)
        startNextWaitingQueue()
      }
    } catch (error: any) {
      updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
        ? { ...queueItem, status: 'failed', message: error?.message || '网络错误，请重试' }
        : queueItem,
      ))
      addToast({ type: 'error', message: error?.message || '网络错误，请重试' })
      runningQueueIdRef.current = null
      setSubmitting(false)
      startNextWaitingQueue()
    }
  }, [addToast, startNextWaitingQueue, startPolling, storeBatchId, updateQueueItems])

  useEffect(() => {
    startQueueItemRef.current = startQueueItem
  }, [startQueueItem])

  useEffect(() => {
    getAccountDetails()
      .then(list => { setAccounts(list); setLoadingAccounts(false) })
      .catch(() => { setLoadingAccounts(false) })
    getAccountCategories()
      .then(list => { setAccountCategories(Array.isArray(list) ? list : []) })
      .catch(() => { setAccountCategories([]) })
    getMaterials(1, 1000)
      .then(res => { if (res.success) setMaterials(res.data.list); setLoadingMaterials(false) })
      .catch(() => { setLoadingMaterials(false) })

    // v1.4.6：恢复刷新页面前的批量发布队列。
    // 注意：v1.0.4 的等待队列只保存在 React 内存里，刷新页面会丢失。
    // 这里从 localStorage 恢复等待任务，并继续轮询正在执行的 batch_id。
    try {
      const storedActiveItems = queueItemsRef.current.filter(item => ACTIVE_QUEUE_STATUSES.includes(item.status))
      const storedRunningItem = storedActiveItems.find(item => item.batch_id && (item.status === 'running' || item.status === 'submitting'))
      const savedBatchId = readStoredBatchId() || storedRunningItem?.batch_id || null

      if (savedBatchId) {
        const localQueueId = storedRunningItem?.local_id || `restored_${savedBatchId}`
        getBatchStatus(savedBatchId).then(res => {
          if (res.success && !res.data.finished) {
            const restoredItem: BatchQueueItem = storedRunningItem
              ? {
                  ...storedRunningItem,
                  local_id: localQueueId,
                  batch_id: savedBatchId,
                  status: 'running',
                  message: storedRunningItem.message || '页面刷新后已恢复正在执行的任务',
                  progress: res.data,
                }
              : {
                  local_id: localQueueId,
                  batch_id: savedBatchId,
                  account_ids: res.data.account_statuses.map(item => item.account_id),
                  material_ids: [],
                  account_count: res.data.account_statuses.length,
                  material_count: 0,
                  total: res.data.total,
                  status: 'running',
                  created_at: Date.now(),
                  message: '页面刷新后已恢复正在执行的任务',
                  progress: res.data,
                }
            runningQueueIdRef.current = restoredItem.local_id
            storeBatchId(savedBatchId)
            updateQueueItems(prev => {
              const rest = prev.filter(item => item.local_id !== restoredItem.local_id && item.batch_id !== savedBatchId)
              return [restoredItem, ...rest]
            })
            setProgress(res.data)
            startPolling(savedBatchId, restoredItem.local_id)
            return
          }

          if (res.success && res.data.finished) {
            setProgress(res.data)
            updateQueueItems(prev => prev.map(item => item.batch_id === savedBatchId
              ? {
                  ...item,
                  status: res.data.cancelled ? 'cancelled' : 'finished',
                  progress: res.data,
                  message: res.data.cancelled ? (res.data.cancel_message || '批量发布已停止') : '已完成',
                }
              : item,
            ))
          }
          clearStoredBatchId()
          runningQueueIdRef.current = null
          setSubmitting(false)
          setTimeout(() => startNextWaitingQueue(), 200)
        }).catch(() => {
          // 查询失败时不丢弃本地队列，稍后继续启动后续等待任务前先给后端一点恢复时间。
          setTimeout(() => startNextWaitingQueue(), 5000)
        })
      } else {
        updateQueueItems(prev => prev.map(item => item.status === 'submitting' && !item.batch_id
          ? { ...item, status: 'waiting', message: '页面刷新后已恢复到等待队列' }
          : item,
        ))
        setTimeout(() => startNextWaitingQueue(), 200)
      }
    } catch {
      setTimeout(() => startNextWaitingQueue(), 200)
    }

    return () => { if (pollingRef.current) clearInterval(pollingRef.current) }
  }, [startPolling, clearStoredBatchId, updateQueueItems, startNextWaitingQueue, storeBatchId])

  /** 提交批量发布任务：如果当前有任务在执行，则加入前端等待队列 */
  const handleSubmit = async () => {
    if (selectedAccounts.size === 0) { addToast({ type: 'warning', message: '请至少选择一个账号' }); return }
    if (selectedMaterials.size === 0) { addToast({ type: 'warning', message: '请至少选择一条素材' }); return }

    const item: BatchQueueItem = {
      local_id: `queue_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
      account_ids: Array.from(selectedAccounts),
      material_ids: Array.from(selectedMaterials),
      account_count: selectedAccounts.size,
      material_count: selectedMaterials.size,
      total,
      status: 'waiting',
      created_at: Date.now(),
      message: '等待执行',
    }

    const hasActiveQueue = queueItemsRef.current.some(queueItem => queueItem.status === 'waiting' || queueItem.status === 'submitting' || queueItem.status === 'running')
    updateQueueItems(prev => [...prev, item])

    if (hasActiveQueue || runningQueueIdRef.current) {
      addToast({ type: 'success', message: `已加入发布队列：${item.account_count} 个账号 × ${item.material_count} 条素材` })
    } else {
      startQueueItem(item)
    }
  }

  const cancelQueueItem = async (item: BatchQueueItem) => {
    if (item.status === 'waiting') {
      updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
        ? { ...queueItem, status: 'cancelled', message: '等待中已取消' }
        : queueItem,
      ))
      addToast({ type: 'warning', message: '已取消等待中的发布任务' })
      return
    }

    if (item.status === 'submitting' && !item.batch_id) {
      updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
        ? { ...queueItem, status: 'cancelled', message: '提交中已请求取消' }
        : queueItem,
      ))
      addToast({ type: 'warning', message: '已请求取消，任务提交完成后会立即停止' })
      return
    }

    if ((item.status === 'running' || item.status === 'submitting') && item.batch_id) {
      updateQueueItems(prev => prev.map(queueItem => queueItem.local_id === item.local_id
        ? { ...queueItem, message: '正在请求后端停止...' }
        : queueItem,
      ))
      try {
        const res = await cancelBatchPublish(item.batch_id)
        if (res.success) {
          addToast({ type: 'warning', message: '已请求停止正在执行的批量发布' })
        } else {
          addToast({ type: 'error', message: res.message || '停止任务失败' })
        }
      } catch {
        addToast({ type: 'error', message: '停止任务请求失败' })
      }
    }
  }

  const clearFinishedQueue = () => {
    updateQueueItems(prev => prev.filter(item => item.status === 'waiting' || item.status === 'submitting' || item.status === 'running'))
  }

  const toggleAccount = (id: string) => setSelectedAccounts(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  const toggleMaterial = (id: number) => setSelectedMaterials(prev => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n })
  const toggleAllAccounts = () => selectedAccounts.size === accounts.length ? setSelectedAccounts(new Set()) : setSelectedAccounts(new Set(accounts.map((a: any) => a.id)))

  /** 按账号分组快速选择：点击分组后只选择该分组下账号，下方仍可手动增减 */
  const selectAccountsByCategory = (category: string) => {
    const normalizedCategory = normalizeCategory(category)
    const ids = accounts
      .filter((account: any) => normalizeCategory(account.category) === normalizedCategory)
      .map((account: any) => account.id)
    if (ids.length === 0) {
      addToast({ type: 'warning', message: `分组「${normalizedCategory}」下暂无账号` })
      setSelectedAccounts(new Set())
      return
    }
    setSelectedAccounts(new Set(ids))
    addToast({ type: 'success', message: `已选择分组「${normalizedCategory}」下 ${ids.length} 个账号` })
  }

  /** 追加勾选指定分组账号，不清空当前已选账号 */
  const appendAccountsByCategory = (category: string) => {
    const normalizedCategory = normalizeCategory(category)
    const ids = accounts
      .filter((account: any) => normalizeCategory(account.category) === normalizedCategory)
      .map((account: any) => account.id)
    if (ids.length === 0) {
      addToast({ type: 'warning', message: `分组「${normalizedCategory}」下暂无账号` })
      return
    }
    setSelectedAccounts(prev => {
      const next = new Set(prev)
      ids.forEach(id => next.add(id))
      return next
    })
    addToast({ type: 'success', message: `已追加分组「${normalizedCategory}」下 ${ids.length} 个账号` })
  }

  const toggleAllMaterials = () => {
    const ids = filteredMaterials.map(m => m.id)
    const allSelected = ids.length > 0 && ids.every(id => selectedMaterials.has(id))
    if (allSelected) {
      setSelectedMaterials(prev => { const n = new Set(prev); ids.forEach(id => n.delete(id)); return n })
    } else {
      setSelectedMaterials(prev => { const n = new Set(prev); ids.forEach(id => n.add(id)); return n })
    }
  }

  const filteredMaterials = materialSearch.trim()
    ? materials.filter(m => m.title.toLowerCase().includes(materialSearch.trim().toLowerCase()))
    : materials

  const total = selectedAccounts.size * selectedMaterials.size
  const hasActiveQueue = queueItems.some(item => item.status === 'waiting' || item.status === 'submitting' || item.status === 'running')
  const isDisabled = submitting || total === 0

  return (
    <div className="space-y-3 sm:space-y-4">
      {/* 标题栏 */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="page-title">批量发布</h1>
          <p className="page-description">多账号  多素材并发发布，提升发布效率</p>
          <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
            批量发布时会忽略素材库中填写的宝贝所在地，统一从随机地址库自动分配地址。
          </p>
        </div>
        <div className="text-sm text-slate-500 bg-slate-100 dark:bg-slate-800 px-3 py-1.5 rounded-lg">
          {selectedAccounts.size} 账号  {selectedMaterials.size} 素材 =&nbsp;
          <span className="font-semibold text-blue-600 dark:text-blue-400">{total} 次发布</span>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* 账号选择 */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title">选择账号</h2>
            <button className="text-sm text-blue-500 hover:underline" onClick={toggleAllAccounts}>
              {selectedAccounts.size === accounts.length && accounts.length > 0 ? '取消全选' : '全选'}
            </button>
          </div>
          <div className="vben-card-body">
            {displayAccountCategories.length > 0 && accounts.length > 0 && (
              <div className="mb-3 rounded-xl border border-blue-100 dark:border-blue-900/40 bg-blue-50/60 dark:bg-blue-950/20 p-3">
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-1 mb-2">
                  <div className="text-sm font-semibold text-slate-700 dark:text-slate-100">按账号分组快速选择</div>
                  <div className="text-xs text-slate-500 dark:text-slate-400">点击分组会替换当前已选，点“追加”会保留当前已选</div>
                </div>
                <div className="flex flex-wrap gap-2">
                  {displayAccountCategories.map(category => {
                    const count = categoryAccountCountMap.get(category) || 0
                    const groupIds = accounts
                      .filter((account: any) => normalizeCategory(account.category) === category)
                      .map((account: any) => account.id)
                    const allGroupSelected = groupIds.length > 0 && groupIds.every(id => selectedAccounts.has(id))
                    return (
                      <div key={category} className={`inline-flex items-center rounded-lg border overflow-hidden ${allGroupSelected ? 'border-blue-400 bg-blue-100 dark:bg-blue-900/40' : 'border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-800'}`}>
                        <button
                          type="button"
                          className="px-2.5 py-1.5 text-xs font-medium text-slate-700 dark:text-slate-100 hover:bg-blue-50 dark:hover:bg-blue-900/30 disabled:opacity-50"
                          disabled={count === 0}
                          onClick={() => selectAccountsByCategory(category)}
                          title="只选择这个分组下的账号"
                        >
                          {category} <span className="text-slate-400">{count}</span>
                        </button>
                        <button
                          type="button"
                          className="border-l border-slate-200 dark:border-slate-700 px-2 py-1.5 text-xs text-blue-600 dark:text-blue-300 hover:bg-blue-50 dark:hover:bg-blue-900/30 disabled:opacity-50"
                          disabled={count === 0}
                          onClick={() => appendAccountsByCategory(category)}
                          title="追加勾选这个分组下的账号"
                        >
                          追加
                        </button>
                      </div>
                    )
                  })}
                </div>
              </div>
            )}
            {loadingAccounts ? (
              <div className="flex justify-center py-8"><Loader2 className="w-8 h-8 animate-spin text-blue-500" /></div>
            ) : accounts.length === 0 ? (
              <p className="text-center text-slate-400 py-8">暂无账号，请先添加账号</p>
            ) : (
              <div className="space-y-1 max-h-72 overflow-y-auto">
                {accounts.map((a: any) => {
                  const checked = selectedAccounts.has(a.id)
                  return (
                    <label key={a.id} className={`flex items-center gap-3 p-2.5 rounded-lg cursor-pointer transition-colors ${checked ? 'bg-blue-50 dark:bg-blue-900/20' : 'hover:bg-slate-50 dark:hover:bg-slate-700'}`}>
                      <input type="checkbox" className="w-4 h-4 text-blue-600 rounded accent-blue-500"
                        checked={checked} onChange={() => toggleAccount(a.id)} />
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate text-slate-800 dark:text-slate-100">
                          {a.note || a.id}
                        </p>
                        {a.note && <p className="text-xs text-slate-400 truncate">{a.id}</p>}
                      </div>
                      {a.enabled !== false && <span className="badge-success flex-shrink-0">启用</span>}
                    </label>
                  )
                })}
              </div>
            )}
          </div>
        </motion.div>

        {/* 素材选择 */}
        <motion.div initial={{ opacity: 0, y: 20 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.05 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title">选择素材</h2>
            <button className="text-sm text-blue-500 hover:underline" onClick={toggleAllMaterials}>
              {selectedMaterials.size === filteredMaterials.length && filteredMaterials.length > 0 ? '取消全选' : '全选'}
            </button>
          </div>
          <div className="vben-card-body">
            <input
              className="input-ios w-full mb-2"
              placeholder="搜索素材标题..."
              value={materialSearch}
              onChange={e => setMaterialSearch(e.target.value)}
            />
            {loadingMaterials ? (
              <div className="flex justify-center py-8"><Loader2 className="w-8 h-8 animate-spin text-blue-500" /></div>
            ) : filteredMaterials.length === 0 ? (
              <p className="text-center text-slate-400 py-8">{materials.length === 0 ? '素材库为空，请先在「素材库」页面添加素材' : '没有匹配的素材'}</p>
            ) : (
              <div className="space-y-1 max-h-72 overflow-y-auto">
                {filteredMaterials.map(m => {
                  const checked = selectedMaterials.has(m.id)
                  return (
                    <label key={m.id} className={`flex items-center gap-3 p-2.5 rounded-lg cursor-pointer transition-colors ${checked ? 'bg-blue-50 dark:bg-blue-900/20' : 'hover:bg-slate-50 dark:hover:bg-slate-700'}`}>
                      <input type="checkbox" className="w-4 h-4 text-blue-600 rounded accent-blue-500"
                        checked={checked} onChange={() => toggleMaterial(m.id)} />
                      {m.images?.[0] ? (
                        <img src={m.images[0]} alt={m.title} className="w-10 h-10 object-cover rounded-lg flex-shrink-0" />
                      ) : (
                        <div className="w-10 h-10 bg-slate-100 dark:bg-slate-700 rounded-lg flex items-center justify-center text-xs text-slate-400 flex-shrink-0">无图</div>
                      )}
                      <div className="flex-1 min-w-0">
                        <p className="text-sm font-medium truncate text-slate-800 dark:text-slate-100">{m.title}</p>
                        <p className="text-xs text-amber-600">{m.price}</p>
                      </div>
                    </label>
                  )
                })}
              </div>
            )}
          </div>
        </motion.div>
      </div>

      {/* 提交按钮 */}
      <div className="flex flex-col items-center gap-2">
        <button className="btn-ios-primary min-w-48" disabled={isDisabled} onClick={handleSubmit}>
          {submitting
            ? <><Loader2 className="w-4 h-4 animate-spin" />提交中...</>
            : <><Play className="w-4 h-4" />{hasActiveQueue ? `加入队列（${total} 次）` : `开始批量发布（${total} 次）`}</>}
        </button>
        {hasActiveQueue && <p className="text-xs text-slate-400">当前已有任务执行或等待中，新提交会自动排队</p>}
      </div>

      {/* 发布队列 */}
      {queueItems.length > 0 && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title"><ListOrdered className="w-4 h-4" />发布队列</h2>
            <button className="text-sm text-slate-500 hover:text-blue-500" onClick={clearFinishedQueue}>清理已完成/已取消</button>
          </div>
          <div className="vben-card-body space-y-2">
            {queueItems.map((item, index) => {
              const percent = item.progress && item.progress.total > 0
                ? Math.round((item.progress.success + item.progress.failed) / item.progress.total * 100)
                : 0
              const canCancel = item.status === 'waiting' || item.status === 'submitting' || item.status === 'running'
              return (
                <div key={item.local_id} className="rounded-xl border border-slate-200 dark:border-slate-700 p-3 bg-slate-50/80 dark:bg-slate-800/60">
                  <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="text-sm font-semibold text-slate-800 dark:text-slate-100">队列 #{index + 1}</span>
                        <span className={getQueueStatusClassName(item.status)}>{getQueueStatusLabel(item.status)}</span>
                        {item.batch_id && <span className="text-xs text-slate-400">批次 {item.batch_id.slice(0, 8)}...</span>}
                      </div>
                      <div className="text-xs text-slate-500 dark:text-slate-300 mt-1">
                        {item.account_count} 个账号 × {item.material_count} 条素材 = {item.total} 次发布
                        {item.message ? ` ｜ ${item.message}` : ''}
                      </div>
                    </div>
                    {canCancel && (
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 rounded-lg border border-red-200 dark:border-red-900/50 px-2.5 py-1.5 text-xs text-red-600 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/20"
                        onClick={() => cancelQueueItem(item)}
                      >
                        {item.status === 'waiting' ? <Trash2 className="w-3.5 h-3.5" /> : <Square className="w-3.5 h-3.5" />}
                        {item.status === 'waiting' ? '取消' : '停止'}
                      </button>
                    )}
                  </div>
                  {(item.status === 'running' || item.status === 'finished' || item.status === 'cancelled') && item.progress && (
                    <div className="mt-3">
                      <div className="w-full bg-slate-200 dark:bg-slate-700 rounded-full h-2">
                        <div className="bg-blue-500 h-2 rounded-full transition-all duration-500" style={{ width: `${percent}%` }} />
                      </div>
                      <div className="flex justify-between text-xs text-slate-400 mt-1">
                        <span>进度 {percent}%</span>
                        <span>成功 {item.progress.success} / 失败 {item.progress.failed}</span>
                      </div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </motion.div>
      )}

      {/* 进度面板 */}
      {progress && (
        <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="vben-card">
          <div className="vben-card-header">
            <h2 className="vben-card-title"><Layers className="w-4 h-4" />发布进度</h2>
            <div className="flex items-center gap-2">
              {progress.finished
                ? <span className={progress.cancelled ? 'badge-warning' : 'badge-success'}>{progress.cancelled ? '已停止' : '已完成'}</span>
                : <Loader2 className="w-4 h-4 animate-spin text-blue-500" />}
              {!progress.finished && progress.batch_id && (
                <button
                  type="button"
                  className="inline-flex items-center gap-1 rounded-lg border border-red-200 dark:border-red-900/50 px-2.5 py-1.5 text-xs text-red-600 dark:text-red-300 hover:bg-red-50 dark:hover:bg-red-900/20"
                  onClick={() => {
                    const activeItem = queueItemsRef.current.find(item => item.batch_id === progress.batch_id)
                    if (activeItem) cancelQueueItem(activeItem)
                    else cancelBatchPublish(progress.batch_id).then(() => addToast({ type: 'warning', message: '已请求停止正在执行的批量发布' }))
                  }}
                >
                  <Square className="w-3.5 h-3.5" />停止当前任务
                </button>
              )}
            </div>
          </div>
          <div className="vben-card-body">
            {progress.cancelled && (
              <div className="mb-4 rounded-xl border border-amber-200 dark:border-amber-900/50 bg-amber-50 dark:bg-amber-900/20 px-3 py-2 text-sm text-amber-700 dark:text-amber-200">
                {progress.cancel_message || '批量发布已手动停止。已发布成功的商品不会回滚，未开始的商品不会继续发布。'}
              </div>
            )}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
              {[
                { label: '总数', value: progress.total, icon: <Layers className="w-5 h-5" />, cls: 'stat-icon-primary' },
                { label: '成功', value: progress.success, icon: <CheckCircle className="w-5 h-5" />, cls: 'stat-icon-success' },
                { label: '失败', value: progress.failed, icon: <XCircle className="w-5 h-5" />, cls: 'stat-icon-warning' },
                { label: progress.cancelled ? '已停止' : '进行中', value: progress.cancelled ? 0 : progress.publishing + progress.pending, icon: <Clock className="w-5 h-5" />, cls: 'stat-icon-info' },
              ].map(item => (
                <div key={item.label} className="stat-card">
                  <div className={item.cls}>{item.icon}</div>
                  <div>
                    <div className="stat-value">{item.value}</div>
                    <div className="stat-label">{item.label}</div>
                  </div>
                </div>
              ))}
            </div>
            {progress.total > 0 && (
              <>
                <div className="w-full bg-slate-200 dark:bg-slate-700 rounded-full h-2 mb-1">
                  <div className="bg-blue-500 h-2 rounded-full transition-all duration-500"
                    style={{ width: `${Math.round((progress.success + progress.failed) / progress.total * 100)}%` }} />
                </div>
                <div className="flex justify-between text-xs text-slate-400">
                  <span>进度 {Math.round((progress.success + progress.failed) / progress.total * 100)}%</span>
                  <span>批次 ID：{progress.batch_id.slice(0, 8)}...</span>
                </div>
              </>
            )}
            {!progress.finished && <p className="text-xs text-slate-400 mt-2">每 3 秒自动刷新进度，可点击“停止当前任务”终止后续发布</p>}
            {progress.account_statuses.length > 0 && (
              <div className="mt-4 border-t border-slate-200 dark:border-slate-700 pt-4">
                <div className="flex items-center justify-between gap-2 mb-3">
                  <h3 className="text-sm font-semibold text-slate-700 dark:text-slate-200">账号自动获取商品状态</h3>
                  <span className="text-xs text-slate-400">按账号展示发布后商品同步结果</span>
                </div>
                <div className="space-y-2 max-h-72 overflow-y-auto pr-1">
                  {progress.account_statuses.map(accountStatus => (
                    <div key={accountStatus.account_id} className="rounded-xl border border-slate-200 dark:border-slate-700 p-3 bg-slate-50/80 dark:bg-slate-800/60">
                      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                        <div className="min-w-0">
                          <div className="text-sm font-medium text-slate-800 dark:text-slate-100 truncate">
                            {accountNameMap.get(accountStatus.account_id) || accountStatus.account_id}
                          </div>
                          <div className="text-xs text-slate-400 truncate">账号ID：{accountStatus.account_id}</div>
                        </div>
                        <span className={getSyncStatusClassName(accountStatus.sync_status)}>{getSyncStatusLabel(accountStatus.sync_status)}</span>
                      </div>
                      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3 text-xs">
                        <div className="rounded-lg bg-white dark:bg-slate-900 px-2.5 py-2">
                          <div className="text-slate-400">发布总数</div>
                          <div className="mt-1 font-semibold text-slate-700 dark:text-slate-100">{accountStatus.total}</div>
                        </div>
                        <div className="rounded-lg bg-white dark:bg-slate-900 px-2.5 py-2">
                          <div className="text-slate-400">发布成功</div>
                          <div className="mt-1 font-semibold text-emerald-600">{accountStatus.success}</div>
                        </div>
                        <div className="rounded-lg bg-white dark:bg-slate-900 px-2.5 py-2">
                          <div className="text-slate-400">发布失败</div>
                          <div className="mt-1 font-semibold text-amber-600">{accountStatus.failed}</div>
                        </div>
                        <div className="rounded-lg bg-white dark:bg-slate-900 px-2.5 py-2">
                          <div className="text-slate-400">待处理</div>
                          <div className="mt-1 font-semibold text-blue-600">{progress.cancelled ? 0 : accountStatus.publishing + accountStatus.pending}</div>
                        </div>
                      </div>
                      <div className="mt-3 text-xs text-slate-500 dark:text-slate-300 break-all">{accountStatus.sync_message}</div>
                      {(accountStatus.sync_status === 'success' || accountStatus.sync_total_count > 0 || accountStatus.sync_saved_count > 0) && (
                        <div className="mt-2 text-xs text-slate-500 dark:text-slate-300">
                          已抓取 {accountStatus.sync_total_count} 件，入库 {accountStatus.sync_saved_count} 件
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        </motion.div>
      )}
    </div>
  )
}

export default BatchPublish
