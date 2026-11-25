// Local diagnostic analyzer. No transport commands, audio modification or network.
#include <vdjDsp8.h>
#include "Features.h"
#include <CommonCrypto/CommonDigest.h>
#include <chrono>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <vector>
#ifndef AFTERHOURS_REPORT_DIR
#error Build must specify the project-local report directory
#endif
struct Frame {double seconds,duration;AudioFeatures f;};
class AfterhoursAnalysis final : public IVdjPluginBufferDsp8 {
    int scanButton=0,waveButton=0;bool waveformMode=false;char label[192]="Idle. Use paused A/B or an isolated silent deck.";
    std::string songHash;int cursor=0,total=0,rate=0;double length=0;
    long long startedAt=0;
    std::vector<Frame> features;
    WaveEnvelope envelope;
    void message(const char* s){std::snprintf(label,sizeof(label),"%s",s);}
    bool paused(){
        double deck=0,playing=1,level=1,pfl=1,count=0;
        if(GetInfo("get_plugindeck",&deck)!=S_OK||GetInfo("play",&playing)!=S_OK||playing!=0)return false;
        if(deck!=1&&deck!=2&&deck!=3&&deck!=4)return false;
        // An isolated, silent deck may scan while other decks play: four real
        // decks, this deck paused, its fader down and its cue off. Never accept
        // the two-deck skin's alias of deck 3, a master insert, or a cue output.
        if(GetInfo("get_decks",&count)==S_OK&&count>=4&&
            GetInfo("volume",&level)==S_OK&&level==0&&GetInfo("pfl",&pfl)==S_OK&&pfl==0)return true;
        // Otherwise only A or B, and only while both are paused.
        double a=1,b=1;return (deck==1||deck==2)&&GetInfo("deck 1 play",&a)==S_OK&&
            GetInfo("deck 2 play",&b)==S_OK&&a==0&&b==0;
    }
    std::string identity(){
        char path[8192]={0};if(GetStringInfo("get_filepath",path,sizeof(path))!=S_OK||!path[0])return "";
        unsigned char hash[CC_SHA256_DIGEST_LENGTH];CC_SHA256(path,(CC_LONG)std::strlen(path),hash);
        std::ostringstream out;for(auto b:hash)out<<std::hex<<std::setw(2)<<std::setfill('0')<<(unsigned)b;
        return out.str();
    }
    bool save(bool complete){
        std::filesystem::path dir(AFTERHOURS_REPORT_DIR);
        std::error_code ec;std::filesystem::create_directories(dir,ec);if(ec)return false;
        if(std::filesystem::is_symlink(dir))return false;
        auto suffix=waveformMode?".waveform":"";
        auto dst=dir/(songHash+suffix+".json"),tmp=dir/(songHash+suffix+".partial");
        if(std::filesystem::is_symlink(tmp)||std::filesystem::is_symlink(dst))return false;
        std::ofstream out(tmp,std::ios::trunc);if(!out)return false;
        out<<std::fixed<<std::setprecision(6)<<"{\"version\":"<<(waveformMode?1:2)<<",\"recordingId\":\""<<songHash<<"\",\"scanStartedAtMs\":"<<startedAt<<",\"sampleRate\":"<<rate
           <<",\"duration\":"<<length<<",\"coveredFrames\":"<<cursor<<",\"totalFrames\":"<<total
           <<",\"bufferReadComplete\":"<<(complete?"true":"false")<<",\"audioSaved\":false,\"features\":[";
        for(size_t i=0;i<features.size();i++){const auto&v=features[i];if(i)out<<",";
            out<<"{\"seconds\":"<<v.seconds<<",\"duration\":"<<v.duration<<",\"rmsDb\":"<<v.f.rmsDb
               <<",\"lowDb\":"<<v.f.lowDb<<",\"highDb\":"<<v.f.highDb<<",\"crestDb\":"<<v.f.crestDb<<"}";}
        out<<"]";
        if(waveformMode){
            out<<",\"envelope\":{\"framesPerBin\":"<<envelope.binFrames<<",\"peak\":[";
            for(size_t i=0;i<envelope.peaks.size();i++){if(i)out<<",";out<<envelope.peaks[i];}
            out<<"],\"rms\":[";for(size_t i=0;i<envelope.rms.size();i++){if(i)out<<",";out<<envelope.rms[i];}
            out<<"]}";
        }
        out<<"}";out.close();if(!out)return false;
        std::filesystem::rename(tmp,dst,ec);return !ec;
    }
public:
    HRESULT VDJ_API OnLoad() override {
        DeclareParameterButton(&scanButton,1,"Scan / continue","Scan");
        DeclareParameterString(label,2,"Analysis status","Status",sizeof(label));
        DeclareParameterButton(&waveButton,3,"Waveform / continue","Waveform");return S_OK;
    }
    HRESULT VDJ_API OnGetPluginInfo(TVdjPluginInfo8* info) override {
        info->PluginName="Afterhours Analysis";info->Author="Afterhours";
        info->Description="Read-only feature scan on paused decks or isolated analysis deck 3";
        info->Version="0.3.0";info->Bitmap=nullptr;info->Flags=0;return S_OK;
    }
    HRESULT VDJ_API OnStart() override {message("Ready. Use paused A/B or isolated decks C/D.");return S_OK;}
    HRESULT VDJ_API OnStop() override {return S_OK;}
    // Pass through the exact original pointer without writing to it.
    short* VDJ_API OnGetSongBuffer(int pos,int nb) override {
        short* buffer=nullptr;return GetSongBuffer(pos,nb,&buffer)==S_OK?buffer:nullptr;
    }
    HRESULT VDJ_API OnParameter(int id) override {
        bool wantWave=id==3;
        if((id!=1&&id!=3)||(wantWave?!waveButton:!scanButton))return S_OK;
        if(wantWave)waveButton=0;else scanButton=0;
        if(!paused()){message("Scan refused: pause A/B or isolate deck C/D.");return S_OK;}
        auto current=identity();double ms=0;
        if(current.empty()||GetInfo("get_time total absolute",&ms)!=S_OK||!std::isfinite(ms)||ms<=0||ms>1200000||SampleRate<8000||SampleRate>192000){
            message("Song buffer unavailable or outside scan limits.");return S_OK;}
        if(current!=songHash||cursor>=total||rate!=SampleRate||wantWave!=waveformMode){
            waveformMode=wantWave;
            songHash=current;cursor=0;features.clear();rate=SampleRate;length=ms/1000.0;total=(int)std::llround(length*rate);
            envelope.reset(rate);
            startedAt=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();
            features.reserve(2401);
        }
        // Small slices keep a background scan from monopolizing the host.
        auto deadline=std::chrono::steady_clock::now()+std::chrono::milliseconds(20);
        while(cursor<total&&std::chrono::steady_clock::now()<deadline){
            if(!paused()||identity()!=songHash){message("Scan stopped: playback or recording changed.");return S_OK;}
            int count=std::min(rate/2,total-cursor);short* buffer=nullptr;
            if(GetSongBuffer(cursor,count,&buffer)!=S_OK||!buffer){
                save(false);message("Buffer incomplete. Load the track fully, then continue.");return S_OK;}
            features.push_back({(double)cursor/rate,(double)count/rate,measure(buffer,count,rate)});
            if(waveformMode)envelope.add(buffer,count);
            cursor+=count;
        }
        bool complete=cursor==total;
        if(complete&&waveformMode)envelope.flush();
        if(!save(complete)){message("Could not save the local feature report.");return S_OK;}
        // A successful callback can still contain zeros from pending decoding.
        // Only the sidecar's independent convergence/end-marker checks validate it.
        if(complete)message("Buffer read finished. Afterhours must verify decoding.");
        else std::snprintf(label,sizeof(label),"%d%% scanned. Press Scan to continue.",(int)(100.0*cursor/total));
        return S_OK;
    }
};
HRESULT VDJ_API DllGetClassObject(const GUID& cls,const GUID& iid,void** object){
    if(!object)return E_FAIL;*object=nullptr;
    if(std::memcmp(&cls,&CLSID_VdjPlugin8,sizeof(GUID))||std::memcmp(&iid,&IID_IVdjPluginBuffer8,sizeof(GUID)))return CLASS_E_CLASSNOTAVAILABLE;
    *object=new AfterhoursAnalysis();return S_OK;
}
