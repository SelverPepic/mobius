/* The guide reads Store-owned listings and checks each app again at Install time. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { apiFetch } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { detailToMessage } from '../../lib/errorDetail.js'
import AppIcon from '../AppIcon.jsx'
import { accessRows } from './accessReview.js'

const CORE_IDS = ['store', 'social', 'memory', 'reflection', 'skills', 'integrations', 'identity']
const PICK_IDS = ['notes', 'habits', 'kanban', 'pages', 'webstudio', 'connect']
const CATALOG_URL = 'https://raw.githubusercontent.com/mobius-os/app-store/main/catalog.json'

function catalogItems(body) {
  if (body?.schema !== 1 || !Array.isArray(body.apps)) throw new Error('App Store listings are unavailable.')
  const items = new Map()
  for (const item of body.apps) {
    if (item && typeof item.id === 'string' && !items.has(item.id)) items.set(item.id, item)
  }
  return items
}

function storePicks(catalog) {
  return PICK_IDS.map(id => catalog?.get(id)).filter(item => item?.name && item?.description && item?.manifest_url && item?.raw_base)
}

function guideDescription(text) {
  return typeof text === 'string' ? text.replaceAll(';', ' —') : ''
}

function asDataUrl(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result)
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(blob)
  })
}

export default function WalkthroughStore({ apps }) {
  const queryClient = useQueryClient()
  const storeApp = apps.find(app => app.slug === 'store')
  const [catalog, setCatalog] = useState(null)
  const [catalogError, setCatalogError] = useState('')
  const [icons, setIcons] = useState({})
  const [review, setReview] = useState(null)
  const reviewRequest = useRef(0)
  const reviewButton = useRef(null)
  const installedButton = useRef(null)
  const [installedNow, setInstalledNow] = useState(new Set())
  const picks = useMemo(() => storePicks(catalog), [catalog])

  useEffect(() => () => { reviewRequest.current += 1 }, [])
  useEffect(() => {
    if (review?.phase === 'installed') installedButton.current?.focus({ preventScroll: true })
    if (review?.phase === 'ready' && !document.activeElement?.closest('dialog.wt__card')) {
      reviewButton.current?.focus({ preventScroll: true })
    }
  }, [review])

  useEffect(() => {
    let active = true
    const controller = new AbortController()
    async function load() {
      let hasLocal = false
      if (storeApp?.id) {
        try {
          const response = await apiFetch(`/apps/${storeApp.id}/source/file?path=catalog.json`, { signal: controller.signal, timeoutMs: 5000 })
          if (!response.ok) throw new Error('Local App Store catalog unavailable')
          const file = await response.json()
          const local = catalogItems(JSON.parse(file.content))
          if (active) { setCatalog(local); hasLocal = true }
        } catch (error) {
          if (error.name === 'AbortError') return
        }
      }
      try {
        const response = await apiFetch(`/proxy?url=${encodeURIComponent(CATALOG_URL)}`, { signal: controller.signal, timeoutMs: 8000 })
        if (!response.ok) throw new Error('Published App Store catalog unavailable')
        const remote = catalogItems(await response.json())
        if (active) { setCatalog(remote); setCatalogError('') }
      } catch (error) {
        if (active && !hasLocal) setCatalogError('App Store picks are not loading right now. You can explore the full collection in App Store later.')
      }
    }
    void load()
    return () => { active = false; controller.abort() }
  }, [storeApp?.id])

  useEffect(() => {
    if (!picks.length) return undefined
    let active = true
    const controller = new AbortController()
    for (const item of picks) {
      void (async () => {
        try {
          const response = await apiFetch(`/proxy?url=${encodeURIComponent(item.manifest_url)}`, { signal: controller.signal, timeoutMs: 8000 })
          if (!response.ok) return
          const manifest = await response.json()
          if (manifest.id !== item.id || typeof manifest.icon !== 'string') return
          const icon = new URL(manifest.icon, item.raw_base)
          if (icon.origin !== new URL(item.raw_base).origin || !icon.pathname.startsWith(new URL(item.raw_base).pathname)) return
          const iconResponse = await apiFetch(`/proxy?url=${encodeURIComponent(icon.href)}`, { signal: controller.signal, timeoutMs: 10_000 })
          if (!iconResponse.ok) return
          const image = await iconResponse.blob()
          if (!image.type.startsWith('image/')) return
          const iconData = await asDataUrl(image)
          if (active) setIcons(current => ({ ...current, [item.id]: iconData }))
        } catch (_) { /* Initials remain available when listing artwork is offline. */ }
      })()
    }
    return () => { active = false; controller.abort() }
  }, [picks])

  async function reviewApp(item) {
    const request = ++reviewRequest.current
    setReview({ id: item.id, phase: 'checking' })
    try {
      const previewResponse = await apiFetch('/apps/preview', {
        method: 'POST', body: JSON.stringify({ manifest_url: item.manifest_url }), timeoutMs: 20_000,
      })
      const reviewed = await previewResponse.json().catch(() => ({}))
      if (request !== reviewRequest.current) return
      if (!previewResponse.ok) throw new Error(detailToMessage(reviewed.detail, 'Could not check this app.'))
      if (reviewed.manifest?.id !== item.id || !reviewed.capability_digest || !reviewed.capability_contract) {
        throw new Error('This app could not be checked. Try again before installing.')
      }
      if (reviewed.installed_contract) {
        setInstalledNow(current => new Set(current).add(item.id))
        setReview({ id: item.id, phase: 'installed' })
        void appQueries.list.invalidate(queryClient)
        return
      }
      setReview({ id: item.id, phase: 'ready', preview: reviewed })
    } catch (error) {
      if (request === reviewRequest.current) setReview({ id: item.id, phase: 'error', error: error.message || 'Could not check this app.' })
    }
  }

  async function install(item) {
    if (review?.id !== item.id || review.phase !== 'ready') return
    const preview = review.preview
    setReview({ id: item.id, phase: 'installing', preview })
    try {
      const response = await apiFetch('/apps/install', {
        method: 'POST',
        body: JSON.stringify({ manifest_url: item.manifest_url, reviewed_capability_digest: preview.capability_digest }),
        timeoutMs: 60_000,
      })
      const data = await response.json().catch(() => ({}))
      if (!response.ok) {
        if (response.status === 409 && data.detail?.code === 'capability_changed') {
          const changed = data.detail
          if (changed.manifest?.id === item.id && changed.capability_contract && changed.capability_digest) {
            setReview({ id: item.id, phase: 'ready', preview: changed, notice: 'This app changed before installation. Nothing was installed. Review its current access below before trying again.' })
            return
          }
        }
        throw new Error(detailToMessage(data.detail, 'Could not install this app.'))
      }
      setInstalledNow(current => new Set(current).add(item.id))
      setReview({ id: item.id, phase: 'installed' })
      void appQueries.list.invalidate(queryClient)
    } catch (error) {
      setReview({ id: item.id, phase: 'error', error: error.message || 'Could not install this app.' })
    }
  }

  return <div className="wt__store">
    <p className="wt__lead">Find the tools you have and add the ones you want.</p>
    <p className="wt__body">Apps give you focused tools for writing, planning, building, and more. Open Apps to see everything installed, even if it is not pinned to your menu.</p>
    <h3 className="wt__section-heading">Included with Möbius</h3>
    <p className="wt__section-copy">Möbius includes these apps from the start.</p>
    <div className="wt__built-ins">
      {CORE_IDS.map(id => apps.find(app => app.slug === id)).filter(Boolean).map(app => <article className="wt__built-in-card" key={app.slug}>
        <AppIcon className="wt__built-in-icon" item={app} label={app.name} />
        <div><h3>{app.name}</h3><p>{guideDescription(app.description)}</p></div>
      </article>)}
    </div>
    <p className="wt__footnote">Memory and Reflection use a connected agent for scheduled work. You can browse Social right away, then join when you want to post.</p>
    <h3 className="wt__section-heading">Discover in the App Store</h3>
    <p className="wt__section-copy">Here are a few apps worth exploring. Some help with everyday tasks, while others help you build, share, or connect devices. Choose Install to see what an app can access before adding it.</p>
    {!catalog && !catalogError && <p className="wt__notice wt__store-loading" role="status">Loading App Store picks…</p>}
    {catalogError && <p className="wt__notice wt__store-loading" role="status">{catalogError}</p>}
    {catalog && picks.length === 0 && <p className="wt__notice wt__store-loading" role="status">These picks are not in the current App Store catalog. You can explore the full collection in App Store later.</p>}
    {catalog && <div className="wt__store-grid">
      {picks.map(item => {
        const installed = installedNow.has(item.id) || apps.some(app => app.slug === item.id || app.source_manifest?.id === item.id)
        const state = review?.id === item.id ? review : null
        const expanded = !!state && state.phase !== 'installed'
        const busy = review?.phase === 'checking' || review?.phase === 'installing'
        const actionLabel = installed ? 'Installed' : expanded ? 'Cancel' : 'Install'
        const rows = state?.preview ? accessRows(state.preview.capability_contract) : []
        return <article className={`wt__store-pick${expanded ? ' is-expanded' : ''}`} key={item.id}>
          <AppIcon className="wt__store-icon" item={{ slug: item.id, icon_url: icons[item.id] }} label={item.name} size={null} />
          <div><span className="wt__store-kind">{item.collection || 'App'}</span><h3>{item.name}</h3><p>{guideDescription(item.description)}</p></div>
          <button type="button" ref={state?.phase === 'installed' ? installedButton : expanded ? reviewButton : null} className={installed || expanded ? 'wt__installed' : 'wt__action'} aria-label={`${actionLabel} ${item.name}`} aria-disabled={installed} aria-expanded={installed ? undefined : expanded} aria-controls={expanded ? `wt-access-${item.id}` : undefined} tabIndex={installed && state?.phase !== 'installed' ? -1 : undefined} disabled={(busy && !expanded) || state?.phase === 'installing'} onClick={() => {
            if (installed) return
            if (expanded) { reviewRequest.current += 1; setReview(null) }
            else void reviewApp(item)
          }}>{actionLabel}</button>
          {expanded && <section className="wt__access" id={`wt-access-${item.id}`} aria-label={`${item.name} requested access`}>
            <div className="wt__access-heading"><strong>Privacy & access</strong><span>Before you install</span></div>
            {state.phase === 'checking' && <p role="status">Checking this app’s current access…</p>}
            {state.phase === 'error' && <><p className="wt__store-error" role="alert">{state.error}</p><button type="button" className="wt__access-retry" onClick={() => void reviewApp(item)}>Retry check</button></>}
            {(state.phase === 'ready' || state.phase === 'installing') && <>
              {state.phase === 'ready' && <span className="sr-only" role="status">Current access ready for {item.name}</span>}
              {state.notice && <p className="wt__store-error" role="alert">{state.notice}</p>}
              {rows.length === 0
                ? <p>No special permissions requested.</p>
                : <ul className="wt__access-list">{rows.map(row => <li key={row.key}><strong>{row.title}</strong><span>{row.detail}</span></li>)}</ul>}
              <button type="button" className="wt__access-install wt__action" disabled={state.phase === 'installing'} onClick={() => void install(item)}>{state.phase === 'installing' ? 'Installing…' : 'Install app'}</button>
              {state.phase === 'installing' && <span className="sr-only" role="status">Installing {item.name}</span>}
            </>}
          </section>}
          {state?.phase === 'installed' && <p className="wt__store-success" role="status">Installed. Find it in Apps after the guide.</p>}
        </article>
      })}
    </div>}
    <p className="wt__footnote">You do not need to install anything now. Find more apps in App Store whenever you are ready.</p>
  </div>
}
