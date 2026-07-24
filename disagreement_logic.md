# Cross-model disagreement detection — logic review

This describes how the validation pipeline decides that two BirdNET-Go
detections "disagree," so it can be checked against the intended behavior.
Implemented in `candidate_finder.py`, functions `fetch_window()` and
`find_disagreements()`.

## 1. Base query

```sql
SELECT d.id, d.detected_at, d.begin_time, d.end_time, d.source_id,
       d.model_id, l.scientific_name, d.confidence, d.clip_name
FROM detections d
JOIN labels l ON l.id = d.label_id
WHERE d.detected_at >= <7 days ago>
  AND d.id NOT IN (SELECT detection_id FROM detection_reviews)
```

Fields used:

| Field | Table | Purpose |
|---|---|---|
| `model_id` | `detections` | Which engine produced this detection |
| `source_id` | `detections` → `audio_sources.id` | Which specific microphone/stream (e.g. `t***s` RTSP, `S****r` USB) — comparisons never cross two different mics |
| `scientific_name` | `labels` (via `detections.label_id`) | The species called |
| `begin_time`, `end_time` | `detections` | Epoch **milliseconds** of the detection's audio segment |

Rows already reviewed by a human in BirdNET-Go (present in `detection_reviews`,
either `correct` or `false_positive`) are excluded up front, before
disagreement matching runs at all.

## 2. Matching algorithm (`find_disagreements`, proximity = 15s)

1. Group the remaining rows by `source_id`.
2. Within each source group, sort by `begin_time` ascending.
3. For each detection `r`, scan forward through later detections `r2` in the
   same group while `(r2.begin_time - r.begin_time) / 1000 <= 15` seconds —
   stop as soon as one falls outside the window (list is sorted, so nothing
   further can qualify either).
4. Of those within 15s, keep the ones with a **different `model_id`**.
5. Of those, keep the ones with a **different `scientific_name`** — these are
   the actual disagreements.
6. If any are found, `r` and its disagreeing match(es) are recorded as a
   pair, and every detection id involved is marked "used" so it can't be
   reused in a second pairing.

## Points to double-check with the developer

- **Only `begin_time` is used for proximity — `end_time` is fetched but not
  used.** True interval-overlap matching was tried first and found almost no
  pairs, because BirdNET and Perch don't window the same clip identically
  internally. The ±15s `begin_time` proximity is a looser stand-in for "these
  two detections are about the same acoustic event."

- **Model identity is a hardcoded map, not a join against `ai_models.name`**:
  ```python
  MODEL_NAMES = {1: "BirdNET", 2: "Perch", 3: "BattyBirdNET"}
  ```
  Cross-checked against the live `ai_models` table and it currently matches
  exactly, but it's not derived dynamically — if a new model is added or IDs
  are ever renumbered, this silently goes stale.

- **Greedy, first-match pairing**: each detection can belong to at most one
  disagreement pair. If 3+ detections from different models cluster within
  15s at one source (rare), only the first qualifying pair gets flagged, not
  all combinations.

- **`source_id` is per audio-source *row*, not per physical mic.**
  `audio_sources` has multiple rows for the same physical RTSP mic (e.g. id 2
  and id 5, same URI, same `t***s` display name) — apparently created across
  app restarts. A disagreement straddling such a boundary (rare, only right
  at a restart) would be missed, since the two detections would carry
  different `source_id` values despite being the same physical source.
