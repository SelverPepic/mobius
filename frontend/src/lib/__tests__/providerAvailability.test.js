import test from 'node:test'
import assert from 'node:assert/strict'
import {
  PROVIDER_AVAILABILITY_PHASE,
  configuredProviderOrder,
  configuredProviderSet,
  mobiusTrialAttention,
  providerAvailabilityNeedsAttention,
  resolveProviderAvailability,
  visibleProviderModels,
} from '../providerAvailability.js'

test('availability has explicit loading, ready, and error phases', () => {
  assert.equal(
    resolveProviderAvailability({ data: undefined, isError: false }).phase,
    PROVIDER_AVAILABILITY_PHASE.LOADING,
  )
  assert.equal(
    resolveProviderAvailability({ data: {}, isError: false }).phase,
    PROVIDER_AVAILABILITY_PHASE.READY,
  )
  assert.equal(
    resolveProviderAvailability({ data: undefined, isError: true }).phase,
    PROVIDER_AVAILABILITY_PHASE.ERROR,
  )
})

test('provider-specific settings list only connected providers', () => {
  assert.deepEqual(
    configuredProviderOrder(['claude', 'codex'], new Set(['codex'])),
    ['codex'],
  )
  assert.deepEqual(
    configuredProviderOrder(['claude', 'codex'], new Set()),
    [],
  )
})

test('configured is the only provider availability authority', () => {
  const configured = configuredProviderSet({
    codex: { configured: true },
    claude: { configured: false },
    retiredAlias: { authenticated: true },
    unavailable: { configured: true, available: false },
    future: {},
  })

  assert.deepEqual([...configured], ['codex'])
})

test('an unavailable retained provider exposes only its selected model', () => {
  const models = [{ id: 'one' }, { id: 'two' }]
  const configured = new Set(['codex'])

  assert.deepEqual(visibleProviderModels('codex', models, configured), models)
  assert.deepEqual(
    visibleProviderModels('claude', models, configured, 'claude', 'two'),
    [{ id: 'two' }],
  )
  assert.deepEqual(visibleProviderModels('future', models, configured, 'claude', 'two'), [])
})

test('attention means status failure or no configured provider, not optional disconnects', () => {
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.ERROR,
    configuredProviders: new Set(),
  }), true)
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.READY,
    configuredProviders: new Set(),
  }), true)
  assert.equal(providerAvailabilityNeedsAttention({
    phase: PROVIDER_AVAILABILITY_PHASE.READY,
    configuredProviders: new Set(['codex']),
  }), false)
})

test('mobius trial attention stays silent when usable or state is unknown', () => {
  // No lifecycle field (app/embed caller, signed out, or unreachable account).
  assert.equal(mobiusTrialAttention(undefined), null)
  assert.equal(mobiusTrialAttention({}), null)
  // A usable trial keeps the normal "Trial active" presentation.
  assert.equal(
    mobiusTrialAttention({ trial_state: 'active', trial_usable: true }),
    null,
  )
})

test('mobius trial attention labels each unusable lifecycle state', () => {
  const ready = mobiusTrialAttention({
    trial_state: 'ready', trial_usable: false, needs_activation: true,
  })
  assert.equal(ready.label, 'Needs activation')
  assert.equal(ready.needsActivation, true)
  assert.match(ready.subtitle, /\$2 trial/)

  assert.equal(
    mobiusTrialAttention({ trial_state: 'expired', trial_usable: false }).label,
    'Trial expired',
  )
  assert.equal(
    mobiusTrialAttention({ trial_state: 'ineligible', trial_usable: false }).label,
    'Trial unavailable',
  )
  // active but out of credit is a distinct unusable case.
  assert.equal(
    mobiusTrialAttention({ trial_state: 'active', trial_usable: false }).label,
    'No credit',
  )
})
