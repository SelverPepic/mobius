import { shortcutMatches } from './keyboardShortcuts.js'

/*
 * Shell shortcuts for a document nested inside an app frame, such as the
 * embedded agent chat. The browser delivers a key only to the focused
 * document, so this document captures the chords its parent advertises
 * (`moebius:frame-shortcuts`) and forwards the named action
 * (`moebius:shell-shortcut`). The app frame relays it to the shell, which
 * accepts only actions it advertised. An app that opted out advertises none.
 */
export function installFrameShellShortcuts(win = window) {
  const parent = win.parent
  if (!parent || parent === win) return () => {}
  let shortcuts = []

  const onMessage = (event) => {
    if (event.source !== parent || event.data?.type !== 'moebius:frame-shortcuts') return
    shortcuts = Array.isArray(event.data.shortcuts) ? event.data.shortcuts : []
  }
  const onKeyDown = (event) => {
    const shortcut = shortcuts.find(item => shortcutMatches(event, item?.binding))
    if (!shortcut) return
    event.preventDefault()
    event.stopImmediatePropagation()
    parent.postMessage({ type: 'moebius:shell-shortcut', actionId: shortcut.actionId }, '*')
  }

  win.addEventListener('message', onMessage)
  win.document.addEventListener('keydown', onKeyDown, true)
  parent.postMessage({ type: 'moebius:frame-shortcuts-request' }, '*')
  return () => {
    win.removeEventListener('message', onMessage)
    win.document.removeEventListener('keydown', onKeyDown, true)
  }
}
