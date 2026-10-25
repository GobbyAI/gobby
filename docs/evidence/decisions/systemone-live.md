## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 262144,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 145,
            "output_tokens": 0
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
              "blue": 0.0024786979141979484,
              "red": 0.997521302085802
            },
            "confidence": 0.9950426041716041
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 156,
            "output_tokens": 0
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
            "probability": 0.991786789772699
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 161,
            "output_tokens": 0
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
            "score": 1.7774596032657974,
            "probabilities": {
              "0": 0.009030336026689302,
              "1": 0.20447972468082393,
              "2": 0.7864899392924868
            },
            "confidence": 0.6661894048986962
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500"
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 500,
      "finding": "http_status: 500"
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 1038090,
      "status": 500,
      "finding": "http_status: 500"
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 1101004,
      "status": 500,
      "finding": "http_status: 500"
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500"
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500"
    }
  ],
  "measured_chars_per_token": null,
  "limit_probe_chars_per_token": 4.0,
  "estimated_input_overhead_tokens": null,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 262144,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 145,
            "output_tokens": 0
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
              "blue": 0.0024786979141979484,
              "red": 0.997521302085802
            },
            "confidence": 0.9950426041716041
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 156,
            "output_tokens": 0
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
            "probability": 0.991786789772699
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 161,
            "output_tokens": 0
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
            "score": 1.7774596032657974,
            "probabilities": {
              "0": 0.009030336026689302,
              "1": 0.20447972468082393,
              "2": 0.7864899392924868
            },
            "confidence": 0.6661894048986962
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "response_error": {
            "content_type": "application/json; charset=utf-8",
            "body": "{\n    \"error\": {\n        \"message\": \"- request.questions.square.type: Invalid discriminator value. Expected 'choice' | 'score' | 'noul'\",\n        \"type\": \"invalid_request_error\",\n        \"param\": \"questions.square.type\",\n        \"code\": \"invalid_value\"\n    }\n}"
          },
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (1637 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 1038090,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (188891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 1101004,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (200329 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    }
  ],
  "measured_chars_per_token": null,
  "limit_probe_chars_per_token": 4.0,
  "estimated_input_overhead_tokens": null,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 262144,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 145,
            "output_tokens": 0
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
              "blue": 0.0024786979141979484,
              "red": 0.997521302085802
            },
            "confidence": 0.9950426041716041
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 156,
            "output_tokens": 0
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
            "probability": 0.991786789772699
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 161,
            "output_tokens": 0
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
            "score": 1.7774596032657974,
            "probabilities": {
              "0": 0.009030336026689302,
              "1": 0.20447972468082393,
              "2": 0.7864899392924868
            },
            "confidence": 0.6661894048986962
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "response_error": {
            "content_type": "application/json; charset=utf-8",
            "body": "{\n    \"error\": {\n        \"message\": \"- request.questions.square.type: Invalid discriminator value. Expected 'choice' | 'score' | 'noul'\",\n        \"type\": \"invalid_request_error\",\n        \"param\": \"questions.square.type\",\n        \"code\": \"invalid_value\"\n    }\n}"
          },
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (1637 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 1038090,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (188891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 1101004,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (200329 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 500,
      "finding": "http_status: 500",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: input (891 tokens) is too large to process. increase the physical batch size (current batch size: 512)\",\n        \"type\": \"server_error\",\n        \"param\": null,\n        \"code\": \"500\"\n    }\n}"
      }
    }
  ],
  "measured_chars_per_token": null,
  "limit_probe_chars_per_token": 4.0,
  "estimated_input_overhead_tokens": null,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 65536,
  "backend_max_question_tokens": null,
  "long_context": false,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 145,
            "output_tokens": 0
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
              "blue": 0.0024786979141979484,
              "red": 0.997521302085802
            },
            "confidence": 0.9950426041716041
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 156,
            "output_tokens": 0
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
            "probability": 0.991786789772699
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 161,
            "output_tokens": 0
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
            "score": 1.7774596032657974,
            "probabilities": {
              "0": 0.009030336026689302,
              "1": 0.20447972468082393,
              "2": 0.7864899392924868
            },
            "confidence": 0.6661894048986962
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "response_error": {
            "content_type": "application/json; charset=utf-8",
            "body": "{\n    \"error\": {\n        \"message\": \"- request.questions.square.type: Invalid discriminator value. Expected 'choice' | 'score' | 'noul'\",\n        \"type\": \"invalid_request_error\",\n        \"param\": \"questions.square.type\",\n        \"code\": \"invalid_value\"\n    }\n}"
          },
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 200,
      "response_model": "clef",
      "usage": {
        "input_tokens": 891,
        "output_tokens": 0
      },
      "chars_per_token": 4.597081930415264
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 200,
      "response_model": "clef",
      "usage": {
        "input_tokens": 1637,
        "output_tokens": 0
      },
      "chars_per_token": 5.004276114844227
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 355438,
      "finding": "ReadTimeout: probe did not return valid JSON"
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 377028,
      "finding": "ReadTimeout: probe did not return valid JSON"
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "finding": "ReadTimeout: probe did not return valid JSON"
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "finding": "ReadTimeout: probe did not return valid JSON"
    }
  ],
  "measured_chars_per_token": 5.490616621983914,
  "limit_probe_chars_per_token": 5.490616621983914,
  "estimated_input_overhead_tokens": 145.0,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 65536,
  "backend_max_question_tokens": null,
  "long_context": false,
  "probe_timeout_seconds": 900.0,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [],
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
      "finding": {
        "reason": "timeout",
        "detail": "no answer within 30s"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [],
      "questions": {
        "red": {
          "type": "noul",
          "instructions": "The described square is red."
        }
      },
      "finding": {
        "reason": "timeout",
        "detail": "no answer within 30s"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [],
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
        "reason": "timeout",
        "detail": "no answer within 30s"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "response_error": {
            "content_type": "application/json; charset=utf-8",
            "body": "{\n    \"error\": {\n        \"message\": \"- request.questions.square.type: Invalid discriminator value. Expected 'choice' | 'score' | 'noul'\",\n        \"type\": \"invalid_request_error\",\n        \"param\": \"questions.square.type\",\n        \"code\": \"invalid_value\"\n    }\n}"
          },
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 259522,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 275251,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    }
  ],
  "measured_chars_per_token": null,
  "limit_probe_chars_per_token": 4.0,
  "estimated_input_overhead_tokens": null,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```
## Decision capture 2026-10-25

Service round trips validate all four shapes. Raw probes record usage and cost, chars-per-token, both input-limit responses and the alias comparison. A failed shape remains a finding; successful probes do not waive it.

The measured ratio uses the difference in state characters divided by the difference in reported input tokens across two probes, removing fixed question overhead. Limit-probe sizes account for the estimated overhead. If calibration is unavailable, the measured ratio is null and probes assume 4 characters per token with zero overhead. The responses establish whether the backend rejects or truncates.

OpenRouter privacy flags are checked on every service exchange and raw probe. Input scope, documented errors and usage: [OpenRouter Jev](https://openrouter.ai/docs/guides/community/jev.md), [decision API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request.md), [TypeSafe models](https://docs.typesafe.ai/models), and [local wire contracts](wire-contracts.md). Documentation references are those recorded in the wire contracts; this run measures the backend independently.

```json
{
  "wire_api": "systemone",
  "api_base": "http://127.0.0.1:1234",
  "request_model": "clef",
  "identity_contract": "model_card",
  "backend_max_state_tokens": 65536,
  "backend_max_question_tokens": null,
  "long_context": false,
  "probe_timeout_seconds": 900.0,
  "shapes": [
    {
      "shape": "choice",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 145,
            "output_tokens": 0
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
              "blue": 0.0024786979141979484,
              "red": 0.997521302085802
            },
            "confidence": 0.9950426041716041
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "noul",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 156,
            "output_tokens": 0
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
            "probability": 0.991786789772699
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "score",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 200,
          "usage": {
            "input_tokens": 161,
            "output_tokens": 0
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
            "score": 1.7774596032657974,
            "probabilities": {
              "0": 0.009030336026689302,
              "1": 0.20447972468082393,
              "2": 0.7864899392924868
            },
            "confidence": 0.6661894048986962
          }
        },
        "backend_identity": null,
        "response_model": "clef"
      }
    },
    {
      "shape": "box",
      "request_model": "clef",
      "state": {
        "description": "A red square is centered on a white background."
      },
      "exchanges": [
        {
          "status": 400,
          "response_error": {
            "content_type": "application/json; charset=utf-8",
            "body": "{\n    \"error\": {\n        \"message\": \"- request.questions.square.type: Invalid discriminator value. Expected 'choice' | 'score' | 'noul'\",\n        \"type\": \"invalid_request_error\",\n        \"param\": \"questions.square.type\",\n        \"code\": \"invalid_value\"\n    }\n}"
          },
          "usage": {}
        }
      ],
      "questions": {
        "square": {
          "type": "box",
          "instructions": "Locate the red square.",
          "image": "omitted"
        }
      },
      "finding": {
        "reason": "http_status",
        "detail": "HTTP 400"
      }
    }
  ],
  "probes": [
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 200,
      "response_model": "clef",
      "usage": {
        "input_tokens": 891,
        "output_tokens": 0
      },
      "chars_per_token": 4.597081930415264
    },
    {
      "probe": "chars-per-token",
      "request_model": "clef",
      "state_chars": 8192,
      "status": 200,
      "response_model": "clef",
      "usage": {
        "input_tokens": 1637,
        "output_tokens": 0
      },
      "chars_per_token": 5.004276114844227
    },
    {
      "probe": "just-under",
      "request_model": "clef",
      "state_chars": 355438,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "over-limit",
      "request_model": "clef",
      "state_chars": 377028,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "pinned-model",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    },
    {
      "probe": "alias",
      "request_model": "clef",
      "state_chars": 4096,
      "status": 504,
      "finding": "http_status: 504",
      "response_error": {
        "content_type": "application/json; charset=utf-8",
        "body": "{\n    \"error\": {\n        \"message\": \"clef: Engine protocol systemOne request timed out after 30000ms.\",\n        \"type\": \"engine_error\",\n        \"param\": null,\n        \"code\": \"request_timeout\"\n    }\n}"
      }
    }
  ],
  "measured_chars_per_token": 5.490616621983914,
  "limit_probe_chars_per_token": 5.490616621983914,
  "estimated_input_overhead_tokens": 145.0,
  "alias_matches_pinned_response": false,
  "canonical_slug": {},
  "privacy_policy": null
}
```

## Clef diagnostic 2026-10-25T21:30:10.721182+00:00 — http://127.0.0.1:1234 — audit

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T21:30:10.721182+00:00",
  "backend": "http://127.0.0.1:1234",
  "model": "clef",
  "phase": "audit",
  "wire_api": "systemone",
  "identity_contract": "model_card",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-0kh2ovmr",
  "rows": [],
  "diagnostic_timeout_seconds": 90,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "audit": {
    "/api/v1/models": {
      "status": 200,
      "body": {
        "models": [
          {
            "type": "decision",
            "publisher": "ggml-org",
            "key": "clef",
            "display_name": "Clef",
            "architecture": "clef",
            "quantization": {
              "name": "Q8_0",
              "bits_per_weight": 8
            },
            "size_bytes": 29663361696,
            "params_string": "27B",
            "max_context_length": 262144,
            "format": "gguf",
            "decision_type": "clef",
            "has_vision_adapter": true,
            "loaded_instances": [
              {
                "id": "clef",
                "config": {
                  "context_length": 262144,
                  "eval_batch_size": 65536,
                  "physical_batch_size": 65536,
                  "parallel": 4,
                  "flash_attention": true,
                  "offload_kv_cache_to_gpu": true
                }
              }
            ]
          },
          {
            "type": "decision",
            "publisher": "ggml-org",
            "key": "clef-flash",
            "display_name": "Clef Flash",
            "architecture": "clef",
            "quantization": {
              "name": "Q8_0",
              "bits_per_weight": 8
            },
            "size_bytes": 10578965120,
            "params_string": "9.1B",
            "max_context_length": 262144,
            "format": "gguf",
            "decision_type": "clef",
            "has_vision_adapter": true,
            "loaded_instances": []
          },
          {
            "type": "llm",
            "publisher": "openai",
            "key": "openai/gpt-oss-20b",
            "display_name": "GPT-OSS 20B",
            "architecture": "gpt_oss",
            "quantization": {
              "name": "MXFP4",
              "bits_per_weight": 4
            },
            "size_bytes": 12104314420,
            "params_string": "20B",
            "loaded_instances": [],
            "max_context_length": 131072,
            "format": "mlx",
            "capabilities": {
              "vision": false,
              "trained_for_tool_use": true,
              "reasoning": {
                "allowed_options": [
                  "low",
                  "medium",
                  "high"
                ],
                "default": "low"
              }
            },
            "description": null,
            "variants": [
              "openai/gpt-oss-20b@mxfp4"
            ],
            "selected_variant": "openai/gpt-oss-20b@mxfp4"
          },
          {
            "type": "llm",
            "publisher": "qwen",
            "key": "qwen/qwen3.8-27b",
            "display_name": "Qwen3.8 27B",
            "architecture": "qwen3_5",
            "quantization": {
              "name": "8bit",
              "bits_per_weight": 8
            },
            "size_bytes": 29528355154,
            "params_string": "27B",
            "loaded_instances": [],
            "max_context_length": 262144,
            "format": "mlx",
            "capabilities": {
              "vision": true,
              "trained_for_tool_use": true,
              "reasoning": {
                "allowed_options": [
                  "off",
                  "low",
                  "medium",
                  "xhigh",
                  "on"
                ],
                "default": "xhigh"
              }
            },
            "description": null,
            "variants": [
              "qwen/qwen3.8-27b@4bit",
              "qwen/qwen3.8-27b@8bit"
            ],
            "selected_variant": "qwen/qwen3.8-27b@8bit"
          },
          {
            "type": "llm",
            "publisher": "prism-ml",
            "key": "prism-ml/bonsai-27b",
            "display_name": "Bonsai 27B",
            "architecture": "qwen3_5",
            "quantization": {
              "name": "2bit",
              "bits_per_weight": 2
            },
            "size_bytes": 8521076756,
            "params_string": "27B",
            "loaded_instances": [],
            "max_context_length": 262144,
            "format": "mlx",
            "capabilities": {
              "vision": true,
              "trained_for_tool_use": true,
              "reasoning": {
                "allowed_options": [
                  "off",
                  "on"
                ],
                "default": "on"
              }
            },
            "description": null,
            "variants": [
              "prism-ml/bonsai-27b@2bit"
            ],
            "selected_variant": "prism-ml/bonsai-27b@2bit"
          },
          {
            "type": "llm",
            "publisher": "meta",
            "key": "meta/muse-glimmer",
            "display_name": "Muse Glimmer",
            "architecture": "muse-glimmer",
            "quantization": {
              "name": "Q4_K_M",
              "bits_per_weight": 4
            },
            "size_bytes": 18157122004,
            "params_string": "28B",
            "loaded_instances": [],
            "max_context_length": 131072,
            "format": "gguf",
            "capabilities": {
              "vision": true,
              "trained_for_tool_use": true,
              "reasoning": {
                "allowed_options": [
                  "low",
                  "medium",
                  "high",
                  "xhigh"
                ],
                "default": "high"
              }
            },
            "description": null,
            "variants": [
              "meta/muse-glimmer@q4_k_m"
            ],
            "selected_variant": "meta/muse-glimmer@q4_k_m"
          },
          {
            "type": "embedding",
            "publisher": "Qwen",
            "key": "text-embedding-qwen3-embedding-8b@q8_0",
            "display_name": "Qwen3 Embedding 8B",
            "quantization": {
              "name": "Q8_0",
              "bits_per_weight": 8
            },
            "size_bytes": 8047105824,
            "params_string": "8B",
            "loaded_instances": [],
            "max_context_length": 40960,
            "format": "gguf"
          },
          {
            "type": "embedding",
            "publisher": "Qwen",
            "key": "text-embedding-qwen3-embedding-8b@f16",
            "display_name": "Qwen3 Embedding 8B",
            "quantization": {
              "name": "F16",
              "bits_per_weight": 16
            },
            "size_bytes": 15141156384,
            "params_string": "8B",
            "loaded_instances": [],
            "max_context_length": 40960,
            "format": "gguf"
          },
          {
            "type": "embedding",
            "publisher": "nomic-ai",
            "key": "text-embedding-nomic-embed-text-v1.5@f16",
            "display_name": "Nomic Embed Text v1.5",
            "quantization": {
              "name": "F16",
              "bits_per_weight": 16
            },
            "size_bytes": 274290560,
            "params_string": null,
            "loaded_instances": [
              {
                "id": "text-embedding-nomic-embed-text-v1.5@f16",
                "config": {
                  "context_length": 2048
                }
              }
            ],
            "max_context_length": 2048,
            "format": "gguf"
          },
          {
            "type": "llm",
            "publisher": "google",
            "key": "google/gemma-4-31b",
            "display_name": "Gemma 4 31B",
            "architecture": "gemma4",
            "quantization": {
              "name": "8bit",
              "bits_per_weight": 8
            },
            "size_bytes": 33795677782,
            "params_string": "31B",
            "loaded_instances": [],
            "max_context_length": 262144,
            "format": "mlx",
            "capabilities": {
              "vision": true,
              "trained_for_tool_use": true,
              "reasoning": {
                "allowed_options": [
                  "off",
                  "on"
                ],
                "default": "on"
              }
            },
            "description": null,
            "variants": [
              "google/gemma-4-31b@8bit"
            ],
            "selected_variant": "google/gemma-4-31b@8bit"
          },
          {
            "type": "embedding",
            "publisher": "nomic-ai",
            "key": "text-embedding-nomic-embed-text-v1.5@q4_k_m",
            "display_name": "Nomic Embed Text v1.5",
            "quantization": {
              "name": "Q4_K_M",
              "bits_per_weight": 4
            },
            "size_bytes": 84106624,
            "params_string": null,
            "loaded_instances": [],
            "max_context_length": 2048,
            "format": "gguf"
          }
        ]
      }
    },
    "/api/v0/models": {
      "status": 200,
      "body": {
        "data": [
          {
            "id": "text-embedding-nomic-embed-text-v1.5@f16",
            "object": "model",
            "type": "embeddings",
            "publisher": "nomic-ai",
            "arch": "nomic-bert",
            "compatibility_type": "gguf",
            "quantization": "F16",
            "state": "loaded",
            "max_context_length": 2048,
            "loaded_context_length": 2048
          },
          {
            "id": "clef",
            "object": "model",
            "type": "decision",
            "publisher": "ggml-org",
            "arch": "clef",
            "compatibility_type": "gguf",
            "quantization": "Q8_0",
            "state": "loaded",
            "max_context_length": 262144,
            "loaded_context_length": 262144
          },
          {
            "id": "clef-flash",
            "object": "model",
            "type": "decision",
            "publisher": "ggml-org",
            "arch": "clef",
            "compatibility_type": "gguf",
            "quantization": "Q8_0",
            "state": "not-loaded",
            "max_context_length": 262144
          },
          {
            "id": "openai/gpt-oss-20b",
            "object": "model",
            "type": "llm",
            "publisher": "openai",
            "arch": "gpt_oss",
            "compatibility_type": "mlx",
            "quantization": "MXFP4",
            "state": "not-loaded",
            "max_context_length": 131072,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "qwen/qwen3.8-27b",
            "object": "model",
            "type": "vlm",
            "publisher": "qwen",
            "arch": "qwen3_5",
            "compatibility_type": "mlx",
            "quantization": "8bit",
            "state": "not-loaded",
            "max_context_length": 262144,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "prism-ml/bonsai-27b",
            "object": "model",
            "type": "vlm",
            "publisher": "prism-ml",
            "arch": "qwen3_5",
            "compatibility_type": "mlx",
            "quantization": "2bit",
            "state": "not-loaded",
            "max_context_length": 262144,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "meta/muse-glimmer",
            "object": "model",
            "type": "vlm",
            "publisher": "meta",
            "arch": "muse-glimmer",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "not-loaded",
            "max_context_length": 131072,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "text-embedding-qwen3-embedding-8b@q8_0",
            "object": "model",
            "type": "embeddings",
            "publisher": "Qwen",
            "arch": "qwen3",
            "compatibility_type": "gguf",
            "quantization": "Q8_0",
            "state": "not-loaded",
            "max_context_length": 40960,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "text-embedding-qwen3-embedding-8b@f16",
            "object": "model",
            "type": "embeddings",
            "publisher": "Qwen",
            "arch": "qwen3",
            "compatibility_type": "gguf",
            "quantization": "F16",
            "state": "not-loaded",
            "max_context_length": 40960,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "google/gemma-4-31b",
            "object": "model",
            "type": "vlm",
            "publisher": "google",
            "arch": "gemma4",
            "compatibility_type": "mlx",
            "quantization": "8bit",
            "state": "not-loaded",
            "max_context_length": 262144,
            "capabilities": [
              "tool_use"
            ]
          },
          {
            "id": "text-embedding-nomic-embed-text-v1.5@q4_k_m",
            "object": "model",
            "type": "embeddings",
            "publisher": "nomic-ai",
            "arch": "nomic-bert",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "not-loaded",
            "max_context_length": 2048
          }
        ],
        "object": "list"
      }
    },
    "/v1/models": {
      "status": 200,
      "body": {
        "data": [
          {
            "id": "text-embedding-nomic-embed-text-v1.5@f16",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "clef",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "clef-flash",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "openai/gpt-oss-20b",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "qwen/qwen3.8-27b",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "prism-ml/bonsai-27b",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "meta/muse-glimmer",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "text-embedding-qwen3-embedding-8b@q8_0",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "text-embedding-qwen3-embedding-8b@f16",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "google/gemma-4-31b",
            "object": "model",
            "owned_by": "organization_owner"
          },
          {
            "id": "text-embedding-nomic-embed-text-v1.5@q4_k_m",
            "object": "model",
            "owned_by": "organization_owner"
          }
        ],
        "object": "list"
      }
    },
    "lms ps --json": [
      {
        "type": "embedding",
        "modelKey": "text-embedding-nomic-embed-text-v1.5@f16",
        "format": "gguf",
        "displayName": "Nomic Embed Text v1.5",
        "publisher": "nomic-ai",
        "path": "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.f16.gguf",
        "sizeBytes": 274290560,
        "indexedModelIdentifier": "nomic-ai/nomic-embed-text-v1.5-GGUF/nomic-embed-text-v1.5.f16.gguf",
        "deviceIdentifier": null,
        "architecture": "nomic-bert",
        "quantization": {
          "name": "F16",
          "bits": 16
        },
        "identifier": "text-embedding-nomic-embed-text-v1.5@f16",
        "ttlMs": null,
        "lastUsedTime": null,
        "maxContextLength": 2048,
        "contextLength": 2048,
        "status": "computingEmbedding",
        "queued": 123,
        "parallel": null,
        "engineConfigFileEnabled": false
      },
      {
        "type": "decision",
        "modelKey": "clef",
        "format": "gguf",
        "displayName": "Clef",
        "publisher": "ggml-org",
        "path": "ggml-org/Clef-GGUF/Clef-Q8_0.gguf",
        "sizeBytes": 29663361696,
        "indexedModelIdentifier": "ggml-org/Clef-GGUF/Clef-Q8_0.gguf",
        "deviceIdentifier": null,
        "paramsString": "27B",
        "architecture": "clef",
        "quantization": {
          "name": "Q8_0",
          "bits": 8
        },
        "identifier": "clef",
        "ttlMs": null,
        "lastUsedTime": 1792962521378,
        "decisionType": "clef",
        "maxContextLength": 262144,
        "hasVisionAdapter": true,
        "contextLength": 262144,
        "status": "idle",
        "queued": 0,
        "parallel": 4,
        "engineConfigFileEnabled": false
      }
    ],
    "lms server status --json": {
      "running": true,
      "port": 1234
    }
  },
  "load_fields_required": [
    "parallel slots",
    "context per slot",
    "physicalBatchSize",
    "evalBatchSize",
    "flash attention",
    "KV offload"
  ],
  "missing_fields_are_unknown": true
}
```

