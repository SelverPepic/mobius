"""Candidate-owned, offline startup smoke: ``python -m app.startup_selftest``.

Updaters may invoke this stable entry point across versions; internal calls
belong to this tree and can change together. Import the server without running
lifespan, then exercise local provider registry and settings resolution using
configured DATA_DIR / DATABASE_URL. This is not full startup or chat validation:
no credentials, authentication, model discovery, SDK turns or network calls.
Absent credentials or an unselected model are normal, not update failures.
"""


def main() -> None:
  import app.main  # noqa: F401
  from app.config import get_settings
  from app import providers

  data_dir = get_settings().data_dir
  providers.sync_app_model_providers(data_dir)
  for provider_id in tuple(providers.PROVIDERS):
    providers.get_provider(provider_id, data_dir=data_dir)
    providers.effective_agent_settings(data_dir, provider=provider_id)
  providers.background_agent_settings(data_dir)


if __name__ == "__main__":
  main()
