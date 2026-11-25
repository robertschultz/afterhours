#pragma once
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <vector>
struct AudioFeatures {
    double rmsDb=-120,lowDb=-120,highDb=-120,crestDb=0;
};
inline double db(double power) { return 10*std::log10(std::max(power,1e-12)); }
// Stereo energy prevents opposite-phase stereo content cancelling to silence.
inline AudioFeatures measure(const int16_t* samples,int frames,int sampleRate) {
    double total=0,lowEnergy=0,highEnergy=0,peak=0;
    double low[2]={0,0},smooth[2]={0,0};
    const double a=1-std::exp(-2*3.141592653589793*180/sampleRate);
    const double b=1-std::exp(-2*3.141592653589793*3000/sampleRate);
    for(int n=0;n<frames;n++) for(int c=0;c<2;c++) {
        double x=samples[2*n+c]/32768.0;
        low[c]+=a*(x-low[c]);smooth[c]+=b*(x-smooth[c]);
        double high=x-smooth[c];total+=x*x;lowEnergy+=low[c]*low[c];highEnergy+=high*high;
        peak=std::max(peak,x*x);
    }
    double count=std::max(1,2*frames);
    AudioFeatures f{db(total/count),db(lowEnergy/count),db(highEnergy/count),0};
    f.crestDb=std::max(0.0,db(peak)-f.rmsDb);return f;
}

// Display envelope only: two unsigned amplitudes per 20 ms, never PCM/sign/phase.
// Preserve bins across buffer boundaries, including odd sample rates.
struct WaveEnvelope {
    int binFrames=1,pending=0,peak=0;
    double squares=0;
    std::vector<int> peaks,rms;
    void reset(int rate){binFrames=std::max(1,rate/50);pending=peak=0;squares=0;peaks.clear();rms.clear();}
    void flush(){if(!pending)return;peaks.push_back(peak);rms.push_back((int)std::llround(std::sqrt(squares/(2*pending))));pending=peak=0;squares=0;}
    void add(const int16_t* input,int frames){
        for(int i=0;i<frames;i++){
            for(int c=0;c<2;c++){int x=input[2*i+c];peak=std::max(peak,std::abs(x));squares+=(double)x*x;}
            if(++pending==binFrames)flush();
        }
    }
};
