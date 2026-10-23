#ifndef MA_RING_H
#define MA_RING_H
#include <stddef.h>
#include <stdint.h>
void ma_push(const float *samples,uint32_t frames,uint32_t channels,uint32_t rate,uint32_t interleaved,uint64_t position,uint64_t monotonic_ns);
void ma_push_planes(const void *planes,uint32_t frames,uint32_t channels,uint32_t rate,uint64_t position,uint64_t monotonic_ns);
int ma_pop(float *samples,uint32_t *frames,uint32_t *rate,uint64_t *position,uint64_t *monotonic_ns);
int ma_overflow(void);
void ma_error(void);
void ma_ready(void);
int ma_is_ready(void);
#endif
