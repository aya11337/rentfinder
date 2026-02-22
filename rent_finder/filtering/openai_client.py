"""
OpenAI GPT-4o-mini filter for rent-finder.

Sends each EnrichedListing to GPT-4o-mini with the structured system prompt
and parses the JSON response into a validated FilterResult.

Error handling:
  AuthenticationError → raise OpenAIAuthError (aborts pipeline)
  RateLimitError      → retry up to 3 times honouring retry-after header
  APIConnectionError  → retry once after 5s
  APIStatusError 5xx  → retry once after 5s
  JSONDecodeError     → retry once with explicit JSON reminder; else REJECT
  ValidationError     → REJECT with rejection_reasons=["llm_response_invalid"]
"""

from __future__ import annotations

import json
import time
from typing import Literal

import openai
from pydantic import BaseModel, Field, field_validator

from rent_finder.filtering.prompt import build_messages
from rent_finder.ingestion.models import EnrichedListing
from rent_finder.utils.logging_config import get_logger

log = get_logger(__name__)

# Required keys in score_breakdown — must match the system prompt categories
_REQUIRED_BREAKDOWN_KEYS: frozenset[str] = frozenset({
    "neighbourhood",
    "laundry",
    "transit",
    "natural_light",
    "condition",
    "parking",
    "furnished",
    "move_in_timing",
})


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OpenAIAuthError(Exception):
    """Raised when OpenAI rejects the API key — aborts the entire pipeline."""


# ---------------------------------------------------------------------------
# FilterResult model
# ---------------------------------------------------------------------------

class FilterResult(BaseModel):
    """Validated response from GPT-4o-mini."""

    decision: Literal["PASS", "REJECT"]
    rejection_reasons: list[str] = Field(default_factory=list)
    scam_flag: bool = False
    total_score: int = Field(ge=0, le=24)
    score_breakdown: dict[str, int]
    reasoning: str = Field(min_length=1, max_length=800)

    @field_validator("score_breakdown")
    @classmethod
    def validate_breakdown(cls, v: dict[str, int]) -> dict[str, int]:
        missing = _REQUIRED_BREAKDOWN_KEYS - v.keys()
        if missing:
            raise ValueError(f"score_breakdown missing keys: {missing}")
        out_of_range = {k: s for k, s in v.items() if not (0 <= s <= 3)}
        if out_of_range:
            raise ValueError(f"score_breakdown values must be 0-3: {out_of_range}")
        return v

    @field_validator("total_score")
    @classmethod
    def validate_total_matches_breakdown(cls, v: int, info: object) -> int:
        data = getattr(info, "data", {})
        breakdown = data.get("score_breakdown", {})
        if breakdown:
            expected = sum(breakdown.values())
            if v != expected:
                raise ValueError(
                    f"total_score {v} does not match sum of score_breakdown {expected}"
                )
        return v


# ---------------------------------------------------------------------------
# Fallback helpers
# ---------------------------------------------------------------------------

def _reject_result(  # noqa: E501
    reasons: list[str], reasoning: str = "Filter evaluation failed."
) -> FilterResult:
    """Return a safe REJECT FilterResult for error cases."""
    breakdown = {k: 0 for k in _REQUIRED_BREAKDOWN_KEYS}
    return FilterResult(
        decision="REJECT",
        rejection_reasons=reasons,
        scam_flag=False,
        total_score=0,
        score_breakdown=breakdown,
        reasoning=reasoning,
    )


def _parse_response(content: str) -> FilterResult | None:
    """
    Attempt to parse and validate a GPT response string.

    Returns FilterResult on success, None on any parse or validation error.
    """
    try:
        data = json.loads(content)
        return FilterResult(**data)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.warning("llm_parse_error", error=str(exc), content_snippet=content[:200])
        return None


# ---------------------------------------------------------------------------
# Main filter function
# ---------------------------------------------------------------------------

def filter_listing(
    listing: EnrichedListing,
    *,
    api_key: str,
    model: str = "gpt-4o-mini",
    max_tokens: int = 600,
) -> FilterResult:
    """
    Send a listing to GPT-4o-mini and return a validated FilterResult.

    Args:
        listing: The enriched listing (with or without description).
        api_key: OpenAI API key.
        model: OpenAI model name.
        max_tokens: Max completion tokens.

    Returns:
        FilterResult — never raises on non-auth errors; falls back to REJECT.

    Raises:
        OpenAIAuthError: If OpenAI rejects the API key (pipeline must abort).
    """
    client = openai.OpenAI(api_key=api_key)
    messages = build_messages(listing)

    def _call(msgs: list[dict]) -> str:
        response = client.chat.completions.create(
            model=model,
            messages=msgs,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""

    # ── Attempt 1 ──────────────────────────────────────────────────────────
    try:
        content = _call(messages)
    except openai.AuthenticationError as exc:
        raise OpenAIAuthError(f"OpenAI authentication failed: {exc}") from exc

    except openai.RateLimitError as exc:
        retry_after = int(getattr(exc, "retry_after", None) or 5)
        log.warning("openai_rate_limit", retry_after=retry_after, listing_id=listing.listing_id)
        for attempt in range(1, 4):
            wait = retry_after * attempt
            log.warning("openai_rate_limit_retry", attempt=attempt, wait_s=wait)
            time.sleep(wait)
            try:
                content = _call(messages)
                break
            except openai.RateLimitError:
                if attempt == 3:
                    log.error("openai_rate_limit_exhausted", listing_id=listing.listing_id)
                    return _reject_result(["llm_rate_limit_exhausted"])
        else:
            return _reject_result(["llm_rate_limit_exhausted"])

    except (openai.APIConnectionError, openai.APIStatusError) as exc:
        log.warning("openai_api_error_retry", error=str(exc), listing_id=listing.listing_id)
        time.sleep(5)
        try:
            content = _call(messages)
        except Exception as exc2:
            log.error("openai_api_error_final", error=str(exc2), listing_id=listing.listing_id)
            return _reject_result(["llm_api_error"])

    except Exception as exc:
        log.error("openai_unexpected_error", error=str(exc), listing_id=listing.listing_id)
        return _reject_result(["llm_unexpected_error"])

    # ── Parse attempt 1 ────────────────────────────────────────────────────
    result = _parse_response(content)
    if result is not None:
        log.info(
            "filter_result",
            listing_id=listing.listing_id,
            decision=result.decision,
            score=result.total_score,
        )
        return result

    # ── Retry with explicit JSON reminder ──────────────────────────────────
    log.warning("llm_parse_retry", listing_id=listing.listing_id)
    retry_messages = messages + [
        {"role": "assistant", "content": content},
        {
            "role": "user",
            "content": (
                "Your previous response was not valid JSON. "
                "Please respond with ONLY the JSON object — no extra text."
            ),
        },
    ]
    try:
        content2 = _call(retry_messages)
        result2 = _parse_response(content2)
        if result2 is not None:
            return result2
    except openai.AuthenticationError as exc:
        raise OpenAIAuthError(f"OpenAI authentication failed: {exc}") from exc
    except Exception as exc:
        log.error("openai_retry_error", error=str(exc), listing_id=listing.listing_id)

    log.warning("llm_response_invalid", listing_id=listing.listing_id)
    return _reject_result(["llm_response_invalid"])
