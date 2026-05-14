from __future__ import annotations


def rrf_fuse(
    result_lists: list[list[tuple[str, float]]],
    k: int = 60,
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    if weights is None:
        if len(result_lists) == 3:
            weights = [1.0, 1.0, 0.5]
        else:
            weights = [1.0] * len(result_lists)

    scores: dict[str, float] = {}
    for list_i, (result_list, weight) in enumerate(zip(result_lists, weights)):
        for rank, (chunk_id, _) in enumerate(result_list, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weight / (k + rank)

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def entity_overlap_bonus(
    query_entities: list[str],
    chunk_entities_map: dict[str, list[str]],
    bonus_per_match: float = 0.1,
) -> dict[str, float]:
    query_lower = {e.lower() for e in query_entities}
    result: dict[str, float] = {}
    for chunk_id, chunk_entities in chunk_entities_map.items():
        count = sum(1 for e in chunk_entities if e.lower() in query_lower)
        if count > 0:
            result[chunk_id] = count * bonus_per_match
    return result
