import base64
import json
import os
import time
import uuid

import pytest

from app.browser_login import _account_email, _remove_stale_singletons, new_project_id


def test_session_identity_is_read_from_krea_cookie():
    encoded = base64.urlsafe_b64encode(json.dumps({
        "user": {"email": "Imported@Example.com"}
    }).encode()).decode().rstrip("=")
    assert _account_email([{"name": "sb-superb-auth-token",
                            "value": "base64-" + encoded}]) == "imported@example.com"
    assert _account_email([{"name": "other", "value": encoded}]) == ""


def test_new_video_project_matches_krea_uuid_v7_format():
    first, second = uuid.UUID(new_project_id()), uuid.UUID(new_project_id())
    assert first.version == second.version == 7
    assert first != second
    assert abs((first.int >> 80) - int(time.time() * 1000)) < 1000


@pytest.mark.skipif(os.name == "nt", reason="Windows test runners cannot always create symlinks")
def test_stale_chromium_profile_links_are_removed_without_touching_live_links(tmp_path):
    lock = tmp_path / "SingletonLock"
    socket = tmp_path / "SingletonSocket"
    cookie = tmp_path / "SingletonCookie"
    lock.symlink_to("old-host-123")
    socket.symlink_to(tmp_path / "missing-socket")
    cookie.symlink_to("old-cookie")
    _remove_stale_singletons(tmp_path)
    assert not any(path.is_symlink() for path in (lock, socket, cookie))

    live_socket = tmp_path / "live-socket"
    live_socket.touch()
    lock.symlink_to("current-host-456")
    socket.symlink_to(live_socket)
    cookie.symlink_to("current-cookie")
    _remove_stale_singletons(tmp_path)
    assert all(path.is_symlink() for path in (lock, socket, cookie))
