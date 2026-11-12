## Study results

Ship rule: 1. G regressions: 0/15.

| Metric | Value | Bar | Result |
| --- | ---: | ---: | --- |
| accuracy_lift | 1.6 | >= 0.3 | PASS |
| findability_lift | 1.2 | >= 0.5 | PASS |
| j_regressions | 0 | <= 1 | PASS |
| gate_precision | 1 | >= 0.85 | PASS |
| gate_recall | 1 | >= 0.6 | PASS |
| confidence_separation | undefined | >= 0.15 | FAIL |
| schema_rejection_rate | 0 | <= 0.1 | PASS |
| decision_cost_usd | 0 | <= 0.05 | PASS |
| carry_precision | 0.818182 | >= 0.85 | FAIL |
| carry_recall | 0.818182 | >= 0.6 | PASS |
| sufficiency_precision | 0 | >= 0.85 | FAIL |
| sufficiency_recall | 0 | >= 0.6 | FAIL |

### Ship configuration

```yaml
code_index.community_label.enabled: true
ai.decisions.community_label.mode: off
ai.decisions.community_label_carry.mode: off
ai.decisions.community_label_sufficiency.mode: off
```
