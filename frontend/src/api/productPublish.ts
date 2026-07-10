/**
 * 商品发布 API 接口层
 *
 * 功能：
 * 1. 素材库 CRUD
 * 2. 单品发布 / 批量发布
 * 3. 发布日志查询
 */
import { get, post, put, del } from '@/utils/request'
import type { ApiResponse } from '@/types'

const PREFIX = '/api/v1/product-publish'

// ==================== 类型定义 ====================

export interface ProductMaterial {
  id: number
  user_id: number
  username?: string  // 管理员场景返回
  title: string
  description: string
  price: number
  original_price?: number | null
  category?: string | null
  images: string[]
  delivery_method: 'express' | 'pickup'
  postage: number
  address?: string | null
  brand?: string | null
  condition: string
  remark?: string | null
  created_at: string
  updated_at: string
}

export interface MaterialCreateParams {
  title: string
  description: string
  price: number
  original_price?: number | null
  category?: string
  images: string[]
  delivery_method?: 'express' | 'pickup'
  postage?: number
  address?: string
  brand?: string
  condition?: string
  remark?: string
}

export interface MaterialListResponse {
  success: boolean
  message: string
  data: {
    list: ProductMaterial[]
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}

export interface PublishLog {
  id: number
  user_id: number
  username?: string  // 管理员场景返回
  account_id: string
  title: string
  description?: string
  price?: string
  material_id?: number | null
  batch_id?: string | null
  status: 'pending' | 'publishing' | 'success' | 'failed'
  item_url?: string | null
  item_id?: string | null
  error_message?: string | null
  resolved_address_id?: number | null
  resolved_address_text?: string | null
  address_source?: 'material' | 'account_pool' | 'global_pool' | 'personal_pool' | null
  created_at: string
  updated_at: string
}

export interface PublishLogListResponse {
  success: boolean
  message: string
  data: {
    list: PublishLog[]
    total: number
    page: number
    page_size: number
    total_pages: number
  }
}

export interface BatchAccountStatus {
  account_id: string
  total: number
  success: number
  failed: number
  publishing: number
  pending: number
  sync_status: 'pending' | 'running' | 'success' | 'failed' | 'skipped' | 'unknown'
  sync_message: string
  sync_total_count: number
  sync_saved_count: number
}

export interface BatchStatusResponse {
  success: boolean
  message: string
  data: {
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
}

export interface PublishSingleResponseData {
  item_url?: string | null
  item_id?: string | null
  log_id?: number
  sync_status?: 'success' | 'failed' | 'skipped'
  sync_message?: string | null
  sync_total_count?: number
  sync_saved_count?: number
}

export type PublishSingleResponse = ApiResponse<PublishSingleResponseData>

export interface PublishBatchResponseData {
  batch_id: string
  total: number
}

export type PublishBatchResponse = ApiResponse<PublishBatchResponseData>



// ==================== 1688 导入接口 ====================

export interface Auth1688Status {
  has_cookie: boolean
  status: string
  last_check_at?: string | null
  updated_at?: string | null
}

/** 保存1688 Cookie */
export const save1688Cookie = (cookie: string): Promise<ApiResponse<{ has_cookie: boolean; status: string }>> =>
  post(`${PREFIX}/1688/auth/cookie/save`, { cookie })

/** 查询1688登录状态 */
export const get1688AuthStatus = (): Promise<ApiResponse<Auth1688Status>> =>
  get(`${PREFIX}/1688/auth/status`)

/** 清除1688 Cookie */
export const clear1688Cookie = (): Promise<ApiResponse<{ has_cookie: boolean; status: string }>> =>
  post(`${PREFIX}/1688/auth/cookie/clear`, {})


export interface Browser1688Screenshot {
  image: string
  url: string
  title: string
}

/** 打开1688服务器浏览器登录页 */
export const start1688BrowserLogin = (): Promise<ApiResponse<{ started: boolean; url: string; title: string }>> =>
  post(`${PREFIX}/1688/browser/start`, {})

/** 获取1688服务器浏览器截图 */
export const get1688BrowserScreenshot = (): Promise<ApiResponse<Browser1688Screenshot>> =>
  get(`${PREFIX}/1688/browser/screenshot`)

/** 点击1688服务器浏览器 */
export const click1688Browser = (x: number, y: number): Promise<ApiResponse> =>
  post(`${PREFIX}/1688/browser/click`, { x, y })

/** 向1688服务器浏览器当前焦点输入文字 */
export const type1688Browser = (text: string): Promise<ApiResponse> =>
  post(`${PREFIX}/1688/browser/type`, { text })

/** 向1688服务器浏览器发送按键 */
export const press1688Browser = (key: string): Promise<ApiResponse> =>
  post(`${PREFIX}/1688/browser/press`, { key })


/** 拖动1688服务器浏览器，用于滑块验证 */
export const drag1688Browser = (
  startX: number,
  startY: number,
  endX: number,
  endY: number
): Promise<ApiResponse> =>
  post(`${PREFIX}/1688/browser/drag`, {
    start_x: startX,
    start_y: startY,
    end_x: endX,
    end_y: endY,
  })

/** 完成1688服务器浏览器登录并自动保存Cookie */
export const finish1688BrowserLogin = (): Promise<ApiResponse<{ has_cookie: boolean; cookie_count: number; url: string; title: string }>> =>
  post(`${PREFIX}/1688/browser/finish`, {})

/** 关闭1688服务器浏览器 */
export const close1688BrowserLogin = (): Promise<ApiResponse> =>
  post(`${PREFIX}/1688/browser/close`, {})


// ==================== 素材库接口 ====================

/** 创建素材 */
export const createMaterial = (params: MaterialCreateParams): Promise<ApiResponse> =>
  post(`${PREFIX}/materials`, params)

/** 分页查询素材列表 */
export const getMaterials = (
  page = 1,
  pageSize = 20,
  filters?: { title?: string; category?: string; condition?: string }
): Promise<MaterialListResponse> => {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  if (filters?.title) params.append('title', filters.title)
  if (filters?.category) params.append('category', filters.category)
  if (filters?.condition) params.append('condition', filters.condition)
  return get(`${PREFIX}/materials?${params}`)
}

/** 获取单条素材详情 */
export const getMaterial = (id: number): Promise<ApiResponse> =>
  get(`${PREFIX}/materials/${id}`)

/** 更新素材 */
export const updateMaterial = (
  id: number,
  params: Partial<MaterialCreateParams>
): Promise<ApiResponse> => put(`${PREFIX}/materials/${id}`, params)

/** 删除素材 */
export const deleteMaterial = (id: number): Promise<ApiResponse> =>
  del(`${PREFIX}/materials/${id}`)

/** 批量删除素材 */
export const batchDeleteMaterials = (ids: number[]): Promise<ApiResponse> =>
  post(`${PREFIX}/materials/batch-delete`, { ids })


const buildMaterialExportQuery = (filters?: { title?: string; category?: string; condition?: string }) => {
  const params = new URLSearchParams()
  if (filters?.title) params.append('title', filters.title)
  if (filters?.category) params.append('category', filters.category)
  if (filters?.condition) params.append('condition', filters.condition)
  return params
}

/** 导出素材库 Excel */
export const exportMaterials = async (filters?: { title?: string; category?: string; condition?: string }): Promise<Blob> => {
  const params = buildMaterialExportQuery(filters)
  const token = localStorage.getItem('auth_token')
  const response = await fetch(`${PREFIX}/materials/export${params.toString() ? `?${params}` : ''}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (!response.ok) throw new Error('导出失败')
  return response.blob()
}

/** 导出素材库原图 ZIP：materials.xlsx + images/ */
export const exportMaterialsWithImages = async (filters?: { title?: string; category?: string; condition?: string }): Promise<Blob> => {
  const params = buildMaterialExportQuery(filters)
  const token = localStorage.getItem('auth_token')
  const response = await fetch(`${PREFIX}/materials/export-with-images${params.toString() ? `?${params}` : ''}`, {
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  })
  if (!response.ok) throw new Error('导出原图失败')
  return response.blob()
}

/** 导入素材库 Excel，可同时上传 images 文件夹原图 */
export const importMaterials = async (
  file: File,
  imageFiles?: File[]
): Promise<ApiResponse<{ created: number; failed: number; matched_images?: number; errors?: Array<{ row: number; reason: string }> }>> => {
  const formData = new FormData()
  formData.append('file', file)
  ;(imageFiles || []).forEach(f => {
    const relativePath = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name
    formData.append('image_files', f, relativePath)
  })
  return post(`${PREFIX}/materials/import`, formData)
}

// ==================== 发布接口 ====================

/** 单品发布（同步，超时时间需设长） */
export const publishSingle = (params: {
  account_id: string
  title: string
  description: string
  price: number
  original_price?: number | null
  category?: string
  images: string[]        // 本地绝对路径，由 uploadProductImages 返回
  address?: string
  delivery_method?: string
  postage?: number
  brand?: string
  condition?: string
}): Promise<PublishSingleResponse> =>
  post(`${PREFIX}/publish/single`, params, { timeout: 600000 }) // 10分钟超时

/** 批量发布（异步，立即返回 batch_id） */
export const publishBatch = (params: {
  account_ids: string[]
  material_ids: number[]
}): Promise<PublishBatchResponse> => post(`${PREFIX}/publish/batch`, params)

/** 查询批量发布任务状态 */
export const getBatchStatus = (batchId: string): Promise<BatchStatusResponse> =>
  get(`${PREFIX}/publish/batch/${batchId}/status`)

/** 停止正在执行的批量发布任务 */
export const cancelBatchPublish = (batchId: string): Promise<ApiResponse<{ batch_id: string }>> =>
  post(`${PREFIX}/publish/batch/${batchId}/cancel`, {})

// ==================== 图片上传 ====================

/** 上传商品图片（返回本地路径供 Playwright 使用 + URL 供预览）
 *  注意：不要手动设置 Content-Type，axios 会自动添加正确的 multipart boundary
 */
export const uploadProductImages = async (files: File[]): Promise<{
  success: boolean
  message: string
  data?: { paths: string[]; urls: string[] }
}> => {
  const formData = new FormData()
  files.forEach(f => formData.append('files', f))
  return post(`${PREFIX}/upload/images`, formData)
}

/** 分页查询发布日志 */
export const getPublishLogs = (
  page = 1,
  pageSize = 20,
  accountId?: string,
  status?: string
): Promise<PublishLogListResponse> => {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  })
  if (accountId) params.append('account_id', accountId)
  if (status) params.append('status', status)
  return get(`${PREFIX}/logs?${params}`)
}

export const clearPublishLogs = async (): Promise<{ success: boolean; message: string }> => {
  return del<{ success: boolean; message: string }>(`${PREFIX}/logs/clear`)
}
