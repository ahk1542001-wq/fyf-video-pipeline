"""Stage B exit-gate tests - B5 artifact storage seam (backend.storage.backend).

Covers atomic path-safe writes, get/exists/delete/uri_for, the closed key
pattern, and path-escape rejection (ownership/safety gate).
"""

from __future__ import annotations

import pytest

from backend.storage.backend import (
    ArtifactStorage,
    InvalidArtifactKeyError,
    LocalArtifactStorage,
)


@pytest.fixture()
def storage(tmp_path) -> LocalArtifactStorage:
    return LocalArtifactStorage(tmp_path / "artifacts")


def test_put_get_roundtrip(storage):
    uri = storage.put("assets/clip.mp4", b"\x00\x01bytes")
    assert storage.get("assets/clip.mp4") == b"\x00\x01bytes"
    assert uri == storage.uri_for("assets/clip.mp4")
    assert uri.startswith("file://")


def test_put_is_atomic_no_temp_leftover(storage):
    storage.put("a/b.txt", b"data")
    names = sorted(p.name for p in (storage.root / "a").iterdir())
    assert names == ["b.txt"], names


def test_put_overwrites_existing_key(storage):
    storage.put("k.bin", b"first")
    storage.put("k.bin", b"second")
    assert storage.get("k.bin") == b"second"


def test_exists_and_delete_are_idempotent(storage):
    assert storage.exists("gone.bin") is False
    storage.put("gone.bin", b"x")
    assert storage.exists("gone.bin") is True
    storage.delete("gone.bin")
    assert storage.exists("gone.bin") is False
    storage.delete("gone.bin")  # no error on a second delete


def test_get_missing_raises_file_not_found(storage):
    with pytest.raises(FileNotFoundError):
        storage.get("missing.bin")


def test_put_requires_bytes(storage):
    with pytest.raises(TypeError):
        storage.put("k.bin", "not-bytes")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad_key",
    [
        "",                     # empty
        "../escape",            # leading dot / traversal
        "..",                   # traversal
        "a/../b",               # embedded traversal segment
        "/abs/path",            # absolute
        ".hidden",              # leading dot
        "has space.png",        # whitespace
        "bad\x00key",           # control character
        "a" * 300,              # too long
    ],
)
def test_invalid_keys_are_rejected(storage, bad_key):
    with pytest.raises(InvalidArtifactKeyError):
        storage.put(bad_key, b"x")
    with pytest.raises(InvalidArtifactKeyError):
        storage.uri_for(bad_key)


def test_valid_nested_keys_are_accepted(storage):
    for key in ["a", "assets/img.png", "renders/2026/clip-v1.mp4", "A1/b-2_c.txt"]:
        storage.put(key, b"ok")
        assert storage.get(key) == b"ok"


def test_local_storage_satisfies_protocol(storage):
    assert isinstance(storage, ArtifactStorage)
