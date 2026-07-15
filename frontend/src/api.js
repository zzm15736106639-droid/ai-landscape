export async function api(path, options = {}) {
  const response = await fetch(path, options)
  let data = null
  try {
    data = await response.json()
  } catch {
    data = null
  }
  if (!response.ok || data?.ok === false) {
    throw new Error(data?.error || `请求失败 (${response.status})`)
  }
  return data
}

export function jsonOptions(body, method = 'POST') {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export function videoUrl(path) {
  return `/api/video?path=${encodeURIComponent(path)}`
}

export function isDesktop() {
  return Boolean(window.aiLandscape)
}
