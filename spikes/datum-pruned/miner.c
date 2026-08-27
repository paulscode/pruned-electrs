/* Grind nonces for a stratum share. Takes the 76-byte header prefix and a
 * 32-byte target in display order, prints "nonce <n>" for the first hash at or
 * under the target. Python drives the stratum side; this only exists because
 * DATUM's share difficulty floor of 1 costs about 2**32 hashes, which Python
 * cannot do inside the 150-second stale window. */
#include <openssl/sha.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int unhex(const char *hex, unsigned char *out, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned v;
        if (sscanf(hex + 2 * i, "%2x", &v) != 1) return 0;
        out[i] = (unsigned char)v;
    }
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 5) {
        fprintf(stderr, "usage: %s <prefix76hex> <target32hex> <count> <threads>\n", argv[0]);
        return 2;
    }
    unsigned char prefix[76], target[32];
    if (!unhex(argv[1], prefix, 76) || !unhex(argv[2], target, 32)) {
        fprintf(stderr, "bad hex\n");
        return 2;
    }
    unsigned long long count = strtoull(argv[3], NULL, 10);
    int threads = atoi(argv[4]);

    /* The first 64 bytes never change, so hash them once and copy the state. */
    SHA256_CTX base;
    SHA256_Init(&base);
    SHA256_Update(&base, prefix, 64);

    volatile long long winner = -1;

#pragma omp parallel num_threads(threads)
    {
        unsigned char tail[16];
        memcpy(tail, prefix + 64, 12);
        unsigned char first[32], second[32], display[32];

#pragma omp for schedule(static)
        for (long long n = 0; n < (long long)count; n++) {
            if ((n & 0xffff) == 0 && winner >= 0) continue;
            tail[12] = (unsigned char)(n & 0xff);
            tail[13] = (unsigned char)((n >> 8) & 0xff);
            tail[14] = (unsigned char)((n >> 16) & 0xff);
            tail[15] = (unsigned char)((n >> 24) & 0xff);

            SHA256_CTX c = base;
            SHA256_Update(&c, tail, 16);
            SHA256_Final(first, &c);
            SHA256_Init(&c);
            SHA256_Update(&c, first, 32);
            SHA256_Final(second, &c);

            for (int i = 0; i < 32; i++) display[i] = second[31 - i];
            if (memcmp(display, target, 32) <= 0) {
#pragma omp critical
                if (winner < 0) winner = n;
            }
        }
    }

    if (winner >= 0) {
        printf("nonce %lld\n", winner);
        return 0;
    }
    printf("none\n");
    return 1;
}
