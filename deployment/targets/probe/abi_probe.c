#include <gnu/libc-version.h>
#include <stdint.h>
#include <stdio.h>

int main(void) {
#if !defined(__aarch64__)
#error "III target ABI probe must be compiled for AArch64"
#endif
#if __BYTE_ORDER__ == __ORDER_LITTLE_ENDIAN__
  const char *byte_order = "little";
#else
  const char *byte_order = "big";
#endif
  printf("pointer_bits=%zu\n", sizeof(void *) * 8U);
  printf("endianness=%s\n", byte_order);
  printf("libc_name=glibc\n");
  printf("libc_version=%s\n", gnu_get_libc_version());
  printf("compiler_id=gcc\n");
  printf("compiler_version=%d.%d.%d\n", __GNUC__, __GNUC_MINOR__, __GNUC_PATCHLEVEL__);
  printf("compiler_target=aarch64-linux-gnu\n");
  return 0;
}
