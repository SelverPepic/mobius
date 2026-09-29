/* Stationary first-run slideshow: learn and optionally set up Möbius without automatic workspace navigation. */
import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Download } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import { getInstallPromptSnapshot, requestInstall, subscribeInstallPrompt } from '../../lib/installPrompt.js'
import { prepareShellInstallPass } from '../../lib/shellInstallPass.js'
import { detectInstallPlatform, installCopyForPlatform } from '../../utils/installPlatform.js'
import { AgentSetup, HandleSetup } from './WalkthroughSetup.jsx'
import WalkthroughStore from './WalkthroughStore.jsx'
import './WalkthroughOverlay.css'

const SLIDES = ['welcome', 'connect', 'chat', 'apps', 'settings', 'identity']
const CHAPTERS = ['Welcome', 'Connect an agent', 'Chat & projects', 'Apps', 'Settings', 'Möbius · You']
const GUIDE_COUNT = SLIDES.length

export default function WalkthroughOverlay({ apps }) {
  const queryClient = useQueryClient()
  const dialogRef = useRef(null)
  const closingRef = useRef(false)
  const titleRef = useRef(null)
  const scrollRef = useRef(null)
  const installAbortRef = useRef(null)
  const [stepIndex, setStepIndex] = useState(0)
  const [platform] = useState(() => detectInstallPlatform())
  const [installCopy] = useState(() => installCopyForPlatform(platform))
  const [showInstallHelp, setShowInstallHelp] = useState(false)
  const [installBusy, setInstallBusy] = useState(false)
  const [installFeedback, setInstallFeedback] = useState('')
  const installState = useSyncExternalStore(subscribeInstallPrompt, getInstallPromptSnapshot, getInstallPromptSnapshot)
  const slide = SLIDES[stepIndex]

  function finish() {
    if (closingRef.current) return
    closingRef.current = true
    if (dialogRef.current?.open) dialogRef.current.close()
    queryClient.setQueryData(ownerQueries.walkthrough.key, previous => ({
      ...(previous || { completed_at: null }), completed: true,
    }))
    try { localStorage.setItem('mobius:walkthrough-completed', '1') } catch (_) {}
    api.owner.walkthrough.complete().catch(() => {})
  }

  function goTo(index) {
    setStepIndex(index)
    scrollRef.current?.scrollTo({ top: 0 })
  }

  function keepTabInside(event) {
    if (event.key !== 'Tab') return
    const controls = [...dialogRef.current.querySelectorAll('button, a[href], input, select, textarea, summary, [tabindex]')]
      .filter(node => node.tabIndex >= 0 && !node.disabled && !node.closest('[inert]') && node.getClientRects().length)
    const first = controls[0]
    const last = controls.at(-1)
    if ((event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
      event.preventDefault()
      const destination = event.shiftKey ? last : first
      destination?.focus()
    }
  }

  useLayoutEffect(() => {
    const dialog = dialogRef.current
    if (!dialog.open) dialog.showModal()
    titleRef.current?.focus({ preventScroll: true })
    return () => { if (dialog.open) dialog.close() }
  }, [])

  useEffect(() => { titleRef.current?.focus({ preventScroll: true }) }, [stepIndex])
  useEffect(() => () => installAbortRef.current?.abort(), [])

  async function handleInstall() {
    setInstallFeedback('')
    if (platform.ios) {
      const controller = new AbortController()
      installAbortRef.current = controller
      setInstallBusy(true)
      await prepareShellInstallPass({ force: true, signal: controller.signal })
      if (controller.signal.aborted) return
      installAbortRef.current = null
      setInstallBusy(false)
    }
    if (installState !== 'ready') {
      setShowInstallHelp(value => !value)
      return
    }
    setInstallBusy(true)
    const result = await requestInstall()
    setInstallBusy(false)
    if (result.outcome === 'accepted') {
      setInstallFeedback('Installed on this device. Your guide is still here.')
      return
    }
    if (result.outcome === 'fallback-ready') {
      setInstallFeedback('Tap Install again to use your browser’s regular prompt.')
      return
    }
    setShowInstallHelp(true)
    setInstallFeedback(result.outcome === 'dismissed'
      ? 'Not installed. You can do this from your browser menu later.'
      : 'The browser prompt was unavailable. Use the steps below instead.')
  }

  const installLabel = installBusy ? 'Opening…' : installState === 'ready' ? 'Install' : showInstallHelp ? 'Hide' : installCopy.ctaLabel

  return <dialog ref={dialogRef} className="wt__card" aria-modal="true" aria-labelledby="wt-title" onCancel={event => { event.preventDefault(); finish() }} onKeyDown={keepTabInside}>
      <div className="wt__topline">
        <div className="wt__brand"><span className="wt__mark" aria-hidden="true"><span /></span><span>Möbius / Getting started</span><span className="wt__count"><span aria-hidden="true">{String(stepIndex + 1).padStart(2, '0')} / {String(GUIDE_COUNT).padStart(2, '0')}</span><span className="sr-only">Step {stepIndex + 1} of {GUIDE_COUNT}</span></span></div>
        <button type="button" className="wt__close" onClick={finish} aria-label="Close guide" title="Close guide">×</button>
      </div>
      <div className="wt__layout">
        <nav className="wt__rail" aria-label="Guide sections">
          <div className="wt__rail-art" aria-hidden="true"><span className="wt__rail-orbit wt__rail-orbit--one" /><span className="wt__rail-orbit wt__rail-orbit--two" /><span className="wt__rail-core" /></div>
          <p className="wt__rail-title">Möbius, at a glance.</p>
          <p className="wt__rail-copy">Meet your agent, keep bigger work together, and find apps for what you want to do.</p>
          <ol className="wt__chapters">{CHAPTERS.map((chapter, index) => <li key={chapter}><button type="button" className={index === stepIndex ? 'is-current' : index < stepIndex ? 'is-past' : ''} aria-current={index === stepIndex ? 'step' : undefined} onClick={() => goTo(index)}><span>{String(index + 1).padStart(2, '0')}</span>{chapter}</button></li>)}</ol>
        </nav>
        <div className="wt__main">
          <div className="wt__slide" ref={scrollRef} role="region" aria-labelledby="wt-title" tabIndex={0}>
        {slide === 'welcome' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Welcome to Möbius</h2>
          <p className="wt__lead">Möbius is a place to think out loud and make things happen.</p>
          <p className="wt__body">Bring a question, a rough idea, or a bigger ambition. Your agent can help you find a way forward, while apps give you tools to write, plan, build, and share. Möbius keeps your conversations and work together.</p>
          <p className="wt__body wt__body--second">This guide shows you around. Along the way, you can connect an agent and choose a public handle.</p>
          {installState !== 'installed' && <section className="wt__install" aria-labelledby="wt-install-title">
            <span className="wt__install-icon" aria-hidden="true"><Download width={19} height={19} /></span>
            <div><h3 id="wt-install-title">Keep Möbius close</h3><p>{installState === 'ready' ? 'Install it on this device for a full-screen, one-tap launch.' : installCopy.summary}</p></div>
            <button type="button" className="wt__install-btn" onClick={handleInstall} disabled={installBusy} aria-expanded={installState === 'ready' ? undefined : showInstallHelp} aria-controls={installState === 'ready' ? undefined : 'wt-install-help'}>{installLabel}</button>
            {showInstallHelp && <div className="wt__install-help" id="wt-install-help"><strong>{installCopy.title}</strong><span>{installCopy.body}</span></div>}
            {installFeedback && <p className="wt__install-feedback" role="status">{installFeedback}</p>}
          </section>}
          {installState === 'installed' && installFeedback && <p className="wt__install-feedback" role="status">{installFeedback}</p>}
        </>}

        {slide === 'connect' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Connect an agent</h2>
          <p className="wt__lead">Connect an agent to power Chat.</p>
          <p className="wt__body">Choose OpenAI Codex or Claude Code below, then follow the sign-in steps. Once connected, you can ask questions, plan work, and create things together. You can change your provider or model in Settings.</p>
          <AgentSetup />
        </>}

        {slide === 'chat' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Chat and Projects</h2>
          <p className="wt__lead">Start in Chat. Give bigger work a home in Projects.</p>
          <p className="wt__body">Ask a question or describe what you want to make in everyday words. When the work has several parts, create a Project to keep its chats, files, and finished results together. Your agent can use a Goal to show the plan, track progress, and pause when it needs a decision from you.</p>
          <div className="wt__feature-grid">
            <article><span>Ask and create</span><h3>Chat</h3><p>Ask a question, sketch an idea, or keep refining something in one conversation.</p></article>
            <article><span>Build over time</span><h3>Projects</h3><p>Start from scratch or a template, then keep every part of the work together.</p></article>
            <article><span>Track longer work</span><h3>Goals</h3><p>Follow multi-step work and see when your agent needs your input.</p></article>
          </div>
        </>}

        {slide === 'apps' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Explore apps</h2>
          <WalkthroughStore apps={apps} />
        </>}

        {slide === 'settings' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Your settings</h2>
          <p className="wt__lead">Set up Möbius the way you like it.</p>
          <p className="wt__body">In Settings, manage AI providers and models, choose models for background work, change the theme, check for updates, and sign out. Use the Integrations app to connect outside services.</p>
          <div className="wt__feature-grid wt__feature-grid--four">
            <article><span>Connect</span><h3>AI providers</h3><p>Connect or reconnect an agent and choose the model for Chat.</p></article>
            <article><span>Keep going</span><h3>Background agents</h3><p>Pick models for scheduled work from apps such as Memory and Reflection.</p></article>
            <article><span>Make it yours</span><h3>Appearance</h3><p>Choose light or dark mode.</p></article>
            <article><span>Stay current</span><h3>Möbius</h3><p>Check for platform updates and manage this installation.</p></article>
          </div>
          <p className="wt__footnote">Find Settings in the Möbius menu whenever you need it.</p>
        </>}

        {slide === 'identity' && <>
          <h2 id="wt-title" ref={titleRef} tabIndex={-1}>Choose a handle</h2>
          <p className="wt__lead">Give your Möbius profile a name people can recognize.</p>
          <p className="wt__body">Your @handle is the name others see when you share a page or write an app review. It helps them recognize you without showing your email address. Choose one below. You can change it later in Möbius · You.</p>
          <HandleSetup />
        </>}
          </div>

          <div className="wt__footer">
            {stepIndex > 0 && <button type="button" className="wt__back" onClick={() => goTo(stepIndex - 1)}>Back</button>}
            <button type="button" className="wt__next" onClick={() => stepIndex === SLIDES.length - 1 ? finish() : goTo(stepIndex + 1)}>
              {slide === 'identity' ? 'Finish guide' : 'Next'}
            </button>
          </div>
        </div>
      </div>
  </dialog>
}
