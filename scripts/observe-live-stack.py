#!/usr/bin/env python3
"""Sample native process liveness and local health endpoints throughout a live set."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


OPENER = build_opener(ProxyHandler({}), NoRedirect())


def binding(value):
    name, sep, target = value.partition('=')
    if not sep or not name.isidentifier() or not target:
        raise argparse.ArgumentTypeError('Use LABEL=VALUE.')
    return name, target


def endpoint(value):
    name, target = binding(value)
    parsed = urlsplit(target)
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password:
        raise argparse.ArgumentTypeError('Health endpoints must be loopback HTTP URLs.')
    return name, target


def process_table():
    result = subprocess.run(['/bin/ps','-Ao','pid=,rss=,%cpu=,comm='],
                            capture_output=True, text=True, check=True)
    rows = {}
    for line in result.stdout.splitlines():
        pid, rss, cpu, command = line.strip().split(maxsplit=3)
        rows[int(pid)] = {'pid':int(pid), 'rssKiB':int(rss), 'cpuPercent':float(cpu),
                          'executable':Path(command).name}
    return rows


def process_health(pidfiles, executables):
    rows = process_table()
    result = {}
    for name, value in pidfiles:
        try:pid = int(Path(value).read_text().strip())
        except (OSError, ValueError):pid = None
        result[name] = rows.get(pid)
    for name, executable in executables:
        result[name] = next((row for row in rows.values() if row['executable']==executable), None)
    return result


def service_health(url):
    since = time.monotonic()
    try:
        with OPENER.open(url, timeout=3) as response:
            body = json.loads(response.read(512_000))
        result = {'ok':True, 'responseMs':round((time.monotonic()-since)*1000,1)}
        telemetry = body.get('telemetry')
        if isinstance(telemetry, dict):
            at = telemetry.get('sampledAt')
            if isinstance(at, (int, float)):
                result['telemetryAgeSeconds'] = round(time.time()-at, 3)
            result['playingDecks'] = sum(d.get('playing') is True for d in telemetry.get('decks', []))
        audio = body.get('audio')
        if isinstance(audio, dict):
            result.update(relayCapture=audio.get('capture') is True, relaySignal=audio.get('signal') is True)
        unreal = body.get('unreal')
        if isinstance(unreal, dict) and type(unreal.get('streamers')) is int:
            result['unrealStreamers'] = unreal['streamers']
        return result
    except (OSError, ValueError, AttributeError, TypeError) as exc:
        return {'ok':False, 'errorType':type(exc).__name__}


def emit(kind, **fields):
    print(json.dumps({'type':kind,'at':time.time(),**fields}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pidfile', action='append', type=binding, default=[])
    parser.add_argument('--process', action='append', type=binding, default=[])
    parser.add_argument('--url', action='append', type=endpoint, default=[])
    parser.add_argument('--seconds', type=float, default=10800)
    parser.add_argument('--interval', type=float, default=5)
    args = parser.parse_args()
    if args.seconds <= 0 or not 1 <= args.interval <= 60 or not (args.pidfile or args.process or args.url):
        parser.error('Supply a process or endpoint, a positive duration, and interval from 1 to 60 seconds.')
    start = time.monotonic()
    previous = {}
    counts = {'samples':0,'processMisses':0,'processRestarts':0,'endpointFailures':0}
    with ThreadPoolExecutor(max_workers=4) as executor:
        while time.monotonic()-start < args.seconds:
            tick = time.monotonic()
            processes = process_health(args.pidfile, args.process)
            services = dict(zip((name for name, _ in args.url),
                                executor.map(service_health, (url for _, url in args.url))))
            for name, row in processes.items():
                if row is None:
                    counts['processMisses'] += 1
                    continue
                pid = row['pid']
                if name in previous and previous[name] != pid:
                    counts['processRestarts'] += 1
                    emit('restart', name=name, oldPid=previous[name], pid=pid)
                previous[name] = pid
            counts['endpointFailures'] += sum(not row['ok'] for row in services.values())
            counts['samples'] += 1
            emit('sample', processes=processes, services=services)
            remaining = min(start+args.seconds, tick+args.interval)-time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
    emit('complete', seconds=round(time.monotonic()-start,1), **counts)
    return int(any(counts[key] for key in ('processMisses','processRestarts','endpointFailures')))


if __name__ == '__main__':
    sys.exit(main())
