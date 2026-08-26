"""
local_provider.py
==================

`LocalProvider` - thin `OpenAICompatibleClient` subclass for any
locally-hosted, OpenAI-compatible inference server: LM Studio, Ollama
(its `/v1` OpenAI-compat endpoint, not its native API), vLLM,
OpenWebUI, text-generation-webui, etc. Base URL and model come from
`LOCAL_API_BASE`/`LOCAL_MODEL` (see `config.py`) - never hardcoded,
since which of these a given machine runs varies.

No API key required by default (`_requires_api_key()` -> `False`) -
most local servers run with no auth at all; if a specific setup DOES
require a bearer token, setting `LOCAL_API_KEY` still works (the header
is only omitted when `config.api_key` is empty, same as every other
`OpenAICompatibleClient`).
"""

from __future__ import annotations

from .base import OpenAICompatibleClient


class LocalProvider(OpenAICompatibleClient):
    name = "local"

    _SUPPORTS_STREAMING = True
    _SUPPORTS_TOOLS = False  # most local servers/models don't reliably support function calling
    _SUPPORTS_IMAGES = False
    _SUPPORTS_REASONING = False

    DEFAULT_BASE_URL = "http://localhost:1234/v1"  # LM Studio's own default

    #: no fixed catalog - whatever's loaded locally IS the model; cost
    #: is always $0 (no metered API), so no `_MODEL_CATALOG` needed -
    #: `get_model_info()`'s catalog lookup miss already returns
    #: `input_cost_per_1m=None` ("unknown/not applicable"), which is
    #: correct here too (local inference has real compute cost, just
    #: not a per-token BILLED one this package can estimate).
    _MODEL_CATALOG = {}

    def _requires_api_key(self) -> bool:
        return False

    def get_model_info(self, model=None):
        info = super().get_model_info(model)
        info.input_cost_per_1m = 0.0
        info.output_cost_per_1m = 0.0
        return info
