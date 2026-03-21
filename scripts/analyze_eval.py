import argparse
import glob
import json
import math
import os
import re
from pathlib import Path

import pandas as pd
import yaml


def dataframe_to_markdown(df, index=False):
    try:
        return df.to_markdown(index=index)
    except ImportError:
        return df.to_string(index=index)


def parse_name(path):
    base = os.path.basename(path).replace('.csv', '')
    parts = base.split('_')
    # expected:
    #   results_<scenario>_<autoscaler>_runN
    # or
    #   results_<scenario>_<profile>_<autoscaler>_runN
    if len(parts) < 4:
        return None
    scenario = parts[1].strip().lower()
    if len(parts) >= 5 and parts[-1].strip().lower().startswith('run'):
        autoscaler = parts[-2].strip().lower()
        run = parts[-1].strip().lower()
    else:
        autoscaler = parts[2].strip().lower()
        run = parts[3].strip().lower()
    return scenario, autoscaler, run


def parse_k6_summary(log_path):
    p = Path(log_path)
    if not p.exists():
        return {}

    text = p.read_text(encoding='utf-8', errors='ignore')

    out = {}
    checks_re = re.search(r'checks[.\s:]+([0-9.]+)%\s+✓\s*([0-9,]+)\s+✗\s*([0-9,]+)', text)
    failed_re = re.search(r'http_req_failed[.\s:]+([0-9.]+)%\s+✓\s*([0-9,]+)\s+✗\s*([0-9,]+)', text)
    http_reqs_re = re.search(r'http_reqs[.\s:]+([0-9,]+)\s+[0-9.]+/s', text)
    iterations_re = re.search(r'iterations[.\s:]+([0-9,]+)\s+[0-9.]+/s', text)
    dropped_re = re.search(r'dropped_iterations[.\s:]+([0-9,]+)\s+[0-9.]+/s', text)

    if checks_re:
        out['k6 checks pass (%)'] = round(float(checks_re.group(1)), 2)
        out['k6 checks pass'] = int(checks_re.group(2).replace(',', ''))
        out['k6 checks fail'] = int(checks_re.group(3).replace(',', ''))
    if failed_re:
        out['k6 http_req_failed (%)'] = round(float(failed_re.group(1)), 2)
        out['k6 http_req_failed'] = int(failed_re.group(2).replace(',', ''))
        out['k6 http_req_ok'] = int(failed_re.group(3).replace(',', ''))
    if http_reqs_re:
        out['k6 http_reqs'] = int(http_reqs_re.group(1).replace(',', ''))
    if iterations_re:
        out['k6 iterations'] = int(iterations_re.group(1).replace(',', ''))
    if dropped_re:
        out['k6 dropped_iterations'] = int(dropped_re.group(1).replace(',', ''))

    return out


def infer_sample_interval(df):
    if 'elapsed_s' not in df.columns or len(df) < 2:
        return 1.0
    diffs = pd.to_numeric(df['elapsed_s'], errors='coerce').diff().dropna()
    if diffs.empty:
        return 1.0
    return float(diffs.median())


def t_critical_95(n):
    # two-sided 95% t critical values for dof 1..30, then ~normal
    table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447,
        7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179,
        13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101,
        19: 2.093, 20: 2.086, 21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064,
        25: 2.060, 26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    }
    if n <= 1:
        return float('nan')
    dof = n - 1
    if dof in table:
        return table[dof]
    return 1.96


def mean_ci95(series):
    s = pd.to_numeric(series, errors='coerce').dropna()
    n = len(s)
    if n == 0:
        return (float('nan'), float('nan'))
    m = float(s.mean())
    if n == 1:
        return (m, float('nan'))
    sd = float(s.std(ddof=1))
    t = t_critical_95(n)
    if math.isnan(t):
        return (m, float('nan'))
    half = t * sd / math.sqrt(n)
    return (m, half)