## Clef diagnostic 2026-10-25T21:30:59.423522+00:00 — http://127.0.0.1:1234 — small

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T21:30:59.423522+00:00",
  "backend": "http://127.0.0.1:1234",
  "model": "clef",
  "phase": "small",
  "wire_api": "systemone",
  "identity_contract": "model_card",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-woxrd2h6",
  "rows": [
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:30:59.631425+00:00",
      "valid": false,
      "error_type": "DecisionsUnavailable",
      "reason": "timeout",
      "elapsed_seconds": 90.01687433396,
      "exchanges": [
        {
          "status": 504,
          "model": null,
          "usage": null
        },
        {
          "status": 504,
          "model": null,
          "usage": null
        }
      ]
    }
  ],
  "diagnostic_timeout_seconds": 90.0,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "config_snapshot": {
    "api_base": "http://127.0.0.1:1234",
    "wire_api": "systemone",
    "identity_contract": "model_card",
    "model": "clef",
    "timeout_seconds": 90.0,
    "max_input_tokens": 8192,
    "backend_max_state_tokens": 65536,
    "backend_max_question_tokens": null,
    "long_context": false,
    "failure_cooldown_seconds": 0.0,
    "community_label": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_confidence": 0.5
    },
    "community_label_carry": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "community_label_sufficiency": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_score": 0.5
    },
    "tool_rerank": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "found_work": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "accept_below": 0.1,
      "accept_above": 0.9
    }
  },
  "stopped": {
    "error_type": "DecisionsUnavailable",
    "detail": "timeout: no answer within 90s"
  }
}
```

## Clef diagnostic 2026-10-25T21:37:55.680438+00:00 — http://127.0.0.1:11434 — audit

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T21:37:55.680438+00:00",
  "backend": "http://127.0.0.1:11434",
  "model": "clef:27b",
  "phase": "audit",
  "wire_api": "systemone",
  "identity_contract": "model_card",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-w345g86v",
  "rows": [],
  "diagnostic_timeout_seconds": 90,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "audit": {
    "/api/version": {
      "status": 200,
      "body": {
        "version": "0.40.11"
      }
    },
    "/api/tags": {
      "status": 200,
      "body": {
        "models": [
          {
            "name": "clef:27b",
            "model": "clef:27b",
            "modified_at": "2026-10-25T16:37:41.002559958-05:00",
            "size": 18107132722,
            "digest": "039c167bd0ec3719dd055f448dd55ea2365330bc05f2737e042ea0d23659756b",
            "details": {
              "parent_model": "",
              "format": "safetensors",
              "family": "",
              "families": null,
              "parameter_size": "",
              "quantization_level": "",
              "runner": "mlx"
            },
            "capabilities": [
              "decision",
              "vision"
            ]
          },
          {
            "name": "gemma4:31b",
            "model": "gemma4:31b",
            "modified_at": "2026-04-14T21:52:35.701358443-05:00",
            "size": 19868981791,
            "digest": "6316f0629137b426c9d9b853ffc4c8209589f30ee39aebede6285096c0ff47e7",
            "details": {
              "parent_model": "",
              "format": "gguf",
              "family": "gemma4",
              "families": [
                "gemma4"
              ],
              "parameter_size": "31.3B",
              "quantization_level": "Q4_K_M",
              "context_length": 262144,
              "embedding_length": 5376,
              "runner": "ggml"
            },
            "capabilities": [
              "completion",
              "vision",
              "tools",
              "thinking"
            ]
          },
          {
            "name": "nomic-embed-text:latest",
            "model": "nomic-embed-text:latest",
            "modified_at": "2026-04-05T17:35:16.03874095-05:00",
            "size": 274302450,
            "digest": "0a109f422b47e3a30ba2b10eca18548e944e8a23073ee3f3e947efcf3c45e59f",
            "details": {
              "parent_model": "",
              "format": "gguf",
              "family": "nomic-bert",
              "families": [
                "nomic-bert"
              ],
              "parameter_size": "137M",
              "quantization_level": "F16",
              "context_length": 2048,
              "embedding_length": 768,
              "runner": "ggml"
            },
            "capabilities": [
              "embedding"
            ]
          }
        ]
      }
    }
  },
  "load_fields_required": [
    "parallel slots",
    "context per slot",
    "physicalBatchSize",
    "evalBatchSize",
    "flash attention",
    "KV offload"
  ],
  "missing_fields_are_unknown": true
}
```

