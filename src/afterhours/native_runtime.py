"""Start missing loopback services for the local native app. Never send commands.

Healthy services belong to their existing owner: do not restart or rekey them.
The launch lock serializes simultaneous app opens. Only a new child that fails
its own startup is terminated; a healthy service outlives this helper/app.
"""
import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


class StartupError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise StartupError('The local service redirected its health check.')


def read_json(port, path):
    # Ignore shell proxy settings and redirects: health checks stay on loopback.
    opener=build_opener(ProxyHandler({}),NoRedirect())
    with opener.open(Request(f'http://127.0.0.1:{port}{path}'),timeout=1) as response:
        raw=response.read(2_000_001)
        if len(raw)>2_000_000:raise StartupError('The local response is too large.')
        value=json.loads(raw)
        if not isinstance(value,dict):raise StartupError('The local response is invalid.')
        return value


def probe(name,port):
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=.3):pass
    except ConnectionRefusedError:return 'missing'
    except OSError:return 'occupied'
    try:
        config=read_json(port,'/api/config' if name=='music' else '/api/performance/config')
        if config.get('service') != f'afterhours-{name}':return 'occupied'
        if not isinstance(config.get('token'),str) or not 16<=len(config['token'])<=256:return 'occupied'
        if name=='music':
            state=read_json(port,'/api/set-session')
            valid=type(state.get('active')) is bool and isinstance(state.get('phase'),str) and isinstance(state.get('order'),list)
        else:
            state=read_json(port,'/api/performance')
            valid=(isinstance(config.get('director'),dict) and isinstance(state.get('director'),dict)
                   and isinstance(state.get('segments'),list) and state.get('mode') in ('paused','playing','blackout'))
        return 'ready' if valid else 'occupied'
    except (OSError,ValueError,StartupError):return 'occupied'


def read_manifest(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size>16_384:raise StartupError('The local runtime configuration is missing or invalid.')
    config=json.loads(path.read_bytes())
    if not isinstance(config,dict) or config.get('version')!=1:raise StartupError('The local runtime configuration is unsupported.')
    for key in ('python','musicRoot','performanceRoot','dataRoot','runtimeRoot'):
        if not isinstance(config.get(key),str) or not Path(config[key]).is_absolute():
            raise StartupError('The local runtime configuration needs absolute paths.')
    ports=config.setdefault('ports',{'music':8765,'performance':8768,'vdj':7474,'reply':7475})
    if not isinstance(ports,dict) or set(ports)!=set(('music','performance','vdj','reply')) or any(type(p) is not int or not 1024<=p<=65535 for p in ports.values()) or len(set(ports.values()))!=4:
        raise StartupError('The local runtime ports are invalid.')
    return config


def child_environment(config):
    env=os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE='1',PYTHONUNBUFFERED='1',
               AFTERHOURS_DATA_ROOT=config['dataRoot'],
               AFTERHOURS_ANALYSIS_REPORTS_DIR=str(Path(config['dataRoot'])/'.runtime/audio-analysis'))
    # The native app does not receive or persist credentials. Read only the
    # existing private configuration, into the child's environment, if needed.
    source=Path(config['dataRoot'])/'.env.local'
    if source.is_file() and not source.is_symlink() and source.stat().st_size<65_536:
        for line in source.read_text().splitlines():
            match=re.fullmatch(r'\s*(?:export\s+)?(OPENAI_API_KEY|FAL_KEY)\s*=\s*(.*?)\s*',line)
            if match and not env.get(match[1]):
                value=match[2]
                if len(value)>1 and value[0]==value[-1] and value[0] in ('"',"'"):value=value[1:-1]
                if value and not any(c.isspace() for c in value):env[match[1]]=value
    return env


def launch(config,name):
    root=Path(config['musicRoot' if name=='music' else 'performanceRoot'])
    module = 'server' if name == 'music' else 'performance_server'
    script = root / f'{module}.py'
    source = (root.parent if root.name == 'engine' else root) / 'src'
    if (source / 'afterhours' / f'{module}.py').is_file():
        entry = ['-m', f'afterhours.{module}']
        root = source
    elif script.is_file():
        entry = [str(script)]
    else:
        entry = []
    if not entry or not os.access(config['python'],os.X_OK):
        raise StartupError(f'The {name} service installation is missing. Rebuild the local app configuration.')
    ports=config['ports']
    command=[config['python'],'-u',*entry,'--port',str(ports[name])]
    if name=='music':command+=['--vdj-port',str(ports['vdj']),'--vdj-reply-port',str(ports['reply'])]
    else:command+=['--audio-port',str(ports['music'])]
    # No playback commands, provider allowances or microphone/video sessions.
    log=Path(config['runtimeRoot'])/(name+'.log')
    fd=os.open(log,os.O_WRONLY|os.O_CREAT|os.O_APPEND|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'ab') as stream:
        os.fchmod(stream.fileno(),0o600)
        return subprocess.Popen(command,cwd=root,env=child_environment(config),stdin=subprocess.DEVNULL,
                                stdout=stream,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)


def ensure(config,startup_timeout=12,lock_timeout=15):
    runtime=Path(config['runtimeRoot']);runtime.mkdir(parents=True,exist_ok=True)
    fd=os.open(runtime/'launch.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'a+') as lock:
        deadline=time.monotonic()+lock_timeout
        while True:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except OSError as exc:
                if exc.errno not in (errno.EACCES,errno.EAGAIN):raise
                if time.monotonic()>=deadline:raise StartupError('Another app is still starting the local services. Reconnect shortly.')
                time.sleep(.1)
        statuses={};errors=[]
        for name in ('music','performance'):
            port=config['ports'][name];status=probe(name,port)
            if status=='ready':statuses[name]='reused';continue
            if status=='occupied':
                statuses[name]='unavailable';errors.append(f'Port {port} is occupied by an unrecognized or unresponsive service.');continue
            child=None
            try:
                child=launch(config,name);deadline=time.monotonic()+startup_timeout
                while child.poll() is None:
                    if probe(name,port)=='ready':break
                    if time.monotonic()>=deadline:raise StartupError(f'The {name} service did not become ready. Check its local startup log.')
                    time.sleep(.1)
                else:raise StartupError(f'The {name} service exited during startup. Check its local startup log.')
                # An occupied port cannot cause adoption of a failed child.
                if child.poll() is not None:raise StartupError(f'The {name} service exited during startup.')
                statuses[name]='started'
            except (OSError,ValueError,StartupError) as exc:
                if child and child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        child.kill();child.wait(timeout=3)
                statuses[name]='unavailable'
                errors.append(str(exc) if isinstance(exc,StartupError) else f'The {name} service could not start. Check its local startup log.')
        return {'ready':not errors,'services':statuses,'message':' '.join(errors) if errors else 'Local services are ready.'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--manifest',required=True,type=Path);args=parser.parse_args()
    config=None
    try:
        config=read_manifest(args.manifest)
        result=ensure(config)
    except (OSError,ValueError,StartupError) as exc:
        result={'ready':False,'services':{},'message':str(exc) if isinstance(exc,StartupError) else 'Local service startup failed. Check the app runtime configuration.'}
    if config:
        temporary=Path(config['runtimeRoot'])/f'.startup-{os.getpid()}.tmp'
        try:
            fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as stream:json.dump({**result,'checkedAt':time.time()},stream)
            os.replace(temporary,temporary.with_name('last-startup.json'))
        except OSError:pass  # A log failure must not stop healthy services.
        finally:temporary.unlink(missing_ok=True)
    print(json.dumps(result),flush=True)
    return 0 if result['ready'] else 1


if __name__=='__main__':raise SystemExit(main())
