import os
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
USERS_DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "nordsheet_users.json")