### Run 7: clean-reload small-request baseline (2026-10-25)

After a clean Clef reload, LM Studio completed 193 rows: 9 warmups, 180 measured calls, and 4 raw calibration/identity probes. Exit 0; no HTTP 504s. Both Clef and Nomic were IDLE at the start. The recorded load remained context_length=262144, parallel=4, physical_batch_size=65536, eval_batch_size=65536, flash attention enabled, KV cache on GPU. Per-slot context and queue cancellation behavior were not captured.

| Application token target | Shape | Measured input_tokens range | p50 seconds | p95 seconds | Calls <=1 s / 20 |
| --- | --- | --- | --- | --- | --- |
| 512 | Choice | 513–522 | 0.8945 | 0.9800 | 19 |
| 512 | Noul | 535–548 | 1.0036 | 1.0166 | 6 |
| 512 | Score | 525–537 | 1.0771 | 1.1610 | 0 |
| 2048 | Choice | 1627–1638 | 2.9544 | 3.0273 | 0 |
| 2048 | Noul | 1653–1665 | 2.7651 | 2.9796 | 0 |
| 2048 | Score | 1642–1653 | 2.7307 | 2.8745 | 0 |
| 8192 | Choice | 6097–6110 | 10.7913 | 11.6437 | 0 |
| 8192 | Noul | 6123–6133 | 10.6500 | 11.3931 | 0 |
| 8192 | Score | 6113–6120 | 10.4222 | 11.4238 | 0 |

