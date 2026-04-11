import argparse
import os
from importlib.machinery import SourceFileLoader

import uvicorn

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - optional dependency fallback
    def load_dotenv(*args, **kwargs):
        return False


ROOT_DIR = os.path.dirname(os.path.dirname(__file__))
load_dotenv(os.path.join(ROOT_DIR, ".env"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AudioToTxt FastAPI server.")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "info"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.reload:
        uvicorn.run(
            "dev_app:app",
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[
                os.path.join(ROOT_DIR, "fastapi"),
            ],
            reload_includes=[
                "*.py",
                "*.html",
                "*.css",
                "*.js",
            ],
            reload_excludes=[
                ".git/*",
                ".venv/*",
                "data/*",
                "tests/*",
            ],
            log_level=args.log_level,
        )
        return

    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    module = SourceFileLoader("audiototxt_fastapi_app", app_path).load_module()
    uvicorn.run(
        module.app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
