const PROJECT_ID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// A Project file is an authenticated resource, not a public URL. Recognize
// agent-written download links so chat can use the same authorized request as
// the Project file browser instead of navigating without its bearer session.
export function projectFileDownloadTarget(href, origin, base = '') {
  let url
  try { url = new URL(href, origin) } catch { return null }
  if (url.origin !== origin) return null
  const prefix = `${base}/api/projects/`
  if (!url.pathname.startsWith(prefix)) return null
  const route = url.pathname.slice(prefix.length)
  const parts = route.split('/')
  if (parts.length !== 2 || !PROJECT_ID_RE.test(parts[0]) || parts[1] !== 'file') return null
  const paths = url.searchParams.getAll('path')
  if (paths.length !== 1 || !paths[0] || url.searchParams.get('download') !== 'true') return null
  const filename = paths[0].split(/[\\/]/).pop()
  if (!filename) return null
  return {
    requestPath: `/projects/${parts[0]}/file?path=${encodeURIComponent(paths[0])}&download=true`,
    filename,
  }
}

export async function fetchProjectFileDownload(target, request) {
  const response = await request(target.requestPath)
  if (!response.ok) throw new Error(`Project file download failed: ${response.status}`)
  return response.blob()
}
