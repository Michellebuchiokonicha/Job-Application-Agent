"""Score one cut-off job and KEEP the raw answer, to tell a loop from a long answer.

Usage (from the project folder):
    .venv/bin/python scripts/diagnose_cutoff.py [job_id]      # default 66

The raw answer quotes your CV, so it is saved outside the repo, next to the database.
"""

import re
import sys
from collections import Counter

import ollama

from jobagent.core.config import Settings
from jobagent.core.store import connect, get_job
from jobagent.match.cv import cv_for, load_cv
from jobagent.match.score import (
    MODEL_TIMEOUT,
    NUM_CTX,
    NUM_PREDICT,
    SYSTEM_PROMPT,
    FitAssessment,
    build_prompt,
)

job_id = int(sys.argv[1]) if len(sys.argv) > 1 else 66

# 1. The job, exactly as the scorer would get it.
settings = Settings.from_env()
conn = connect(settings.db_path)
row = get_job(conn, job_id)
description = row["full_description"] or row["description"]
cv = load_cv(cv_for(row["title"]))
print(f"Job #{job_id}: {row['title']} at {row['company']} ({len(description)} chars)")

# 2. The same messages FitScorer.score() builds.
messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {
        "role": "user",
        "content": build_prompt(row["title"], row["company"], description, cv),
    },
]

# 3. The same call FitScorer._chat() makes, but nothing is thrown away.
client = ollama.Client(host=settings.ollama_host, timeout=MODEL_TIMEOUT)
response = client.chat(
    model=settings.inbox_model,
    messages=messages,
    format=FitAssessment.model_json_schema(),
    options={"temperature": 0, "num_ctx": NUM_CTX, "num_predict": NUM_PREDICT},
    think=False,
)
content = response["message"]["content"]

# 4. The evidence.
print(f"done_reason:   {response.get('done_reason')}")
print(f"prompt tokens: {response.get('prompt_eval_count')}")
print(f"answer tokens: {response.get('eval_count')} (cap {NUM_PREDICT})")

# Each requirement the model listed. A loop repeats the same ones; a long answer doesn't.
reqs = re.findall(r'"requirement"\s*:\s*"([^"]*)"', content)
counts = Counter(reqs)
print(f"requirements listed: {len(reqs)}, distinct: {len(counts)}")
for text, n in counts.most_common(5):
    print(f"  x{n}  {text[:80]}")

print("\n--- last 1,500 characters of the answer ---")
print(content[-1500:])

out_dir = settings.db_path.parent / "diagnostics"
out_dir.mkdir(parents=True, exist_ok=True)
out = out_dir / f"answer_{job_id}.json"
out.write_text(content, encoding="utf-8")
print(f"\n(full answer saved to {out})")
