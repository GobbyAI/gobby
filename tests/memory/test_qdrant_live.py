"""Live Qdrant smoke against the isolated test stack (docker-compose.test.yml).

Skips only when GOBBY_TEST_QDRANT_URL is unset. The pre-push gate exports that
variable and fails on the skip reason, so a stopped stack cannot pass silently.
"""

from __future__ import annotations

import os
import uuid

import pytest
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams

pytestmark = pytest.mark.integration


def test_qdrant_test_stack_creates_and_deletes_a_collection() -> None:
    url = os.environ.get("GOBBY_TEST_QDRANT_URL")
    if not url:
        pytest.skip("GOBBY_TEST_QDRANT_URL is unset")

    collection = f"gobby_test_smoke_{uuid.uuid4().hex}"
    client = QdrantClient(url=url, timeout=10)
    try:
        assert client.get_collections() is not None
        client.create_collection(
            collection_name=collection,
            vectors_config=VectorParams(size=4, distance=Distance.COSINE),
        )
        assert client.collection_exists(collection)
    finally:
        if client.collection_exists(collection):
            client.delete_collection(collection)
        client.close()
