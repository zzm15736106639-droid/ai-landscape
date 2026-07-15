export const OUTPUT_WIDTH = 1280
export const OUTPUT_HEIGHT = 720

export const DEFAULT_SUBTITLE_LAYOUT = {
  center_x: 640,
  center_y: 640,
  font_size: 44,
  basis_width: 1280,
  basis_height: 720,
}

export const DEFAULT_SUBTITLE_STYLE = {
  font_id: 'source_han_sans_sc_heavy',
  outline_width: 0,
  shadow_opacity_percent: 0,
}

export const SUBTITLE_FONTS = [
  { id: 'source_han_sans_sc_heavy', label: '思源黑体 Heavy', family: 'Source Han Sans SC Heavy' },
  { id: 'source_han_serif_sc_heavy', label: '思源宋体 Heavy', family: 'Source Han Serif SC Heavy' },
]

export const terminalStatuses = new Set(['done', 'error', 'cancelled'])

export function createWindow(index = 1) {
  return {
    id: crypto.randomUUID(),
    name: `窗口 ${index}`,
    videos: [],
    activeVideoId: '',
    selectedVideoIds: [],
    boundsByVideoId: {},
    subtitleByVideoId: {},
    subtitleLayoutByVideoId: {},
    subtitleStyleByVideoId: {},
    subtitleOverridesById: {},
    fixedEffectIds: [],
    randomEffectIds: [],
    previewEffectId: '',
    previewMode: 'vertical',
    jobId: '',
    job: null,
  }
}

export function defaultBounds(video) {
  return {
    upper_y: 0,
    lower_y: video?.height || 0,
    basis_height: video?.height || 1,
  }
}

export function normalizeStem(value) {
  return String(value || '')
    .normalize('NFKC')
    .trim()
    .toLocaleLowerCase()
    .replace(/\.[^.]+$/, '')
}

export function formatDuration(seconds) {
  const value = Math.max(0, Number(seconds) || 0)
  const minutes = Math.floor(value / 60)
  const remaining = Math.floor(value % 60)
  return `${minutes}:${String(remaining).padStart(2, '0')}`
}
