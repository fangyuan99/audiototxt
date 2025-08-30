import os
from importlib.machinery import SourceFileLoader
import uvicorn


def main() -> None:
    app_path = os.path.join(os.path.dirname(__file__), "app.py")
    module = SourceFileLoader("audiototxt_fastapi_app", app_path).load_module()
    uvicorn.run(
        module.app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()