def compute_replica_seconds(df):
    if 'pod_spec_replicas' not in df.columns:
        return 0.0
    repl = pd.to_numeric(df['pod_spec_replicas'], errors='coerce').fillna(0.0)
    if 'elapsed_s' in df.columns and len(df) > 1:
        t = pd.to_numeric(df['elapsed_s'], errors='coerce').ffill().fillna(0.0)
        dt = t.diff().fillna(infer_sample_interval(df)).clip(lower=0.0)
    else:
        dt = pd.Series([infer_sample_interval(df)] * len(df))
    return float((repl * dt).sum())


def compute_ready_replica_seconds(df):
    if 'pod_ready_replicas' not in df.columns:
        return float('nan')
    repl = pd.to_numeric(df['pod_ready_replicas'], errors='coerce').fillna(0.0)
    if 'elapsed_s' in df.columns and len(df) > 1:
        t = pd.to_numeric(df['elapsed_s'], errors='coerce').ffill().fillna(0.0)
        dt = t.diff().fillna(infer_sample_interval(df)).clip(lower=0.0)
    else:
        dt = pd.Series([infer_sample_interval(df)] * len(df))
    return float((repl * dt).sum())


def safe_json_dict(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, float) and math.isnan(value):
        return {}
    if not isinstance(value, str):
        return {}
    value = value.strip()
    if not value:
        return {}
    try:
        obj = json.loads(value)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def service_window_state(raw_state, svc):
    state_map = safe_json_dict(raw_state)
    svc_state = state_map.get(svc, {})
    if not isinstance(svc_state, dict):
        svc_state = {}
    return {
        'active': bool(svc_state.get('active_short', False) or svc_state.get('active_long', False)),
        'evaluable': bool(svc_state.get('evaluable_for_slo', False)),
        'latency_fresh': bool(svc_state.get('latency_fresh', False)),
        'truth_fresh': bool(svc_state.get('truth_fresh', False)),
        'topology_fresh': bool(svc_state.get('topology_fresh', False)),
        'truth_rps': float(svc_state.get('truth_rps', 0.0) or 0.0),
        'truth_rps_long': float(svc_state.get('truth_rps_long', 0.0) or 0.0),
        'rps': float(svc_state.get('rps', 0.0) or 0.0),
        'rps_long': float(svc_state.get('rps_long', 0.0) or 0.0),
        'count': int(svc_state.get('count', 0) or 0),
        'count_long': int(svc_state.get('count_long', 0) or 0),
        'truth_req_count': int(svc_state.get('truth_req_count', 0) or 0),
        'truth_req_count_long': int(svc_state.get('truth_req_count_long', 0) or 0),
        'truth_5xx_rate': float(svc_state.get('truth_5xx_rate', 0.0) or 0.0),
        'truth_timeout_rate': float(svc_state.get('truth_timeout_rate', 0.0) or 0.0),
        'evidence_confidence': float(svc_state.get('evidence_confidence', 0.0) or 0.0),
    }


def load_service_slo_map(slo_file, namespace):
    out = {}
    if not slo_file:
        return out
    p = Path(slo_file)
    if not p.exists():
        return out
    docs = list(yaml.safe_load_all(p.read_text(encoding='utf-8')))
    for doc in docs:
        if not doc or doc.get('kind') != 'ServiceSLO':
            continue
        md = doc.get('metadata', {})
        spec = doc.get('spec', {})
        if md.get('namespace', namespace) != namespace:
            continue
        target = spec.get('targetDeployment')
        if not target:
            continue
        try:
            out[str(target)] = float(spec.get('sloLatency', 0.0))
        except Exception:
            continue
    return out


def compute_cluster_replica_seconds(df):
    if 'all_spec_replicas' in df.columns:
        repl = pd.to_numeric(df['all_spec_replicas'], errors='coerce').fillna(0.0)
    elif 'all_deployment_spec_replicas_json' in df.columns:
        repl = df['all_deployment_spec_replicas_json'].apply(
            lambda s: float(sum(safe_json_dict(s).values()))
        )
    else:
        return float('nan')

    if 'elapsed_s' in df.columns and len(df) > 1:
        t = pd.to_numeric(df['elapsed_s'], errors='coerce').ffill().fillna(0.0)
        dt = t.diff().fillna(infer_sample_interval(df)).clip(lower=0.0)
    else:
        dt = pd.Series([infer_sample_interval(df)] * len(df))
    return float((repl * dt).sum())


