// smoke.js — k6 SMOKE load test (todo 37, Wave 8 / C6b).
//
// Minimal-traffic sanity against the live ingress: a couple of constant VUs for
// a short window, asserting POST /v1/predict returns 200 and stays fast
// (p95 < 500 ms). This is the "is the deploy alive under trivial load" gate.
// It deliberately stays LOW and bounded so it never pressures the kind node —
// the cluster recently recovered from host swap-thrash (see learnings.md).
//
// Driven by scripts/run_load_test.sh (SCENARIO=smoke), which passes TARGET /
// HOST_HEADER / API_KEY / DURATION via `k6 -e` and writes --out json. Standalone:
//   k6 run -e TARGET=http://localhost:8080 load_tests/k6/smoke.js

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const HOST_HEADER = __ENV.HOST_HEADER || 'inferenceops.local';
const API_KEY = __ENV.API_KEY || 'dev-key-REPLACE-ME-0000000000000000';
const DURATION = __ENV.DURATION || '30s';

// 20 features — matches the fraud-detector model's expected input width.
const BODY = JSON.stringify({
  features: [
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
  ],
});

export const options = {
  scenarios: {
    smoke: {
      executor: 'constant-vus',
      vus: 2,
      duration: DURATION,
    },
  },
  thresholds: {
    // Sanity: the deploy answers, fast, with (near) zero errors.
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
  // Short-circuit guards the JSON parse: a down endpoint returns no body, so
  // r.json() is only ever called on a 200 (keeps the failure path a clean
  // check-fail, not a VU exception).
  check(res, {
    'status is 200': (r) => r.status === 200,
    'body has prediction': (r) => r.status === 200 && r.json('prediction') !== undefined,
  });
}
