"""Tests for bounded, offloaded LLM image preparation."""

from __future__ import annotations

import asyncio
import base64
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gobby.llm.base import VisionInputError
from gobby.llm.image_payloads import (
    MAX_IMAGE_BYTES,
    MAX_REQUEST_IMAGE_BYTES,
    MAX_REQUEST_IMAGES,
    prepare_image_data,
    prepare_image_inputs,
)


async def test_oversized_image_is_rejected_before_open_or_encoding(tmp_path: Path) -> None:
    image_path = tmp_path / "large.png"
    image_path.write_bytes(b"x" * (MAX_IMAGE_BYTES + 1))

    with (
        patch.object(Path, "open") as open_mock,
        patch.object(base64, "standard_b64encode") as encode_mock,
        pytest.raises(VisionInputError, match="exceeds"),
    ):
        await prepare_image_data(str(image_path))

    open_mock.assert_not_called()
    encode_mock.assert_not_called()


async def test_growth_after_stat_is_caught_by_bounded_read(tmp_path: Path) -> None:
    image_path = tmp_path / "growing.png"
    image_path.write_bytes(b"x")

    class GrowingFile:
        requested_size = 0

        def __enter__(self) -> GrowingFile:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def fileno(self) -> int:
            return 0

        def read(self, size: int) -> bytes:
            self.requested_size = size
            return b"x" * size

    growing_file = GrowingFile()
    regular_small = SimpleNamespace(st_mode=stat.S_IFREG, st_size=1)
    with (
        patch.object(Path, "stat", return_value=regular_small),
        patch.object(Path, "open", return_value=growing_file),
        patch.object(os, "fstat", return_value=regular_small),
        patch.object(base64, "standard_b64encode") as encode_mock,
        pytest.raises(VisionInputError, match="grew beyond"),
    ):
        await prepare_image_data(str(image_path))

    assert growing_file.requested_size == MAX_IMAGE_BYTES + 1
    encode_mock.assert_not_called()


async def test_non_regular_input_is_rejected_before_read(tmp_path: Path) -> None:
    with (
        patch.object(Path, "open") as open_mock,
        pytest.raises(VisionInputError, match="not a regular file"),
    ):
        await prepare_image_data(str(tmp_path))
    open_mock.assert_not_called()


async def test_supported_media_type_and_encoding_are_preserved(tmp_path: Path) -> None:
    image_path = tmp_path / "image.gif"
    image_bytes = b"GIF89a"
    image_path.write_bytes(image_bytes)

    path, mime_type, encoded, data_url = await prepare_image_data(str(image_path))

    assert path == image_path
    assert mime_type == "image/gif"
    assert encoded == base64.standard_b64encode(image_bytes).decode("utf-8")
    assert data_url == f"data:image/gif;base64,{encoded}"


async def test_image_preparation_does_not_block_event_loop() -> None:
    started = threading.Event()
    release = threading.Event()

    def blocking_prepare(*args: object) -> tuple[Path, str, str, str]:
        started.set()
        assert release.wait(timeout=2)
        return Path("image.png"), "image/png", "eA==", "data:image/png;base64,eA=="

    with patch("gobby.llm.image_payloads._prepare_image_data_sync", blocking_prepare):
        task = asyncio.create_task(prepare_image_data("image.png"))
        assert await asyncio.to_thread(started.wait, 1)
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        try:
            await asyncio.wait_for(heartbeat.wait(), timeout=1)
        finally:
            release.set()
            await task


def _gif_data_url() -> str:
    encoded = base64.standard_b64encode(b"GIF89a").decode("utf-8")
    return f"data:image/gif;base64,{encoded}"


async def test_data_url_is_decoded() -> None:
    path, mime_type, encoded, data_url = await prepare_image_data(_gif_data_url())

    assert path is None
    assert mime_type == "image/gif"
    assert encoded == base64.standard_b64encode(b"GIF89a").decode("utf-8")
    assert data_url == _gif_data_url()


async def test_disallowed_mime_is_rejected(tmp_path: Path) -> None:
    bmp_path = tmp_path / "image.bmp"
    bmp_path.write_bytes(b"BM")
    with pytest.raises(VisionInputError, match=r"Disallowed image MIME type .*image/bmp"):
        await prepare_image_data(str(bmp_path))

    with pytest.raises(VisionInputError, match="Disallowed image MIME type"):
        await prepare_image_data("data:image/bmp;base64,Qk0=")


async def test_relative_path_is_rejected() -> None:
    with pytest.raises(VisionInputError, match="Image path must be absolute: relative.png"):
        await prepare_image_data("relative.png")


async def test_malformed_data_url_is_rejected() -> None:
    with pytest.raises(VisionInputError, match="Malformed data URL"):
        await prepare_image_data("data:image/png")


async def test_invalid_base64_data_url_is_rejected() -> None:
    with pytest.raises(VisionInputError, match="Invalid image base64"):
        await prepare_image_data("data:image/png;base64,!!!!")


async def test_image_count_limit_names_offending_input() -> None:
    images = [_gif_data_url() for _ in range(MAX_REQUEST_IMAGES)] + ["data:image/gif;base64,extra"]
    with pytest.raises(
        VisionInputError,
        match=r"Too many images \(max 8\): data:image/gif;base64,extra",
    ):
        await prepare_image_inputs(images)


async def test_aggregate_decoded_size_limit_names_offending_input(tmp_path: Path) -> None:
    paths = []
    for index in range(5):
        path = tmp_path / f"image-{index}.png"
        path.write_bytes(b"x" * MAX_IMAGE_BYTES)
        paths.append(str(path))

    with pytest.raises(
        VisionInputError,
        match=rf"Images exceed {MAX_REQUEST_IMAGE_BYTES} byte aggregate limit: {paths[-1]}",
    ):
        await prepare_image_inputs(paths)
