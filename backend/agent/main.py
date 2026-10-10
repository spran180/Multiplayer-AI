"""ASGI entrypoint for the agent service."""
from backend.main import app

__all__ = ["app"]


def main() -> None:
    """Run the API when invoked as ``python -m backend.agent.main``."""
    import os

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
