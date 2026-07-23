// stress.js — k6 STRESS load test (todo 37, Wave 8 / C6b).
//
// Ramps VUs well past the model-server's expected capacity (2 replicas, HPA
// max 10) to find the breaking point and RECORD the achieved max-RPS + error
// rate. Errors under stress are EXPECTED — that is the point of the test — so
// the thresholds here are deliberately lenient: the run completes and always
// writes its JSON summary (the benchmark generator, todo 40, parses it). The
// ramp is BOUNDED in time and ends at 0 VUs so the HPA scales back down and no
// load is left on the kind node afterwards (cluster-stable-aware).
//
// Driven by scripts/run_load_test.sh (SCENARIO=stress). Standalone:
//   k6 run -e TARGET=http://localhost:8080 load_tests/k6/stress.js
//
// DURATION does not apply here: the stages below are a fixed, bounded ramp
// (~90s) tuned for the recovering kind cluster. Override the peak with MAX_VUS.

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const HOST_HEADER = __ENV.HOST_HEADER || 'inferenceops.local';
const API_KEY = __ENV.API_KEY || 'dev-key-REPLACE-ME-0000000000000000';
const MAX_VUS = parseInt(__ENV.MAX_VUS || '80', 10);

// 20 features — matches the fraud-detector model's expected input width.
const BODY = JSON.stringify({
  features: [
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
  ],
});

export const options = {
  scenarios: {
    stress: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: 20 },      // warm up within capacity
        { duration: '30s', target: MAX_VUS }, // ramp past capacity (HPA scales)
        { duration: '20s', target: MAX_VUS }, // hold at peak to sustain the break
        { duration: '20s', target: 0 },       // ramp down → HPA recovers
      ],
    },
  },
  thresholds: {
    // Record, don't gate: stress is expected to shed load. Keep the run green
    // so the JSON summary always lands for todo 40's benchmark parser. The
    // achieved max-RPS (http_reqs rate) + error rate (http_req_failed) are
    // captured in the summary + JSON regardless of these lenient bounds.
    http_reqs: ['rate>0'],
    http_req_failed: ['rate<1.0'],
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
  // Under stress some requests fail (timeouts / 5xx) — that is the signal, not
  // a script bug. The check records the success ratio; thresholds stay lenient.
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
}
