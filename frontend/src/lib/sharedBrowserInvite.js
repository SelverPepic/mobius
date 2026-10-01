/* Consume a shared-browser invitation from a fragment without persisting it. */
export function consumeSharedBrowserInvite(location, history) {
  const fragment = location.hash?.replace(/^#/, '') || ''
  const invite = new URLSearchParams(fragment).get('invite') || ''
  if (fragment) history.replaceState(history.state, '', location.pathname + location.search)
  return invite
}

export function watchSharedBrowserInvites(win, onInvite) {
  const admit = () => {
    const invite = consumeSharedBrowserInvite(win.location, win.history)
    if (invite) onInvite(invite)
  }
  win.addEventListener('hashchange', admit)
  return () => win.removeEventListener('hashchange', admit)
}
