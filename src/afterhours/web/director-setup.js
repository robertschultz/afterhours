// Explicit On/Record action only. Reference setup never admits or retries a session.
export async function beginDirectorSession({character,referenceReady,request,post,onProgress=()=>{}}){
  if(request.castIds?.length){
    onProgress('Preparing the selected cast references…');
    await post('/api/director/cast',{consent:true,ids:request.castIds},180000);
  }else if(!referenceReady){
    onProgress('Preparing the performer reference…');
    await post('/api/director/reference',{consent:true,character},60000);
  }
  onProgress('Connecting live video…');
  return post('/api/director/start',request);
}
