import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from web_app import create_web_app  # noqa: E402


runtime_env = dict(os.environ)
runtime_env.setdefault("BOT_DATA_DIR", "/tmp/audiototxt_bot")
app = create_web_app(
    root_dir=ROOT_DIR,
    data_dir=os.getenv("WEB_DATA_DIR", "/tmp/audiototxt_data"),
    environ=runtime_env,
    allow_embedded_telegram=False,
)
