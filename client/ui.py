"""Command-line interface for the chat client."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .client import ChatClient


HELP_TEXT = """commands:
  /connect [host] [port]
  /reconnect
  /register <username> <password>
  /login <username> <password>
  /logout
  /msg <username> <content>
  /gmsg <group_id> <content>
  /chat private <username>
  /chat group <group_id>
  /create-group <name>
  /join <group_id>
  /leave <group_id>
  /history private <username> [limit]
  /history group <group_id> [limit]
  /status
  /heartbeat
  /quit
Bare text is sent to the current /chat target.
"""


def run_cli(client: "ChatClient") -> None:
    print(HELP_TEXT)
    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            client.disconnect()
            return

        if not line:
            continue

        try:
            should_continue = handle_line(client, line)
        except Exception as exc:
            print(f"command failed: {exc}")
            continue

        if not should_continue:
            client.disconnect()
            return


def handle_line(client: "ChatClient", line: str) -> bool:
    if not line.startswith("/"):
        send_to_current_chat(client, line)
        return True

    command, rest = _split_once(line[1:])
    command = command.lower()

    if command in {"help", "h", "?"}:
        print(HELP_TEXT)
    elif command == "connect":
        handle_connect(client, rest)
    elif command == "reconnect":
        client.reconnect()
    elif command == "register":
        username, password = _split_required(rest, 2, "usage: /register <username> <password>")
        client.register(username, password)
    elif command == "login":
        username, password = _split_required(rest, 2, "usage: /login <username> <password>")
        client.login(username, password)
    elif command == "logout":
        client.logout()
    elif command == "msg":
        receiver, content = _split_required(rest, 2, "usage: /msg <username> <content>")
        client.send_private(receiver, content)
    elif command == "gmsg":
        group_id, content = _split_required(rest, 2, "usage: /gmsg <group_id> <content>")
        client.send_group(group_id, content)
    elif command == "chat":
        handle_chat_target(client, rest)
    elif command == "create-group":
        name = rest.strip()
        if not name:
            raise ValueError("usage: /create-group <name>")
        client.create_group(name)
    elif command == "join":
        group_id = _single_arg(rest, "usage: /join <group_id>")
        client.join_group(group_id)
    elif command == "leave":
        group_id = _single_arg(rest, "usage: /leave <group_id>")
        client.leave_group(group_id)
    elif command == "history":
        handle_history(client, rest)
    elif command == "status":
        handle_status(client)
    elif command == "heartbeat":
        client.heartbeat()
    elif command in {"quit", "exit"}:
        return False
    else:
        raise ValueError(f"unknown command: /{command}")

    return True


def handle_connect(client: "ChatClient", rest: str) -> None:
    args = rest.split()
    if len(args) > 2:
        raise ValueError("usage: /connect [host] [port]")
    host = args[0] if args else None
    port = int(args[1]) if len(args) == 2 else None
    client.connect(host, port)


def handle_chat_target(client: "ChatClient", rest: str) -> None:
    chat_type, target = _split_required(rest, 2, "usage: /chat private <username> | /chat group <group_id>")
    if chat_type not in {"private", "group"}:
        raise ValueError("chat type must be private or group")
    client.state.current_chat_type = chat_type
    client.state.current_target = target
    print(f"current chat: {chat_type} {target}")


def handle_history(client: "ChatClient", rest: str) -> None:
    parts = rest.split()
    if len(parts) not in {2, 3}:
        raise ValueError("usage: /history private <username> [limit] | /history group <group_id> [limit]")

    chat_type, target = parts[0], parts[1]
    limit = int(parts[2]) if len(parts) == 3 else 50
    if chat_type == "private":
        client.request_private_history(target, limit)
        local_items = client.history.recent_private(target, limit)
    elif chat_type == "group":
        client.request_group_history(target, limit)
        local_items = client.history.recent_group(target, limit)
    else:
        raise ValueError("history type must be private or group")

    if local_items:
        print("local recent:")
        for line in client.history.format_items(local_items, current_user=client.state.username):
            print(line)


def handle_status(client: "ChatClient") -> None:
    print(f"connected: {client.state.connected}")
    print(f"username: {client.state.username or '-'}")
    print(f"login_confirmed: {client.state.login_confirmed}")
    print(f"current_chat: {client.state.current_chat_type or '-'} {client.state.current_target or ''}".rstrip())
    print(f"groups: {', '.join(sorted(client.state.groups)) or '-'}")
    if client.state.user_status:
        print("known user status:")
        for username, status in sorted(client.state.user_status.items()):
            print(f"  {username}: {status}")


def send_to_current_chat(client: "ChatClient", content: str) -> None:
    if client.state.current_chat_type == "private" and client.state.current_target:
        client.send_private(client.state.current_target, content)
    elif client.state.current_chat_type == "group" and client.state.current_target:
        client.send_group(client.state.current_target, content)
    else:
        raise ValueError("no current chat target; use /chat private <username> or /chat group <group_id>")


def _split_once(text: str) -> tuple[str, str]:
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _split_required(text: str, count: int, usage: str) -> list[str]:
    parts = text.strip().split(maxsplit=count - 1)
    if len(parts) != count or any(part == "" for part in parts):
        raise ValueError(usage)
    return parts


def _single_arg(text: str, usage: str) -> str:
    parts = text.split()
    if len(parts) != 1:
        raise ValueError(usage)
    return parts[0]
