# Suspended Card Priority

A sortable, per-card Browser column for deciding which suspended cards to process.
The default queue is `deck:math is:suspended`. Card content, tags, suspension,
due dates, and review history are never modified.

## Use

Copy or symlink this folder into `addons21`, then restart Anki. In the Browser,
switch to **Cards** mode and choose **Priority → Show Math priority queue**.
The **Priority** column sorts highest first; it can also be enabled by
right-clicking the column headers.

- **Score next batch** assesses up to 12 suspended cards in the current search.
- **Score all remaining in this search** continues in batches, saving each batch.
- **Re-score selected cards** assesses the selected suspended cards again.
- **Explain current priority** shows the rationale, dimensions, and confidence.
- **Show overlapping cards** opens the cited unsuspended reference cards.
- **Stop scoring** cancels the active request and preserves completed batches.

Scoring uses the signed-in **Codex CLI**, with the user's configured model and
reasoning settings. Run `codex login` first. Scoring sends rendered card text
and retrieved reference text to OpenAI and consumes the account's Codex usage.
It runs in the background so browsing can continue. No scoring runs automatically
at startup. Set `codex_path` in the add-on configuration if discovery fails.

## What the score means

The model rates conceptual value, computational burden, and overlap from 0–4.
With each rating normalized to 0–1, the score is:

`clamp(100 × (0.25 + 0.75 × conceptual) × (1 − 0.85 × overlap)
       − 35 × computation − 20 × overlap × computation, 0, 100)`

Thus a new structural insight ranks high; an already-covered mechanical exercise
ranks low. Formula recall is not automatically conceptual. Explanations on the
back are context, not automatically credited as tested knowledge. Each rendered
cloze is assessed separately, including its actual hidden target.

All unsuspended cards, including unseen new and buried cards, are indexed locally.
A TF–IDF shortlist (8 references by default, including sibling clozes) is sent
for semantic assessment. Word overlap only retrieves candidates; the model judges
whether they test the same knowledge. This is approximate retrieval, not an
exhaustive semantic comparison: paraphrases and image-only references can be missed.
Ranking does not currently penalize redundancy among the suspended candidates.

Blank means unscored. **Review** means insufficient confidence, including
image-dependent cards: the first version reads text, not images or audio.
**Stale** means the candidate content changed. Scores are snapshots; changes to
unsuspended cards can make previous overlap estimates outdated. Re-running scoring
detects changes to the reference snapshot and refreshes affected rankings.

Scores and evidence live in `suspended-card-priority.json` inside the current
Anki profile, separately from the collection. They do not sync to other devices.
The card fingerprint and reference snapshot fingerprint support safe resumption.
No personal card data belongs in the add-on folder or source repository.

## Tests

From the Anki repository: `out/pyenv/bin/python -m pytest
qt/tests/test_suspended_card_priority.py -q`.
