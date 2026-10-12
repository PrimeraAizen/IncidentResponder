"""Generates the provisioned Grafana dashboard (infra/grafana/provisioning/dashboards/shop.json).

Run after changing panels: python3 scripts/gen_dashboard.py
Kept as code because a hand-edited Grafana JSON is hard to review.
"""

import json
from pathlib import Path

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
OUT = Path(__file__).resolve().parent.parent / "infra/grafana/provisioning/dashboards/shop.json"


def timeseries(title: str, queries: list[tuple[str, str]], unit: str, x: int, y: int) -> dict:
    return {"type": "timeseries", "title": title, "datasource": PROM,
            "gridPos": {"x": x, "y": y, "w": 12, "h": 8},
            "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
            "options": {"legend": {"displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}},
            "targets": [{"refId": chr(65 + i), "expr": expr, "legendFormat": legend, "datasource": PROM}
                        for i, (expr, legend) in enumerate(queries)]}


PANELS = [
    ("Requests / s by service", [("sum by (service) (rate(http_requests_total[1m]))", "{{service}}")], "reqps"),
    ("5xx error ratio by service", [('sum by (service) (rate(http_requests_total{status=~"5.."}[1m])) / '
                                     "sum by (service) (rate(http_requests_total[1m]))", "{{service}}")], "percentunit"),
    ("p95 latency by service / route", [("histogram_quantile(0.95, sum by (le, service, route) "
                                         "(rate(http_request_duration_seconds_bucket[1m])))", "{{service}} {{route}}")], "s"),
    ("p95 upstream latency (caller → target)", [("histogram_quantile(0.95, sum by (le, service, target) "
                                                 "(rate(upstream_request_duration_seconds_bucket[1m])))",
                                                 "{{service}} → {{target}}")], "s"),
    ("Goroutines", [("sum by (service) (go_goroutines)", "{{service}}")], "short"),
    ("Heap in use", [("sum by (service) (go_memstats_heap_alloc_bytes)", "{{service}}")], "bytes"),
    ("DB pool connections", [("db_pool_acquired_conns", "{{service}} in use"), ("db_pool_max_conns", "{{service}} max")], "short"),
    ("DB pool wait p95", [("histogram_quantile(0.95, sum by (le, service) "
                           "(rate(db_pool_acquire_duration_seconds_bucket[1m])))", "{{service}}")], "s"),
    ("DB query p95", [("histogram_quantile(0.95, sum by (le, service, query) "
                       "(rate(db_query_duration_seconds_bucket[1m])))", "{{service}} {{query}}")], "s"),
    ("Cache requests / s (inventory)", [("sum by (result) (rate(cache_requests_total[1m]))", "{{result}}")], "reqps"),
    ("Events / s", [("sum by (status) (rate(events_published_total[1m]))", "published {{status}}"),
                    ("sum(rate(events_consumed_total[1m]))", "consumed")], "short"),
    ("Scrape targets up", [("up", "{{service}}")], "short"),
]


def main() -> None:
    panels = [timeseries(title, queries, unit, x=12 * (i % 2), y=8 * (i // 2))
              for i, (title, queries, unit) in enumerate(PANELS)]
    panels.append({"type": "logs", "title": "Warnings and errors (Loki)", "datasource": LOKI,
                   "gridPos": {"x": 0, "y": 8 * ((len(PANELS) + 1) // 2), "w": 24, "h": 10},
                   "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending"},
                   "targets": [{"refId": "A", "datasource": LOKI,
                                "expr": '{service=~".+"} |~ "(?i)(\\"level\\":\\"(warn|error)\\"|error|fatal)"'}]})
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    dashboard = {"uid": "shop-overview", "title": "Shop — service overview", "schemaVersion": 39, "version": 2,
                 "refresh": "5s", "time": {"from": "now-15m", "to": "now"}, "tags": ["incident-responder"],
                 "timezone": "browser", "editable": True, "panels": panels}
    OUT.write_text(json.dumps(dashboard, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({len(panels)} panels)")


if __name__ == "__main__":
    main()
