/* Original SPSC ring: callbacks never wait, allocate, log or write. */
#include "ring.h"
#include <stdatomic.h>
#define CAPACITY 256u
struct block {float samples[480];uint32_t frames,rate;uint64_t position,monotonic_ns;};
static struct block blocks[CAPACITY];
static _Atomic uint32_t write_index,read_index,overflow;
static void push(const float *samples,const float *const *planes,uint32_t frames,uint32_t channels,uint32_t rate,uint32_t interleaved,uint64_t position,uint64_t monotonic_ns){
 if(!channels||channels>32||!rate||rate>384000||!monotonic_ns){atomic_store(&overflow,1);return;}
 for(uint32_t offset=0;offset<frames;offset+=480){uint32_t write=atomic_load_explicit(&write_index,memory_order_relaxed),read=atomic_load_explicit(&read_index,memory_order_acquire);if(write-read>=CAPACITY){atomic_store(&overflow,1);return;}struct block *b=&blocks[write%CAPACITY];b->frames=frames-offset<480?frames-offset:480;b->rate=rate;b->position=position+offset;b->monotonic_ns=monotonic_ns+(uint64_t)offset*1000000000u/rate;
 for(uint32_t i=0;i<b->frames;i++){float value=0;for(uint32_t c=0;c<channels;c++){value+=planes?planes[c][offset+i]:samples[interleaved?(offset+i)*channels+c:c*frames+offset+i];}b->samples[i]=value/(float)channels;}atomic_store_explicit(&write_index,write+1,memory_order_release);}
}
void ma_push(const float *samples,uint32_t frames,uint32_t channels,uint32_t rate,uint32_t interleaved,uint64_t position,uint64_t monotonic_ns){push(samples,NULL,frames,channels,rate,interleaved,position,monotonic_ns);}
void ma_push_planes(const void *planes,uint32_t frames,uint32_t channels,uint32_t rate,uint64_t position,uint64_t monotonic_ns){push(NULL,(const float *const*)planes,frames,channels,rate,0,position,monotonic_ns);}
int ma_pop(float *samples,uint32_t *frames,uint32_t *rate,uint64_t *position,uint64_t *monotonic_ns){uint32_t read=atomic_load_explicit(&read_index,memory_order_relaxed);if(read==atomic_load_explicit(&write_index,memory_order_acquire))return 0;struct block *b=&blocks[read%CAPACITY];*frames=b->frames;*rate=b->rate;*position=b->position;*monotonic_ns=b->monotonic_ns;for(uint32_t i=0;i<b->frames;i++)samples[i]=b->samples[i];atomic_store_explicit(&read_index,read+1,memory_order_release);return 1;}
int ma_overflow(void){return atomic_exchange(&overflow,0);}
void ma_error(void){atomic_store(&overflow,1);}

static _Atomic uint32_t ready;
void ma_ready(void){atomic_store_explicit(&ready,1,memory_order_release);}
int ma_is_ready(void){return atomic_load_explicit(&ready,memory_order_acquire);}