"""Participant filter identities use portal and userId, never employee names."""
import re


def meeting_participants(portal: str, metadata: dict) -> list[dict]:
    people = []
    seen = set()
    for person in metadata.get("participants") or []:
        if not isinstance(person, dict) or isinstance(person.get("userId"), bool):
            continue
        value = str(person.get("userId", ""))
        if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) <= 0:
            continue
        user_id = str(int(value))
        identity = portal + ":user:" + user_id
        if identity in seen:
            continue
        seen.add(identity)
        name = person.get("name")
        label = name.strip() if isinstance(name, str) and name.strip() else "Участник #" + user_id
        people.append({"id": identity, "label": label, "user_id": user_id, "portal": portal,
                       "named": isinstance(name, str) and bool(name.strip())})
    return people


def options(records: list[dict]) -> list[dict]:
    people = {}
    for record in sorted(records, key=lambda r: r.get("startDate") or "", reverse=True):
        for person in meeting_participants(record["portal"], record["metadata"]):
            current = people.get(person["id"])
            if current is None:
                people[person["id"]] = {**person, "count": 1}
            else:
                current["count"] += 1
                if person["named"] and not current["named"]:
                    current.update(label=person["label"], named=True)
    return sorted(people.values(), key=lambda p: (p["label"].casefold(), p["portal"], p["user_id"]))
