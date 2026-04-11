import os
from importlib.machinery import SourceFileLoader

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv(*args, **kwargs):
        return False


ROOT_DIR = os.path.dirname(__file__)
load_dotenv(os.path.join(ROOT_DIR, ".env"))

APP_PATH = os.path.join(ROOT_DIR, "fastapi", "app.py")
app = SourceFileLoader("audiototxt_fastapi_app", APP_PATH).load_module().app
