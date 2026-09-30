/* Summarize the platform's live install contract without app-specific permission copy. */
const DATA_GRANTS = {
  filesystem_api: ['Owner files', 'Can use the guarded owner-filesystem API.'],
  github_access: ['GitHub data', 'Can use your connected GitHub account.'],
  github_connect: ['GitHub connection', 'Can manage your GitHub connection.'],
  manage_apps: ['Installed apps', 'Can install and uninstall apps.'],
  manage_skills: ['Agent skills', 'Can install and remove agent skills.'],
  connections_manage: ['Integrations', 'Can manage connected services.'],
  connect_manage: ['Connected computers', 'Can manage computers connected to Möbius.'],
  identity_manage: ['Möbius profile', 'Can manage your Möbius identity.'],
  railway_manage: ['Railway deployment', 'Can manage your Railway deployment.'],
}

function readableKey(key) {
  return key.replaceAll('_', ' ').replace(/^./, letter => letter.toUpperCase())
}

function accessLevel(level, subject) {
  return level === 'write'
    ? `Can read and change ${subject}.`
    : `Can read ${subject}, but cannot change it.`
}

export function accessRows(contract) {
  if (!contract || typeof contract !== 'object') return []
  const agent = contract.agent || {}
  const data = contract.data || {}
  const rows = []
  const add = (key, title, detail, tag = 'Access') => rows.push({ key, title, detail, tag })

  if (agent.system_prompt) add('prompt', 'Agent chats', 'Adds app instructions to new agent chats while the app is installed.', 'All chats')
  if (agent.embeds_agent) add('embedded-agent', 'Agent in this app', 'Can start agent chats inside the app.', 'Agent')
  if (agent.skills?.length) add('skills', 'Agent skills', `Adds ${agent.skills.join(', ')} to the agent’s available guides.`, String(agent.skills.length))
  for (const [index, tool] of (agent.tools || []).entries()) {
    add(`tool-${index}`, tool.title || tool.name || 'Agent tool', tool.description || 'Gives the agent an app-provided tool.', 'Agent')
  }

  const logs = data.chat_logs || {}
  if (logs.effective === 'summary' || logs.effective === 'summary_with_deleted') {
    add('chat-logs', 'Chat history', logs.effective === 'summary_with_deleted'
      ? 'Can read redacted chat text, including recoverable deleted chats. Hidden reasoning, tool calls, and detected secrets are removed.'
      : 'Can read redacted chat text. Hidden reasoning, tool calls, and detected secrets are removed.', 'Redacted')
  }
  for (const [key, subject] of [
    ['shared_memory', 'shared memory'],
    ['cross_app_access', 'other apps’ private data'],
  ]) {
    if (data[key] === 'read' || data[key] === 'write') {
      add(key, key === 'shared_memory' ? 'Shared memory' : 'Other apps’ data', accessLevel(data[key], subject), data[key] === 'write' ? 'Read + write' : 'Read')
    }
  }
  if (data.share_with_apps === 'read' || data.share_with_apps === 'write') {
    add('share_with_apps', 'Shares its data', data.share_with_apps === 'write'
      ? 'Other authorized apps can read and change this app’s data.'
      : 'Other authorized apps can read this app’s data.', data.share_with_apps === 'write' ? 'Read + write' : 'Read')
  }
  const described = new Set(['chat_logs', 'shared_memory', 'cross_app_access', 'share_with_apps'])
  for (const [key, value] of Object.entries(data).sort()) {
    if (described.has(key) || value === false || value === null || value === undefined || value === 'none') continue
    const known = DATA_GRANTS[key]
    add(key, known?.[0] || readableKey(key), known?.[1] || `Requests the ${readableKey(key)} data grant. See App Store for more detail.`, known ? 'Access' : 'Review')
  }

  if (contract.background) add('background', 'Background work', contract.background.mode === 'scheduled'
    ? 'Can run app code on a schedule, even when the app is closed.'
    : 'Can run app code on demand, even when the app is closed.', 'Server job')
  for (const [key, capability] of Object.entries(contract.runtime || {}).sort()) {
    add(`runtime-${key}`, capability.title || readableKey(key), capability.description || 'Can use this device capability.', `v${capability.version || '?'}`)
  }
  const publicAccess = contract.public || {}
  if (publicAccess.network?.length) add('public-network', 'Public network access', `Public app sessions can fetch from ${[...new Set(publicAccess.network.map(rule => rule.origin))].join(', ')}.`, 'Public')
  if (publicAccess.storage?.read || publicAccess.storage?.write_prefix) add('public-storage', 'Public app data', publicAccess.storage.write_prefix
    ? `Visitors can submit files under ${publicAccess.storage.write_prefix}${publicAccess.storage.read ? ' and read the app’s public data' : ''}.`
    : 'Visitors can read the app’s public data.', 'Public')
  if (contract.service) add('service', 'App service', 'Runs an app service for the access scope shown in App Store.', 'Service')
  if (contract.model_provider) add('model-provider', 'AI provider', 'Adds an AI model provider to Möbius.', 'Provider')
  const knownSections = new Set(['schema', 'agent', 'data', 'background', 'offline', 'runtime', 'public', 'service', 'model_provider'])
  for (const [key, value] of Object.entries(contract)) {
    if (!knownSections.has(key) && value !== null && value !== false) {
      add(`other-${key}`, readableKey(key), `The platform reports an additional ${readableKey(key)} declaration. Review its details in App Store.`, 'Review')
    }
  }
  return rows
}
