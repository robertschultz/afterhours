#ifndef AFTERHOURS_REPORT_DIR
#define AFTERHOURS_REPORT_DIR "/tmp/afterhours-plugin-fixture"
#endif
#include "AfterhoursAnalysis.cpp"
#include <cassert>
#include <iostream>
class Peer:public IVdjCallbacks8{
public:
 bool playing=false,available=true,ownPlaying=false;double deck=1,count=2,volume=1,pfl=1;int sends=0;int* button=nullptr;int* waveButton=nullptr;char* status=nullptr;
 std::vector<short> samples=std::vector<short>(44100*2,1234);
 HRESULT SendCommand(const char*)override{sends++;return E_FAIL;}
 HRESULT GetInfo(const char* q,double* out)override{
  if(std::strcmp(q,"get_plugindeck")==0)*out=deck;
  else if(std::strcmp(q,"get_decks")==0)*out=count;
  else if(std::strcmp(q,"volume")==0)*out=volume;
  else if(std::strcmp(q,"pfl")==0)*out=pfl;
  else if(std::strcmp(q,"play")==0)*out=ownPlaying?1:0;
  else if(std::strstr(q,"play"))*out=playing?1:0;
  else if(std::strcmp(q,"get_time total absolute")==0)*out=1000;
  else return E_FAIL;return S_OK;
 }
 HRESULT GetStringInfo(const char*,void* out,int size)override{std::snprintf((char*)out,size,"fixture-track-no-media");return S_OK;}
 HRESULT DeclareParameter(void*p,int type,int id,const char*,const char*,float)override{
  if(type==VDJPARAM_BUTTON){if(id==1)button=(int*)p;else if(id==3)waveButton=(int*)p;}
  if(type==VDJPARAM_STRING)status=(char*)p;return S_OK;}
 HRESULT GetSongBuffer(int pos,int nb,short**out)override{
  if(!available||pos<0||pos+nb>44100){*out=nullptr;return E_FAIL;}
  *out=samples.data()+2*pos;return S_OK;
 }
};
int main(){
 Peer cb;AfterhoursAnalysis p;p.cb=&cb;p.SampleRate=44100;p.OnLoad();p.OnStart();
 cb.playing=true;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));
 cb.playing=false;cb.available=false;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"incomplete"));
 cb.available=true;auto copy=cb.samples;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"Buffer read finished"));
 assert(copy==cb.samples);assert(cb.sends==0);assert(p.OnGetSongBuffer(20,10)==cb.samples.data()+40);
 std::vector<std::pair<std::filesystem::path,std::string>> originals;
 for(auto&e:std::filesystem::directory_iterator(AFTERHOURS_REPORT_DIR))if(e.path().extension()==".json"&&e.path().string().find(".waveform.")==std::string::npos){std::ifstream in(e.path());originals.push_back({e.path(),std::string(std::istreambuf_iterator<char>(in),{})});}
 *cb.waveButton=1;p.OnParameter(3);assert(std::strstr(cb.status,"Buffer read finished"));
 for(auto&entry:originals){std::ifstream in(entry.first);assert(std::string(std::istreambuf_iterator<char>(in),{})==entry.second);}
 assert(copy==cb.samples&&cb.sends==0);
 cb.playing=true;cb.deck=3;cb.count=4;cb.volume=0;cb.pfl=0;
 *cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"Buffer read finished"));
 cb.volume=.1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.volume=0;
 cb.pfl=1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.pfl=0;
 cb.count=2;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.count=4;
 cb.ownPlaying=true;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.ownPlaying=false;
 cb.deck=4;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"Buffer read finished"));
 cb.volume=.1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.volume=0;
 cb.pfl=1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.pfl=0;
 cb.ownPlaying=true;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.ownPlaying=false;
 cb.deck=0;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));
 // 0.3.0, four-deck rotation: A or B may scan while other decks play, under
 // the same isolation as C/D: four real decks, this deck paused, fader down, cue off.
 for(double deck:{1.0,2.0}){
  cb.deck=deck;cb.count=4;cb.volume=0;cb.pfl=0;cb.playing=true;cb.ownPlaying=false;
  *cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"Buffer read finished"));
  cb.volume=.1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.volume=0;
  cb.pfl=1;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.pfl=0;
  cb.count=2;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.count=4;
  cb.ownPlaying=true;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));cb.ownPlaying=false;
 }
 // The two-deck rule is unchanged: both A and B paused, whatever the fader.
 cb.deck=1;cb.count=2;cb.volume=1;cb.pfl=1;cb.playing=false;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"Buffer read finished"));
 cb.deck=5;cb.count=4;cb.volume=0;cb.pfl=0;*cb.button=1;p.OnParameter(1);assert(std::strstr(cb.status,"refused"));
 std::cout<<"Plugin fixture passed: playback guard, incomplete buffer, full coverage, pass-through, no transport.\n";
}
