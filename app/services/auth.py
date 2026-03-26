import os
import json
import re
import secrets
import hashlib
import datetime
from typing import Dict, Optional, Tuple
from app.core.config import USERS_DB_PATH

def _load_users() -> Dict[str, dict]:
    if not os.path.exists(USERS_DB_PATH):
        return {}
    try:
        with open(USERS_DB_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_users(users: Dict[str, dict]) -> None:
    os.makedirs(os.path.dirname(USERS_DB_PATH), exist_ok=True)
    with open(USERS_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)

def _hash_password(password: str, salt: Optional[str] = None) -> Tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        120000,
    ).hex()
    return salt, pwd_hash

def _verify_password(password: str, salt: str, pwd_hash: str) -> bool:
    _, candidate = _hash_password(password, salt)
    return secrets.compare_digest(candidate, pwd_hash)

def create_user_account(name: str, email: str, password: str, title: str) -> Tuple[bool, str]:
    users = _load_users()
    email_key = (email or "").strip().lower()

    if not name.strip() or not email_key or not password.strip() or not title.strip():
        return False, "Fyll i namn, e-post, lösenord och jobbtitel."
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email_key):
        return False, "Ange en giltig e-postadress."
    if len(password) < 6:
        return False, "Lösenordet behöver vara minst 6 tecken."
    if email_key in users:
        return False, "Det finns redan ett konto med den e-posten."

    salt, pwd_hash = _hash_password(password)
    users[email_key] = {
        "name": name.strip(),
        "email": email_key,
        "title": title.strip(),
        "salt": salt,
        "password_hash": pwd_hash,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
    }
    _save_users(users)
    return True, "Konto skapat. Du kan nu logga in."

def authenticate_user(email: str, password: str):
    users = _load_users()
    email_key = (email or "").strip().lower()
    user = users.get(email_key)

    if not user:
        return False, None, "Kontot hittades inte."
    if not _verify_password(password, user.get("salt", ""), user.get("password_hash", "")):
        return False, None, "Fel e-post eller lösenord."

    safe_user = {k: v for k, v in user.items() if k not in {"salt", "password_hash"}}
    return True, safe_user, "Inloggning lyckades."