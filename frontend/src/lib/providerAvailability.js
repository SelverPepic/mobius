/**
 * Canonical provider-availability policy for model pickers.
 *
 * Model discovery and provider connection are deliberately separate: the
 * registry can contain fallback models for providers that cannot run. Resolve
 * the status request into an explicit phase, then apply this fail-closed rule
 * everywhere a provider choice is exposed.
 */
export const PROVIDER_AVAILABILITY_PHASE = Object.freeze({
  LOADING: 'loading',
  READY: 'ready',
  ERROR: 'error',
})

export function providerIsConfigured(info) {
  return info?.available !== false && info?.configured === true
}

export function configuredProviderSet(statusByProvider) {
  return new Set(
    Object.entries(statusByProvider || {})
      .filter(([, info]) => providerIsConfigured(info))
      .map(([providerId]) => providerId),
  )
}

export function configuredProviderOrder(providerOrder, configuredProviders) {
  const configured = configuredProviders instanceof Set
    ? configuredProviders
    : new Set()
  return (Array.isArray(providerOrder) ? providerOrder : [])
    .filter(providerId => configured.has(providerId))
}

export function resolveProviderAvailability(statusQuery) {
  if (statusQuery?.data !== undefined) {
    return {
      phase: PROVIDER_AVAILABILITY_PHASE.READY,
      configuredProviders: configuredProviderSet(statusQuery.data),
    }
  }
  return {
    phase: statusQuery?.isError
      ? PROVIDER_AVAILABILITY_PHASE.ERROR
      : PROVIDER_AVAILABILITY_PHASE.LOADING,
    configuredProviders: new Set(),
  }
}

export function providerAvailabilityNeedsAttention(availability) {
  return availability.phase === PROVIDER_AVAILABILITY_PHASE.ERROR
    || (
      availability.phase === PROVIDER_AVAILABILITY_PHASE.READY
      && availability.configuredProviders.size === 0
    )
}

/**
 * A disconnected active provider keeps only its selected row for context. The
 * rest of that provider's registry is not actionable and must not look like a
 * list of available choices.
 */
export function visibleProviderModels(
  providerId,
  models,
  configuredProviders,
  retainedProvider = '',
  retainedModel = '',
) {
  const rows = Array.isArray(models) ? models : []
  if (configuredProviders.has(providerId)) return rows
  if (providerId !== retainedProvider) return []
  return rows.filter(model => model?.id === retainedModel)
}

/**
 * Derive the owner-facing Möbius trial label from its provider-status entry.
 *
 * The backend keeps a not-yet-usable trial CONFIGURED so its model row stays
 * visible and selectable, and (for a true owner caller) exposes the real
 * lifecycle in `trial_state` / `trial_usable` / `needs_activation`. Returns
 * null when the state is unknown (app/embed caller, signed out, or the account
 * service was unreachable) or the trial is usable — callers then keep their
 * normal "Trial active" presentation. Otherwise returns
 * `{ state, label, subtitle, needsActivation }` so the composer picker and
 * Settings can show an accurate label alongside an "Open Möbius · You"
 * activation affordance instead of a false "Trial active".
 */
export function mobiusTrialAttention(info) {
  const state = info?.trial_state
  if (!state || info?.trial_usable === true) return null
  if (state === 'ready') {
    return {
      state,
      label: 'Needs activation',
      subtitle: 'Activate your $2 trial in Möbius · You.',
      needsActivation: true,
    }
  }
  if (state === 'expired') {
    return {
      state,
      label: 'Trial expired',
      subtitle: 'Your Möbius trial has ended. See your options in Möbius · You.',
      needsActivation: false,
    }
  }
  if (state === 'ineligible') {
    return {
      state,
      label: 'Trial unavailable',
      subtitle: 'This account isn’t eligible for the trial. See Möbius · You.',
      needsActivation: false,
    }
  }
  if (state === 'active') {
    return {
      state,
      label: 'No credit',
      subtitle: 'No trial credit left. See your options in Möbius · You.',
      needsActivation: false,
    }
  }
  return null
}
