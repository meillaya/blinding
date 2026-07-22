# Model Card — {{model_id}} @ {{version}}

## Model Details

| Field | Value |
|---|---|
| Model ID | {{model_id}} |
| Registry version | {{version}} |
| Framework | {{framework}} |
| Artifact URI | {{artifact_uri}} |
| Lifecycle status | {{status}} |

> The registry version is DECOUPLED from git semver (prior-plan M9): it is an
> opaque label assigned at registration, independent of any git tag.

## Intended Use

Binary fraud detection over the artifact's engineered feature set. Serving
suitability per environment is decided by the lifecycle status above:
`registered` → `staging` → `prod`. Approved for production serving:
{{approved_for_prod}}.

## Metrics (eval gate)

| Metric | Value |
|---|---|
| accuracy | {{accuracy}} |
| f1 | {{f1}} |
| roc_auc | {{roc_auc}} |
| latency p95 (ms) | {{latency_p95_ms}} |

## Limitations

- Metrics are from the offline evaluation split only; online data drift and
  serving latency under load are NOT captured here.
- `approved_for_prod` is flipped only by the eval gate — this card documents
  state; it does NOT itself approve the model for production.
- Promotion to `prod` is rejected while `approved_for_prod` is false.

## Lineage

{{lineage}}