Each cell excludes its warmup and includes 20 samples. p95 is the nearest-rank 19th sample. Inputs are synthetic padded red-square states with a unique nonce, not community-label production requests. Token targets are Gobby estimates; measured counts come from response usage.input_tokens. Timings cover the complete DecisionService call, including HTTP/model-card identity work; they do not isolate prefill. No measured 750 tokens/s prefill result or guaranteed 512–768-token latency budget is established.

The successful reload supports a state-dependent failure in the earlier run. It does not prove that run 6's timed-out large requests remained queued or caused the later first-call 504; request cancellation/slot-release traces and embedding overlap are missing. Shared Nomic embedding contention remains a possible confound.

LM Studio large phases were skipped under the Orchestrator's ruling. Prior large-request HTTP 504s remain failed observations, not an expected successful boundary test. No large-request success, cancellation recovery, or 64k latency is claimed.

Artifacts: /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-cbes45jw. Raw capture below: Clef diagnostic 2026-10-25T21:34:50.247409+00:00.

## Clef diagnostic 2026-10-25T21:34:50.247409+00:00 — http://127.0.0.1:1234 — small

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T21:34:50.247409+00:00",
  "backend": "http://127.0.0.1:1234",
  "model": "clef",
  "phase": "small",
  "wire_api": "systemone",
  "identity_contract": "model_card",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-cbes45jw",
  "rows": [
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:34:50.280307+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.6978217090363614,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 513,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:51.978289+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8258443329832517,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 520,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:52.804233+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8162947499658912,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 514,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:53.620624+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8147711660130881,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:54.435493+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8291768750059418,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 522,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:55.264768+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8383414169657044,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 522,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:56.103222+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9164143329835497,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:57.019737+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9102289589936845,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:57.930063+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8937257080106065,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 515,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:58.823907+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8929523339611478,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:34:59.716961+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8858367080101743,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:00.602906+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8940782499848865,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:01.497091+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9073872090084478,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:02.404763+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8909354169736616,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:03.295816+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.8950154579943046,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 520,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:04.190934+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9070695000118576,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:05.098286+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0057214589905925,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:06.104131+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9800212919944897,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:07.084266+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9688955840538256,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 513,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:08.053309+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9598714589956217,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:09.013454+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9552061249851249,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:35:09.968795+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9574682500096969,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:10.926495+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9785113329999149,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:11.905116+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9861238329904154,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 538,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:12.891522+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9943761669564992,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 541,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:13.886005+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9950937089743093,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:14.881202+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9931690419907682,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:15.874476+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.005570458015427,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 545,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:16.880144+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0015247079427354,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:17.881768+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0077348339837044,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:18.889691+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0029010829748586,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:19.892709+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0013586250133812,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:20.894238+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 0.9973087080288678,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:21.891660+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0050624170107767,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:22.897462+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0043807090260088,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:23.902043+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.001607415964827,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 535,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:24.903788+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.012275832996238,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:25.916231+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0072387920226902,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 541,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:26.923578+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0085414169589058,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 541,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:27.932407+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0148646660381928,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 545,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:28.947458+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0203144580009393,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 548,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:29.967869+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0165947909699753,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:35:30.984595+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0291409169440158,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 530,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:32.013841+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0767395420116372,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 526,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:33.090687+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0768701249617152,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 531,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:34.167676+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0795334160211496,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:35.247325+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.077469625044614,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 530,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:36.324899+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0762263750075363,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 528,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:37.401243+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0772838750272058,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:38.478630+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0784101670142263,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 529,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:39.557146+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0786347080138512,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 537,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:40.636225+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0741976670105942,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:41.710545+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.075322667020373,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 529,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:42.785973+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0789840830257162,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 535,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:43.865072+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0809170419815928,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:44.946286+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.073687250027433,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:46.020092+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.2044225829886273,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 528,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:47.224623+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.160988250048831,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 527,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:48.385715+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.1052312920219265,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 525,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:49.491142+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0656304170261137,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 534,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:50.556951+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.053928708017338,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 534,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:51.611077+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0495615000254475,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:52.660745+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 1.0616583339869976,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:35:53.722670+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9907254579593427,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:56.714269+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 3.0081545000430197,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1632,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:35:59.723074+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 3.0021976659772918,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1632,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:02.725756+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.97706162504619,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:05.703265+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9612877080217004,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:08.665084+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9268890420207754,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:11.592729+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.869580667000264,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:14.463550+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.8149179170140997,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:17.278880+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.8085893749957904,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:20.088044+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.751948833989445,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1635,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:22.841063+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 3.0303934999974445,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:25.871932+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9956530839554034,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:28.868048+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.974558874964714,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1633,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:31.843452+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.937465582974255,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1633,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:34.781674+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.994460957997944,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1635,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:37.776795+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9475972920190543,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1638,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:40.724856+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.916281459038146,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:43.641586+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.943849458999466,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1629,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:46.585898+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.962614374991972,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1627,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:49.548969+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 3.02730708301533,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1638,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:52.576682+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9107112500350922,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1632,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:36:55.487668+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.8566697080386803,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1655,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:36:58.344888+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 3.0127413750160486,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1661,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:01.358540+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7928501659771428,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1661,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:04.151920+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.769152667024173,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1653,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:06.921789+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7222806659992784,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1663,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:09.644585+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.713524416962173,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1653,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:12.359696+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.733038124977611,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1659,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:15.093571+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.761019666970242,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1656,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:17.854862+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7987768329912797,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1653,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:20.654438+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.6969062500284053,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1663,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:23.352888+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9796339999884367,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1665,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:26.333312+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.820512874983251,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1662,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:29.154238+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7892847909824923,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1656,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:31.944350+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7911390410154127,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1660,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:34.735997+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.781459499965422,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1661,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:37.518714+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.720258167013526,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1660,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:40.239637+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7582485000020824,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1658,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:42.998398+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.757054416986648,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1654,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:45.756244+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7723147500073537,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1661,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:48.529301+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7602741249720566,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1655,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:51.290319+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7470295829698443,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1662,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:37:54.037887+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7116960419807583,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1649,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:56.750028+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.6867611659690738,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1653,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:37:59.437397+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.692386374983471,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:02.130239+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7319284999975935,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:04.862801+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.769020542036742,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:07.632802+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.733190333994571,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1642,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:10.366757+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.685134041996207,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1646,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:13.052511+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.6755189999821596,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:15.728478+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7127002920024097,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1652,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:18.442025+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.769454749999568,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1646,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:21.212242+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.856351500027813,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:24.069350+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.874451458978001,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1649,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:26.944787+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.9074878340470605,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1649,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:29.853017+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.8198102089809254,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:32.673586+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7113395829801448,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:35.385822+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7550587079604156,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1647,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:38.141395+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7927683329908177,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1653,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:40.934898+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.7294177500298247,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:43.665261+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.72040454094531,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:46.386630+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.706693042011466,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:38:49.093773+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 2.6430479169939645,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:38:51.739672+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.220858165994287,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6106,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:02.962194+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.779803624958731,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6110,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:13.743467+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.820802416012157,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6100,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:24.566154+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.911800125031732,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6106,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:35.479392+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.008097584010102,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6104,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:46.489691+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.026073749992065,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:39:57.517495+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.885280041024089,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:40:09.405134+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.016295499983244,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:40:20.424260+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.718102624989115,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6100,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:40:31.144541+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.375988374988083,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:40:42.523287+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.64367137500085,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:40:54.168652+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.80003029096406,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6101,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:04.970968+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.672640624979977,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6104,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:15.646442+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.782630250032526,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6102,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:26.430763+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.940785999991931,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:37.373889+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.734278957999777,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6104,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:48.111807+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.584092917037196,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:41:58.699164+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.730961250024848,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:42:09.432941+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.52707129198825,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:42:19.962222+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.672564457985573,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6097,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:42:30.637204+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.69620229199063,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:42:41.335659+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.439513624995016,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6131,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:42:51.777871+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.461216250027064,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:02.241636+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.345789166982286,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:12.589316+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.389811915985774,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6130,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:22.981794+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.655234916950576,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:33.638780+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.478897999972105,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:44.145766+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.495243332989048,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:43:54.658060+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.492813458025921,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:05.154104+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.686967707995791,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:15.845388+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.424802916997578,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6125,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:26.274256+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.485146833991166,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6126,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:36.772077+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.713216750009451,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6133,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:47.489192+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.39309016696643,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6131,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:44:58.885672+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.679749833012465,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:45:09.568319+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.643480208003893,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6123,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:45:20.213193+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.932636042009108,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6132,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:45:31.150320+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.6447126660496,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:45:41.798315+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.7302687499905,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6125,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:45:52.534524+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.749306290992536,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:46:03.286215+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.336016000015661,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:46:14.625298+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.557520875008777,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:46:26.185446+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.55044587503653,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6122,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:46:37.739109+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.341520124988165,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6120,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:46:49.086954+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.423785291030072,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:00.514088+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 11.557974541967269,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:12.074742+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.65804804203799,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6117,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:22.735196+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.300191457965411,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:33.038392+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.674133000022266,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6115,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:43.715609+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.8293819159735,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6113,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:47:54.546661+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.761734375031665,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:05.310029+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.627265874994919,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6117,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:15.939203+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.464508291974198,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:26.406195+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.379951416980475,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6119,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:36.789666+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.4727474579704,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6115,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:47.265213+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.254785500001162,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6117,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:48:57.523655+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.31247224996332,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6120,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:07.841478+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.340050540980883,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6115,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:18.183879+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.306739250023384,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:28.494629+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.226861540984828,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:38.723613+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.318943249993026,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6120,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:49.046132+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.282222250010818,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6118,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:49:59.331641+00:00",
      "valid": true,
      "response_model": "clef",
      "identity": null,
      "elapsed_seconds": 10.292984750005417,
      "exchanges": [
        {
          "status": 200,
          "model": "clef",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "label": "calibration-4096",
      "transport": "curl",
      "started_utc": "2026-10-25T21:50:09.626272+00:00",
      "request_sha256": "f9713dd70ca40a92157b46875a62523c1f89afbf588d9e18586dadff92a9242b",
      "request_bytes": 4216,
      "state_chars": 4096,
      "request_model": "clef",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.001075 1.332464 1.332683",
      "status": 200,
      "elapsed_seconds": 1.538686375017278,
      "response": {
        "model": "clef",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9918513027806852
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        }
      }
    },
    {
      "label": "calibration-8192",
      "transport": "curl",
      "started_utc": "2026-10-25T21:50:11.167764+00:00",
      "request_sha256": "89b1f518199a3df01688cb576f68604a1d4f15991a7ac66dde448b1e6195bc38",
      "request_bytes": 8312,
      "state_chars": 8192,
      "request_model": "clef",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000820 2.490769 2.491093",
      "status": 200,
      "elapsed_seconds": 2.546285916992929,
      "response": {
        "model": "clef",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9920270440748284
          }
        },
        "usage": {
          "input_tokens": 1637,
          "output_tokens": 0
        }
      }
    },
    {
      "label": "pinned-model",
      "transport": "curl",
      "started_utc": "2026-10-25T21:50:13.715199+00:00",
      "request_sha256": "f9713dd70ca40a92157b46875a62523c1f89afbf588d9e18586dadff92a9242b",
      "request_bytes": 4216,
      "state_chars": 4096,
      "request_model": "clef",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000545 1.314697 1.314833",
      "status": 200,
      "elapsed_seconds": 1.4535924170049839,
      "response": {
        "model": "clef",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9918513027806852
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        }
      }
    },
    {
      "label": "alias",
      "transport": "curl",
      "started_utc": "2026-10-25T21:50:15.170474+00:00",
      "request_sha256": "f9713dd70ca40a92157b46875a62523c1f89afbf588d9e18586dadff92a9242b",
      "request_bytes": 4216,
      "state_chars": 4096,
      "request_model": "clef",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.002021 1.343197 1.343364",
      "status": 200,
      "elapsed_seconds": 1.4902372909709811,
      "response": {
        "model": "clef",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9918513027806852
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        }
      }
    }
  ],
  "diagnostic_timeout_seconds": 90.0,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "config_snapshot": {
    "api_base": "http://127.0.0.1:1234",
    "wire_api": "systemone",
    "identity_contract": "model_card",
    "model": "clef",
    "timeout_seconds": 90.0,
    "max_input_tokens": 8192,
    "backend_max_state_tokens": 65536,
    "backend_max_question_tokens": null,
    "long_context": false,
    "failure_cooldown_seconds": 0.0,
    "community_label": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_confidence": 0.5
    },
    "community_label_carry": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "community_label_sufficiency": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_score": 0.5
    },
    "tool_rerank": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "found_work": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "accept_below": 0.1,
      "accept_above": 0.9
    }
  },
  "summary": [
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 0.8945468539895955,
      "p95_seconds": 0.9800212919944897,
      "within_1s": 19,
      "max_seconds": 1.0057214589905925
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 1.0036408960004337,
      "p95_seconds": 1.0165947909699753,
      "within_1s": 6,
      "max_seconds": 1.0203144580009393
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 1.0770769999944605,
      "p95_seconds": 1.160988250048831,
      "within_1s": 0,
      "max_seconds": 1.2044225829886273
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 2.9544425000203773,
      "p95_seconds": 3.02730708301533,
      "within_1s": 0,
      "max_seconds": 3.0303934999974445
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 2.7650861669972073,
      "p95_seconds": 2.9796339999884367,
      "within_1s": 0,
      "max_seconds": 3.0127413750160486
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 2.730673125013709,
      "p95_seconds": 2.874451458978001,
      "within_1s": 0,
      "max_seconds": 2.9074878340470605
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 10.791330270498293,
      "p95_seconds": 11.64367137500085,
      "within_1s": 0,
      "max_seconds": 11.885280041024089
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 10.649973791500088,
      "p95_seconds": 11.39309016696643,
      "within_1s": 0,
      "max_seconds": 11.557520875008777
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 10.422229854477337,
      "p95_seconds": 11.423785291030072,
      "within_1s": 0,
      "max_seconds": 11.557974541967269
    }
  ]
}
```

### Run 8: Ollama small-request baseline (2026-10-25)

Ollama 0.40.11 with clef:27b and response_version identity completed 193 rows: 9 warmups, 180 measured calls, and 4 raw probes. Exit 0; no errors. All 60 measured calls at the 512 application-token target finished within 1 second.

| Application token target | Shape | Measured input_tokens range | p50 seconds | p95 seconds | Calls <=1 s / 20 |
| --- | --- | --- | --- | --- | --- |
| 512 | Choice | 513–521 | 0.5887 | 0.6369 | 20 |
| 512 | Noul | 535–544 | 0.6089 | 0.6231 | 20 |
| 512 | Score | 526–536 | 0.6107 | 0.6155 | 20 |
| 2048 | Choice | 1628–1639 | 1.8836 | 1.9602 | 0 |
| 2048 | Noul | 1652–1662 | 1.9436 | 1.9897 | 0 |
| 2048 | Score | 1642–1651 | 1.9319 | 1.9781 | 0 |
| 8192 | Choice | 6093–6109 | 7.1664 | 7.2836 | 0 |
| 8192 | Noul | 6121–6130 | 7.2172 | 7.3883 | 0 |
| 8192 | Score | 6114–6121 | 7.1268 | 7.3886 | 0 |

The same sampling and token-count caveats as run 7 apply. Backend-reported usage differs from the application estimate. These total-call measurements do not establish a 1,100 tokens/s prefill rate or a measured 1,000-token latency ceiling.

The raw 4096-character probe reported 891 input tokens, 0 cached tokens, and 1.0706 s total. The 8192-character probe reported 1637 input tokens, 37 cached tokens, and 1.8408 s. Pinned/alias probes repeated the 4096-character body byte-for-byte (SHA256 3c60d4340fa15a1a9f69ee70a7a79eb878ed48b34399b121c810cfe7b4272c9a), reported prompt_eval_cached_count=891, and took 0.0349/0.0286 s. Those are full-input cache hits, not uncached throughput measurements or evidence that differently named aliases resolve identically: both requested clef:27b.

Ollama large phases were not run; no large-input success or failure is inferred. Representative community-label Choice requests and quality-preserving input reductions remain unmeasured.

Artifacts: /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-9vw0t9jh. Audit: /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-w345g86v. Raw capture: Clef diagnostic 2026-10-25T21:51:07.867711+00:00.

## Clef diagnostic 2026-10-25T21:51:07.867711+00:00 — http://127.0.0.1:11434 — small

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T21:51:07.867711+00:00",
  "backend": "http://127.0.0.1:11434",
  "model": "clef:27b",
  "phase": "small",
  "wire_api": "systemone",
  "identity_contract": "response_version",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-9vw0t9jh",
  "rows": [
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:51:08.368028+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 10.88070191704901,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 522,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:19.249151+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.636884875013493,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:19.886281+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5440794579917565,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:20.430551+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6104526249691844,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 515,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:21.041188+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5478310830076225,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 520,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:21.589254+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5586727079935372,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:22.148137+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5543102920055389,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:22.702623+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5457310000201687,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 519,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:23.248620+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5565562500269152,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:23.805408+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5469623330282047,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:24.352647+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.555839458014816,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 520,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:24.908704+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6227373330038972,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:25.531617+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5713197499862872,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 513,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:26.103126+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6381358329672366,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 517,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:26.741448+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5889749589841813,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 521,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:27.330620+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5884084170102142,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 514,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:27.919262+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6028208339703269,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:28.522308+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6003204580047168,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 520,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:29.122900+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5949357919744216,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 518,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:29.718036+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5898151659639552,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 516,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:30.308248+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6043126250151545,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 519,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:51:30.912946+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6085678749950603,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 541,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:31.521778+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5936095830402337,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 535,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:32.115562+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6000372090493329,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:32.715844+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5866422920371406,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:33.302661+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.609359540976584,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:33.912359+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6090523339807987,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:34.521592+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6093016249942593,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 540,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:35.131102+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6047392080072314,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:35.736165+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5989965000189841,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 537,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:36.335410+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6056362500530668,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 544,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:36.941314+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6049707499914803,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:37.546481+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5636623330065049,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:38.110329+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6087256249738857,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:38.719288+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6026028330088593,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:39.322181+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6195514579885639,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:39.941934+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6156273330561817,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:40.557780+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6185194999561645,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 536,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:41.176511+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6224599580164067,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 543,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:41.799148+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6230952079640701,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 541,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:42.422412+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6366465409519151,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 542,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:43.059281+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6210342079866678,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 539,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:51:43.680593+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6447636670200154,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:44.325678+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5660034590400755,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 531,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:44.891887+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.602469666977413,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 526,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:45.494466+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6126679999870248,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 530,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:46.107318+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.604563832981512,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:46.712083+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6119376249844208,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 529,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:47.324125+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6154828340513632,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 536,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:47.939898+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6108728330000304,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 528,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:48.551220+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6105953750084154,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 530,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:49.161997+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6099943750305101,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 530,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:49.772181+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6115621670032851,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:50.383935+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5930127920000814,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 528,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:50.977229+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6271702920203097,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:51.604655+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.615438417007681,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 533,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:52.220372+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6127129580127075,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 529,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:52.833265+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6087055419920944,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 534,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:53.442193+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.5989135840209201,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 532,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:54.041295+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6035107499919832,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 526,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:54.645001+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6144429999985732,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 534,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:55.259616+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6133854999789037,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 534,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "estimated_tokens": 512,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:55.873208+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 0.6027540829963982,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 528,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:51:56.476893+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 2.3569784160354175,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1631,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:51:58.834528+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.7634331659646705,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1639,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:00.598448+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8722739170189016,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1635,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:02.471354+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9504027499933727,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:04.422368+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8583809590199962,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1632,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:06.281403+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.803045500011649,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1633,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:08.084903+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9602391670341603,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1630,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:10.045600+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8320524169830605,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1634,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:11.878276+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8131968340021558,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1630,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:13.692207+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.895024375000503,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1628,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:15.587755+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8236498750047758,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:17.411990+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8155605000210926,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1635,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:19.228350+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9256918749888428,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1632,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:21.154648+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8154165409505367,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1639,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:22.970694+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.956225500034634,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:24.927327+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8507647090009414,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:26.778544+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.986691833997611,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1636,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:28.765764+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9589979579905048,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1631,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:30.725283+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9488909580395557,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1635,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:32.674622+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9594085419666953,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1637,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:34.634478+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9545769999967888,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1634,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:52:36.589788+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9888787920353934,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1655,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:38.579387+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9785245829843916,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1657,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:40.558344+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9896854580147192,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1662,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:42.548460+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9670855840086006,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1656,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:44.516265+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9749126250389963,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1659,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:46.491777+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9657536249724217,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1655,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:48.458275+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9489205839927308,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1652,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:50.407952+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9447428329731338,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1655,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:52.353227+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9428600840037689,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1659,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:54.296824+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9316070000058971,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1657,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:56.229153+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9442582079791464,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1658,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:52:58.174061+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9259102499927394,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1662,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:00.100410+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9184581660083495,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1657,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:02.019564+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 2.0899060840019956,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1659,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:04.110142+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9682350420043804,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1659,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:06.079089+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9344469589996152,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1660,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:08.014255+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.922882832994219,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1656,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:09.937630+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.8926375829614699,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1660,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:11.830872+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.905141499999445,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1660,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:13.736469+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.941741999995429,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1658,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:15.678642+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9315641669672914,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1656,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:53:17.610793+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9271929999813437,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:19.538587+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.919784208002966,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:21.458872+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9605884170159698,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1646,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:23.419944+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9142959169694223,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1649,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:25.334683+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9229464999516495,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:27.258128+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9050966250360943,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:29.163726+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9326113329734653,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1648,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:31.097005+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.978072792000603,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:33.075587+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9730589579558,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1649,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:35.049257+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9808351250248961,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1647,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:37.030813+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9465328750084154,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1645,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:38.977907+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9472519580158405,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:40.925605+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9587795829866081,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1645,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:42.884823+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9389425829867832,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1646,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:44.824208+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.925892458006274,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1647,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:46.750836+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9279457080410793,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1646,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:48.679227+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9306431660079397,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1642,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:50.610285+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9690707080299035,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1650,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:52.580641+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.931094541971106,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1644,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:54.512459+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.9066200419911183,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1647,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "estimated_tokens": 2048,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:53:56.419592+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 1.910897666006349,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 1651,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:53:58.333426+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.606613958021626,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6106,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:05.941881+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.283587499987334,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:13.227325+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.187274541007355,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6104,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:20.416246+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.161649375048,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6102,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:27.579546+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.17109595797956,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6108,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:34.752520+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.243660999985877,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6109,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:41.997498+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.181059124995954,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6101,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:49.180165+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.228152375028003,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:54:56.409937+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.337238708976656,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:03.749207+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.0405338749988005,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6104,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:10.791795+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.140870500006713,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:17.934281+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 6.9361344169592485,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6102,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:24.872018+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.037211166985799,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:31.910817+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.008059125044383,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6103,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:38.919752+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 6.987042583001312,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:45.908401+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.0046082499902695,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6107,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:52.914606+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.05277216702234,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6093,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:55:59.969182+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.217703250003979,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6105,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:07.188478+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.028770332981367,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6099,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:14.218924+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.207618291955441,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6099,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:21.427932+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.182921917003114,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6100,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:56:28.612556+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.206185457995161,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:35.820305+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.081949707993772,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:42.905104+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.248242917004973,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:50.154322+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.208532124990597,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:56:57.364434+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.188764874998014,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6123,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:04.555043+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.383550332975574,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:11.940194+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.248981292010285,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6123,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:19.190795+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.225956916983705,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:26.418337+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.13987079198705,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6124,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:33.559831+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.048642334004398,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6121,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:40.609684+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.091861625027377,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6121,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:47.702944+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.187239625025541,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6123,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T21:57:54.891680+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.352086667029653,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6123,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:02.245169+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.274993125000037,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:09.522039+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.12377300002845,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6121,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:16.647541+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.183009458996821,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6130,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:23.831612+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.292131208989304,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6124,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:31.125409+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.388258000020869,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6129,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:38.515176+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.243815457972232,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6126,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:45.760586+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.092970624973532,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6128,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T21:58:52.854961+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.388633624999784,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6127,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 0,
      "warmup": true,
      "started_utc": "2026-10-25T21:59:00.245664+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 6.983138250012416,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 1,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:07.230412+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.040863583039027,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6115,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 2,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:14.272871+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.04502187500475,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6120,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 3,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:21.319487+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.047686375037301,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 4,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:28.368819+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.1204225000110455,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 5,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:35.490837+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.041863250022288,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 6,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:42.534042+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.149464000016451,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 7,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:49.684918+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.001571333967149,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 8,
      "warmup": false,
      "started_utc": "2026-10-25T21:59:56.688073+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.303729625011329,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6115,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 9,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:03.993414+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.008135541982483,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 10,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:11.003000+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.021085167012643,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6121,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 11,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:18.025692+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.257627207960468,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6119,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 12,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:25.284933+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.172242583998013,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6119,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 13,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:32.459031+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.133197624993045,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6117,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 14,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:39.594731+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.348377292044461,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6118,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 15,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:46.944741+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.388607791974209,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6121,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 16,
      "warmup": false,
      "started_utc": "2026-10-25T22:00:54.334740+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.433348916994873,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6116,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 17,
      "warmup": false,
      "started_utc": "2026-10-25T22:01:01.769697+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.260131541988812,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6117,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 18,
      "warmup": false,
      "started_utc": "2026-10-25T22:01:09.031984+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.046638083993457,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6119,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 19,
      "warmup": false,
      "started_utc": "2026-10-25T22:01:16.081186+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 7.276713208004367,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "estimated_tokens": 8192,
      "sample": 20,
      "warmup": false,
      "started_utc": "2026-10-25T22:01:23.359503+00:00",
      "valid": true,
      "response_model": "clef:27b",
      "identity": "response:clef:27b",
      "elapsed_seconds": 6.975028874992859,
      "exchanges": [
        {
          "status": 200,
          "model": "clef:27b",
          "usage": {
            "input_tokens": 6114,
            "output_tokens": 0
          }
        }
      ]
    },
    {
      "label": "calibration-4096",
      "transport": "curl",
      "started_utc": "2026-10-25T22:01:30.335265+00:00",
      "request_sha256": "3c60d4340fa15a1a9f69ee70a7a79eb878ed48b34399b121c810cfe7b4272c9a",
      "request_bytes": 4220,
      "state_chars": 4096,
      "request_model": "clef:27b",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000568 1.035523 1.035799",
      "status": 200,
      "elapsed_seconds": 1.0706026669940911,
      "response": {
        "model": "clef:27b",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9919691867837398
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        },
        "prompt_eval_cached_count": 0
      }
    },
    {
      "label": "calibration-8192",
      "transport": "curl",
      "started_utc": "2026-10-25T22:01:31.406939+00:00",
      "request_sha256": "3fcc5f2b6ecf499621453db11cdd73ea661dca6d8ddb06272efabcb788727b92",
      "request_bytes": 8316,
      "state_chars": 8192,
      "request_model": "clef:27b",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000225 1.822006 1.822195",
      "status": 200,
      "elapsed_seconds": 1.840773083968088,
      "response": {
        "model": "clef:27b",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9915870224290223
          }
        },
        "usage": {
          "input_tokens": 1637,
          "output_tokens": 0
        },
        "prompt_eval_cached_count": 37
      }
    },
    {
      "label": "pinned-model",
      "transport": "curl",
      "started_utc": "2026-10-25T22:01:33.248571+00:00",
      "request_sha256": "3c60d4340fa15a1a9f69ee70a7a79eb878ed48b34399b121c810cfe7b4272c9a",
      "request_bytes": 4220,
      "state_chars": 4096,
      "request_model": "clef:27b",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000264 0.014256 0.014400",
      "status": 200,
      "elapsed_seconds": 0.034892666968517005,
      "response": {
        "model": "clef:27b",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9920002454916221
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        },
        "prompt_eval_cached_count": 891
      }
    },
    {
      "label": "alias",
      "transport": "curl",
      "started_utc": "2026-10-25T22:01:33.284046+00:00",
      "request_sha256": "3c60d4340fa15a1a9f69ee70a7a79eb878ed48b34399b121c810cfe7b4272c9a",
      "request_bytes": 4220,
      "state_chars": 4096,
      "request_model": "clef:27b",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "200 0.000683 0.005230 0.005509",
      "status": 200,
      "elapsed_seconds": 0.02857008302817121,
      "response": {
        "model": "clef:27b",
        "answers": {
          "probe": {
            "type": "noul",
            "noul": 0.9920002454916221
          }
        },
        "usage": {
          "input_tokens": 891,
          "output_tokens": 0
        },
        "prompt_eval_cached_count": 891
      }
    }
  ],
  "diagnostic_timeout_seconds": 90.0,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "config_snapshot": {
    "api_base": "http://127.0.0.1:11434",
    "wire_api": "systemone",
    "identity_contract": "response_version",
    "model": "clef:27b",
    "timeout_seconds": 90.0,
    "max_input_tokens": 8192,
    "backend_max_state_tokens": 65536,
    "backend_max_question_tokens": null,
    "long_context": false,
    "failure_cooldown_seconds": 0.0,
    "community_label": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_confidence": 0.5
    },
    "community_label_carry": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "community_label_sufficiency": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_score": 0.5
    },
    "tool_rerank": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "min_probability": 0.5
    },
    "found_work": {
      "mode": "off",
      "evaluated_model": null,
      "evaluated_backend": null,
      "accept_below": 0.1,
      "accept_above": 0.9
    }
  },
  "summary": [
    {
      "shape": "choice",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 0.5886916879971977,
      "p95_seconds": 0.636884875013493,
      "within_1s": 20,
      "max_seconds": 0.6381358329672366
    },
    {
      "shape": "noul",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 0.6088889794773422,
      "p95_seconds": 0.6230952079640701,
      "within_1s": 20,
      "max_seconds": 0.6366465409519151
    },
    {
      "shape": "score",
      "target_application_tokens": 512,
      "n": 20,
      "p50_seconds": 0.6107341040042229,
      "p95_seconds": 0.6154828340513632,
      "within_1s": 20,
      "max_seconds": 0.6271702920203097
    },
    {
      "shape": "choice",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 1.8836491460097022,
      "p95_seconds": 1.9602391670341603,
      "within_1s": 0,
      "max_seconds": 1.986691833997611
    },
    {
      "shape": "noul",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 1.9435591459914576,
      "p95_seconds": 1.9896854580147192,
      "within_1s": 0,
      "max_seconds": 2.0899060840019956
    },
    {
      "shape": "score",
      "target_application_tokens": 2048,
      "n": 20,
      "p50_seconds": 1.9318529374722857,
      "p95_seconds": 1.978072792000603,
      "within_1s": 0,
      "max_seconds": 1.9808351250248961
    },
    {
      "shape": "choice",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 7.16637266651378,
      "p95_seconds": 7.283587499987334,
      "within_1s": 0,
      "max_seconds": 7.337238708976656
    },
    {
      "shape": "noul",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 7.217244520987151,
      "p95_seconds": 7.388258000020869,
      "within_1s": 0,
      "max_seconds": 7.388633624999784
    },
    {
      "shape": "score",
      "target_application_tokens": 8192,
      "n": 20,
      "p50_seconds": 7.126810062502045,
      "p95_seconds": 7.388607791974209,
      "within_1s": 0,
      "max_seconds": 7.433348916994873
    }
  ]
}
```