def get_slo_from_file(slo_file, namespace, target, fallback):
    if not slo_file:
        return fallback
    p = Path(slo_file)
    if not p.exists():
        return fallback
    docs = list(yaml.safe_load_all(p.read_text(encoding='utf-8')))
    for doc in docs:
        if not doc or doc.get('kind') != 'ServiceSLO':
            continue
        md = doc.get('metadata', {})
        spec = doc.get('spec', {})
        if md.get('namespace', namespace) != namespace:
            continue
        if spec.get('targetDeployment') == target:
            try:
                return float(spec.get('sloLatency', fallback))
            except Exception:
                return fallback
    return fallback


def apply_score_window(df, warmup_seconds):
    if 'in_score_window' in df.columns:
        mask = pd.to_numeric(df['in_score_window'], errors='coerce').fillna(0).astype(int) == 1
        if mask.any():
            return df[mask].copy()
    if 'elapsed_s' in df.columns:
        e = pd.to_numeric(df['elapsed_s'], errors='coerce').fillna(0.0)
        return df[e >= warmup_seconds].copy()
    return df


def compute_metrics(df, slo_ms):
    if 'latency_p90_ms' not in df.columns or 'pod_spec_replicas' not in df.columns:
        return None

    p90 = pd.to_numeric(df['latency_p90_ms'], errors='coerce').fillna(0.0)
    repl = pd.to_numeric(df['pod_spec_replicas'], errors='coerce').fillna(0.0)
    ready_repl = pd.to_numeric(df.get('pod_ready_replicas', pd.Series([0.0] * len(df))), errors='coerce').fillna(0.0)

    total = len(p90)
    if total == 0:
        return None

    exceed = (p90 - slo_ms).clip(lower=0.0)
    violated = exceed > 0

    if 'elapsed_s' in df.columns and len(df) > 1:
        t = pd.to_numeric(df['elapsed_s'], errors='coerce').ffill().fillna(0.0)
        dt = t.diff().fillna(infer_sample_interval(df)).clip(lower=0.0)
    else:
        dt = pd.Series([infer_sample_interval(df)] * len(df))
    total_duration = float(dt.sum())
    violated_duration = float(dt[violated].sum()) if total_duration > 0 else 0.0

    slo_adherence = 100.0 * float((~violated).sum()) / total
    violation_rate = 100.0 - slo_adherence
    mean_exceed = float(exceed[violated].mean()) if violated.any() else 0.0
    p95_exceed = float(exceed[violated].quantile(0.95)) if violated.any() else 0.0
    time_weighted_violation_rate = (100.0 * violated_duration / total_duration) if total_duration > 0 else 0.0

    scaling_actions = int((repl != repl.shift(1)).sum() - 1)
    scaling_actions = max(0, scaling_actions)

    return {
        'SLO Target (ms)': float(slo_ms),
        'SLO Adherence (%)': round(slo_adherence, 2),
        'SLO Violation Rate (%)': round(violation_rate, 2),
        'SLO Violation Duration (s)': round(violated_duration, 2),
        'Time-Weighted Violation Rate (%)': round(time_weighted_violation_rate, 2),
        'Mean Exceedance (ms)': round(mean_exceed, 2),
        'P95 Exceedance (ms)': round(p95_exceed, 2),
        # These are based on the sampled deployment in the CSV (typically front-end),
        # not aggregate cluster-wide replicas.
        'Avg Sampled Replicas': round(float(repl.mean()), 2),
        'Peak Sampled Replicas': int(repl.max()) if len(repl) else 0,
        'Sampled Replica-Seconds': round(compute_replica_seconds(df), 2),
        'Avg Ready Replicas': round(float(ready_repl.mean()), 2),
        'Peak Ready Replicas': int(ready_repl.max()) if len(ready_repl) else 0,
        'Ready Replica-Seconds': round(compute_ready_replica_seconds(df), 2),
        'Sampled Scaling Actions': scaling_actions,
    }


