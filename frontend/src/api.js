/* Tiny API client — JWT in localStorage, JSON in/out. */
const TOKEN_KEY = 'vap_token'
const USER_KEY = 'vap_user'

export function getToken() { return localStorage.getItem(TOKEN_KEY) }
export function getUser() {
  try { return JSON.parse(localStorage.getItem(USER_KEY)) } catch { return null }
}
export function setSession(data) {
  localStorage.setItem(TOKEN_KEY, data.access_token)
  localStorage.setItem(USER_KEY, JSON.stringify({ ...data.user, tenant_name: data.tenant_name }))
}
export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

export async function api(path, options = {}) {
  const hadToken = !!getToken()
  // Auth endpoints establish the session — their 401 means the
  // credentials (or invite) were wrong, never an expired session.
  const isAuthEndpoint = path.includes('/api/auth/')
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(hadToken ? { Authorization: `Bearer ${getToken()}` } : {}),
      ...(options.headers || {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  })
  if (res.status === 401) {
    if (!isAuthEndpoint && hadToken) {
      clearSession()
      window.location.reload()
      throw new Error('Session expired')
    }
    // No session to expire (login/setup/public call): surface the
    // backend's own reason, e.g. 'Invalid email or password'.
    let msg = 'Authentication failed (401)'
    try { const j = await res.json(); if (j.detail) msg = j.detail } catch {}
    throw new Error(msg)
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`
    try { const j = await res.json(); if (j.detail) msg = j.detail } catch {}
    throw new Error(msg)
  }
  if (res.status === 204) return null
  return res.json()
}

/* Multipart upload (CSV files) — same auth/error handling, no JSON body. */
export async function apiUpload(path, formData) {
  const hadToken = !!getToken()
  const res = await fetch(path, {
    method: 'POST',
    body: formData,
    headers: { ...(hadToken ? { Authorization: `Bearer ${getToken()}` } : {}) },
  })
  if (res.status === 401) {
    if (hadToken) {
      clearSession()
      window.location.reload()
      throw new Error('Session expired')
    }
    let msg = 'Authentication failed (401)'
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch {}
    throw new Error(msg)
  }
  if (!res.ok) {
    let msg = `Upload failed (${res.status})`
    try { const j = await res.json(); if (j.detail) msg = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail) } catch {}
    throw new Error(msg)
  }
  return res.json()
}
