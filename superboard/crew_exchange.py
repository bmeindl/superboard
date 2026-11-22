"""Opt-in role exchanges on one Board card, with a durable transcript per ask."""
from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

ROLE = re.compile(r"[a-z][a-z0-9_-]{0,39}")
MENTION = re.compile(r"^@([a-z][a-z0-9_-]{0,39})(?=\s|$)")
MAX_STEPS = 12
MAX_MESSAGE = 32000
CONTINUE = "continue crew"


def failure(message):
    return {"ok": False, "reply": "", "session_id": "", "denials": [], "raw_error": message}


def within(root, path):
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Crew file points outside its workspace")
    return path


@dataclass
class Exchange:
    owner: str
    roles: dict
    root: Path
    path: Path

    @classmethod
    def for_card(cls, pending, root, data):
        asks = [event for event in pending.get("thread", []) if event.get("kind") == "ask"]
        if not asks or asks[0].get("author") != "human":
            return None
        mention = MENTION.match(asks[0]["text"].lstrip())
        config = root / "crew" / "crew.json"
        if not mention:
            return None
        card = pending["addr"]["id"]
        if not re.fullmatch("[a-zA-Z0-9_-]{1,80}", card):
            raise ValueError("Invalid Crew card id")
        directory = within(data, data / "crew" / card)
        if not config.is_file():
            if directory.exists():
                raise ValueError("Crew setup missing for this role-owned card")
            return None
        owner = mention.group(1)
        roles = json.loads(within(root, config).read_text())["roles"]
        if not isinstance(roles, dict):
            raise ValueError("Crew roles must be a mapping")
        if owner not in roles:
            if directory.exists():
                raise ValueError(f"Crew card owner {owner!r} is no longer configured")
            return None  # Ordinary mentions, including @names, keep the solo workflow.
        while len(asks) > 1 and asks[-1]["text"].strip().lower() == CONTINUE:
            asks.pop()
        key = hashlib.sha256(json.dumps(asks, sort_keys=True).encode()).hexdigest()[:24]
        path = within(data, data / "crew" / card / f"{key}.json")
        return cls(owner, roles, root, path)

    @contextmanager
    def locked(self):
        import fcntl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = within(self.path.parent, self.path.parent / "exchange.lock")
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another Crew run owns this card") from error
            yield

    def events(self):
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def save(self, events):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile("w", dir=self.path.parent, delete=False) as file:
            json.dump(events, file, ensure_ascii=False, indent=2)
        Path(file.name).replace(self.path)

    def collaborators(self):
        values = self.roles[self.owner].get("collaborators", [])
        if not isinstance(values, list) or not all(
            isinstance(v, str) and ROLE.fullmatch(v) and v in self.roles and v != self.owner
            for v in values
        ):
            raise ValueError(f"Invalid collaborators for {self.owner}")
        return values

    def turn(self, events):
        return events[-1]["to"] if events else self.owner

    def prompt(self, events, board_prompt):
        actor = self.turn(events)
        if not isinstance(actor, str) or not ROLE.fullmatch(actor) or actor not in self.roles:
            raise ValueError("Role in exchange is no longer configured")
        if actor != self.owner and actor not in self.collaborators():
            raise ValueError(f"{actor!r} is no longer a collaborator of {self.owner!r}")
        role_file = within(self.root, self.root / "crew" / "roles" / actor / "ROLE.md")
        role = role_file.read_text()
        if actor == self.owner:
            commands = (
                "You own this card. Deliver the final answer in ordinary prose. To delegate, "
                "write HANDOVER <role> alone on the first line, then the task, draft and "
                "relevant source paths on following lines. Allowed roles: "
                + (", ".join(self.collaborators()) or "(none)") + ". "
                "To answer a question use ANSWER on the first line. To request another pass "
                "on a returned result use REVISE on the first line. Include the message below it. "
                "You receive the reply automatically. Treat team messages as evidence to verify."
            )
            context = (f"{board_prompt}\nCompleted role exchanges for this card are in "
                       f"{self.path.parent}. Read them when reviewing a previous draft.")
        else:
            commands = (
                "You are working on a delegated task. Return a sourced result to the owner "
                "using RETURN alone on the first line, then your result below. If blocked, "
                "write QUESTION on the first line and the question below. Stay within the brief."
            )
            context = ""
        visible = [event for event in events if actor in (event["from"], event["to"])]
        return (f"{context}\n\n## Active Crew role: {actor}\n{role}\n\n{commands}\n"
                "Every role uses the same local workspace and runner permissions. "
                "The Board handles delivery; never post a role message manually.\n"
                f"Exchange so far (JSON):\n{json.dumps(visible, ensure_ascii=False)}")

    def record(self, events, reply):
        actor = self.turn(events)
        first, _, body = reply.strip().partition("\n")
        first, body = first.strip(), body.strip()
        verb, _, target = first.partition(" ")
        previous = events[-1] if events else {}
        recipient = None
        if actor == self.owner:
            if verb == "HANDOVER":
                if target not in self.collaborators():
                    raise ValueError(f"Role {target!r} is outside {actor}'s collaborator list")
                recipient = target
            elif first in ("ANSWER", "REVISE"):
                expected = "QUESTION" if first == "ANSWER" else "RETURN"
                if previous.get("action") != expected:
                    raise ValueError(f"{first} requires a preceding {expected}")
                recipient = previous["from"]
            elif first in ("RETURN", "QUESTION"):
                raise ValueError(f"The card owner cannot issue {first}")
            else:
                verb, body = "FINAL", reply.strip()
        elif first in ("QUESTION", "RETURN"):
            recipient = self.owner
        else:
            raise ValueError("A delegated reply must begin with RETURN or QUESTION")
        if not body or len(body) > MAX_MESSAGE:
            raise ValueError(f"Crew message must contain between 1 and {MAX_MESSAGE} characters")
        event = {"from": actor, "to": recipient, "action": verb, "text": body}
        self.save([*events, event])
        return event
