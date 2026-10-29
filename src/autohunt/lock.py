from pathlib import Path


class ControllerLock:
    """Single-controller filesystem lock; refuses a second owner without deleting state."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as f:
                f.write("autohunt\n")
        except FileExistsError as exc:
            raise RuntimeError(f"controller lock exists: {self.path}") from exc

    def release(self) -> None:
        if self.path.exists():
            self.path.unlink()
