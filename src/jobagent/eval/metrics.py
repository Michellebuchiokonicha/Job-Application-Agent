"""Classification metrics, written from scratch (no scikit-learn)."""


def confusion_matrix(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> dict[str, dict[str, int]]:
    """matrix[true][pred] = count. Rows are your labels, columns are predictions.

    Raises KeyError on a label not in `labels` (fail loudly, never drop data) and
    ValueError if the two lists differ in length (zip strict).
    """
    matrix = {t: {p: 0 for p in labels} for t in labels}
    for true, pred in zip(y_true, y_pred, strict=True):
        matrix[true][pred] += 1
    return matrix


def _safe_div(numerator: float, denominator: float) -> float:
    """0.0 when the denominator is 0 (same as scikit-learn's zero_division=0).

    Cautious on purpose: a class that is never predicted must not score 1.0.
    """
    return numerator / denominator if denominator else 0.0


def per_class(
    y_true: list[str], y_pred: list[str], labels: list[str]
) -> dict[str, dict[str, float]]:
    """{label: {"precision", "recall", "f1", "support"}} for every label."""
    m = confusion_matrix(y_true, y_pred, labels)
    report: dict[str, dict[str, float]] = {}
    for c in labels:
        tp = m[c][c]  # diagonal: truly c, predicted c
        row = sum(m[c][p] for p in labels)  # everything truly c
        col = sum(m[t][c] for t in labels)  # everything predicted c
        fn = row - tp  # truly c, predicted something else (missed)
        fp = col - tp  # predicted c, truly something else (false alarm)

        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        f1 = _safe_div(2 * precision * recall, precision + recall)
        report[c] = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "support": row,
        }
    return report


def macro_f1(report: dict[str, dict[str, float]]) -> float:
    """Unweighted mean F1 over classes that appear in your labels (support > 0).

    Macro = every class counts equally, so rare classes (interview, assessment)
    can't hide behind common ones. Classes with no examples are left out: their
    F1 is 0 by construction, which would punish the classifier for a gap in the
    labels rather than for a mistake. Their false alarms still lower the recall
    of the classes they were taken from.
    """
    present = [r["f1"] for r in report.values() if r["support"] > 0]
    return _safe_div(sum(present), len(present))
