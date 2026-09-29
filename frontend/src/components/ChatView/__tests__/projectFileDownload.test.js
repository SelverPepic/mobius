import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  projectFileDownloadTarget,
  fetchProjectFileDownload,
} from '../markdown/projectFileDownload.js'

const ORIGIN = 'https://mobius.example'
const PROJECT = '11111111-1111-5111-8111-111111111111'
const ROUTE = `/api/projects/${PROJECT}/file?path=Report%20OCR.pdf&download=true`

test('Project download links become same-origin authorized requests, including v5 projects', () => {
  const expected = {
    requestPath: `/projects/${PROJECT}/file?path=Report%20OCR.pdf&download=true`,
    filename: 'Report OCR.pdf',
  }
  assert.deepEqual(projectFileDownloadTarget(ROUTE, ORIGIN), expected)
  assert.deepEqual(projectFileDownloadTarget(`${ORIGIN}${ROUTE}`, ORIGIN), expected)
  assert.deepEqual(projectFileDownloadTarget(
    `/proxy/8001${ROUTE}`, ORIGIN, '/proxy/8001',
  ), expected)
})

test('Project link recognition never sends local authorization to external or unrelated URLs', () => {
  for (const href of [
    `https://other.example${ROUTE}`,
    `//other.example${ROUTE}`,
    `/api/projects/${PROJECT}/files?path=Report%20OCR.pdf&download=true`,
    `/api/projects/${PROJECT}/file?path=Report%20OCR.pdf`,
    `/api/projects/${PROJECT}/file?path=&download=true`,
    `/api/projects/${PROJECT}/file?path=a.pdf&path=b.pdf&download=true`,
    '/api/projects/not-a-project/file?path=a.pdf&download=true',
    '/api/chats/11111111-1111-5111-8111-111111111111/generated-files/a.pdf',
  ]) {
    assert.equal(projectFileDownloadTarget(href, ORIGIN), null, href)
  }
})

test('Project download uses the authorized request response rather than the raw link', async () => {
  const target = projectFileDownloadTarget(ROUTE, ORIGIN)
  const file = new Blob(['pdf bytes'], { type: 'application/pdf' })
  const requested = []
  const result = await fetchProjectFileDownload(target, async path => {
    requested.push(path)
    return { ok: true, blob: async () => file }
  })
  assert.deepEqual(requested, [target.requestPath])
  assert.equal(result, file)
})

test('denied Project download does not turn an error response into a file', async () => {
  const target = projectFileDownloadTarget(ROUTE, ORIGIN)
  let readBody = false
  await assert.rejects(
    fetchProjectFileDownload(target, async () => ({
      ok: false,
      status: 403,
      blob: async () => { readBody = true },
    })),
    /403/,
  )
  assert.equal(readBody, false)
})
