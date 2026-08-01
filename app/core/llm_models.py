"""Shared LLM model fallback order used by non-ADK generation paths."""

MODELS_TO_TRY = [
    # "gemini-3.5-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-2.5-flash",
    "gemini-3.1-pro-preview",
    "gemini-flash-latest",
]
