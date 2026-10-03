"""Central configuration and Azure client factories.

AUTH_MODE=entra uses DefaultAzureCredential (managed identity / az login) — the production pattern.
AUTH_MODE=key uses API keys — acceptable only in a sandbox.
"""
import os
from functools import lru_cache

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # dotenv is optional for offline tests
    pass


def env(name: str, default: str | None = None, required: bool = False) -> str | None:
    value = os.getenv(name, default)
    if value is not None and value.strip() == "":
        value = default
    if required and not value:
        raise RuntimeError(f"Missing required setting {name}. Copy .env.example to .env and fill it in.")
    return value


AUTH_MODE = (env("AUTH_MODE", "entra") or "entra").lower()
SEARCH_INDEX = env("SEARCH_INDEX", "example-policies-index")
SEMANTIC_RANKER = (env("SEMANTIC_RANKER", "false") or "false").lower() == "true"
MIN_RERANKER_SCORE = float(env("MIN_RERANKER_SCORE", "1.0"))
TOP_K = int(env("TOP_K", "5"))
EMBED_DIM = int(env("EMBED_DIM", "1536"))
APPROVAL_API_URL = env("APPROVAL_API_URL", "http://127.0.0.1:8000")
APPROVED_EMAIL_DOMAINS = [d.strip().lower() for d in (env("APPROVED_EMAIL_DOMAINS", "authority.gov.ae") or "").split(",") if d.strip()]


@lru_cache
def credential():
    from azure.identity import DefaultAzureCredential
    return DefaultAzureCredential()


@lru_cache
def openai_client():
    from openai import AzureOpenAI
    endpoint = env("AZURE_OPENAI_ENDPOINT", required=True)
    version = env("AZURE_OPENAI_API_VERSION", "2024-10-21")
    if AUTH_MODE == "key":
        return AzureOpenAI(azure_endpoint=endpoint, api_key=env("AZURE_OPENAI_API_KEY", required=True), api_version=version)
    from azure.identity import get_bearer_token_provider
    token_provider = get_bearer_token_provider(credential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=token_provider, api_version=version)


def _search_credential():
    if AUTH_MODE == "key":
        from azure.core.credentials import AzureKeyCredential
        return AzureKeyCredential(env("SEARCH_API_KEY", required=True))
    return credential()


@lru_cache
def search_client():
    from azure.search.documents import SearchClient
    return SearchClient(env("SEARCH_ENDPOINT", required=True), SEARCH_INDEX, _search_credential())


@lru_cache
def search_index_client():
    from azure.search.documents.indexes import SearchIndexClient
    return SearchIndexClient(env("SEARCH_ENDPOINT", required=True), _search_credential())


def chat_deployment() -> str:
    return env("CHAT_DEPLOYMENT", required=True)


def embed_deployment() -> str:
    return env("EMBED_DEPLOYMENT", required=True)


def chat_temperature():
    t = env("CHAT_TEMPERATURE", "")
    return float(t) if t not in (None, "") else None
