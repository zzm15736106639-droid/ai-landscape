import { useEffect, useMemo, useRef, useState } from 'react'
import { Maximize2, Pause, Play, RotateCcw } from 'lucide-react'
import { api, jsonOptions, videoUrl } from '../api.js'
import {
  DEFAULT_SUBTITLE_LAYOUT,
  DEFAULT_SUBTITLE_STYLE,
  OUTPUT_HEIGHT,
  SUBTITLE_FONTS,
  formatDuration,
} from '../constants.js'

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value))
}

function currentCueForTime(subtitle, overrides, currentTime) {
  const cues = subtitle?.cues || []
  const index = cues.findIndex(cue => currentTime >= cue.start && currentTime < cue.end)
  if (index < 0) return null
  const override = Object.prototype.hasOwnProperty.call(overrides || {}, index)
    ? overrides[index]
    : cues[index].text
  return { ...cues[index], index, text: override }
}

export default function Player({
  video,
  mode,
  onModeChange,
  bounds,
  onBoundsChange,
  locked,
  effect,
  subtitle,
  subtitleOverrides,
  subtitleLayout,
  subtitleStyle,
  onSubtitleLayoutChange,
  onSubtitleTextChange,
}) {
  const videoRef = useRef(null)
  const viewportRef = useRef(null)
  const [playing, setPlaying] = useState(false)
  const [currentTime, setCurrentTime] = useState(0)
  const [duration, setDuration] = useState(Number(video?.duration) || 0)
  const [viewportSize, setViewportSize] = useState({ width: 0, height: 0 })
  const [drag, setDrag] = useState(null)
  const [subtitleEditing, setSubtitleEditing] = useState(false)
  const [realPreview, setRealPreview] = useState({ status: 'idle', url: '', key: '' })

  const layout = subtitleLayout || DEFAULT_SUBTITLE_LAYOUT
  const style = subtitleStyle || DEFAULT_SUBTITLE_STYLE
  const activeCue = useMemo(
    () => currentCueForTime(subtitle, subtitleOverrides, currentTime),
    [subtitle, subtitleOverrides, currentTime],
  )

  useEffect(() => {
    const node = viewportRef.current
    if (!node) return undefined
    const update = () => setViewportSize({ width: node.clientWidth, height: node.clientHeight })
    update()
    const observer = new ResizeObserver(update)
    observer.observe(node)
    return () => observer.disconnect()
  }, [video?.id, mode])

  const previewSignature = activeCue?.text
    ? JSON.stringify([activeCue.text, layout, style])
    : ''
  const realStatus = mode === 'horizontal' && !subtitleEditing && realPreview.key === previewSignature
    ? realPreview.status
    : 'idle'

  useEffect(() => {
    if (mode !== 'horizontal' || !activeCue?.text || subtitleEditing) return undefined
    const signature = previewSignature
    const controller = new AbortController()
    const timer = window.setTimeout(async () => {
      setRealPreview(previous => ({ ...previous, status: 'loading', key: signature }))
      try {
        const data = await api('/api/ai-landscape/subtitle-preview-frame', {
          ...jsonOptions({ text: activeCue.text, layout, style }),
          signal: controller.signal,
        })
        setRealPreview({ status: 'ready', url: data.url, key: signature })
      } catch (error) {
        if (error.name !== 'AbortError') {
          setRealPreview({ status: 'error', url: '', key: signature })
        }
      }
    }, 500)
    return () => {
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [activeCue?.text, layout, mode, previewSignature, style, subtitleEditing])

  useEffect(() => {
    if (!drag) return undefined
    const handleMove = event => {
      const rect = viewportRef.current?.getBoundingClientRect()
      if (!rect || !rect.height) return
      if (drag.type === 'upper' || drag.type === 'lower') {
        const y = clamp((event.clientY - rect.top) / rect.height * video.height, 0, video.height)
        const minimum = video.width * 9 / 16
        if (drag.type === 'upper') {
          onBoundsChange({ ...bounds, upper_y: Math.min(y, bounds.lower_y - minimum) })
        } else {
          onBoundsChange({ ...bounds, lower_y: Math.max(y, bounds.upper_y + minimum) })
        }
      } else if (drag.type === 'subtitle-y') {
        const delta = (event.clientY - drag.startY) / rect.height * OUTPUT_HEIGHT
        onSubtitleLayoutChange({
          ...layout,
          center_x: 640,
          center_y: clamp(drag.centerY + delta, 0, OUTPUT_HEIGHT),
        })
      } else if (drag.type === 'subtitle-size') {
        const delta = Math.max(event.clientX - drag.startX, event.clientY - drag.startY)
        const outputDelta = delta / rect.height * OUTPUT_HEIGHT * 0.25
        onSubtitleLayoutChange({
          ...layout,
          center_x: 640,
          font_size: clamp(drag.fontSize + outputDelta, 18, 120),
        })
      }
    }
    const handleUp = () => {
      setDrag(null)
      window.setTimeout(() => setSubtitleEditing(false), 120)
    }
    window.addEventListener('pointermove', handleMove)
    window.addEventListener('pointerup', handleUp, { once: true })
    return () => {
      window.removeEventListener('pointermove', handleMove)
      window.removeEventListener('pointerup', handleUp)
    }
  }, [bounds, drag, layout, onBoundsChange, onSubtitleLayoutChange, video])

  if (!video) {
    return <div className="player-empty">选择一个视频进行编辑</div>
  }

  const verticalStyle = { aspectRatio: `${video.width} / ${video.height}` }
  const upperPercent = bounds ? bounds.upper_y / video.height * 100 : 0
  const lowerPercent = bounds ? bounds.lower_y / video.height * 100 : 100
  const scale = viewportSize.height / OUTPUT_HEIGHT || 1
  const font = SUBTITLE_FONTS.find(item => item.id === style.font_id) || SUBTITLE_FONTS[0]
  const fontSize = layout.font_size * scale
  const outline = style.outline_width * scale
  const shadowDistance = layout.font_size * 2 / 44 * scale
  const shadowAlpha = style.shadow_opacity_percent / 100
  const textWidth = clamp((activeCue?.text?.length || 1) * fontSize + 28, 80, viewportSize.width || 80)
  const subtitleBoxStyle = {
    top: `${layout.center_y / OUTPUT_HEIGHT * 100}%`,
    width: `${textWidth}px`,
    fontFamily: `'${font.family}', sans-serif`,
    fontSize: `${fontSize}px`,
    WebkitTextStroke: `${outline}px #000`,
    textShadow: shadowAlpha > 0
      ? `${shadowDistance}px ${shadowDistance}px 0 rgba(0,0,0,${shadowAlpha})`
      : 'none',
  }

  const togglePlayback = async () => {
    const node = videoRef.current
    if (!node) return
    if (node.paused) {
      await node.play()
    } else {
      node.pause()
    }
  }

  return (
    <section className="player-panel">
      <header className="player-header">
        <div className="mode-segment" role="tablist" aria-label="预览方向">
          <button className={mode === 'vertical' ? 'active' : ''} onClick={() => onModeChange('vertical')}>竖屏</button>
          <button className={mode === 'horizontal' ? 'active' : ''} onClick={() => onModeChange('horizontal')}>横屏</button>
        </div>
        <strong title={video.name}>{video.name}</strong>
        <span>{video.width}×{video.height}</span>
      </header>
      <div className="player-stage">
        <div
          ref={viewportRef}
          className={`player-viewport ${mode}`}
          style={mode === 'vertical' ? verticalStyle : undefined}
        >
          <video
            key={video.path}
            ref={videoRef}
            src={videoUrl(video.path)}
            playsInline
            preload="metadata"
            onLoadedMetadata={event => setDuration(event.currentTarget.duration || video.duration || 0)}
            onTimeUpdate={event => setCurrentTime(event.currentTarget.currentTime)}
            onPlay={() => setPlaying(true)}
            onPause={() => setPlaying(false)}
            onEnded={() => setPlaying(false)}
          />
          {mode === 'vertical' && bounds && (
            <div className="bounds-layer">
              <button
                className="bound-line upper"
                style={{ top: `${upperPercent}%` }}
                disabled={locked}
                aria-label="拖动取景上限"
                onPointerDown={event => {
                  event.preventDefault()
                  setDrag({ type: 'upper' })
                }}
              ><span>上限 {Math.round(bounds.upper_y)}px</span></button>
              <div className="bound-shade top" style={{ height: `${upperPercent}%` }} />
              <button
                className="bound-line lower"
                style={{ top: `${lowerPercent}%` }}
                disabled={locked}
                aria-label="拖动取景下限"
                onPointerDown={event => {
                  event.preventDefault()
                  setDrag({ type: 'lower' })
                }}
              ><span>下限 {Math.round(bounds.lower_y)}px</span></button>
              <div className="bound-shade bottom" style={{ height: `${100 - lowerPercent}%` }} />
            </div>
          )}
          {mode === 'horizontal' && effect && (
            PathExtension(effect.original_name) === '.gif'
              ? <img className="effect-overlay" src={effect.source_url} alt="" />
              : <video className="effect-overlay" key={effect.id} src={effect.source_url} autoPlay loop muted playsInline />
          )}
          {mode === 'horizontal' && activeCue?.text && realStatus === 'ready' && !subtitleEditing && (
            <img className="subtitle-real-overlay" src={realPreview.url} alt="" />
          )}
          {mode === 'horizontal' && activeCue && (activeCue.text || subtitleEditing) && (
            <div
              className={`subtitle-editor ${realStatus === 'ready' && !subtitleEditing ? 'real-ready' : ''}`}
              style={subtitleBoxStyle}
            >
              <button
                className="subtitle-move"
                aria-label="上下移动字幕"
                disabled={locked}
                onPointerDown={event => {
                  event.preventDefault()
                  setSubtitleEditing(true)
                  setDrag({ type: 'subtitle-y', startY: event.clientY, centerY: layout.center_y })
                }}
              ><Maximize2 size={13} /></button>
              <input
                value={activeCue.text}
                disabled={locked}
                aria-label="字幕文字"
                onFocus={() => {
                  videoRef.current?.pause()
                  setSubtitleEditing(true)
                }}
                onBlur={() => setSubtitleEditing(false)}
                onChange={event => onSubtitleTextChange(activeCue.index, event.target.value.replace(/[\r\n]+/g, ' '))}
              />
              <button
                className="subtitle-resize"
                aria-label="调整字幕字号"
                disabled={locked}
                onPointerDown={event => {
                  event.preventDefault()
                  setSubtitleEditing(true)
                  setDrag({
                    type: 'subtitle-size',
                    startX: event.clientX,
                    startY: event.clientY,
                    fontSize: layout.font_size,
                  })
                }}
              />
            </div>
          )}
        </div>
      </div>
      <div className="player-controls">
        <button className="icon-button" onClick={togglePlayback} title={playing ? '暂停' : '播放'}>
          {playing ? <Pause size={17} /> : <Play size={17} />}
        </button>
        <input
          className="timeline"
          type="range"
          min="0"
          max={Math.max(duration, 0.01)}
          step="0.01"
          value={Math.min(currentTime, duration || 0)}
          onChange={event => {
            const value = Number(event.target.value)
            if (videoRef.current) videoRef.current.currentTime = value
            setCurrentTime(value)
          }}
        />
        <span>{formatDuration(currentTime)} / {formatDuration(duration)}</span>
        {mode === 'horizontal' && activeCue?.text && (
          <span className={`real-status ${realStatus}`}>
            {realStatus === 'loading' && '字幕真实效果生成中'}
            {realStatus === 'ready' && '字幕真实效果已生成'}
            {realStatus === 'error' && '字幕真实效果生成失败'}
          </span>
        )}
        {mode === 'horizontal' && activeCue && !activeCue.text && !subtitleEditing && (
          <button
            className="text-button"
            disabled={locked}
            onClick={() => onSubtitleTextChange(activeCue.index, subtitle.cues[activeCue.index].text)}
          >恢复当前字幕</button>
        )}
        <button
          className="icon-button"
          title="回到开头"
          onClick={() => {
            if (videoRef.current) videoRef.current.currentTime = 0
            setCurrentTime(0)
          }}
        ><RotateCcw size={16} /></button>
      </div>
    </section>
  )
}

function PathExtension(value) {
  const match = String(value || '').toLowerCase().match(/\.[^.]+$/)
  return match?.[0] || ''
}
