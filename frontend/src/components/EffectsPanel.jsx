import { useRef, useState } from 'react'
import { Check, FolderPlus, Save, Settings2, Trash2, X } from 'lucide-react'
import { api, isDesktop, jsonOptions } from '../api.js'

export default function EffectsPanel({
  effects,
  presets,
  fixedIds,
  randomIds,
  previewId,
  locked,
  onFixedChange,
  onRandomChange,
  onPreviewChange,
  onRefresh,
  onNotice,
}) {
  const inputRef = useRef(null)
  const [manage, setManage] = useState(false)
  const [managedIds, setManagedIds] = useState([])
  const [presetId, setPresetId] = useState('')

  const importEffects = async () => {
    try {
      if (isDesktop()) {
        const paths = await window.aiLandscape.pickEffectFiles()
        if (!paths?.length) return
        await api('/api/effects/import-paths', jsonOptions({ paths }))
      } else {
        inputRef.current?.click()
        return
      }
      await onRefresh()
    } catch (error) {
      onNotice(error.message, 'error')
    }
  }

  const uploadEffects = async event => {
    const files = [...(event.target.files || [])]
    event.target.value = ''
    if (!files.length) return
    const body = new FormData()
    files.forEach(file => body.append('files', file))
    try {
      await api('/api/effects/upload', { method: 'POST', body })
      await onRefresh()
    } catch (error) {
      onNotice(error.message, 'error')
    }
  }

  const deleteManaged = async () => {
    if (!managedIds.length) return
    try {
      await api('/api/effects', jsonOptions({ ids: managedIds }, 'DELETE'))
      onFixedChange(fixedIds.filter(id => !managedIds.includes(id)))
      onRandomChange(randomIds.filter(id => !managedIds.includes(id)))
      setManagedIds([])
      setManage(false)
      await onRefresh()
    } catch (error) {
      onNotice(error.message, 'error')
    }
  }

  const savePreset = async () => {
    const name = window.prompt('配置名称')?.trim()
    if (!name) return
    try {
      await api('/api/effect-presets', jsonOptions({ name, fixed_ids: fixedIds, random_ids: randomIds }))
      await onRefresh()
      onNotice('特效配置已保存')
    } catch (error) {
      onNotice(error.message, 'error')
    }
  }

  const applyPreset = value => {
    setPresetId(value)
    const preset = presets.find(item => item.id === value)
    if (!preset) return
    const available = new Set(effects.filter(item => item.available).map(item => item.id))
    onFixedChange((preset.fixed_ids || []).filter(id => available.has(id)))
    onRandomChange((preset.random_ids || []).filter(id => available.has(id)))
  }

  return (
    <section className="effects-panel">
      <header className="section-header">
        <h2>特效</h2>
        <div className="toolbar-actions">
          <button className="icon-button" title="导入特效" disabled={locked} onClick={importEffects}><FolderPlus size={16} /></button>
          <button
            className={`icon-button ${manage ? 'active' : ''}`}
            title={manage ? '退出管理' : '管理特效'}
            disabled={locked || !effects.length}
            onClick={() => { setManage(value => !value); setManagedIds([]) }}
          >{manage ? <X size={16} /> : <Settings2 size={16} />}</button>
          {manage && (
            <button className="icon-button danger" title="删除勾选" disabled={!managedIds.length} onClick={deleteManaged}><Trash2 size={16} /></button>
          )}
        </div>
      </header>
      <input ref={inputRef} type="file" accept=".gif,.mov,.webm" multiple hidden onChange={uploadEffects} />
      <div className="preset-row">
        <select value={presetId} disabled={locked} onChange={event => applyPreset(event.target.value)}>
          <option value="">特效配置</option>
          {presets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <button className="icon-button" title="保存当前配置" disabled={locked} onClick={savePreset}><Save size={15} /></button>
        <button className="text-button" disabled={locked || !randomIds.length} onClick={() => onRandomChange([])}>清空随机池</button>
      </div>
      {!effects.length ? (
        <div className="empty-line">暂无特效</div>
      ) : (
        <div className="effect-grid">
          {effects.map(effect => {
            const fixed = fixedIds.includes(effect.id)
            const random = randomIds.includes(effect.id)
            const managed = managedIds.includes(effect.id)
            return (
              <article
                key={effect.id}
                className={`effect-item ${previewId === effect.id ? 'previewing' : ''} ${!effect.available ? 'unavailable' : ''}`}
                onClick={() => !manage && effect.available && onPreviewChange(effect.id)}
              >
                <div className="effect-thumb">
                  <img src={effect.preview_url} alt="" loading="lazy" />
                  {manage && (
                    <button
                      className={`manage-check ${managed ? 'checked' : ''}`}
                      aria-label="选择特效"
                      onClick={event => {
                        event.stopPropagation()
                        setManagedIds(ids => managed ? ids.filter(id => id !== effect.id) : [...ids, effect.id])
                      }}
                    >{managed && <Check size={14} />}</button>
                  )}
                </div>
                <div className="effect-name" title={effect.name}>{effect.name}</div>
                {!manage && (
                  <div className="effect-toggles" onClick={event => event.stopPropagation()}>
                    <label><input type="checkbox" checked={fixed} disabled={locked || !effect.available} onChange={() => onFixedChange(fixed ? fixedIds.filter(id => id !== effect.id) : [...fixedIds, effect.id])} />固定</label>
                    <label><input type="checkbox" checked={random} disabled={locked || !effect.available} onChange={() => onRandomChange(random ? randomIds.filter(id => id !== effect.id) : [...randomIds, effect.id])} />随机池</label>
                  </div>
                )}
              </article>
            )
          })}
        </div>
      )}
    </section>
  )
}
