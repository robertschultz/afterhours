#include "Features.h"
#include <cassert>
#include <iostream>
#include <vector>
int main(){
 const int rate=44100,n=rate;std::vector<int16_t> samples(n*2);
 auto quiet=measure(samples.data(),n,rate);assert(quiet.rmsDb==-120);
 for(int i=0;i<n;i++){samples[2*i]=(int16_t)(16384*std::sin(2*3.141592653589793*60*i/rate));samples[2*i+1]=-samples[2*i];}
 auto copy=samples;auto bass=measure(samples.data(),n,rate);assert(samples==copy);
 assert(std::abs(bass.rmsDb+9.03)<.1);assert(bass.lowDb>bass.highDb+20);
 for(int i=0;i<n;i++)samples[2*i]=samples[2*i+1]=(int16_t)(16384*std::sin(2*3.141592653589793*8000*i/rate));
 auto high=measure(samples.data(),n,rate);assert(high.highDb>high.lowDb+20);
 WaveEnvelope wave;wave.reset(rate);std::vector<int16_t> impulses(2000*2,0);
 impulses[20]=-32768;impulses[21]=32767;auto original=impulses;
 wave.add(impulses.data(),301);wave.add(impulses.data()+602,1699);wave.flush();
 assert(impulses==original);assert(wave.peaks.size()==3);assert(wave.peaks[0]==32768);
 assert(wave.peaks[1]==0&&wave.peaks[2]==0);assert(wave.rms[0]>0&&wave.rms[0]<wave.peaks[0]);
 std::cout<<"Feature tests passed: silence, stereo phase, RMS, frequency bands, unchanged input.\n";
}
