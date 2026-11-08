"""Explicit Docker check: build the image first, then run this file directly."""

import contextlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_chat import ChatPeer, initial_sync


# Capture only output from our own containers; never inspect host credentials.
def docker(*args: str, check: bool = True) -> str:
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True).stdout.strip()


# Wait for the published localhost port, using an ordinary username-only client.
def connect(address: tuple[str, int], username: str) -> ChatPeer:
    deadline = time.monotonic() + 10
    while True:
        try:
            return ChatPeer(address, username)
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


# Use isolated container/volume names and synthetic messages. Remove only these
# test resources, leaving the built image and any real server/database untouched.
def main() -> None:
    image = "socket-chat-server:latest"
    name = "socket-chat-check-" + uuid.uuid4().hex[:12]
    volume = name + "-data"
    docker("run", "--rm", "--entrypoint", "python", image, "-c",
           "import os; from pathlib import Path; "
           "assert os.getuid() == 10001; "
           "assert {p.name for p in Path('/app').iterdir()} == "
           "{'server.py', 'server_network.py', 'server_ai.py', 'database.py', 'common'}")
    # Fake runtime variables test configuration without reading the real .env
    # or sending any provider request. Nothing secret is printed or baked in.
    docker("run", "--rm", "--entrypoint", "python",
           "-e", "OPENAI_BASE_URL=https://example.invalid/v1", "-e", "OPENAI_MODEL=test/model",
           "-e", "OPENAI_API_KEY=synthetic-test-key", image, "-c",
           "from server_ai import load_ai_config; c = load_ai_config(); "
           "assert c is not None and c.model == 'test/model'")
    try:
        docker("volume", "create", volume)
        for restart in (False, True):
            docker("run", "--detach", "--name", name, "--publish", "127.0.0.1::5000",
                   "--mount", f"type=volume,src={volume},dst=/data", image)
            port = int(docker("port", name, "5000/tcp").rsplit(":", 1)[1])
            with contextlib.ExitStack() as peers:
                alice = peers.enter_context(connect(("127.0.0.1", port), "Alice"))
                bob = peers.enter_context(connect(("127.0.0.1", port), "Bob"))
                assert alice.ok("ping")["pong"] is True
                room = alice.ok("open_private", {"target_user_id": bob.user["id"]})["room"]["room_key"]
                if not restart:
                    user_id = alice.user["id"]
                    group = alice.ok("create_group", {"name": "Docker group"})["room"]["room_key"]
                    bob.ok("join_group", {"room_key": group})
                    message_id = alice.ok("send_message", {"room_key": room, "content": "Docker persistence check"})["message"]["id"]
                    alice.ok("set_heart", {"room_key": room, "message_id": message_id, "hearted": True})
                    bob.ok("set_pin", {"room_key": room, "message_id": message_id})
                    alice.ok("trigger_effect", {"room_key": room})
                else:
                    assert alice.user["id"] == user_id
                groups = bob.ok("get_directory")["groups"]
                assert len(groups) == 1 and groups[0]["room_key"] == group
                assert {member["id"] for member in groups[0]["members"]} == {alice.user["id"], bob.user["id"]}
                data = bob.ok("sync_room", initial_sync(room))
                messages = data["older_messages"] + data["new_messages"]
                assert len(messages) == 1 and messages[0]["content"] == "Docker persistence check"
                assert messages[0]["heart_count"] == 1
                assert data["pinned_message"]["id"] == message_id
                assert data["effect_revision"] == (0 if restart else 1)
            docker("stop", "--time", "10", name)
            assert docker("inspect", "--format", "{{.State.ExitCode}}", name) == "0"
            docker("rm", name)
        print("Docker checks passed: non-root server-only image, runtime AI config, published TCP, two clients, persistent users/groups/members/history/hearts/pin, transient sound reset and graceful stop.")
    finally:
        docker("rm", "--force", name, check=False)
        docker("volume", "rm", volume, check=False)


if __name__ == "__main__":
    main()
