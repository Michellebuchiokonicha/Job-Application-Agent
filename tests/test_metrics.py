"""Small cases worked out by hand, so every expected number can be checked on paper."""

import pytest

from jobagent.eval.metrics import confusion_matrix, macro_f1, per_class

LABELS = ["interview", "rejection", "other"]

#            0            1            2        3          4
Y_TRUE = ["interview", "interview", "interview", "other", "rejection"]
Y_PRED = ["interview", "interview", "other", "interview", "rejection"]
# interview: 3 real, 2 caught, 1 missed (-> other), 1 false alarm (from other)
# rejection: 1 real, 1 caught
# other:     1 real, 0 caught (it was called interview), 1 false alarm (from interview)


def test_confusion_matrix_cells():
    m = confusion_matrix(Y_TRUE, Y_PRED, LABELS)
    assert m["interview"]["interview"] == 2  # caught
    assert m["interview"]["other"] == 1  # missed
    assert m["other"]["interview"] == 1  # false alarm
    assert m["rejection"]["rejection"] == 1
    assert sum(sum(row.values()) for row in m.values()) == len(Y_TRUE)


def test_confusion_matrix_has_every_label_even_if_unused():
    m = confusion_matrix(["other"], ["other"], LABELS)
    assert m["interview"] == {"interview": 0, "rejection": 0, "other": 0}


def test_unknown_label_fails_loudly():
    with pytest.raises(KeyError):
        confusion_matrix(["offer"], ["other"], LABELS)


def test_length_mismatch_fails_loudly():
    with pytest.raises(ValueError):
        confusion_matrix(["other", "other"], ["other"], LABELS)


def test_per_class_interview():
    r = per_class(Y_TRUE, Y_PRED, LABELS)["interview"]
    assert r["precision"] == pytest.approx(2 / 3)  # 2 right out of 3 predicted
    assert r["recall"] == pytest.approx(2 / 3)  # 2 caught out of 3 real
    assert r["f1"] == pytest.approx(2 / 3)
    assert r["support"] == 3


def test_per_class_perfect_and_zero():
    r = per_class(Y_TRUE, Y_PRED, LABELS)
    assert r["rejection"] == {"precision": 1.0, "recall": 1.0, "f1": 1.0, "support": 1}
    # 'other' predicted once (wrongly) and its one real example was missed.
    assert r["other"]["precision"] == 0.0
    assert r["other"]["recall"] == 0.0
    assert r["other"]["f1"] == 0.0  # 0/0 handled, no ZeroDivisionError


def test_never_predicted_class_scores_zero_not_perfect():
    # The classifier never says "interview", so precision is 0/0 -> 0.0.
    r = per_class(["interview", "other"], ["other", "other"], LABELS)
    assert r["interview"]["precision"] == 0.0
    assert r["interview"]["recall"] == 0.0


def test_macro_f1_ignores_classes_with_no_examples():
    r = per_class(["interview", "other"], ["interview", "other"], LABELS)
    # 'rejection' has support 0 and is left out: (1.0 + 1.0) / 2, not 2/3.
    assert macro_f1(r) == pytest.approx(1.0)


def test_macro_f1_averages_equally():
    r = per_class(Y_TRUE, Y_PRED, LABELS)
    # interview 2/3, rejection 1.0, other 0.0 -> each counts equally
    assert macro_f1(r) == pytest.approx((2 / 3 + 1.0 + 0.0) / 3)


def test_empty_input():
    r = per_class([], [], LABELS)
    assert macro_f1(r) == 0.0
