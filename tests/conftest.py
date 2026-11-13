import pytest


@pytest.fixture(autouse=True)
def _isolate_eval_runs(tmp_path, monkeypatch):
    """Eval runs triggered by tests go to a temp folder, never the real evals/runs/."""
    from jobagent import cli

    monkeypatch.setattr(cli, "RUNS_DIR", tmp_path / "runs")
