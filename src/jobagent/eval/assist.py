"""Model suggestions for labelling, computed one email ahead in a background thread.

The model takes ~1 s per email. Asking it only when an email appears would make you
wait every time; instead, while you read email N, the model is already working on
email N+1. Same total work, but the wait is hidden behind your reading time.
"""

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from jobagent.inbox.classify import Prediction

Predict = Callable[[str, str], Prediction | None]


class Suggester:
    def __init__(self, predict: Predict):
        self._predict = predict
        # One worker: requests run in submission order and never overload the model.
        self._pool = ThreadPoolExecutor(max_workers=1)
        self._futures: dict[str, Future] = {}
        self.last_error: str | None = None  # shown to you; failures are never silent

    def prefetch(self, gmail_message_id: str, subject: str, body: str) -> None:
        """Start predicting this email in the background (no-op if already started).

        Takes plain strings, not a DB row: SQLite connections must stay on the
        thread that created them, so the main thread reads the email first.
        """
        if gmail_message_id not in self._futures:
            self._futures[gmail_message_id] = self._pool.submit(
                self._predict, subject, body
            )

    def get(self, gmail_message_id: str) -> Prediction | None:
        """Wait for this email's suggestion. None if never requested or it failed."""
        future = self._futures.get(gmail_message_id)
        if future is None:
            return None
        try:
            return future.result()
        # Deliberately broad: whatever goes wrong in the model (server down, bad
        # response), labelling must carry on by hand. The error is kept and shown.
        except Exception as e:  # noqa: BLE001
            self.last_error = f"{type(e).__name__}: {e}"[:120]
            return None

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
