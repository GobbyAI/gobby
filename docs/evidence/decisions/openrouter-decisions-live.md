## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "openrouter-decisions",
  "api_base": "https://openrouter.ai/api",
  "request_model": "typesafe/jev-1.14",
  "identity_contract": "response_version",
  "backend_max_state_tokens": 32000,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 328,
            "output_tokens": 31,
            "cost": 1.3776e-05
          }
        }
      ],
      "questions": {
        "color": {
          "type": "choice",
          "instructions": "Choose the square's color.",
          "criteria": {
            "red": "The square is red",
            "blue": "The square is blue"
          }
        }
      },
      "result": {
        "answers": {
          "color": {
            "choice": "red",
            "probabilities": {
              "red": 1.0,
              "blue": 0.0
            },
            "confidence": 1.0
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    },
    {
      "shape": "noul",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 288,
            "output_tokens": 20,
            "cost": 1.2096e-05
          }
        }
      ],
      "questions": {
        "red": {
          "type": "noul",
          "instructions": "The described square is red."
        }
      },
      "result": {
        "answers": {
          "red": {
            "probability": 0.99
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    },
    {
      "shape": "score",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 318,
            "output_tokens": 18,
            "cost": 1.3356e-05
          }
        }
      ],
      "questions": {
        "clarity": {
          "type": "score",
          "instructions": "Rate how specifically the description identifies the shape.",
          "criteria": [
            "vague",
            "clear",
            "clear and specific"
          ]
        }
      },
      "result": {
        "answers": {
          "clarity": {
            "score": 1.96,
            "probabilities": {
              "0": 0.0,
              "1": 0.04,
              "2": 0.96
            },
            "confidence": 0.94
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    },
    {
      "shape": "box",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 529,
            "output_tokens": 20,
            "cost": 2.2218e-05
          }
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "result": {
        "answers": {
          "square": {
            "top": 0.05,
            "bottom": 0.77,
            "left": 0.2,
            "right": 0.75,
            "confidence": 0.47
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    },
    {
      "probe": "chars-per-token",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 8192,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1769,
        "output_tokens": 20,
        "cost": 7.4298e-05
      },
      "chars_per_token": 4.6308648954211415
    },
    {
      "probe": "just-under",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 172642,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 31669,
        "output_tokens": 20,
        "cost": 0.001330098
      },
      "chars_per_token": 5.451450945719789
    },
    {
      "probe": "over-limit",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 183198,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 33588,
        "output_tokens": 20,
        "cost": 0.001410696
      },
      "chars_per_token": 5.454269381922115
    },
    {
      "probe": "pinned-model",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    },
    {
      "probe": "alias",
      "request_model": "typesafe/jev-1.14-20261014",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    }
  ],
  "measured_chars_per_token": 5.497986577181208,
  "limit_probe_chars_per_token": 5.497986577181208,
  "estimated_input_overhead_tokens": 279.0,
  "alias_matches_pinned_response": true,
  "canonical_slug": {
    "typesafe/jev-1.14": "typesafe/jev-1.14-20261014"
  },
  "privacy_policy": {
    "zdr": true,
    "data_collection": "deny"
  }
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "openrouter-decisions",
  "api_base": "https://openrouter.ai/api",
  "request_model": "typesafe/jev-1.14",
  "identity_contract": "response_version",
  "backend_max_state_tokens": 262144,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 328,
            "output_tokens": 31,
            "cost": 1.3776e-05
          }
        }
      ],
      "questions": {
        "color": {
          "type": "choice",
          "instructions": "Choose the square's color.",
          "criteria": {
            "red": "The square is red",
            "blue": "The square is blue"
          }
        }
      },
      "result": {
        "answers": {
          "color": {
            "choice": "red",
            "probabilities": {
              "red": 1.0,
              "blue": 0.0
            },
            "confidence": 1.0
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    },
    {
      "shape": "noul",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 288,
            "output_tokens": 20,
            "cost": 1.2096e-05
          }
        }
      ],
      "questions": {
        "red": {
          "type": "noul",
          "instructions": "The described square is red."
        }
      },
      "result": {
        "answers": {
          "red": {
            "probability": 0.99
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    },
    {
      "shape": "score",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 318,
            "output_tokens": 18,
            "cost": 1.3356e-05
          }
        }
      ],
      "questions": {
        "clarity": {
          "type": "score",
          "instructions": "Rate how specifically the description identifies the shape.",
          "criteria": [
            "vague",
            "clear",
            "clear and specific"
          ]
        }
      },
      "finding": {
        "reason": "parse",
        "detail": "score differs from the probability-weighted index"
      }
    },
    {
      "shape": "box",
      "request_model": "typesafe/jev-1.14",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "privacy_verified": true,
          "usage": {
            "input_tokens": 527,
            "output_tokens": 20,
            "cost": 2.2134e-05
          }
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "result": {
        "answers": {
          "square": {
            "top": 0.05,
            "bottom": 0.77,
            "left": 0.15,
            "right": 0.74,
            "confidence": 0.42
          }
        },
        "backend_identity": "response:typesafe/jev-1.14-20261014",
        "response_model": "typesafe/jev-1.14-20261014"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    },
    {
      "probe": "chars-per-token",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 8192,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1769,
        "output_tokens": 20,
        "cost": 7.4298e-05
      },
      "chars_per_token": 4.6308648954211415
    },
    {
      "probe": "just-under",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 1425317,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 259427,
        "output_tokens": 20,
        "cost": 0.010895934
      },
      "chars_per_token": 5.494096605210714
    },
    {
      "probe": "over-limit",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 1511793,
      "privacy_verified": true,
      "status": 400,
      "finding": "http_status: 400",
      "response_error": {
        "content_type": "application/json",
        "body": "{\"error\":{\"message\":\"HTTP 400: {\\\"detail\\\":{\\\"error_type\\\":\\\"max_tokens_exceeded\\\"}}\",\"code\":400}}"
      }
    },
    {
      "probe": "pinned-model",
      "request_model": "typesafe/jev-1.14",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    },
    {
      "probe": "alias",
      "request_model": "typesafe/jev-1.14-20261014",
      "state_chars": 4096,
      "privacy_verified": true,
      "status": 200,
      "response_model": "typesafe/jev-1.14-20261014",
      "usage": {
        "input_tokens": 1024,
        "output_tokens": 20,
        "cost": 4.3008e-05
      },
      "chars_per_token": 4.0
    }
  ],
  "measured_chars_per_token": 5.497986577181208,
  "limit_probe_chars_per_token": 5.497986577181208,
  "estimated_input_overhead_tokens": 279.0,
  "alias_matches_pinned_response": true,
  "canonical_slug": {
    "typesafe/jev-1.14": "typesafe/jev-1.14-20261014"
  },
  "privacy_policy": {
    "zdr": true,
    "data_collection": "deny"
  }
}
```
