
#include <stdio.h>
#include <string.h>
static unsigned char enc[] = {60,54,59,61,33,40,105,44,5,34,106,40,5,106,56,60,47,41,57,110,46,107,106,52,5,109,60,105,59,39,0};   /* 每个字节 XOR 0x5A */
int main(void) {
    char buf[128];
    printf("input the password: ");
    if (!fgets(buf, sizeof(buf), stdin)) return 1;
    buf[strcspn(buf, "\n")] = 0;
    char dec[128];
    for (unsigned i = 0; i < sizeof(enc); i++) dec[i] = enc[i] ^ 0x5A;
    if (strcmp(buf, dec) == 0) printf("Correct! %s\n", dec);
    else printf("Wrong!\n");
    return 0;
}
