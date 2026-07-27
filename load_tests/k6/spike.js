// spike.js — k6 SPIKE load test (todo 38, Wave 8 / C6b).
//
// A SUDDEN burst, not a ramp: idle (0 VUs) → near-vertical jump to a high VU
// count → hold briefly → near-vertical drop back to idle (0 VUs). Where
// stress.js (todo 37) climbs gradually to hunt the breaking point, the spike's
// whole point is the abruptness — it slams RPS up fast enough to trip the
// model-server HPA (cpu 70% / memory 80% / external inferenceops_rps, min 2 /
// max 10) into scaling up, then drops to 0 VUs so the HPA scales back down and
// no load is left on the kind node (cluster-stable-aware).
//
// This is the k6 complement to todo 20's lightweight curl spike
// (scripts/spike_load.sh), which stays wired to `make load-test-spike` as the
// verified autoscaling demo. This script runs through the parameterized wrapper
// (SCENARIO=spike scripts/run_load_test.sh) so it also writes the --out json
// stream that the benchmark generator (todo 40) parses.
//
// Driven by scripts/run_load_test.sh (SCENARIO=spike). Standalone:
//   k6 run -e TARGET=http://localhost:8080 load_tests/k6/spike.js
//
// DURATION does not apply here: the stages below are a fixed, bounded spike
// (~96s) tuned for the recovering kind cluster. Override the peak with SPIKE_VUS.
// The 75s hold is deliberate: this cluster's HPA reacts on a lag (the external
// inferenceops_rps metric flows gateway -> Prometheus -> adapter -> HPA in
// ~30-50s, then the new pods need cold-start time), so the burst must be held
// long enough for the scale-up to MATERIALIZE (currentReplicas > 2) within the
// spike window. It still ends at 0 VUs so the HPA scales back down afterwards.

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://localhost:8080';
const HOST_HEADER = __ENV.HOST_HEADER || 'inferenceops.local';
const API_KEY = __ENV.API_KEY || 'dev-key-REPLACE-ME-0000000000000000';
const SPIKE_VUS = parseInt(__ENV.SPIKE_VUS || '70', 10);

// 20 features — matches the fraud-detector model's expected input width.
const BODY = JSON.stringify({
  features: [
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
    0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0,
  ],
});

export const options = {
  scenarios: {
    spike: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '5s', target: 0 },          // idle baseline (HPA at min 2)
        { duration: '3s', target: SPIKE_VUS },  // SUDDEN jump — the spike
        { duration: '75s', target: SPIKE_VUS }, // hold the burst → HPA scales up
        { duration: '3s', target: 0 },          // SUDDEN drop back to idle
        { duration: '10s', target: 0 },         // idle → HPA scales back down
      ],
    },
  },
  thresholds: {
    // Record, don't gate: the burst overloads the 2-replica baseline before the
    // HPA can react, so errors/latency during the spike are the SIGNAL, not a
    // failure (same philosophy as stress.js). Keep the run green so the JSON
    // summary always lands for todo 40's parser. The acceptance — "HPA scales
    // up, then recovers within SLO" — is verified externally: QA correlates
    // `kubectl get hpa model-server` with the burst and re-measures p95 under
    // light load afterwards (a follow-up smoke), rather than gating the spike.
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
  // During the burst some requests fail (the baseline sheds load until the HPA
  // scales up) — that is the spike signal, not a script bug. The check records
  // the success ratio; thresholds stay lenient.
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
}
