import os
import sys
from pathlib import Path

from dotenv import load_dotenv


ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
load_dotenv(ROOT_DIR / ".env")

from web_app import create_web_app  # noqa: E402


app = create_web_app(
    root_dir=ROOT_DIR,
    data_dir=os.getenv("WEB_DATA_DIR", str(ROOT_DIR / "data" / "web")),
    allow_embedded_telegram=True,
)