def compute_system_metrics(df, service_slo_map):
    if not service_slo_map or 'service_p90_json' not in df.columns:
        return {}, {}

    total_checks = 0
    total_viol = 0
    any_active_checks = 0
    any_active_viol = 0
    per_service = {
        svc: {'checks': 0, 'viol': 0, 'skipped_inactive': 0, 'skipped_unevaluable': 0}
        for svc in service_slo_map
    }

    state_col_present = 'service_state_json' in df.columns
    for _, row in df.iterrows():
        raw = row['service_p90_json']
        p90_map = safe_json_dict(raw)
        row_any_active = False
        row_any_viol = False
        for svc, slo in service_slo_map.items():
            if svc not in p90_map:
                continue
            svc_state = service_window_state(row['service_state_json'], svc) if state_col_present else {}
            active = bool(svc_state.get('active', False)) if state_col_present else True
            evaluable = bool(svc_state.get('evaluable', False)) if state_col_present else True
            if not active:
                per_service[svc]['skipped_inactive'] += 1
                continue
            if not evaluable:
                per_service[svc]['skipped_unevaluable'] += 1
                continue
            try:
                p90 = float(p90_map.get(svc, 0.0) or 0.0)
            except Exception:
                continue
            row_any_active = True
            per_service[svc]['checks'] += 1
            total_checks += 1
            if p90 > float(slo):
                per_service[svc]['viol'] += 1
                total_viol += 1
                row_any_viol = True

        if row_any_active:
            any_active_checks += 1
            if row_any_viol:
                any_active_viol += 1

    service_rates = {}
    for svc, d in per_service.items():
        if d['checks'] == 0:
            continue
        service_rates[svc] = round(100.0 * d['viol'] / d['checks'], 2)

    if total_checks == 0:
        return {}, service_rates

    cluster_replica_sec = compute_cluster_replica_seconds(df)
    out = {
        'System SLO Violation Rate (%)': round(100.0 * total_viol / total_checks, 2),
        'Any Active Service Violation Rate (%)': round(100.0 * any_active_viol / any_active_checks, 2) if any_active_checks else float('nan'),
        'Services Covered': int(len(service_rates)),
        'System Active Service Checks': int(total_checks),
        'System Active Windows': int(any_active_checks),
    }
    if not math.isnan(cluster_replica_sec):
        out['System Cost Proxy (Replica-Seconds)'] = round(cluster_replica_sec, 2)
    return out, service_rates


def build_result_rows(files, args):
    rows = []
    for file in sorted(files):
        parsed = parse_name(file)
        if not parsed:
            print(f"Skipping unexpected filename format: {os.path.basename(file)}")
            continue
        scenario, autoscaler, run = parsed
        if args.scenario and scenario != args.scenario.lower():
            continue

        df = pd.read_csv(file, on_bad_lines='skip')
        score_df = apply_score_window(df, args.warmup_seconds)

        if args.default_slo_ms is not None:
            slo_ms = float(args.default_slo_ms)
        elif scenario in args.slo_map:
            slo_ms = float(args.slo_map[scenario])
        else:
            slo_ms = get_slo_from_file(args.slo_file, args.namespace, args.slo_target, 100.0)

        metrics = compute_metrics(score_df, slo_ms)
        if not metrics:
            print(f"Skipping {os.path.basename(file)}: missing required columns")
            continue

        system_metrics, service_rates = compute_system_metrics(score_df, args.service_slo_map)

        sampled_deployment = 'unknown'
        if 'deployment' in score_df.columns:
            vals = (
                score_df['deployment']
                .dropna()
                .astype(str)
                .str.strip()
                .unique()
            )
            if len(vals) == 1:
                sampled_deployment = vals[0]
            elif len(vals) > 1:
                sampled_deployment = 'multiple'

        row = {
            'Scenario': scenario.upper(),
            'Autoscaler': autoscaler.upper(),
            'Run': run,
            'File': os.path.basename(file),
            'Sampled Deployment': sampled_deployment,
            'Service Violation JSON': json.dumps(service_rates, sort_keys=True),
        }
        row.update(metrics)
        row.update(system_metrics)

        run_num = run[3:] if run.startswith('run') else run
        k6_log = Path(args.k6_log_dir) / f'k6_{autoscaler}_run{run_num}.log'
        row.update(parse_k6_summary(k6_log))
        rows.append(row)
    return rows


