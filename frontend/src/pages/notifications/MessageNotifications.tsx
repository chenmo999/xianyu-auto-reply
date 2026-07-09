import { useState, useEffect } from 'react'
import type { FormEvent } from 'react'
import { motion } from 'framer-motion'
import { Mail, RefreshCw, Plus, Trash2, Power, PowerOff, X, Loader2 } from 'lucide-react'
import { getMessageNotifications, setMessageNotification, setMessageNotificationsBatch, getNotificationChannels, deleteMessageNotification } from '@/api/notifications'
import { getAccountDetails } from '@/api/accounts'
import { useUIStore } from '@/store/uiStore'
import { useAuthStore } from '@/store/authStore'
import { PageLoading } from '@/components/common/Loading'
import { Select } from '@/components/common/Select'
import { ConfirmModal } from '@/components/common/ConfirmModal'
import type { MessageNotification, NotificationChannel, Account } from '@/types'

export function MessageNotifications() {
  const { addToast } = useUIStore()
  const { isAuthenticated, token, _hasHydrated } = useAuthStore()
  const [loading, setLoading] = useState(true)
  const [notifications, setNotifications] = useState<MessageNotification[]>([])
  const [channels, setChannels] = useState<NotificationChannel[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [isModalOpen, setIsModalOpen] = useState(false)
  const [formAccountIds, setFormAccountIds] = useState<string[]>([])
  const [formAccountSearch, setFormAccountSearch] = useState('')
  const [formChannelId, setFormChannelId] = useState('')
  const [formEnabled, setFormEnabled] = useState(true)
  const [saving, setSaving] = useState(false)

  // 删除确认弹窗状态
  const [deleteConfirm, setDeleteConfirm] = useState<{ open: boolean; notification: MessageNotification | null }>({ open: false, notification: null })
  const [deleting, setDeleting] = useState(false)

  const loadNotifications = async () => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    try {
      setLoading(true)
      const result = await getMessageNotifications()
      if (result.success) {
        setNotifications(result.data || [])
      }
    } catch {
      addToast({ type: 'error', message: '加载消息通知失败' })
    } finally {
      setLoading(false)
    }
  }

  const loadChannels = async () => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    try {
      const result = await getNotificationChannels()
      if (result.success) {
        setChannels(result.data || [])
      }
    } catch {
      // ignore
    }
  }

  const loadAccounts = async () => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    try {
      const data = await getAccountDetails()
      setAccounts(data)
    } catch {
      // ignore
    }
  }

  useEffect(() => {
    if (!_hasHydrated || !isAuthenticated || !token) return
    loadChannels()
    loadAccounts()
    loadNotifications()
  }, [_hasHydrated, isAuthenticated, token])

  const handleToggleEnabled = async (notification: MessageNotification) => {
    try {
      await setMessageNotification(notification.cookie_id, notification.channel_id, !notification.enabled)
      addToast({ type: 'success', message: notification.enabled ? '通知已禁用' : '通知已启用' })
      loadNotifications()
    } catch {
      addToast({ type: 'error', message: '操作失败' })
    }
  }

  const handleDelete = async (notification: MessageNotification) => {
    setDeleting(true)
    try {
      await deleteMessageNotification(String(notification.id))
      addToast({ type: 'success', message: '通知已删除' })
      setDeleteConfirm({ open: false, notification: null })
      loadNotifications()
    } catch {
      addToast({ type: 'error', message: '删除失败' })
    } finally {
      setDeleting(false)
    }
  }

  const openAddModal = () => {
    setFormAccountIds([])
    setFormAccountSearch('')
    setFormChannelId('')
    setFormEnabled(true)
    setIsModalOpen(true)
  }

  const closeModal = () => {
    setIsModalOpen(false)
  }

  const getAccountLabel = (account: Account) => {
    return account.note ? `${account.id} (${account.note})` : account.id
  }

  const filteredAccounts = accounts.filter((account) => {
    const keyword = formAccountSearch.trim().toLowerCase()
    if (!keyword) return true
    return getAccountLabel(account).toLowerCase().includes(keyword)
  })

  const selectedAccountSet = new Set(formAccountIds)

  const toggleFormAccount = (accountId: string) => {
    setFormAccountIds((prev) => (
      prev.includes(accountId)
        ? prev.filter((id) => id !== accountId)
        : [...prev, accountId]
    ))
  }

  const selectAllFilteredAccounts = () => {
    setFormAccountIds((prev) => {
      const next = new Set(prev)
      filteredAccounts.forEach((account) => next.add(account.id))
      return Array.from(next)
    })
  }

  const clearSelectedAccounts = () => {
    setFormAccountIds([])
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (formAccountIds.length === 0) {
      addToast({ type: 'warning', message: '请至少勾选一个账号' })
      return
    }
    if (!formChannelId) {
      addToast({ type: 'warning', message: '请选择通知渠道' })
      return
    }

    setSaving(true)
    try {
      const result = await setMessageNotificationsBatch(formAccountIds, Number(formChannelId), formEnabled)
      addToast({ type: 'success', message: result.message || `已保存 ${formAccountIds.length} 个账号的通知配置` })
      closeModal()
      loadNotifications()
    } catch {
      addToast({ type: 'error', message: '保存失败' })
    } finally {
      setSaving(false)
    }
  }

  if (loading) {
    return <PageLoading />
  }

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-4">
        <div>
          <h1 className="page-title">消息通知</h1>
        </div>
        <div className="flex gap-3">
          <button onClick={openAddModal} className="btn-ios-primary ">
            <Plus className="w-4 h-4" />
            添加通知
          </button>
          <button onClick={loadNotifications} className="btn-ios-secondary ">
            <RefreshCw className="w-4 h-4" />
            刷新
          </button>
        </div>
      </div>

      {/* Notifications List */}
      <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className="vben-card"
      >
        <div className="vben-card-header 
                      flex items-center justify-between">
          <h2 className="vben-card-title ">
            <Mail className="w-4 h-4" />
            通知规则
          </h2>
          <span className="badge-primary">{notifications.length} 条规则</span>
        </div>
        <div className="overflow-x-auto">
          <table className="table-ios">
            <thead>
              <tr>
                <th>账号ID</th>
                <th>通知渠道</th>
                <th>状态</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {notifications.length === 0 ? (
                <tr>
                  <td colSpan={4} className="text-center py-8 text-gray-500">
                    <div className="flex flex-col items-center gap-2">
                      <Mail className="w-12 h-12 text-gray-300" />
                      <p>暂无消息通知配置</p>
                    </div>
                  </td>
                </tr>
              ) : (
                notifications.map((notification) => (
                  <tr key={`${notification.cookie_id}-${notification.channel_id}`}>
                    <td className="font-medium text-blue-600 dark:text-blue-400">
                      {(() => {
                        const account = accounts.find(acc => acc.id === notification.cookie_id)
                        return account?.note ? `${notification.cookie_id} (${account.note})` : notification.cookie_id
                      })()}
                    </td>
                    <td className="text-sm">
                      {notification.channel_name || `渠道 ${notification.channel_id}`}
                    </td>
                    <td>
                      {notification.enabled ? (
                        <span className="badge-success">启用</span>
                      ) : (
                        <span className="badge-danger">禁用</span>
                      )}
                    </td>
                    <td>
                      <div className="flex gap-1">
                        <button
                          onClick={() => handleToggleEnabled(notification)}
                          className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-slate-700 transition-colors"
                          title={notification.enabled ? '禁用' : '启用'}
                        >
                          {notification.enabled ? (
                            <PowerOff className="w-4 h-4 text-amber-500" />
                          ) : (
                            <Power className="w-4 h-4 text-emerald-500" />
                          )}
                        </button>
                        <button
                          onClick={() => setDeleteConfirm({ open: true, notification })}
                          className="p-2 rounded-lg hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
                          title="删除"
                        >
                          <Trash2 className="w-4 h-4 text-red-500" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </motion.div>

      {/* 添加通知弹窗 */}
      {isModalOpen && (
        <div className="modal-overlay">
          <div className="modal-content max-w-2xl min-h-[520px]">
            <div className="modal-header flex items-center justify-between">
              <h2 className="text-lg font-semibold">添加消息通知</h2>
              <button onClick={closeModal} className="p-1 hover:bg-gray-100 dark:hover:bg-slate-700 rounded-lg">
                <X className="w-4 h-4 text-gray-500" />
              </button>
            </div>
            <form onSubmit={handleSubmit}>
              <div className="modal-body space-y-4">
                <div className="input-group">
                  <div className="flex items-center justify-between gap-3">
                    <label className="input-label">选择账号 *</label>
                    <span className="text-xs text-slate-500 dark:text-slate-400">
                      已选 {formAccountIds.length} 个
                    </span>
                  </div>
                  <input
                    type="text"
                    value={formAccountSearch}
                    onChange={(e) => setFormAccountSearch(e.target.value)}
                    className="input-ios"
                    placeholder="搜索账号ID或备注"
                  />
                  <div className="mt-2 flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={selectAllFilteredAccounts}
                      className="px-3 py-1.5 text-xs rounded-lg bg-blue-50 text-blue-600 hover:bg-blue-100 dark:bg-blue-900/30 dark:text-blue-300 dark:hover:bg-blue-900/50 transition-colors"
                    >
                      勾选当前列表
                    </button>
                    <button
                      type="button"
                      onClick={clearSelectedAccounts}
                      className="px-3 py-1.5 text-xs rounded-lg bg-slate-100 text-slate-600 hover:bg-slate-200 dark:bg-slate-700 dark:text-slate-200 dark:hover:bg-slate-600 transition-colors"
                    >
                      清空
                    </button>
                  </div>
                  <div className="mt-3 max-h-64 overflow-y-auto rounded-xl border border-slate-200 dark:border-slate-700 divide-y divide-slate-100 dark:divide-slate-700 bg-white dark:bg-slate-800">
                    {filteredAccounts.length === 0 ? (
                      <div className="px-4 py-6 text-center text-sm text-slate-500 dark:text-slate-400">
                        没有匹配的账号
                      </div>
                    ) : (
                      filteredAccounts.map((account) => (
                        <label
                          key={account.pk?.toString() || account.id}
                          className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-slate-50 dark:hover:bg-slate-700/60 transition-colors"
                        >
                          <input
                            type="checkbox"
                            checked={selectedAccountSet.has(account.id)}
                            onChange={() => toggleFormAccount(account.id)}
                            className="h-4 w-4 rounded border-slate-300 text-blue-600 focus:ring-blue-500"
                          />
                          <span className="text-sm text-slate-700 dark:text-slate-200">
                            {getAccountLabel(account)}
                          </span>
                        </label>
                      ))
                    )}
                  </div>
                </div>
                <div className="input-group">
                  <label className="input-label">选择通知渠道 *</label>
                  <Select
                    value={formChannelId}
                    onChange={setFormChannelId}
                    options={[
                      { value: '', label: '请选择通知渠道' },
                      ...channels.map((channel) => ({
                        value: String(channel.id),
                        label: channel.name || channel.channel_name || `渠道 ${channel.id}`,
                      })),
                    ]}
                    placeholder="请选择通知渠道"
                  />
                </div>
                <div className="flex items-center justify-between pt-2">
                  <span className="text-sm font-medium text-slate-700 dark:text-slate-200">启用此通知</span>
                  <button
                    type="button"
                    onClick={() => setFormEnabled(!formEnabled)}
                    className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                      formEnabled ? 'bg-blue-600' : 'bg-slate-300 dark:bg-slate-600'
                    }`}
                  >
                    <span
                      className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${
                        formEnabled ? 'translate-x-6' : 'translate-x-1'
                      }`}
                    />
                  </button>
                </div>
              </div>
              <div className="modal-footer">
                <button type="button" onClick={closeModal} className="btn-ios-secondary" disabled={saving}>
                  取消
                </button>
                <button type="submit" className="btn-ios-primary" disabled={saving}>
                  {saving ? (
                    <span className="flex items-center gap-2">
                      <Loader2 className="w-4 h-4 animate-spin" />
                      保存中...
                    </span>
                  ) : (
                    `保存 ${formAccountIds.length || ''}`
                  )}
                </button>
              </div>
            </form>
          </div>
        </div>
      )}

      {/* 删除确认弹窗 */}
      <ConfirmModal
        isOpen={deleteConfirm.open}
        title="删除确认"
        message="确定要删除这个消息通知吗？"
        confirmText="删除"
        cancelText="取消"
        type="danger"
        loading={deleting}
        onConfirm={() => deleteConfirm.notification && handleDelete(deleteConfirm.notification)}
        onCancel={() => setDeleteConfirm({ open: false, notification: null })}
      />
    </div>
  )
}
