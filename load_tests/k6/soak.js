// soak.js — k6 SOAK load test (todo 38, Wave 8 / C6b).
//
// Sustained MODERATE traffic over a configurable duration — the "does it stay
// stable / leak over time" test. A constant, comfortable VU count holds p95 well
// inside the SLO for the whole window; if the model-server leaked memory or
// otherwise degraded, latency would trend up and/or a container would restart,
// and the run would fall out of SLO. The k6 thresholds gate the latency/error
// side; the RESTART side is verified externally (kubectl restartCount before/
// after — see the QA recipe), because k6 cannot see pod restarts. Moderate by
// design so a long soak never pressures the kind node (cluster-stable-aware).
//
// Driven by scripts/run_load_test.sh (SCENARIO=soak), which passes DURATION via
// `k6 -e` and writes --out json. Standalone:
//   k6 run -e DURATION=5m load_tests/k6/soak.js
//
// DURATION default is 90s (a quick verification soak); override for a real soak
// (e.g. DURATION=30m). SOAK_VUS default 8 (moderate, ~ within baseline capacity).

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const HOST_HEADER = __ENV.HOST_HEADER || 'inferenceops.local';
const API_KEY = __ENV.API_KEY || 'dev-key-REPLACE-ME-0000000000000000';
const DURATION = __ENV.DURATION || '90s';
const SOAK_VUS = parseInt(__ENV.SOAK_VUS || '8', 10);

// 20 features — matches the fraud-detector model's expected input width.
const BODY = JSON.stringify({
  features: [
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
  ],
});

export const options = {
  scenarios: {
    soak: {
      executor: 'constant-vus',
      vus: SOAK_VUS,
      duration: DURATION,
    },
  },
  thresholds: {
    // Stability gate: latency stays inside the SLO and errors stay low for the
    // WHOLE window. A leak/degradation pushes p95 past 500 ms or errors past 1%,
    // failing the run — the soak's signal that something drifted over time. A
    // healthy soak holds these with headroom for the entire duration.
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
    checks: ['rate>0.99'],
  },
};

export default function () {
  const res = http.post(`${TARGET}/v1/predict`, BODY, {
    headers: {
      'Content-Type': 'application/json',
      Host: HOST_HEADER,
      'X-API-Key': API_KEY,
    },
  });
  // Short-circuit guards the JSON parse: a failed request has no body, so
  // r.json() is only ever called on a 200 (keeps the failure path a clean
  // check-fail, not a VU exception) — same guard as smoke.js.
  check(res, {
    'status is 200': (r) => r.status === 200,
    'body has prediction': (r) => r.status === 200 && r.json('prediction') !== undefined,
  });
}
