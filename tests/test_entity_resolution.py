import pytest
from unittest.mock import AsyncMock, MagicMock
from app.infrastructure.graph_repository import GraphRepository, levenshtein_distance, string_similarity


def test_levenshtein_distance() -> None:
    """Test Levenshtein distance edge cases and typical spelling variations."""
    assert levenshtein_distance("cat", "cat") == 0
    assert levenshtein_distance("cat", "bat") == 1
    assert levenshtein_distance("cotton aphid", "cotton aphids") == 1
    assert levenshtein_distance("silverleaf whitefly", "silver leaf whitefly") == 1
    assert levenshtein_distance("", "hello") == 5


def test_string_similarity() -> None:
    """Test normalized similarity score boundaries and precision."""
    assert string_similarity("cat", "cat") == 1.0
    assert string_similarity("cotton aphid", "cotton aphids") > 0.90
    assert string_similarity("Green mirid", "Green Vegetable Bug") < 0.50
    assert string_similarity("", "") == 0.0


@pytest.mark.anyio
async def test_resolve_entity_name_caching_and_matching() -> None:
    """Verify that caching works, and exact, alias, and fuzzy resolutions are correct."""
    # 1. Setup mock database query responses
    mock_db = MagicMock()
    mock_db.run_query = AsyncMock(return_value=[
        {"name": "Cotton aphid", "aliases": ["aphid", "aphids"]},
        {"name": "Silverleaf whitefly", "aliases": ["slw"]}
    ])

    repo = GraphRepository(mock_db)

    # 2. Assert direct alias lookup
    resolved_alias = await repo.resolve_entity_name("Pest", "slw")
    assert resolved_alias == "Silverleaf whitefly"

    # 3. Assert case-insensitive match
    resolved_case = await repo.resolve_entity_name("Pest", "cotton aphids")
    assert resolved_case == "Cotton aphid"

    # 4. Assert fuzzy string matching within threshold (similarity >= 0.85)
    resolved_fuzzy = await repo.resolve_entity_name("Pest", "Silverleaf Whitefl")
    assert resolved_fuzzy == "Silverleaf whitefly"

    # 5. Assert fallback to original name when below threshold
    resolved_new = await repo.resolve_entity_name("Pest", "Helicoverpa armigera")
    assert resolved_new == "Helicoverpa armigera"

    # 6. Verify cache was only fetched from the database once
    mock_db.run_query.assert_called_once()
