"""
Unit tests for rent_finder.filtering.openai_client and prompt.

All OpenAI API calls are mocked — zero live API credits consumed.
Tests verify: JSON parsing, Pydantic validation, retry logic, and error fallbacks.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import openai
import pytest
from pydantic import ValidationError

from rent_finder.filtering.openai_client import (
    FilterResult,
    OpenAIAuthError,
    _parse_response,
    _reject_result,
    filter_listing,
)
from rent_finder.filtering.prompt import build_messages, build_user_message
from rent_finder.ingestion.models import EnrichedListing

API_KEY = "sk-test-fake-key-for-unit-tests"
MODEL = "gpt-4o-mini"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_BREAKDOWN = {
    "neighbourhood": 3,
    "laundry": 3,
    "transit": 2,
    "natural_light": 2,
    "condition": 2,
    "parking": 3,
    "furnished": 2,
    "move_in_timing": 3,
}

_VALID_PASS_RESPONSE = {
    "decision": "PASS",
    "rejection_reasons": [],
    "scam_flag": False,
    "total_score": sum(_VALID_BREAKDOWN.values()),
    "score_breakdown": _VALID_BREAKDOWN,
    "reasoning": "Well-located unit in North York with parking and furnished.",
}

_VALID_REJECT_RESPONSE = {
    "decision": "REJECT",
    "rejection_reasons": ["no_parking_confirmed"],
    "scam_flag": False,
    "total_score": 5,
    "score_breakdown": {
        "neighbourhood": 1, "laundry": 1, "transit": 1, "natural_light": 1,
        "condition": 1, "parking": 0, "furnished": 0, "move_in_timing": 0,
    },
    "reasoning": "No parking available and price is borderline.",
}


def _enriched(
    listing_id: str = "TEST001",
    description: str | None = "Bright 1BR in North York, furnished, parking included.",
    price_raw: str | None = "CA$1,400",
    price_cents: int | None = 140000,
    location_raw: str | None = "North York, Ontario",
) -> EnrichedListing:
    return EnrichedListing(
        listing_id=listing_id,
        url=f"https://www.facebook.com/marketplace/item/{listing_id}/",
        title="Furnished 1BR North York",
        price_raw=price_raw,
        price_cents=price_cents,
        location_raw=location_raw,
        bedrooms="1",
        bathrooms="1",
        image_url=None,
        scraped_at=None,
        extra_fields={},
        description=description,
        description_source="primary",
    )


def _mock_completion(content: str) -> MagicMock:
    """Build a mock openai.ChatCompletion response."""
    choice = MagicMock()
    choice.message.content = content
    response = MagicMock()
    response.choices = [choice]
    return response


# ---------------------------------------------------------------------------
# FilterResult model validation
# ---------------------------------------------------------------------------

class TestFilterResultModel:
    def test_valid_pass_parsed(self) -> None:
        result = FilterResult(**_VALID_PASS_RESPONSE)
        assert result.decision == "PASS"
        assert result.total_score == 20
        assert result.score_breakdown["parking"] == 3

    def test_valid_reject_parsed(self) -> None:
        result = FilterResult(**_VALID_REJECT_RESPONSE)
        assert result.decision == "REJECT"
        assert "no_parking_confirmed" in result.rejection_reasons

    def test_invalid_decision_raises(self) -> None:
        data = {**_VALID_PASS_RESPONSE, "decision": "MAYBE"}
        with pytest.raises(ValidationError):
            FilterResult(**data)

    def test_total_score_out_of_range_raises(self) -> None:
        data = {**_VALID_PASS_RESPONSE, "total_score": 25}
        with pytest.raises(ValidationError):
            FilterResult(**data)

    def test_missing_breakdown_key_raises(self) -> None:
        bad_breakdown = {k: v for k, v in _VALID_BREAKDOWN.items() if k != "parking"}
        data = {**_VALID_PASS_RESPONSE, "score_breakdown": bad_breakdown}
        with pytest.raises(Exception, match="parking"):
            FilterResult(**data)

    def test_breakdown_value_out_of_range_raises(self) -> None:
        bad_breakdown = {**_VALID_BREAKDOWN, "parking": 5}
        data = {**_VALID_PASS_RESPONSE, "score_breakdown": bad_breakdown, "total_score": 22}
        with pytest.raises(ValidationError):
            FilterResult(**data)

    def test_total_score_mismatch_raises(self) -> None:
        data = {**_VALID_PASS_RESPONSE, "total_score": 99}
        with pytest.raises(ValidationError):
            FilterResult(**data)

    def test_empty_reasoning_raises(self) -> None:
        data = {**_VALID_PASS_RESPONSE, "reasoning": ""}
        with pytest.raises(ValidationError):
            FilterResult(**data)


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------

class TestParseResponse:
    def test_valid_json_parsed(self) -> None:
        result = _parse_response(json.dumps(_VALID_PASS_RESPONSE))
        assert result is not None
        assert result.decision == "PASS"

    def test_invalid_json_returns_none(self) -> None:
        assert _parse_response("not json at all") is None

    def test_missing_field_returns_none(self) -> None:
        data = {k: v for k, v in _VALID_PASS_RESPONSE.items() if k != "decision"}
        assert _parse_response(json.dumps(data)) is None

    def test_empty_string_returns_none(self) -> None:
        assert _parse_response("") is None


# ---------------------------------------------------------------------------
# _reject_result
# ---------------------------------------------------------------------------

class TestRejectResult:
    def test_returns_reject_decision(self) -> None:
        r = _reject_result(["llm_api_error"])
        assert r.decision == "REJECT"
        assert "llm_api_error" in r.rejection_reasons

    def test_all_breakdown_zeros(self) -> None:
        r = _reject_result(["test"])
        assert r.total_score == 0
        assert all(v == 0 for v in r.score_breakdown.values())

    def test_all_eight_breakdown_keys_present(self) -> None:
        r = _reject_result([])
        assert len(r.score_breakdown) == 8


# ---------------------------------------------------------------------------
# build_user_message / build_messages
# ---------------------------------------------------------------------------

class TestPromptBuilder:
    def test_user_message_contains_listing_id(self) -> None:
        msg = build_user_message(_enriched("ID123"))
        assert "ID123" in msg

    def test_user_message_contains_title(self) -> None:
        msg = build_user_message(_enriched())
        assert "Furnished 1BR North York" in msg

    def test_user_message_contains_price(self) -> None:
        msg = build_user_message(_enriched())
        assert "CA$1,400" in msg

    def test_user_message_contains_description(self) -> None:
        msg = build_user_message(_enriched(description="Walkout basement, parking included."))
        assert "Walkout basement" in msg

    def test_user_message_no_description_placeholder(self) -> None:
        msg = build_user_message(_enriched(description=None))
        assert "No description available" in msg

    def test_build_messages_returns_two_roles(self) -> None:
        msgs = build_messages(_enriched())
        assert len(msgs) == 2
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_system_prompt_contains_price_cap(self) -> None:
        msgs = build_messages(_enriched())
        assert "1,600" in msgs[0]["content"]

    def test_system_prompt_contains_north_york(self) -> None:
        msgs = build_messages(_enriched())
        assert "North York" in msgs[0]["content"]

    def test_system_prompt_contains_parking_requirement(self) -> None:
        msgs = build_messages(_enriched())
        assert "parking" in msgs[0]["content"].lower()


# ---------------------------------------------------------------------------
# filter_listing — happy path
# ---------------------------------------------------------------------------

class TestFilterListingSuccess:
    def test_pass_response_returned(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = _mock_completion(
                json.dumps(_VALID_PASS_RESPONSE)
            )
            result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "PASS"
        assert result.total_score == 20
        assert mock_client.chat.completions.create.call_count == 1

    def test_reject_response_returned(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = _mock_completion(
                json.dumps(_VALID_REJECT_RESPONSE)
            )
            result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "REJECT"
        assert "no_parking_confirmed" in result.rejection_reasons


# ---------------------------------------------------------------------------
# filter_listing — error handling
# ---------------------------------------------------------------------------

class TestFilterListingErrors:
    def test_auth_error_raises_openai_auth_error(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
                "invalid api key", response=MagicMock(), body={}
            )
            with pytest.raises(OpenAIAuthError):
                filter_listing(_enriched(), api_key="bad-key", model=MODEL)

    def test_connection_error_retried_once(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            with patch("rent_finder.filtering.openai_client.time.sleep"):
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                # Fail first call, succeed second
                mock_client.chat.completions.create.side_effect = [
                    openai.APIConnectionError(request=MagicMock()),
                    _mock_completion(json.dumps(_VALID_PASS_RESPONSE)),
                ]
                result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "PASS"
        assert mock_client.chat.completions.create.call_count == 2

    def test_connection_error_both_attempts_returns_reject(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            with patch("rent_finder.filtering.openai_client.time.sleep"):
                mock_client = MagicMock()
                mock_cls.return_value = mock_client
                mock_client.chat.completions.create.side_effect = openai.APIConnectionError(
                    request=MagicMock()
                )
                result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "REJECT"
        assert any("api_error" in r for r in result.rejection_reasons)

    def test_invalid_json_retried_once(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            # First call: bad JSON; second (retry): valid JSON
            mock_client.chat.completions.create.side_effect = [
                _mock_completion("this is not json"),
                _mock_completion(json.dumps(_VALID_PASS_RESPONSE)),
            ]
            result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "PASS"
        assert mock_client.chat.completions.create.call_count == 2

    def test_invalid_json_both_retries_returns_reject(self) -> None:
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = _mock_completion(
                "still not json"
            )
            result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "REJECT"
        assert "llm_response_invalid" in result.rejection_reasons

    def test_validation_error_returns_reject(self) -> None:
        bad_response = {**_VALID_PASS_RESPONSE, "decision": "INVALID_ENUM"}
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = _mock_completion(
                json.dumps(bad_response)
            )
            result = filter_listing(_enriched(), api_key=API_KEY, model=MODEL)

        assert result.decision == "REJECT"

    def test_no_description_listing_still_evaluated(self) -> None:
        listing = _enriched(description=None)
        with patch("rent_finder.filtering.openai_client.openai.OpenAI") as mock_cls:
            mock_client = MagicMock()
            mock_cls.return_value = mock_client
            mock_client.chat.completions.create.return_value = _mock_completion(
                json.dumps(_VALID_REJECT_RESPONSE)
            )
            result = filter_listing(listing, api_key=API_KEY, model=MODEL)

        # API should still have been called
        assert mock_client.chat.completions.create.call_count >= 1
        assert result.decision == "REJECT"
