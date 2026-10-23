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
  const res = await fetch(path, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(getToken() ? { Authorization: `Bearer ${getToken()}` } : {}),
      ...(options.headers || {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
  })
  if (res.status === 401) {
    clearSession()
    window.location.reload()
    throw new Error('Session expired')
  }
  if (!res.ok) {
    let msg = `Request failed (${res.status})`
    try { const j = await res.json(); if (j.detail) msg = j.detail } catch {}
    throw new Error(msg)
  }
  if (res.status === 204) return null
  return res.json()
}
