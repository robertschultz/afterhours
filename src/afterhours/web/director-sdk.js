import {createFalClient} from '@fal-ai/client';
import {wma} from '@fal-ai/client/realtime/wma';
export function openDirector(options,{token,lease,model='minimax/h3-max/director'}){
  if(!['minimax/h3-max/director','minimax/h3-max/reference-director'].includes(model))throw Error('Unsupported live video model.');
  const steps=new Map([['https://wma.fal.run/ice','ice'],['https://fal.run/minimax/h3-max/director/ice','ice-fallback'],['https://wma.fal.run/session','session'],['https://wma.fal.run/session/heartbeat','heartbeat']]);
  steps.set('https://fal.run/minimax/h3-max/reference-director/ice','ice-fallback');
  const client=createFalClient({proxyUrl:'/api/fal/proxy',retry:{maxRetries:0},
    fetch:async(url,init={})=>{
      const target=new URL(typeof url==='string'?url:url.url,location.href);
      if(target.origin!==location.origin||target.pathname!=='/api/fal/proxy')throw Error('Direct credentialed requests are disabled.');
      const headers=new Headers(init.headers);headers.set('X-Performance-Token',token);headers.set('X-Director-Lease',lease);
      const step=steps.get(headers.get('x-fal-target-url'));
      const report=status=>{if(step)options.onDiagnostic?.({kind:'progress',phase:'signaling',detail:{step,status}});};
      report(0);
      const response=await fetch(url,{...init,headers});report(response.status);
      if(!response.ok){
        const body=await response.clone().json().catch(()=>null);
        if(body?.category==='credit_verification_unavailable'){
          options.onDiagnostic?.({kind:'progress',phase:'billing',detail:{issue:'credit_verification_unavailable'}});
          // Keep the provider classification even when SDK versions only report HTTP status.
          throw new Error('Fal could not verify session credit (HTTP 503).');
        }
        if(Array.isArray(body?.validation))for(const detail of body.validation.slice(0,4))options.onDiagnostic?.({kind:'progress',phase:'validation',detail});
      }
      return response;
    }});
  return client.realtime.open(wma(model),options);
}