def maybe_plot(df_runs, csv_files, out_dir):
    try:
        import matplotlib.pyplot as plt
    except Exception:
        print('[warn] matplotlib not installed, skipping plots')
        return

    Path(out_dir).mkdir(parents=True, exist_ok=True)

    vio_col = 'System SLO Violation Rate (%)' if 'System SLO Violation Rate (%)' in df_runs.columns else 'SLO Violation Rate (%)'
    cost_col = (
        'System Cost Proxy (Replica-Seconds)'
        if 'System Cost Proxy (Replica-Seconds)' in df_runs.columns
        else 'Sampled Replica-Seconds'
    )

    fig, ax = plt.subplots(figsize=(8, 5))
    for scaler, g in df_runs.groupby('Autoscaler'):
        ax.scatter(g[vio_col], g[cost_col], label=scaler)
    ax.set_xlabel(vio_col)
    ax.set_ylabel(cost_col)
    ax.set_title('Violation vs Cost (Replica-Seconds)')
    ax.legend()
    fig.tight_layout()
    fig.savefig(Path(out_dir) / 'violation_vs_cost.png', dpi=140)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    for f in csv_files:
        parsed = parse_name(f)
        if not parsed:
            continue
        _, scaler, run = parsed
        d = pd.read_csv(f, on_bad_lines='skip')
        if 'elapsed_s' not in d.columns or 'latency_p90_ms' not in d.columns:
            continue
        x = pd.to_numeric(d['elapsed_s'], errors='coerce')
        y = pd.to_numeric(d['latency_p90_ms'], errors='coerce')
        ax.plot(x, y, alpha=0.6, label=f"{scaler}-{'run' if not run.startswith('run') else ''}{run}")
    ax.set_xlabel('Elapsed (s)')
    ax.set_ylabel('P90 Latency (ms)')
    ax.set_title('P90 Latency vs Time')
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(Path(out_dir) / 'p90_vs_time.png', dpi=140)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 5))
    for f in csv_files:
        parsed = parse_name(f)
        if not parsed:
            continue
        _, scaler, run = parsed
        d = pd.read_csv(f, on_bad_lines='skip')
        repl_col = None
        if 'all_spec_replicas' in d.columns:
            repl_col = 'all_spec_replicas'
        elif 'pod_spec_replicas' in d.columns:
            repl_col = 'pod_spec_replicas'
        if 'elapsed_s' not in d.columns or repl_col is None:
            continue
        x = pd.to_numeric(d['elapsed_s'], errors='coerce')
        y = pd.to_numeric(d[repl_col], errors='coerce')
        ax.plot(x, y, alpha=0.7, label=f"{scaler}-{'run' if not run.startswith('run') else ''}{run}")
    ax.set_xlabel('Elapsed (s)')
    ax.set_ylabel('Replicas (Sampled or Cluster Total)')
    ax.set_title('Replicas vs Time')
    ax.legend(fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(Path(out_dir) / 'replicas_vs_time.png', dpi=140)
    plt.close(fig)


def parse_args():
    p = argparse.ArgumentParser(description='Analyze ThriveScale vs HPA evaluation outputs')
    p.add_argument('--glob', default='results_*.csv', help='CSV glob pattern')
    p.add_argument('--scenario', default=None, help='Filter scenario (e.g., sockshop)')
    p.add_argument('--default-slo-ms', type=float, default=None, help='Default SLO target')
    p.add_argument('--slo-file', default='', help='ServiceSLO yaml to infer SLO target from')
    p.add_argument('--namespace', default='sock-shop', help='Namespace used in --slo-file')
    p.add_argument('--slo-target', default='front-end', help='Target deployment in --slo-file')
    p.add_argument('--warmup-seconds', type=float, default=20, help='Fallback score-window warmup')
    p.add_argument('--plot', action='store_true', help='Generate summary plots if matplotlib exists')
    p.add_argument('--markdown-out', default='', help='Write markdown report to file')
    p.add_argument('--k6-log-dir', default='results/analysis', help='Directory containing k6_<mode>_runN.log files')
    p.add_argument(
        '--slo',
        action='append',
        default=[],
        help='Per-scenario SLO override scenario=ms (e.g., sockshop=150)',
    )
    args = p.parse_args()

    slo_map = {}
    for item in args.slo:
        if '=' not in item:
            continue
        k, v = item.split('=', 1)
        k = k.strip().lower()
        try:
            slo_map[k] = float(v)
        except Exception:
            continue
    args.slo_map = slo_map
    return args


def main():
    args = parse_args()
    args.service_slo_map = load_service_slo_map(args.slo_file, args.namespace)
    files = glob.glob(args.glob)
    if not files:
        print('No CSV files found. Run eval harness first.')
        return

    rows = build_result_rows(files, args)
    if not rows:
        print('No valid CSV rows found for analysis.')
        return

    df_runs = pd.DataFrame(rows)

    print('\n' + '=' * 70)
    print('THESIS EVALUATION - PER RUN')
    print('=' * 70)
    print(dataframe_to_markdown(df_runs.drop(columns=['File']), index=False))
    print('\n[note] Replica and scaling metrics are for the sampled deployment in each CSV')
    print('       (for this workload, this is usually front-end), not full cluster-wide scaling.')

    vio_col = (
        'System SLO Violation Rate (%)'
        if 'System SLO Violation Rate (%)' in df_runs.columns and df_runs['System SLO Violation Rate (%)'].notna().any()
        else 'SLO Violation Rate (%)'
    )
    cost_col = (
        'System Cost Proxy (Replica-Seconds)'
        if 'System Cost Proxy (Replica-Seconds)' in df_runs.columns and df_runs['System Cost Proxy (Replica-Seconds)'].notna().any()
        else 'Sampled Replica-Seconds'
    )

    pbstyle = df_runs[['Scenario', 'Autoscaler', 'Run', vio_col, cost_col]].copy()
    pbstyle = pbstyle.rename(columns={
        vio_col: 'Violation Rate (%)',
        cost_col: f'Cost Proxy ({cost_col})',
    })
    cost_proxy_col = f'Cost Proxy ({cost_col})'
    pbstyle['Normalized Cost Proxy'] = (
        pbstyle.groupby('Scenario')[cost_proxy_col]
        .transform(lambda s: s / s.min() if s.min() > 0 else float('nan'))
        .round(3)
    )
    print('\n' + '=' * 70)
    print('PBSCALER-STYLE VIEW (VIOLATION + COST PROXY)')
    print('=' * 70)
    print(dataframe_to_markdown(pbstyle, index=False))
    print('\n[note] PBScaler reports dollar cost from CPU+memory usage and cloud prices;')
    print('       here cost is a proxy unless full cluster-wide CPU/memory usage is provided.')

    if 'k6 http_req_failed (%)' in df_runs.columns and df_runs['k6 http_req_failed (%)'].notna().any():
        k6_view = df_runs[['Scenario', 'Autoscaler', 'Run', 'k6 http_req_failed (%)', cost_col]].copy()
        k6_view = k6_view.rename(columns={
            'k6 http_req_failed (%)': 'Request Failure Rate (%)',
            cost_col: f'Cost Proxy ({cost_col})',
        })
        print('\n' + '=' * 70)
        print('K6 REQUEST FAILURE VIEW')
        print('=' * 70)
        print(dataframe_to_markdown(k6_view, index=False))

    service_df = pd.DataFrame()
    if 'Service Violation JSON' in df_runs.columns:
        service_rows = []
        for _, r in df_runs.iterrows():
            rates = safe_json_dict(r.get('Service Violation JSON'))
            for svc, rate in rates.items():
                service_rows.append({
                    'Scenario': r['Scenario'],
                    'Autoscaler': r['Autoscaler'],
                    'Run': r['Run'],
                    'Service': svc,
                    'SLO Violation Rate (%)': rate,
                })
        if service_rows:
            service_df = pd.DataFrame(service_rows)
            print('\n' + '=' * 70)
            print('PER-SERVICE VIOLATION RATES')
            print('=' * 70)
            print(dataframe_to_markdown(service_df, index=False))

    metric_cols = [
        'SLO Adherence (%)',
        'SLO Violation Rate (%)',
        'Mean Exceedance (ms)',
        'P95 Exceedance (ms)',
        'Avg Sampled Replicas',
        'Peak Sampled Replicas',
        'Sampled Replica-Seconds',
        'Sampled Scaling Actions',
    ]
    for col in [
        'k6 checks pass (%)',
        'k6 http_req_failed (%)',
        'k6 http_reqs',
        'k6 dropped_iterations',
    ]:
        if col in df_runs.columns:
            metric_cols.append(col)
    for col in ['System SLO Violation Rate (%)', 'System Cost Proxy (Replica-Seconds)', 'Services Covered']:
        if col in df_runs.columns:
            metric_cols.append(col)

    grouped = df_runs.groupby(['Scenario', 'Autoscaler'])[metric_cols].agg(['mean', 'std']).round(2)
    grouped.columns = [f"{c[0]} ({c[1]})" for c in grouped.columns]

    print('\n' + '=' * 70)
    print('THESIS EVALUATION - SUMMARY (MEAN ± STD)')
    print('=' * 70)
    print(dataframe_to_markdown(grouped))

    ci_rows = []
    for (scenario, autoscaler), g in df_runs.groupby(['Scenario', 'Autoscaler']):
        v_m, v_ci = mean_ci95(g[vio_col])
        c_m, c_ci = mean_ci95(g[cost_col])
        ci_rows.append({
            'Scenario': scenario,
            'Autoscaler': autoscaler,
            'Violation Mean': round(v_m, 2),
            'Violation CI95 +/-': round(v_ci, 2) if not math.isnan(v_ci) else float('nan'),
            'Cost Proxy Mean': round(c_m, 2),
            'Cost Proxy CI95 +/-': round(c_ci, 2) if not math.isnan(c_ci) else float('nan'),
        })

    ci_df = pd.DataFrame(ci_rows)
    print('\n' + '=' * 70)
    print('THESIS EVALUATION - 95% CI (KEY METRICS)')
    print('=' * 70)
    print(dataframe_to_markdown(ci_df, index=False))

    if args.plot:
        maybe_plot(df_runs, files, out_dir='results')
        print('\n[ok] Plots written to results/')

    if args.markdown_out:
        out = Path(args.markdown_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        md = []
        md.append('# Thesis Evaluation Summary\n')
        md.append('## Per Run\n')
        md.append(dataframe_to_markdown(df_runs.drop(columns=['File']), index=False))
        md.append('\n\n## PBScaler-Style View (Violation + Cost Proxy)\n')
        md.append(dataframe_to_markdown(pbstyle, index=False))
        if 'k6 http_req_failed (%)' in df_runs.columns and df_runs['k6 http_req_failed (%)'].notna().any():
            k6_md = df_runs[['Scenario', 'Autoscaler', 'Run', 'k6 http_req_failed (%)', cost_col]].copy()
            k6_md = k6_md.rename(columns={
                'k6 http_req_failed (%)': 'Request Failure Rate (%)',
                cost_col: f'Cost Proxy ({cost_col})',
            })
            md.append('\n\n## K6 Request Failure View\n')
            md.append(dataframe_to_markdown(k6_md, index=False))
        md.append('\n\nNote: Cost Proxy is a proxy metric unless full cluster-wide CPU/memory usage is provided.')
        if not service_df.empty:
            md.append('\n\n## Per-Service Violation Rates\n')
            md.append(dataframe_to_markdown(service_df, index=False))
        md.append('\n\n## Mean ± Std\n')
        md.append(dataframe_to_markdown(grouped))
        md.append('\n\n## 95% CI\n')
        md.append(dataframe_to_markdown(ci_df, index=False))
        out.write_text('\n'.join(md), encoding='utf-8')
        print(f"[ok] Markdown report: {out}")


if __name__ == '__main__':
    main()
