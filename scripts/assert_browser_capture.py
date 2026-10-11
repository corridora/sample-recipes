"""Assert actual captured RUM CLIENT -> Python SERVER parentage."""
import base64
import json
from pathlib import Path
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

spans = []
signals = set()
for line in Path('.sample/browser-capture.jsonl').read_text().splitlines():
    record = json.loads(line)
    signals.add(record['path'])
    raw = base64.b64decode(record['body'])
    if record['path'] != '/v1/traces':
        continue
    if 'json' in (record['content_type'] or ''):
        data = json.loads(raw)
        for resource in data.get('resourceSpans', []):
            for scope in resource.get('scopeSpans', []):
                for span in scope.get('spans', []):
                    spans.append({'trace': span['traceId'], 'id': span['spanId'], 'parent': span.get('parentSpanId'), 'kind': span['kind'], 'source': record['source']})
    else:
        message = ExportTraceServiceRequest.FromString(raw)
        for resource in message.resource_spans:
            for scope in resource.scope_spans:
                for span in scope.spans:
                    spans.append({'trace': span.trace_id.hex(), 'id': span.span_id.hex(), 'parent': span.parent_span_id.hex(), 'kind': span.kind, 'source': 'backend'})

clients = [s for s in spans if s['source'] != 'backend' and s['kind'] in (3, 'SPAN_KIND_CLIENT')]
servers = [s for s in spans if s['source'] == 'backend' and s['kind'] == 2]
matched = [(c, s) for c in clients for s in servers if c['trace'] == s['trace'] and c['id'] == s['parent']]
assert matched, f'No actual RUM CLIENT -> SERVER parentage: {len(clients)} browser clients, {len(servers)} servers'
assert {'/v1/traces', '/v1/logs', '/v1/metrics'} <= signals, signals
print(json.dumps({'matched_browser_server_pairs': len(matched), 'trace_id': matched[0][0]['trace'], 'signals': sorted(signals)}))
