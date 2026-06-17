# Near-duplicate detection (`--dedupe`)

Bursts and near-identical re-shoots are usually the **biggest** reclaimable
category in a real library. With `--dedupe`, photocull computes an Apple Vision
**feature print** (a 768-dim embedding) for every image, clusters images whose
embeddings are within `--dedupe-threshold` (L2 distance), and within each
cluster keeps the single best frame — flagging the rest as `duplicate`.

The keeper is chosen by reusing the quality signals already computed:
**face capture quality** first (Apple's own best-shot metric — favours open
eyes / sharp faces), then **sharpness**, then aesthetics and resolution.

Clustering of tens of thousands of images takes only seconds: distances are
computed with blocked BLAS matrix products, not a Python loop.

## Choosing the threshold

This matters, and the safe direction is **lower**:

| Threshold | Behaviour                                                        |
|-----------|------------------------------------------------------------------|
| `0.2`     | Only near-identical frames (very strict).                        |
| `0.3`     | **Default.** Genuine bursts/re-shoots of the same moment.        |
| `0.4-0.5` | Starts merging *distinct* photos of the same scene/outing — risky for deletion. |

On a 1,293-photo test set, `0.5` chained an entire hiking outing (dozens of
different moments) into one 57-image "cluster", whereas `0.3` produced clean
bursts (e.g. nine consecutive frames at one signpost). Because a missed
duplicate is harmless but a false one deletes a unique photo, the default errs
strict. Verify visually before trusting large clusters — the [review
gallery](review.md) lets you sort by `cluster` and eyeball each group.

## Output

The report adds `cluster_id`, `cluster_size` and `is_keeper` columns so you can
audit every group. `--quarantine` moves `duplicate` files along with `delete`.

## How clustering works (and its caveat)

Clusters are formed by **single-link grouping**: any two photos within the
threshold are joined, transitively. That is why too high a threshold causes
*chaining* — A is close to B and B is close to C, so A, B and C all merge even
if A and C are quite different. Keep the threshold low and review large
clusters before deleting.