### Run 9: Ollama raw over-limit probe (2026-10-25)

The raw over-limit request used state_chars=377028. Ollama returned HTTP 413 in approximately 0.04 s with {"error":"text and schema must not exceed 64 KiB"}. This is a backend byte cap, not a measured token limit. The raw request bypassed DecisionService, so this result does not test Gobby's max_input_tokens guard.

The accompanying ollama ps observation showed clef:27b, context 16384, runner mlx, 100% GPU, 26 GB. Context capacity and the 64 KiB request cap have different units and must not be treated as interchangeable.

Artifacts: /var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-2grfypfe. No further live Ollama runs are authorized under the latest Orchestrator ruling.

## Clef diagnostic 2026-10-25T22:02:30.674577+00:00 — http://127.0.0.1:11434 — over-limit

Synthetic inputs; no settings changed. Backend metadata and response bodies only; no credentials.

```json
{
  "time_utc": "2026-10-25T22:02:30.674577+00:00",
  "backend": "http://127.0.0.1:11434",
  "model": "clef:27b",
  "phase": "over-limit",
  "wire_api": "systemone",
  "identity_contract": "response_version",
  "artifacts": "/var/folders/5w/9cmg71vd2m108t5r_fb77l0h0000gn/T/gobby-22604-clef-2grfypfe",
  "rows": [
    {
      "label": "over-limit",
      "transport": "curl",
      "started_utc": "2026-10-25T22:02:30.839571+00:00",
      "request_sha256": "5de438498f7ffbfc3e058249eadc982174bdbb4114a5afbd92f2298cea8bded1",
      "request_bytes": 377152,
      "state_chars": 377028,
      "request_model": "clef:27b",
      "curl_exit": 0,
      "curl_metrics_status_connect_ttfb_total": "413 0.000354 0.011655 0.011878",
      "status": 413,
      "elapsed_seconds": 0.038724750047549605,
      "response": {
        "error": "text and schema must not exceed 64 KiB"
      }
    }
  ],
  "diagnostic_timeout_seconds": 900.0,
  "source_head": "fd501dd46ab67c3d47568b803bd450e27f638188",
  "reference_capture_run": 6,
  "reference_state_chars": 377028,
  "stopped": {
    "error_type": "RuntimeError",
    "detail": "HTTP/transport failure; stop. Observe queue drain before ANY next inference."
  }
}
```
