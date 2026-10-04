"""One set of rules for what kind of failure a provider reported.

Live turns (``chat``) and compaction turns (``compaction``) both need to know
whether a provider refused because the request was too large, the account is
out of credits, a usage or rate limit was hit, or sign-in failed. Each caller
owns its own wording and recovery; this module only owns the classification,
so the callers cannot disagree about what an error means.

Structured fields win over text: an HTTP status or a provider's own error type
is the provider's statement, while text matching is the fallback for errors
that arrive only as a message.
"""

import re
from enum import Enum


class ProviderErrorKind(str, Enum):
  TOO_LARGE = "too_large"
  CREDITS = "credits"
  USAGE_LIMIT = "usage_limit"
  AUTH = "auth"
  OTHER = "other"


_STATUS_KINDS = {
  413: ProviderErrorKind.TOO_LARGE,
  429: ProviderErrorKind.USAGE_LIMIT,
  401: ProviderErrorKind.AUTH,
}

# Claude's ``AssistantMessage.error`` values that name a kind on their own.
_ERROR_TYPE_KINDS = {
  "billing_error": ProviderErrorKind.CREDITS,
  "rate_limit": ProviderErrorKind.USAGE_LIMIT,
  "authentication_failed": ProviderErrorKind.AUTH,
}

# Checked in order: a size or credit refusal can mention a limit or quota, and
# must not be mistaken for a limit that will reset by itself.
_TEXT_RULES = (
  (ProviderErrorKind.TOO_LARGE, re.compile(
    r"request body (?:is )?too large|request_body_too_large|"
    r"request entity too large|payload too large|unexpected status 413\b|"
    r"context_length_exceeded",
    re.IGNORECASE,
  )),
  # ``insufficient_quota`` is OpenAI's out-of-credits code; "not enough
  # credits" covers the Möbius subscription's maximum-request-cost refusal.
  (ProviderErrorKind.CREDITS, re.compile(
    r"out of credits|insufficient[_ ]credits|insufficient_quota|"
    r"billing_error|credit balance is too low|not enough credits",
    re.IGNORECASE,
  )),
  # Grounded in Anthropic limit strings seen in production: "You've hit your
  # weekly limit · resets ...", "... session limit ...", and "Server is
  # temporarily limiting requests ... Rate limited". "limit" with "resets"
  # catches the whole "hit your <period> limit · resets <time>" family without
  # matching an error that merely says "limit".
  (ProviderErrorKind.USAGE_LIMIT, re.compile(
    r"rate[_ ]limit|usage[_ ]limit|weekly limit|session limit|overloaded|"
    r"quota|too many requests|\b429\b|limit[\s\S]*resets|resets[\s\S]*limit",
    re.IGNORECASE,
  )),
  (ProviderErrorKind.AUTH, re.compile(
    r"authentication_failed|authentication (?:failed|error)|"
    r"invalid authentication credentials|invalid_api_key|unauthorized|"
    r"login required|not logged in",
    re.IGNORECASE,
  )),
)


def classify_provider_error(
  text: str | None = None,
  *,
  status: int | None = None,
  error_type: str | None = None,
) -> ProviderErrorKind:
  """Return the kind of a provider failure, structured fields first."""
  if status in _STATUS_KINDS:
    return _STATUS_KINDS[status]
  if error_type in _ERROR_TYPE_KINDS:
    return _ERROR_TYPE_KINDS[error_type]
  if not text:
    return ProviderErrorKind.OTHER
  for kind, pattern in _TEXT_RULES:
    if pattern.search(text):
      return kind
  return ProviderErrorKind.OTHER
