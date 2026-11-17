import pytest


@pytest.fixture(autouse=True)
def _isolate_eval_runs(tmp_path, monkeypatch):
    """Eval runs triggered by tests go to a temp folder, never the real evals/runs/."""
    from jobagent import cli

    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")


class _NoBoards:
    """Company boards that list nothing: tests never call the real APIs."""

    requests = 0

    def jobs(self, company):
        return iter(())


@pytest.fixture(autouse=True)
def _no_real_job_boards(monkeypatch):
    """`jobs fetch` reads company boards by default; keep tests off the network.
    A test that needs board jobs patches cli._make_boards itself."""
    from jobagent import cli

    monkeypatch.setattr(cli, "_make_boards", lambda: _NoBoards())


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch):
    """Tests never open Chrome. A test that needs the browser step patches
    browser.open_and_fill itself (with a fake page)."""
    from jobagent.apply import browser

    def no_browser(*args, **kwargs):
        raise RuntimeError("no browser in tests")

    monkeypatch.setattr(browser, "open_and_fill", no_browser)
