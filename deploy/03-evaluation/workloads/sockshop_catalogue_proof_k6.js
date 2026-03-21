import http from 'k6/http';
import { check } from 'k6';

const base = __ENV.BASE_URL || 'http://front-end.sock-shop';
const rate = Number(__ENV.RATE || 400);
const duration = __ENV.DURATION || '180s';

export const options = {
  scenarios: {
    mixed: {
      executor: 'constant-arrival-rate',
      rate: rate,
      timeUnit: '1s',
      duration: duration,
      preAllocatedVUs: 200,
      maxVUs: 800,
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.5'],
  },
};

function pickPath() {
  // 90% catalogue, 10% root for strong catalogue pressure.
  const r = Math.random();
  if (r < 0.9) return '/catalogue';
  return '/';
}

export default function () {
  const path = pickPath();
  const res = http.get(`${base}${path}`, { timeout: '5s' });
  check(res, {
    'status<500': (r) => r.status < 500,
  });
}
