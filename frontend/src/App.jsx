import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CheckSquare,
  ChevronDown,
  FolderOpen,
  LoaderCircle,
  Plus,
  RotateCcw,
  Square,
  Subtitles,
  Trash2,
  Upload,
  Video,
  X,
} from 'lucide-react'
import { api, isDesktop, jsonOptions } from './api.js'
import EffectsPanel from './components/EffectsPanel.jsx'
import Player from './components/Player.jsx'
import {
  DEFAULT_SUBTITLE_LAYOUT,
  DEFAULT_SUBTITLE_STYLE,
  SUBTITLE_FONTS,
  createWindow,
  defaultBounds,
  formatDuration,
  normalizeStem,
  terminalStatuses,
} from './constants.js'

function clone(value) {
  return JSON.parse(JSON.stringify(value))
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value))
}

function isLocked(windowState) {
  return Boolean(windowState?.jobId && !terminalStatuses.has(windowState?.job?.status))
}

function NumberEditor({ value, min, max, step = 1, disabled, suffix = '', onCommit }) {
  const [draft, setDraft] = useState(null)
  const text = draft === null ? String(value) : draft
  const commit = () => {
    if (text.trim() === '') {
      setDraft(null)
      return
    }
    const number = Number(text)
    if (!Number.isFinite(number)) {
      setDraft(null)
      return
    }
    const next = clamp(number, min, max)
    onCommit(next)
    setDraft(null)
  }
  return (
    <label className="number-editor">
      <input
        type="text"
        inputMode="decimal"
        value={text}
        disabled={disabled}
        onChange={event => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={event => event.key === 'Enter' && event.currentTarget.blur()}
        aria-label={suffix || '数值'}
        data-step={step}
      />
      {suffix && <span>{suffix}</span>}
    </label>
  )
}

export default function App() {
  const initialWindow = useMemo(() => createWindow(1), [])
  const [windows, setWindows] = useState([initialWindow])
  const [activeWindowId, setActiveWindowId] = useState(initialWindow.id)
  const [effects, setEffects] = useState([])
  const [presets, setPresets] = useState([])
  const [outputDir, setOutputDir] = useState('')
  const [gpuMode, setGpuMode] = useState('auto')
  const [workers, setWorkers] = useState(1)
  const [bitrate, setBitrate] = useState(2300)
  const [notice, setNotice] = useState(null)
  const [importing, setImporting] = useState(false)
  const videoInputRef = useRef(null)
  const subtitleInputRef = useRef(null)

  const activeWindow = windows.find(item => item.id === activeWindowId) || windows[0]
  const locked = isLocked(activeWindow)
  const activeVideo = activeWindow?.videos.find(item => item.id === activeWindow.activeVideoId) || null
  const activeBounds = activeVideo
    ? activeWindow.boundsByVideoId[activeVideo.id] || defaultBounds(activeVideo)
    : null
  const activeSubtitle = activeVideo ? activeWindow.subtitleByVideoId[activeVideo.id] : null
  const activeLayout = activeVideo
    ? activeWindow.subtitleLayoutByVideoId[activeVideo.id] || DEFAULT_SUBTITLE_LAYOUT
    : DEFAULT_SUBTITLE_LAYOUT
  const activeStyle = activeVideo
    ? activeWindow.subtitleStyleByVideoId[activeVideo.id] || DEFAULT_SUBTITLE_STYLE
    : DEFAULT_SUBTITLE_STYLE
  const activeOverrides = activeSubtitle
    ? activeWindow.subtitleOverridesById[activeSubtitle.subtitle_id] || {}
    : {}
  const previewEffect = effects.find(item => item.id === (
    activeWindow.previewEffectId || activeWindow.fixedEffectIds[0]
  )) || null

  const showNotice = useCallback((message, type = 'success') => {
    setNotice({ id: Date.now(), message, type })
  }, [])

  useEffect(() => {
    if (!notice) return undefined
    const timer = window.setTimeout(() => setNotice(null), 4000)
    return () => window.clearTimeout(timer)
  }, [notice])

  const updateWindow = useCallback((windowId, updater) => {
    setWindows(current => current.map(item => {
      if (item.id !== windowId) return item
      return typeof updater === 'function' ? updater(item) : { ...item, ...updater }
    }))
  }, [])

  const updateActiveWindow = useCallback(updater => {
    updateWindow(activeWindowId, updater)
  }, [activeWindowId, updateWindow])

  const refreshEffects = useCallback(async () => {
    const data = await api('/api/effects')
    setEffects(data.effects || [])
    setPresets(data.presets || [])
  }, [])

  useEffect(() => {
    const timer = window.setTimeout(() => {
      refreshEffects().catch(error => showNotice(error.message, 'error'))
    }, 0)
    return () => window.clearTimeout(timer)
  }, [refreshEffects, showNotice])

  const jobSignature = windows.map(item => `${item.id}:${item.jobId}:${item.job?.status || ''}`).join('|')
  useEffect(() => {
    const pending = windows.filter(item => item.jobId && !terminalStatuses.has(item.job?.status))
    if (!pending.length) return undefined
    let stopped = false
    const poll = async () => {
      await Promise.all(pending.map(async item => {
        try {
          const data = await api(`/api/jobs/${item.jobId}`)
          if (!stopped) updateWindow(item.id, current => ({ ...current, job: data.job }))
        } catch (error) {
          if (!stopped) showNotice(error.message, 'error')
        }
      }))
    }
    poll()
    const timer = window.setInterval(poll, 1000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [jobSignature]) // eslint-disable-line react-hooks/exhaustive-deps

  const addProbedVideos = async (videoItems, targetWindowId = activeWindowId) => {
    const enriched = await Promise.all(videoItems.map(async video => {
      try {
        const thumb = await api('/api/thumbnail', jsonOptions({ path: video.path }))
        return { ...video, thumbnailUrl: thumb.url }
      } catch {
        return { ...video, thumbnailUrl: '' }
      }
    }))
    updateWindow(targetWindowId, current => {
      const known = new Set(current.videos.map(item => item.path.toLocaleLowerCase()))
      const additions = enriched.filter(item => !known.has(item.path.toLocaleLowerCase()))
      const boundsByVideoId = { ...current.boundsByVideoId }
      additions.forEach(video => { boundsByVideoId[video.id] = defaultBounds(video) })
      const videos = [...current.videos, ...additions]
      return {
        ...current,
        videos,
        boundsByVideoId,
        activeVideoId: current.activeVideoId || additions[0]?.id || '',
      }
    })
    if (enriched.length) showNotice(`已导入 ${enriched.length} 个视频`)
  }

  const chooseVideos = async () => {
    if (locked) return
    if (!isDesktop()) {
      videoInputRef.current?.click()
      return
    }
    try {
      const paths = await window.aiLandscape.pickVideoFiles()
      if (!paths?.length) return
      setImporting(true)
      const videos = []
      const errors = []
      for (const path of paths) {
        try {
          const data = await api('/api/probe', jsonOptions({ path }))
          videos.push(data.video)
        } catch (error) {
          errors.push(`${path}: ${error.message}`)
        }
      }
      await addProbedVideos(videos)
      if (errors.length) showNotice(errors.join('\n'), 'error')
    } finally {
      setImporting(false)
    }
  }

  const uploadVideos = async event => {
    const files = [...(event.target.files || [])]
    event.target.value = ''
    if (!files.length) return
    setImporting(true)
    try {
      const body = new FormData()
      files.forEach(file => body.append('files', file))
      const data = await api('/api/videos/upload', { method: 'POST', body })
      await addProbedVideos(data.videos || [])
      if (data.errors?.length) {
        showNotice(data.errors.map(item => `${item.name}: ${item.error}`).join('\n'), 'error')
      }
    } catch (error) {
      showNotice(error.message, 'error')
    } finally {
      setImporting(false)
    }
  }

  const removeVideo = videoId => {
    updateActiveWindow(current => {
      const videos = current.videos.filter(item => item.id !== videoId)
      const cleanMap = source => {
        const copyMap = { ...source }
        delete copyMap[videoId]
        return copyMap
      }
      return {
        ...current,
        videos,
        activeVideoId: current.activeVideoId === videoId ? videos[0]?.id || '' : current.activeVideoId,
        selectedVideoIds: current.selectedVideoIds.filter(id => id !== videoId),
        boundsByVideoId: cleanMap(current.boundsByVideoId),
        subtitleByVideoId: cleanMap(current.subtitleByVideoId),
        subtitleLayoutByVideoId: cleanMap(current.subtitleLayoutByVideoId),
        subtitleStyleByVideoId: cleanMap(current.subtitleStyleByVideoId),
      }
    })
  }

  const matchSubtitles = subtitleItems => {
    const grouped = new Map()
    subtitleItems.forEach(item => {
      const key = normalizeStem(item.name)
      grouped.set(key, [...(grouped.get(key) || []), item])
    })
    const conflicts = new Set([...grouped].filter(([, items]) => items.length > 1).map(([key]) => key))
    let matches = 0
    updateActiveWindow(current => {
      const next = { ...current.subtitleByVideoId }
      current.videos.forEach(video => {
        const key = normalizeStem(video.name)
        const item = !conflicts.has(key) ? grouped.get(key)?.[0] : null
        if (item) {
          next[video.id] = item
          matches += 1
        }
      })
      return { ...current, subtitleByVideoId: next }
    })
    const unmatched = subtitleItems.length - new Set(
      activeWindow.videos.map(video => normalizeStem(video.name)).filter(key => grouped.has(key) && !conflicts.has(key)),
    ).size
    if (conflicts.size) showNotice(`存在 ${conflicts.size} 个同名字幕冲突，已跳过`, 'error')
    else if (matches) showNotice(`已匹配 ${matches} 个视频${unmatched > 0 ? `，${unmatched} 个字幕未匹配` : ''}`)
    else showNotice('没有找到同名视频', 'error')
  }

  const chooseSubtitles = async () => {
    if (locked || !activeWindow.videos.length) return
    if (!isDesktop()) {
      subtitleInputRef.current?.click()
      return
    }
    try {
      const paths = await window.aiLandscape.pickSubtitleFiles()
      if (!paths?.length) return
      const data = await api('/api/subtitles/import-paths', jsonOptions({ paths }))
      matchSubtitles(data.subtitles || [])
    } catch (error) {
      showNotice(error.message, 'error')
    }
  }

  const uploadSubtitles = async event => {
    const files = [...(event.target.files || [])]
    event.target.value = ''
    if (!files.length) return
    const body = new FormData()
    files.forEach(file => body.append('files', file))
    try {
      const data = await api('/api/subtitles/upload', { method: 'POST', body })
      matchSubtitles(data.subtitles || [])
    } catch (error) {
      showNotice(error.message, 'error')
    }
  }

  const setActiveBounds = next => {
    if (!activeVideo) return
    updateActiveWindow(current => ({
      ...current,
      boundsByVideoId: { ...current.boundsByVideoId, [activeVideo.id]: next },
    }))
  }

  const applyBound = (kind, selectedOnly) => {
    if (!activeVideo || !activeBounds) return
    const targets = selectedOnly
      ? activeWindow.videos.filter(video => activeWindow.selectedVideoIds.includes(video.id))
      : activeWindow.videos
    if (!targets.length) {
      showNotice('请先勾选目标视频', 'error')
      return
    }
    updateActiveWindow(current => {
      const next = { ...current.boundsByVideoId }
      targets.forEach(video => {
        const existing = next[video.id] || defaultBounds(video)
        const activeRatio = activeBounds[`${kind}_y`] / activeBounds.basis_height
        next[video.id] = {
          ...existing,
          [`${kind}_y`]: activeRatio * video.height,
          basis_height: video.height,
        }
      })
      return {
        ...current,
        boundsByVideoId: next,
        selectedVideoIds: selectedOnly ? [] : current.selectedVideoIds,
      }
    })
    showNotice(`${kind === 'upper' ? '上限' : '下限'}已应用到 ${targets.length} 个视频`)
  }

  const setActiveLayout = next => {
    if (!activeVideo) return
    updateActiveWindow(current => ({
      ...current,
      subtitleLayoutByVideoId: { ...current.subtitleLayoutByVideoId, [activeVideo.id]: next },
    }))
  }

  const setActiveStyle = next => {
    if (!activeVideo) return
    updateActiveWindow(current => ({
      ...current,
      subtitleStyleByVideoId: { ...current.subtitleStyleByVideoId, [activeVideo.id]: next },
    }))
  }

  const applySubtitleStyle = selectedOnly => {
    if (!activeVideo) return
    const targets = selectedOnly
      ? activeWindow.videos.filter(video => activeWindow.selectedVideoIds.includes(video.id))
      : activeWindow.videos
    if (!targets.length) {
      showNotice('请先勾选目标视频', 'error')
      return
    }
    updateActiveWindow(current => {
      const layouts = { ...current.subtitleLayoutByVideoId }
      const styles = { ...current.subtitleStyleByVideoId }
      targets.forEach(video => {
        layouts[video.id] = clone(activeLayout)
        styles[video.id] = clone(activeStyle)
      })
      return {
        ...current,
        subtitleLayoutByVideoId: layouts,
        subtitleStyleByVideoId: styles,
        selectedVideoIds: selectedOnly ? [] : current.selectedVideoIds,
      }
    })
    showNotice(`字幕样式已应用到 ${targets.length} 个视频`)
  }

  const updateSubtitleText = (cueIndex, text) => {
    if (!activeSubtitle) return
    updateActiveWindow(current => {
      const currentOverrides = current.subtitleOverridesById[activeSubtitle.subtitle_id] || {}
      return {
        ...current,
        subtitleOverridesById: {
          ...current.subtitleOverridesById,
          [activeSubtitle.subtitle_id]: { ...currentOverrides, [cueIndex]: text.slice(0, 1000) },
        },
      }
    })
  }

  const chooseOutputDirectory = async () => {
    if (!isDesktop()) return
    const value = await window.aiLandscape.pickOutputDirectory()
    if (value) setOutputDir(value)
  }

  const submit = async () => {
    if (!activeWindow.videos.length) return showNotice('请先导入视频', 'error')
    if (!outputDir.trim()) return showNotice('请选择输出目录', 'error')
    const subtitleConfigs = activeWindow.videos.flatMap(video => {
      const subtitle = activeWindow.subtitleByVideoId[video.id]
      if (!subtitle) return []
      const overrides = activeWindow.subtitleOverridesById[subtitle.subtitle_id] || {}
      return [{
        video_path: video.path,
        subtitle_id: subtitle.subtitle_id,
        layout: activeWindow.subtitleLayoutByVideoId[video.id] || DEFAULT_SUBTITLE_LAYOUT,
        style: activeWindow.subtitleStyleByVideoId[video.id] || DEFAULT_SUBTITLE_STYLE,
        cue_text_overrides: Object.entries(overrides).map(([cueIndex, text]) => ({
          cue_index: Number(cueIndex), text,
        })),
      }]
    })
    const payload = {
      videos: activeWindow.videos.map(video => ({ path: video.path })),
      output_dir: outputDir.trim(),
      gpu_mode: gpuMode,
      workers,
      output_video_bitrate_k: bitrate,
      ai_crop_bounds: activeWindow.videos.map(video => ({
        video_path: video.path,
        ...(activeWindow.boundsByVideoId[video.id] || defaultBounds(video)),
      })),
      subtitle_configs: subtitleConfigs,
      effect_all_template_ids: activeWindow.fixedEffectIds,
      effect_random_template_ids: activeWindow.randomEffectIds,
    }
    try {
      const data = await api('/api/ai-landscape', jsonOptions(payload))
      updateActiveWindow(current => ({ ...current, jobId: data.job_id, job: data.job }))
      showNotice('任务已提交')
    } catch (error) {
      showNotice(error.message, 'error')
    }
  }

  const cancelCurrentJob = async () => {
    if (!activeWindow.jobId) return
    try {
      const data = await api(`/api/jobs/${activeWindow.jobId}/cancel`, { method: 'POST' })
      updateActiveWindow(current => ({ ...current, job: data.job }))
    } catch (error) {
      showNotice(error.message, 'error')
    }
  }

  const addWindow = () => {
    const next = createWindow(windows.length + 1)
    setWindows(current => [...current, next])
    setActiveWindowId(next.id)
  }

  const closeWindow = windowId => {
    const target = windows.find(item => item.id === windowId)
    if (isLocked(target) || windows.length === 1) return
    const next = windows.filter(item => item.id !== windowId)
    setWindows(next)
    if (activeWindowId === windowId) setActiveWindowId(next[0].id)
  }

  return (
    <main className="app-shell">
      <header className="app-header">
        <div className="brand"><Video size={21} /><strong>AI Landscape</strong><span>0.1.0</span></div>
        <div className="window-tabs">
          {windows.map(item => (
            <button
              key={item.id}
              className={`window-tab ${item.id === activeWindowId ? 'active' : ''}`}
              onClick={() => setActiveWindowId(item.id)}
            >
              <span>{item.name}</span>
              {item.job && !terminalStatuses.has(item.job.status) && <LoaderCircle className="spin" size={13} />}
              <X size={13} onClick={event => { event.stopPropagation(); closeWindow(item.id) }} />
            </button>
          ))}
          <button className="icon-button" title="新建窗口" onClick={addWindow}><Plus size={16} /></button>
        </div>
      </header>

      <div className="workbench">
        <aside className="left-pane">
          <section className="settings-section">
            <header className="section-header"><h2>输出设置</h2></header>
            <div className="output-directory">
              <input value={outputDir} disabled={locked} placeholder="输出目录" onChange={event => setOutputDir(event.target.value)} />
              {isDesktop() && <button className="icon-button" title="选择输出目录" disabled={locked} onClick={chooseOutputDirectory}><FolderOpen size={16} /></button>}
            </div>
            <div className="setting-grid">
              <label>编码<select value={gpuMode} disabled={locked} onChange={event => setGpuMode(event.target.value)}><option value="auto">自动</option><option value="gpu">GPU</option><option value="cpu">CPU</option></select></label>
              <label>并发<NumberEditor value={workers} min={1} max={8} disabled={locked} onCommit={value => setWorkers(Math.round(value))} /></label>
              <label>码率<NumberEditor value={bitrate} min={300} max={100000} disabled={locked} suffix="kbps" onCommit={value => setBitrate(Math.round(value))} /></label>
            </div>
          </section>

          <section className="videos-section">
            <header className="section-header">
              <h2>视频 <span>{activeWindow.videos.length}</span></h2>
              <div className="toolbar-actions">
                <button className="text-button" disabled={locked || !activeWindow.videos.length} onClick={() => updateActiveWindow(current => ({ ...current, selectedVideoIds: current.selectedVideoIds.length === current.videos.length ? [] : current.videos.map(item => item.id) }))}>
                  {activeWindow.selectedVideoIds.length === activeWindow.videos.length && activeWindow.videos.length ? <CheckSquare size={14} /> : <Square size={14} />}全选
                </button>
                <button className="icon-button" title="导入视频" disabled={locked || importing} onClick={chooseVideos}>{importing ? <LoaderCircle className="spin" size={16} /> : <Upload size={16} />}</button>
              </div>
            </header>
            <input ref={videoInputRef} type="file" accept="video/*" multiple hidden onChange={uploadVideos} />
            <div className="video-list">
              {activeWindow.videos.map(video => {
                const selected = activeWindow.selectedVideoIds.includes(video.id)
                return (
                  <button key={video.id} className={`video-row ${video.id === activeWindow.activeVideoId ? 'active' : ''}`} onClick={() => updateActiveWindow({ activeVideoId: video.id })}>
                    <input type="checkbox" checked={selected} disabled={locked} onClick={event => event.stopPropagation()} onChange={() => updateActiveWindow(current => ({ ...current, selectedVideoIds: selected ? current.selectedVideoIds.filter(id => id !== video.id) : [...current.selectedVideoIds, video.id] }))} />
                    <div className="video-thumb">{video.thumbnailUrl ? <img src={video.thumbnailUrl} alt="" /> : <Video size={18} />}</div>
                    <div className="video-meta"><strong title={video.name}>{video.name}</strong><span>{video.width}×{video.height} · {formatDuration(video.duration)}</span>{activeWindow.subtitleByVideoId[video.id] && <small><Subtitles size={12} />{activeWindow.subtitleByVideoId[video.id].name}</small>}</div>
                    <span className="icon-button remove" role="button" title="移除视频" onClick={event => { event.stopPropagation(); removeVideo(video.id) }}><Trash2 size={14} /></span>
                  </button>
                )
              })}
              {!activeWindow.videos.length && <div className="empty-line">暂无视频</div>}
            </div>
          </section>

          {activeVideo && (
            <section className="bounds-section">
              <header className="section-header"><h2>取景上下限</h2></header>
              <div className="bound-apply-row">
                <span>上限 <b>{Math.round(activeBounds.upper_y)}px</b></span>
                <button className="text-button" disabled={locked} onClick={() => applyBound('upper', false)}>应用全部</button>
                <button className="text-button" disabled={locked} onClick={() => applyBound('upper', true)}>应用选中</button>
                <span>下限 <b>{Math.round(activeBounds.lower_y)}px</b></span>
                <button className="text-button" disabled={locked} onClick={() => applyBound('lower', false)}>应用全部</button>
                <button className="text-button" disabled={locked} onClick={() => applyBound('lower', true)}>应用选中</button>
              </div>
            </section>
          )}

          <section className="subtitle-import-section">
            <header className="section-header">
              <h2>字幕</h2>
              <button className="text-button" disabled={locked || !activeWindow.videos.length} onClick={chooseSubtitles}><Subtitles size={15} />上传字幕</button>
            </header>
            <input ref={subtitleInputRef} type="file" accept=".srt" multiple hidden onChange={uploadSubtitles} />
            {activeSubtitle && <div className="matched-subtitle"><span title={activeSubtitle.name}>{activeSubtitle.name}</span><button className="icon-button" title="清除当前视频字幕" disabled={locked} onClick={() => updateActiveWindow(current => { const next = { ...current.subtitleByVideoId }; delete next[activeVideo.id]; return { ...current, subtitleByVideoId: next } })}><X size={14} /></button></div>}
          </section>

          <EffectsPanel
            effects={effects}
            presets={presets}
            fixedIds={activeWindow.fixedEffectIds}
            randomIds={activeWindow.randomEffectIds}
            previewId={activeWindow.previewEffectId}
            locked={locked}
            onFixedChange={fixedEffectIds => updateActiveWindow({ fixedEffectIds })}
            onRandomChange={randomEffectIds => updateActiveWindow({ randomEffectIds })}
            onPreviewChange={previewEffectId => updateActiveWindow({ previewEffectId })}
            onRefresh={refreshEffects}
            onNotice={showNotice}
          />
        </aside>

        <section className="right-pane">
          <Player
            key={activeVideo?.id || 'empty-player'}
            video={activeVideo}
            mode={activeWindow.previewMode}
            onModeChange={previewMode => updateActiveWindow({ previewMode })}
            bounds={activeBounds}
            onBoundsChange={setActiveBounds}
            locked={locked}
            effect={previewEffect}
            subtitle={activeSubtitle}
            subtitleOverrides={activeOverrides}
            subtitleLayout={activeLayout}
            subtitleStyle={activeStyle}
            onSubtitleLayoutChange={setActiveLayout}
            onSubtitleTextChange={updateSubtitleText}
          />

          {activeVideo && activeWindow.previewMode === 'horizontal' && (
            <section className="subtitle-toolbar">
              <header className="section-header"><h2>字幕样式</h2><span>1280×720</span></header>
              <div className="subtitle-parameter-row">
                <label>字体<select value={activeStyle.font_id} disabled={locked} onChange={event => setActiveStyle({ ...activeStyle, font_id: event.target.value })}>{SUBTITLE_FONTS.map(font => <option key={font.id} value={font.id}>{font.label}</option>)}</select></label>
                <label>字号<NumberEditor value={activeLayout.font_size} min={18} max={120} step={1} disabled={locked} suffix="px" onCommit={font_size => setActiveLayout({ ...activeLayout, font_size })} /></label>
              </div>
              <div className="subtitle-parameter-row">
                <label>描边<NumberEditor value={activeStyle.outline_width} min={0} max={10} step={0.5} disabled={locked} suffix="px" onCommit={outline_width => setActiveStyle({ ...activeStyle, outline_width })} /></label>
                <label>阴影<NumberEditor value={activeStyle.shadow_opacity_percent} min={0} max={100} step={1} disabled={locked} suffix="%" onCommit={shadow_opacity_percent => setActiveStyle({ ...activeStyle, shadow_opacity_percent })} /></label>
              </div>
              <div className="subtitle-style-actions">
                <button className="text-button" disabled={locked} onClick={() => applySubtitleStyle(false)}>应用到全部</button>
                <button className="text-button" disabled={locked} onClick={() => applySubtitleStyle(true)}>应用到选中</button>
                <button className="text-button" disabled={locked} onClick={() => { setActiveLayout(clone(DEFAULT_SUBTITLE_LAYOUT)); setActiveStyle(clone(DEFAULT_SUBTITLE_STYLE)) }}><RotateCcw size={14} />重置样式</button>
              </div>
            </section>
          )}

          <section className="output-action-band">
            <div className="job-summary">
              {activeWindow.job ? <JobSummary job={activeWindow.job} /> : <span>等待提交</span>}
            </div>
            {locked ? (
              <button className="primary-button danger" onClick={cancelCurrentJob}>取消任务</button>
            ) : (
              <button className="primary-button" disabled={!activeWindow.videos.length} onClick={submit}>开始输出</button>
            )}
          </section>
          {activeWindow.job?.results?.length > 0 && <ResultList job={activeWindow.job} />}
        </section>
      </div>
      {notice && <div className={`notice ${notice.type}`}>{notice.message}</div>}
    </main>
  )
}

function JobSummary({ job }) {
  const preprocess = job.preprocess || {}
  if (job.status === 'pending') return <><LoaderCircle className="spin" size={15} />排队中 · 第 {job.queue_position} 位</>
  if (job.status === 'running') {
    if (preprocess.status !== 'done') {
      return <><LoaderCircle className="spin" size={15} />AI分析 {preprocess.completed || 0}/{preprocess.total || 0} · {preprocess.current || preprocess.stage || ''}</>
    }
    return <><LoaderCircle className="spin" size={15} />编码 {job.completed + job.failed}/{job.total}</>
  }
  if (job.status === 'cancelling') return <><LoaderCircle className="spin" size={15} />正在取消</>
  if (job.status === 'cancelled') return <>任务已取消</>
  if (job.status === 'error') return <>任务失败 · {job.error}</>
  return <>完成 {job.completed} · 失败 {job.failed}</>
}

function ResultList({ job }) {
  const [open, setOpen] = useState(false)
  return (
    <section className="result-list">
      <button className="result-toggle" onClick={() => setOpen(value => !value)}>
        <span>输出结果 ({job.results.length})</span><ChevronDown size={16} className={open ? 'open' : ''} />
      </button>
      {open && job.results.map(item => (
        <div key={item.output_index} className={`result-row ${item.status}`}>
          <span>{item.status === 'ok' ? '完成' : '失败'}</span>
          <strong title={item.name}>{item.name}</strong>
          <small>{item.status === 'ok' ? `${item.encoder} · ${item.run_seconds}s` : item.error}</small>
          {item.status === 'ok' && isDesktop() && <button className="icon-button" title="打开文件位置" onClick={() => window.aiLandscape.showItemInFolder(item.output_path)}><FolderOpen size={14} /></button>}
        </div>
      ))}
    </section>
  )
}
