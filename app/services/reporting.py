import hashlib
from typing import List, Dict

def report_state(items=None) -> dict:
    return {"items": items or []}

def report_counter_badge(n: int) -> str:
    return f"Tillagt i rapport: {n} punkt{'er' if n != 1 else ''}"

def _hash_id(*parts: str) -> str:
    raw = "||".join([str(p) for p in parts])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def add_report_item(items: List[dict], item: dict) -> List[dict]:
    if any(x.get("id") == item.get("id") for x in items):
        return items
    item.setdefault("owner", "")
    item.setdefault("status", "Öppen")
    item.setdefault("confidence", "Medel")
    item.setdefault("severity", "yellow")
    return [*items, item]

def remove_report_item(items: List[dict], item_id: str) -> List[dict]:
    return [x for x in items if x.get("id") != item_id]

def _normalize_bullets(lines: List[str]) -> List[str]:
    out = []
    for x in lines:
        s = (x or "").strip()
        if not s:
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        if s:
            out.append(s)
    return out

def add_bullets_to_report(
    items: List[Dict],
    title: str,
    bullets: List[str],
    file_hash: str,
    severity: str = "yellow",
    id_prefix: str = "BULLET",
) -> List[Dict]:
    new_items = items[:]
    for i, b in enumerate(_normalize_bullets(bullets), start=1):
        item_id = _hash_id(id_prefix, title, str(i), b, file_hash)
        if not any(x.get("id") == item_id for x in new_items):
            new_items.append(
                {
                    "id": item_id,
                    "title": title,
                    "text": b,
                    "severity": severity,
                    "owner": "",
                    "status": "Öppen",
                    "confidence": "Medel",
                }
            )
    return new_items