"""PostgreSQL full-text expressions and queries for event discovery."""

from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import F


def event_search_vector():
    """Build the same weighted document for the GIN index and search queries."""
    return (
        SearchVector("title", config="english", weight="A")
        + SearchVector(
            "categories", "park_names", "location", config="english", weight="B"
        )
        + SearchVector("description", config="english", weight="C")
    )


def search_events(events, query):
    """Match an event queryset against user text, ordered by relevance."""
    search_query = SearchQuery(query, config="english", search_type="websearch")
    vector = event_search_vector()
    return (
        events.annotate(search_document=vector)
        .filter(search_document=search_query)
        .annotate(rank=SearchRank(vector, search_query))
        .order_by("-rank", F("start_time").asc(nulls_last=True), "pk")
    )
