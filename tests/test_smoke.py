import jobagent
from jobagent.core import states


def test_package_imports():
    assert jobagent.__name__ == "jobagent"
    assert states.Status is not None
