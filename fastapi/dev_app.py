import os
import sys
from importlib.machinery import SourceFileLoader

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv(*args, **kwargs):
        return False


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

load_dotenv(os.path.join(ROOT_DIR, ".env"))

APP_PATH = os.path.join(os.path.dirname(__file__), "app.py")
app = SourceFileLoader("audiototxt_fastapi_app", APP_PATH).load_module().app
