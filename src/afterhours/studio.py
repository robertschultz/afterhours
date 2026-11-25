"""Local clients can inspect a live set and submit bounded commands."""
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import argparse
import json
import sys

from afterhours.paths import data_root


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['status','start','pause','resume','stop','mix','auto','energy','queue'])
    p.add_argument('value',nargs='?')
    p.add_argument('--reason',default='')
    a=p.parse_args()
    try:
        config=json.loads((data_root()/'.runtime/session.json').read_text())
        data={'action':a.action}
        if a.reason: data['reason']=a.reason
        if a.action=='auto':
            if a.value not in ('on','off'): p.error('auto needs on or off')
            data['value']=a.value=='on'
        if a.action=='energy':
            try: data['value']=float(a.value)
            except (TypeError,ValueError): p.error('energy needs a number from 0 to 1')
            if not 0 <= data['value'] <= 1: p.error('energy needs 0..1')
        if a.action=='queue':
            if not a.value: p.error('queue needs a track ID from status')
            data['trackId']=a.value
        req=Request(config['url']+('/api/state' if a.action=='status' else '/api/command'),
                    data=None if a.action=='status' else json.dumps(data).encode(),
                    headers={'Content-Type':'application/json','X-DJ-Token':config['token']})
        with urlopen(req,timeout=3) as response:
            print(json.dumps(json.load(response),indent=2))
    except HTTPError as e:
        print(e.read().decode(),file=sys.stderr); sys.exit(1)
    except (FileNotFoundError,URLError) as e:
        print(f'Start ./start-dj first. {e}',file=sys.stderr); sys.exit(1)


if __name__=='__main__': main()
